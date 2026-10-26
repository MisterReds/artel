"""artel — command line.

    artel run "<goal>" [--name N] [--provider P] [--web]   create a project and drive it to completion
    artel resume <project>                                  continue a stopped / budget-paused project
    artel status [<project>]                                projects, or one board
    artel log <project> [--follow] [--kinds a,b]            the event trajectory
    artel comment <project> <task> "<text>"                 message a task as the human (unblocks escalations)
    artel metrics <project> [--json] [--scenario S|L]       acceptance metrics from the event log
    artel acceptance run S|L | verify <project>             acceptance scenarios, independent final check
    artel web [--port 8765] [--remote [--host H]]           live dashboard (remote: login, docs/REMOTE.md)
    artel web token [--new]                                 remote-mode access token (--new rotates it)
    artel skills list|view|import|curate|pin|unpin|ledger|rollback|restore
    artel refine history|rollback <project> [<edit>]
    artel plugins list|install <git-url|pip-spec|path>|show|enable|disable|remove|update [<name>]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Optional
import time

from .manager import human_comment, new_kernel, resume, run_goal
from .tz import fmt_ts
from .project import Project, list_projects, load_config

ICONS = {"todo": "·", "ready": "○", "running": "▶", "review": "◎", "done": "✓", "blocked": "✗", "cancelled": "–"}


def _runtime_arg(value: str) -> str:
    if value in ("native", "claude_code", "codex"):
        return value
    raise argparse.ArgumentTypeError(f"unknown runtime {value!r}: native, claude_code, codex")


def _cfg(args) -> dict:
    cfg = load_config(getattr(args, "config", None))
    if getattr(args, "provider", None):
        cfg["provider"] = args.provider
    if getattr(args, "runtime", None):
        cfg["runtime"] = args.runtime
    if getattr(args, "model", None):
        cfg["models"] = {**cfg["models"], "default": args.model}
    if getattr(args, "parallel", None):
        cfg["budgets"] = {**cfg["budgets"], "max_parallel": args.parallel}
    if getattr(args, "max_tokens_total", None):
        cfg["budgets"] = {**cfg["budgets"], "max_total_tokens": args.max_tokens_total}
    return cfg


def print_board(project: Project) -> None:
    m = project.meta
    u = m["usage"]
    from .brand import banner, paint, status

    print(f"{banner()}  {m['name']}  [{status(m['status'])}]  "
          f"{project.slug}")
    print(f"  tokens in/out {u['input_tokens']}/{u['output_tokens']}  requests {u['requests']}  "
          f"audits {m.get('audit_rounds', 0)}")
    for p in project.personas():
        print(f"  @{p.name} — {p.role}")
    for t in project.board.list():
        flag = " [needs human]" if t["needs_human"] else ""
        print(f"  {ICONS.get(t['status'], '?')} {status(t['status']):<9} {t['id']}  {t['assignee'] or '-':<10} "
              f"{t['title']}{flag}  (attempts {t['attempts']})")


def _event_line(e: dict, cfg: dict | None = None) -> str:
    k = e["kind"]
    t = fmt_ts(e["ts"], "%H:%M:%S", cfg)
    who = e.get("actor") or e.get("worker") or ""
    if k == "tool/call":
        body = f"{e['tool']} {json.dumps(e['input'], ensure_ascii=False)[:160]}"
    elif k == "tool/result":
        body = ("ERR " if e["is_error"] else "") + e["output"][:160].replace("\n", " ⏎ ")
    elif k == "llm/response":
        texts = " ".join(b["text"] for b in e["blocks"] if b["type"] == "text")[:160]
        body = f"turn {e['turn']} {e['stop_reason']} {texts}"
    elif k == "board/task_moved":
        body = f"{e['task_id']} {e['frm']} → {e['to']}"
    elif k == "board/comment":
        body = f"{e['task_id']} [{e['author']}] {e['body'][:160]}"
    else:
        rest = {x: v for x, v in e.items() if x not in ("seq", "ts", "kind", "system", "user", "blocks")}
        body = json.dumps(rest, ensure_ascii=False)[:200]
    return f"{e['seq']:>6} {t} {k:<22} {who:<10} {body}"


def _note_owner_stop(result) -> None:
    """Ctrl+C is the owner stopping the project; the log should say so (the project is known once the run returns)."""
    if result:
        result[0].log.append("owner/stop", via="cli")


def _run_goal_cli(args) -> tuple[Project, str]:
    """Run ``args.goal`` to the end with the Ctrl+C handling of ``artel run``."""
    cfg = _cfg(args)
    stop = threading.Event()
    if args.web:
        from .web.server import serve

        threading.Thread(target=serve, kwargs={"port": args.port, "config": cfg}, daemon=True).start()
        from .brand import banner

        print(banner(f"дашборд http://127.0.0.1:{args.port}"))
    holder: dict = {}

    def target():
        holder["result"] = run_goal(args.goal, cfg, name=args.name, stop_event=stop, workdir=args.workdir,
                                    refs=tuple(args.ref or ()), seed=args.seed)

    th = threading.Thread(target=target)
    th.start()
    try:
        while th.is_alive():
            th.join(1)
    except KeyboardInterrupt:
        print("\nstopping: finishing in-flight turns…")
        stop.set()
        th.join()
        _note_owner_stop(holder.get("result"))
    return holder["result"]


def cmd_run(args) -> int:
    project, status = _run_goal_cli(args)
    print_board(project)
    print(f"\nproject: {project.root}\ndeliverables: {project.shared}\nstatus: {status}")
    return 0 if status == "done" else 2


def cmd_resume(args) -> int:
    stop = threading.Event()
    holder: dict = {}
    th = threading.Thread(target=lambda: holder.update(r=resume(args.project, _cfg(args), stop_event=stop, via="cli")))
    th.start()
    try:
        while th.is_alive():
            th.join(1)
    except KeyboardInterrupt:
        stop.set()
        th.join()
        _note_owner_stop(holder.get("r"))
    project, status = holder["r"]
    print_board(project)
    print(f"status: {status}")
    return 0 if status == "done" else 2


def cmd_status(args) -> int:
    cfg = load_config()
    if args.project:
        print_board(Project.open(args.project, cfg))
        return 0
    for p in list_projects():
        print(f"{p['slug']:<42} {p['status']:<14} {p['name'][:60]}")
    return 0


def cmd_log(args) -> int:
    cfg = load_config()
    project = Project.open(args.project, cfg)
    kinds = set(args.kinds.split(",")) if args.kinds else None
    after = 0
    while True:
        for e in project.log.read(after=after, kinds=kinds):
            print(_event_line(e, cfg))
            after = e["seq"]
        if not args.follow:
            return 0
        project.log.wait(after, 2)
        after = max(after, 0)


def cmd_comment(args) -> int:
    project = Project.open(args.project, load_config())
    t = human_comment(project, args.task, args.text)
    print(f"{t['id']} is now {t['status']}")
    return 0


METRIC_LINES = (  # (key in measure(), label, threshold key in check(), format)
    ("duration_h", "Длительность, ч", "hours_min", "{}"),
    ("ended", "Итоговый статус", None, "{}"),
    ("stopped_by_limit", "Остановка по лимиту", "no_limit_stop", "{}"),
    ("limit_waits", "Ожиданий лимита", None, "{}"),
    ("expensive_token_share", "Доля дорогих токенов", "expensive_token_share_max", "{:.1%}"),
    ("expensive_call_share", "Доля дорогих вызовов", "expensive_call_share_max", "{:.1%}"),
    ("tokens_total", "Токенов всего", None, "{}"),
    ("tasks_done", "Задач принято", None, "{}"),
    ("tokens_per_accepted", "Токенов на принятую задачу", None, "{}"),
    ("success_rate", "Доля успешных задач", None, "{:.1%}"),
    ("steps", "Шагов агентов", None, "{}"),
    ("owner_asks", "Обращений к владельцу", None, "{}"),
    ("owner_interventions", "Вмешательств владельца", "owner_interventions_max", "{}"),
    ("autonomy_share", "Автономность", "autonomy_min", "{:.1%}"),
    ("cost_usd", "Стоимость, $", None, "{}"),
)


def format_metrics(m: dict, checks: Optional[dict] = None) -> str:
    lines = []
    for key, label, check_key, fmt in METRIC_LINES:
        v = m.get(key)
        text = "нет данных" if v is None else ("да" if v is True else "нет" if v is False else fmt.format(v))
        mark = ""
        if checks is not None and check_key in checks:
            c = checks[check_key]
            mark = "  " + ("·" if c["ok"] is None else "✓" if c["ok"] else "✗") + f" (порог {c['limit']})"
        lines.append(f"{label}: {text}{mark}")
    for rt, n in sorted((m.get("tokens_by_runtime") or {}).items()):
        lines.append(f"Токенов, {rt}: {n}")
    if m.get("verdict"):
        v = m["verdict"]
        lines.append(f"Вердикт независимой проверки ({v.get('runtime')}): "
                     f"{'выполнено' if v.get('complete') else 'не выполнено'} — {v.get('summary', '')}")
    return "\n".join(lines)


def cmd_metrics(args) -> int:
    from . import acceptance

    project = Project.open(args.project, load_config())
    scenario = args.scenario or project.meta.get("acceptance")
    m = acceptance.measure_project(project)
    try:
        checks = acceptance.check(m, acceptance.load_scenario(scenario)["thresholds"]) if scenario else None
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"metrics": m, "check": checks}, ensure_ascii=False, indent=1))
    else:
        print(format_metrics(m, checks))
    return 0


def cmd_acceptance(args) -> int:
    from . import acceptance

    if args.action == "verify":
        project = Project.open(args.target, _cfg(args))
        res = acceptance.verify(project, _cfg(args))
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 2 if "error" in res else 0
    try:
        scenario = acceptance.load_scenario(args.target)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    run_args = argparse.Namespace(**{**vars(args), "goal": scenario["idea"], "workdir": None, "ref": None,
                                     "seed": None, "web": False, "port": 8765})
    project, status = _run_goal_cli(run_args)
    project.set_meta(acceptance=args.target)
    print(f"проект: {project.slug}\nстатус: {status}\n")
    return cmd_metrics(argparse.Namespace(project=project.slug, scenario=args.target, json=False))


def cmd_web(args) -> int:
    if args.action == "token":
        from .web.auth import Auth

        cfg = load_config()
        auth = Auth(session_days=(cfg.get("web") or {}).get("session_days", 30))
        if not args.new:
            print(("Токен выпущен; хранится только его хэш, показать его нельзя." if auth.has_token()
                   else "Токена ещё нет.") + " Новый токен (все сессии закроются): artel web token --new")
            return 0
        print("Новый токен доступа (показывается один раз; все прежние сессии закрыты):")
        print(f"  {auth.rotate()}")
        return 0
    from .web.server import serve

    serve(port=args.port, config=_cfg(args), host=args.host, remote=True if args.remote else None)
    return 0


def cmd_skills(args) -> int:
    cfg = load_config()
    store = new_kernel(cfg).get("skills")
    a = args.action
    if a == "list":
        for s in store.list(args.arg or ""):
            print(f"{s['name']:<40} {s['category']:<24} {s['origin']:<28} {s['state']:<8} "
                  f"{'pinned ' if s['pinned'] else ''}{s['description'][:70]}")
    elif a == "view":
        print(store.view(args.arg))
    elif a == "import":
        res = store.import_from(args.arg, trust=args.trust, include=args.include, force=args.force)
        print(f"{res['source']} ({res['trust']}): installed {len(res['installed'])}, skipped {len(res['skipped'])}")
        for s in res["skipped"]:
            print(f"  skipped {s['skill']}: {s['reason']}")
    elif a == "curate":
        print(json.dumps(store.curate(), ensure_ascii=False, indent=1))
    elif a in ("pin", "unpin"):
        store.pin(args.arg, a == "pin")
        print(f"{a}ned {args.arg}")
    elif a == "ledger":
        for e in store.ledger(args.arg):
            print(f"{e['id']}  {fmt_ts(e['ts'], '%Y-%m-%d %H:%M', cfg)}  {e['skill']:<36} "
                  f"{e['action']:<14} {e['actor']}  {e.get('reason', '')}")
    elif a == "rollback":
        print(store.rollback(args.arg))
    elif a == "restore":
        print(store.restore(args.arg))
    return 0


def _print_manifest(entry: dict) -> None:
    from .packages import PERMISSIONS

    print(f"  {entry.get('name') or entry.get('id')} {entry.get('version', '')}  <- {entry.get('source', '')}")
    for ep, m in (entry.get("manifests") or {}).items():
        print(f"  плагин {m.get('name', ep)} {m.get('version', '')}"
              + (f" — {m['description']}" if m.get("description") else ""))
        print(f"    provides: {', '.join(m.get('provides', [])) or '—'}")
        print(f"    requires: {', '.join(m.get('requires', [])) or '—'}")
        if m.get("note"):
            print(f"    примечание: {m['note']}")
    perms = entry.get("permissions") or []
    print("  разрешения: " + (", ".join(f"{p} ({PERMISSIONS.get(p, 'своё')})" for p in perms) if perms else "не просит"))


def cmd_plugins(args) -> int:
    from . import packages as P

    cfg = load_config()
    a = args.action
    try:
        if a == "list":
            from .kernel import build_kernel

            kernel = build_kernel(cfg)  # loads what is enabled: the state column is what a run would get
            for r in P.overview(cfg, kernel):
                state = ("ошибка" if r.get("error") else "включён" if r["enabled"] else "выключен") + \
                    (" (ядро)" if r.get("core") else "")
                print(f"  {r['id']:<34} {r['kind']:<8} {str(r.get('version') or ''):<8} {state:<18} "
                      f"{', '.join(r.get('permissions') or [])}")
                if r.get("error"):
                    print(f"      {r['error'].splitlines()[0][:200]}")
            kernel.shutdown(reason="cli")
            return 0
        if not args.target:
            print(f"artel plugins {a}: укажите {'источник' if a == 'install' else 'имя плагина'}")
            return 2
        if a == "install":
            entry = P.install(args.target)
            print(f"установлен {entry['name']} {entry['version']} (выключен):")
            _print_manifest(entry)
            if not args.enable:
                print(f"включить: artel plugins enable {entry['name']}")
                return 0
            a, args.target = "enable", entry["name"]
        if a == "show":
            _print_manifest(P.get(args.target))
            return 0
        if a == "enable":
            if args.target.startswith("artel.plugins."):
                P.set_builtin(args.target, True)
                print(f"включён {args.target}")
                return 0
            _print_manifest(P.get(args.target))
            if not args.yes:
                if not sys.stdin.isatty():
                    print("подтвердите включение флагом --yes")
                    return 2
                answer = input("Включить? Плагин работает внутри Artel с его правами. [y/N] ").strip().lower()
                if answer not in ("y", "yes", "д", "да"):
                    return 1
            entry = P.enable(args.target, config=cfg)
            if entry.get("error"):
                print(f"не загрузился, оставлен выключенным: {entry['error']}")
                return 1
            print(f"включён {entry['name']}")
        elif a == "disable":
            if args.target.startswith("artel.plugins."):
                P.set_builtin(args.target, False)
            else:
                P.disable(args.target)
            print(f"выключен {args.target}")
        elif a == "remove":
            print(f"удалён {P.remove(args.target)['name']}")
        elif a == "update":
            names = list(P.load_registry()["packages"]) if args.target == "all" else [args.target]
            for n in names:
                e = P.update(n)
                print(f"обновлён {e['name']} {e['version']}" + (f" — {e['error']}" if e.get("error") else ""))
    except P.PackageError as exc:
        print(f"ошибка: {exc}")
        return 1
    return 0


def cmd_refine(args) -> int:
    kernel = new_kernel(load_config())
    project = Project.open(args.project, kernel.config)
    refine = kernel.get("refine")
    if args.action == "history":
        for e in refine.history(project):
            print(f"{e['id']}  {e['target']:<13} {e['worker'] or '':<10} {e['content'][:80]}  ← {e['evidence'][:60]}")
    else:
        print(refine.rollback(project, args.edit))
    return 0


def _repo(args) -> "Path":
    from pathlib import Path

    from .evolve.engine import git

    start = Path(getattr(args, "repo", None) or Path(__file__).resolve().parent.parent)
    return Path(git(start, "rev-parse", "--show-toplevel"))


def _evolution(args):
    from .evolve.engine import Evolution, Policy
    from .project import artel_home

    cfg = _cfg(args)
    if getattr(args, "runtimes", None):  # e.g. claude_code,codex: on a limit the work moves to the next subscription
        cfg = {**cfg, "evolve": {**(cfg.get("evolve") or {}), "runtimes": args.runtimes.split(",")}}
    repo = _repo(args)
    tasks = args.tasks.split(",") if getattr(args, "tasks", None) else None
    return Evolution(repo, artel_home() / "evolution" / repo.name, cfg, cfg, tasks=tasks, policy=Policy(),
                     parallel=getattr(args, "parallel_bench", 2) or 2)


def cmd_bench(args) -> int:
    import time
    from pathlib import Path

    from .evolve import bench
    from .project import artel_home

    code = Path(args.code) if args.code else _repo(args)
    tasks = bench.load_tasks(only=args.tasks.split(",") if args.tasks else None)
    out = artel_home() / "bench" / f"{args.label or 'manual'}-{int(time.time())}"
    suite = bench.run_suite(code, _cfg(args), out, tasks=tasks, parallel=args.parallel_bench, label=args.label or "",
                            repeats=args.repeats)
    for r in suite["results"]:
        print(f"{'PASS' if r.get('pass') else 'FAIL'} {r['id']:<16} partial {r.get('partial', 0):.2f}  "
              f"${r.get('cost_usd', 0):.3f}  {r.get('duration_s', 0)}s  rejections {r.get('rejections', 0)}  "
              f"stalls {r.get('stalls', 0)}  {r.get('status', r.get('error', '')[:80])}")
    print(json.dumps(suite["summary"], ensure_ascii=False))
    print(f"suite: {out / 'suite.json'}")
    return 0


def cmd_doctor(args) -> int:
    from pathlib import Path

    from .evolve import diagnose
    from .project import artel_home

    code = Path(args.code) if args.code else _repo(args)
    home = Path(args.home) if args.home else artel_home()
    projects = sorted(home.rglob("project.json"))
    bench_suite = json.loads(Path(args.bench).read_text(encoding="utf-8")) if args.bench else None
    rep = diagnose.doctor(code, [p.parent for p in projects], bench=bench_suite, run_tests=not args.no_tests)
    md = diagnose.render_markdown(rep)
    print(md)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not rep["health"] or rep["health"]["ok"] else 1


def cmd_evolve(args) -> int:
    import time as _time

    from .evolve.engine import InfraError
    from .evolve.supervisor import Heartbeat, Supervisor
    from .project import artel_home

    if args.supervise:  # no dashboard running: keep restarting generations whose process died
        print("supervising evolution: an interrupted generation is resumed automatically (Ctrl+C to stop)", flush=True)
        sup = Supervisor(artel_home() / "evolution", interval_s=60)
        sup.start()
        try:
            while True:
                _time.sleep(3600)
        except KeyboardInterrupt:
            sup.stop()
            return 0
    from .evolve.supervisor import AlreadyRunning, RepoLock

    try:
        lock = RepoLock(_repo(args)).acquire()
    except AlreadyRunning as e:
        print(f"not started: {e}")
        return 75
    hb = Heartbeat(artel_home() / "evolution" / _repo(args).name, ["evolve", *sys.argv[sys.argv.index("evolve") + 1:]]
                   if "evolve" in sys.argv else ["evolve"]).start()
    try:
        return _evolve_loop(args)
    finally:
        hb.stop()
        lock.release()


def _evolve_loop(args) -> int:
    import time as _time

    from .evolve.engine import InfraError

    while True:
        try:
            return _evolve_once(args)
        except InfraError as e:
            if args.no_wait:
                print(f"paused: {e}. Continue with `artel evolve --resume`.")
                return 75
            wait = max(60.0, (e.retry_at or _time.time() + 900) - _time.time() + 60)
            print(f"paused: {e}. Resuming in {int(wait)} s (the earliest reset).", flush=True)
            _time.sleep(wait)
            args.resume = True


def _evolve_once(args) -> int:
    ev = _evolution(args)
    opts = {"parallel": args.parallel_candidates, "finalists": args.finalists, "budget_usd": args.budget,
            "stack": not args.no_stack, "screen_tasks": args.screen_tasks.split(",") if args.screen_tasks else None}
    if args.reopen:
        ev.reopen(args.reopen, args.reason or "invalid measurement")
        args.resume = True
    if args.resume:
        s = ev.resume(**opts)
        print(json.dumps(s, ensure_ascii=False) if s else "no interrupted generation")
        return 0
    if args.cycle:
        for _ in range(args.cycle):
            print(json.dumps(ev.cycle(items=args.roadmap if args.roadmap is not None else 2, study=args.study,
                                      wave=args.wave, **opts), ensure_ascii=False))
        return 0
    if args.roadmap or args.items:
        ids = [i.strip() for i in args.items.split(",") if i.strip()] if args.items else None
        if ids:
            from .roadmap import Roadmap

            known = {it["id"] for it in Roadmap.of_repo(ev.repo).items}
            if set(ids) - known:
                print(f"no such roadmap items: {', '.join(sorted(set(ids) - known))}", file=sys.stderr)
                return 2
        for _ in range(args.generations):
            s = ev.roadmap_generation(args.roadmap or len(ids), wave=args.wave, ids=ids, **opts)
            print(json.dumps(s, ensure_ascii=False))
            if not s.get("roadmap"):
                break
        return 0
    for s in ev.run(args.generations, args.candidates, **opts):
        print(json.dumps(s, ensure_ascii=False))
    return 0


def cmd_providers(args) -> int:
    import subprocess

    from .brand import banner
    from .providers import logout_codex, overview
    from .runtimes.codex import codex_env, codex_settings

    cfg = _cfg(args)
    rt = "claude_code" if args.name == "claude" else args.name
    if args.action in ("use", "model", "models", "disable", "enable", "unlimit"):
        from . import limits
        from .providers import TOGGLABLE, model_catalog, routing, runtimes, save_routing

        r = routing(cfg)
        try:
            if args.action == "models":
                cat = model_catalog(cfg, rt)
                if not cat:
                    print(f"{rt}: список моделей недоступен (CLI не установлен или не отвечает); задайте имя вручную")
                    return 1
                cur = r["models"].get(rt) or ""
                for m in cat:
                    eff = f"  усилие: {', '.join(m['efforts'])}" if m.get("efforts") else ""
                    print(f"  {'*' if m['id'] == cur else ' '} {m['id']:<18} {m['name']}{eff}")
                return 0
            if args.action == "model":
                if rt not in ("claude_code", "codex"):
                    print("модель задаётся для claude_code или codex")
                    return 2
                r = save_routing(cfg, {"models": {rt: args.value or ""}})
                print(f"{rt}: модель {r['models'][rt] or 'по умолчанию'}")
                return 0
            if args.action == "use":
                if rt not in runtimes(cfg) and not (args.role and rt == "default"):
                    print(f"неизвестный рантайм {rt}; доступны: {', '.join(runtimes(cfg))}")
                    return 2
                if args.role:
                    by = dict(r["runtime_by_role"])
                    by.pop(args.role, None) if rt == "default" else by.__setitem__(args.role, rt)
                    r = save_routing(cfg, {"runtime_by_role": by})
                else:
                    r = save_routing(cfg, {"runtime": rt})
            elif args.action in ("disable", "enable"):
                if rt not in TOGGLABLE:
                    print(f"отключить можно: {', '.join(TOGGLABLE)}")
                    return 2
                d = [x for x in r["disabled_runtimes"] if x != rt] + ([rt] if args.action == "disable" else [])
                r = save_routing(cfg, {"disabled_runtimes": d})
            else:
                limits.clear(rt)
                r = routing(cfg)
        except ValueError as e:
            print(e)
            return 1
        print(f"по умолчанию: {r['runtime']}" + (f" → {r['auto_resolves_to']}" if r["runtime"] == "auto" else "")
              + "".join(f"; {k}: {v}" for k, v in r["runtime_by_role"].items())
              + (f"; отключены: {', '.join(r['disabled_runtimes'])}" if r["disabled_runtimes"] else ""))
        return 0
    if args.action in ("install", "uninstall"):
        from .tools import install, uninstall

        name = "claude" if args.name in ("claude", "claude_code") else args.name
        res = install(name) if args.action == "install" else uninstall(name)
        if not res.get("ok"):
            print(res.get("error", "failed"))
        return 0 if res.get("ok") else 1
    if args.action in ("login", "logout") and rt == "claude_code":
        from .providers import logout_claude
        from .runtimes.claude_code import child_env
        from .tools import resolve

        if args.action == "logout":
            res = logout_claude(cfg)
            print(res["detail"])
            return 0 if res["ok"] else 1
        binary = resolve((cfg.get("claude_code") or {}).get("binary", "claude"))
        if not binary:
            print("Claude Code CLI не установлен: artel providers install claude")
            return 1
        return subprocess.call([binary, "auth", "login", "--claudeai"], env=child_env(cfg))  # link, then paste the code
    if args.action == "login" and args.name == "codex":
        c = codex_settings(cfg)  # the CLI prints the link and the one-time code, then waits for the browser
        return subprocess.call([c["binary"], "login", "--device-auth"], env=codex_env(c, cfg))
    if args.action == "logout" and args.name == "codex":
        print(logout_codex(cfg)["detail"])
        return 0
    o = overview(cfg)
    print(banner("провайдеры"))
    for key, label in (("codex", "Codex"), ("claude_code", "Claude Code")):
        st = o[key]
        state = ("вход выполнен" + (f" ({st['method']})" if st.get("method") else "")) if st["logged_in"] else \
            ("не подключён" if st["installed"] else "не установлен")
        print(f"  {label:<14} {state:<34} {st.get('version', '')}")
    for k in o["keys"]:
        print(f"  {k['label']:<14} {'задан' if k['set'] else 'не задан':<34} ${k['env']}")
    em = o["embeddings"]
    print(f"  {'Эмбеддинги':<14} {('включены: ' + em['active']) if em['active'] != 'off' else ('выключены' if em['installed'] else 'не установлены — поиск по словам'):<34} "
          f"{'EmbeddingGemma 2 ' + em['quant'] + ', ' + str(em['dim']) + 'd' if em['installed'] else 'artel providers install embeddings'}")
    r = o["routing"]
    print(f"  рантайм по умолчанию: {r['runtime']}" + (f" (сейчас → {r['auto_resolves_to']})" if r["runtime"] == "auto" else "")
          + "".join(f"; {k}: {v}" for k, v in r["runtime_by_role"].items()))
    print("  модели: " + ", ".join(f"{k} = {v or 'по умолчанию'}" for k, v in r["models"].items()))
    if r["disabled_runtimes"]:
        print(f"  отключены: {', '.join(r['disabled_runtimes'])}")
    for k, v in r["limited"].items():
        print(f"  лимит: {k} до {fmt_ts(v, config=cfg)} — работа идёт на других")
    print("  настройка: artel providers use|models|model|disable|enable <рантайм> — подробнее: artel providers -h")
    return 0


def cmd_report(args) -> int:
    """Failure reports: list, show the exact text, send one to the developers."""
    from . import reports

    if args.action == "list":
        for r in reports.load(args.n):
            d = r.get("diagnosis") or {}
            sent = " · отправлен" if r.get("sent") else ""
            print(f"{r['id']}  {fmt_ts(r['ts'])}  {r['kind']:<9} {r['title'][:70]}{sent}")
            if d.get("cause"):
                print(f"            причина: {d['cause'][:110]}")
        return 0
    if not args.id:
        print("укажите id отчёта: artel report list")
        return 2
    rep = reports.get(args.id)
    if not rep:
        print("нет такого отчёта")
        return 1
    if args.action == "show":
        print(reports.markdown(rep))
        return 0
    res = reports.send_github(args.id)
    print(res.get("url") if res.get("ok") else f"не отправлено: {res.get('error')}")
    return 0 if res.get("ok") else 1


def cmd_why(args) -> int:
    """Ask the diagnostician why something does not work."""
    from .doctor import diagnose, format_report
    from .project import Project

    cfg = _cfg(args)
    project = Project.open(args.project, cfg) if args.project else None
    problem = args.problem or (project.meta.get("blocked_reason") if project else "") or ""
    if not problem:
        print("опишите проблему: artel why \"текст ошибки\" [--project slug] [--task id]")
        return 2
    print(format_report(diagnose(cfg, problem, project, args.task, use_model=not args.no_model)))
    return 0


def cmd_telegram(args) -> int:
    from .brand import banner
    from .telegram import TOKEN_ENV, Store, TelegramBot, token

    if args.action == "pair":
        print(f"Отправьте боту: /pair {Store().new_code()}  (код действует 15 минут)")
        return 0
    if not token():
        print(f"Задайте токен бота в переменной окружения {TOKEN_ENV} (получить: @BotFather → /newbot).")
        return 1
    from .web.server import Runner

    runner = Runner(_cfg(args))
    bot = TelegramBot(runner)
    code = bot.store.new_code() if not bot.store.data["paired"] else None
    print(banner("Telegram-бот запущен") + (f"\nОтправьте боту: /pair {code}" if code else ""), flush=True)
    bot.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def cmd_update(args) -> int:
    from .updater import Updater, UpdateError

    up = Updater(config=_cfg(args))
    try:
        if args.rollback:
            out = up.rollback()
            print(f"rolled back {out['from'][:10]} -> {out['to'][:10]}; restart running Artel processes")
            return 0
        st = up.status(fetch=True)
        if not st.get("available"):
            print(st.get("reason") or "up to date")
            return 0
        from .brand import banner

        print(banner("обновление"))
        print(f"{st['behind']} new commits on {st['channel']} ({st['current'][:10]} -> {st['remote'][:10]})")
        for r in st["roadmap_done"]:
            print(f"  done: {r['id']} — {r['title']} (from {r['from']})")
        for c in st["commits"][:20]:
            print(f"  {c['date'][:10]} {c['subject']}")
        if args.check:
            return 0
        if not st["can_update"]:
            print(f"cannot update: {st['reason']}")
            return 1
        out = up.apply(verify=not args.no_verify)
        print(f"updated {out['from'][:10]} -> {out['to'][:10]}" + (" (dependencies reinstalled)" if out.get("deps") else "")
              + "; restart running Artel processes (the dashboard restarts itself)")
        return 0
    except UpdateError as e:
        print(f"update failed: {e}")
        return 1


def _spawn_windows_reinstall(cmd, env):
    """A new console window for the PowerShell installer. CREATE_NEW_CONSOLE and DETACHED_PROCESS exclude each
    other (CreateProcess fails with both), so only the console flag is used."""
    import subprocess

    return subprocess.Popen(cmd, env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)


def cmd_reinstall(args) -> int:
    """Fresh app clone + fresh venv via the installer; user data in ARTEL_HOME is kept."""
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    import artel

    app = Path(artel.__file__).resolve().parents[1]
    windows = os.name == "nt"
    script = app / ("install.ps1" if windows else "install.sh")
    expected = Path(os.environ.get("ARTEL_DIR") or Path.home() / ".artel" / "app")
    try:
        installed = app.resolve() == expected.resolve()
    except OSError:
        installed = False
    if not script.is_file() or not installed:
        if not args.force:
            why = f"{script.name} not found next to the package" if not script.is_file() else (
                f"{app} is not the installed copy ({expected})")
            print(f"reinstall refused: this looks like a source checkout, not an installation ({why}). "
                  "Install with the one-line command from the README, or pass --force (a checkout is only ever moved aside, never deleted).", file=sys.stderr)
            return 2
        if not script.is_file():
            print(f"reinstall refused: {script} does not exist", file=sys.stderr)
            return 2
    env = dict(os.environ)
    if not installed:
        env["ARTEL_DIR"] = str(app)
    if not installed:
        print(f"Source checkout {app}: reinstall never deletes it. The directory is MOVED aside to "
              f"{app}.bak-<time> (its git state is not checked for deletion because it is not the default "
              f"install location), and a fresh copy is cloned in its place.", file=sys.stderr)
    tmpdir = tempfile.mkdtemp(prefix="artel-reinstall-")  # the app dir is about to be removed: run a copy
    tmp = Path(tmpdir) / script.name
    shutil.copyfile(script, tmp)
    if windows:
        env["ARTEL_PARENT_PID"] = str(os.getpid())
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp), "-Reinstall"]
        if args.yes:
            cmd.append("-Yes")
        _spawn_windows_reinstall(cmd, env)
        print("Переустановка продолжается в новом окне PowerShell.")
        return 0
    cmd = ["bash", str(tmp), "--reinstall"] + (["--yes"] if args.yes else [])
    sys.stdout.flush()
    os.execve("/bin/bash", cmd, env)  # replaces this process, so the venv is released
    return 1


def cmd_roadmap(args) -> int:
    from .roadmap import Roadmap

    repo = _repo(args)
    rm = Roadmap.of_repo(repo)
    if args.action == "render":
        md = rm.render_markdown()
        target = repo / "docs" / "ROADMAP.md"
        if args.check:
            ok = target.exists() and target.read_text(encoding="utf-8") == md
            print("docs/ROADMAP.md is up to date" if ok else "docs/ROADMAP.md is stale: run `artel roadmap render`")
            return 0 if ok else 1
        target.write_text(md, encoding="utf-8")
        print(f"wrote {target}")
    elif args.action == "html":
        from .roadmap_html import render_html

        page = render_html(repo)
        target = repo / "docs" / "roadmap.html"
        if args.check:
            ok = target.exists() and target.read_text(encoding="utf-8") == page
            print("docs/roadmap.html is up to date" if ok else "docs/roadmap.html is stale: run `artel roadmap html`")
            return 0 if ok else 1
        target.write_text(page, encoding="utf-8")
        print(f"wrote {target}")
    elif args.action == "study":
        from .roadmap import next_to_study

        for h in next_to_study(repo, n=args.n):
            print(f"{h.get('relevance', 0):>3} {h['repo']:<45} {h['category']:<14} {h.get('description', '')[:80]}")
    elif args.action == "next":
        for it in rm.next_items(args.n, wave=args.wave, mode=args.mode):
            s = it.get("source") or {}
            print(f"W{it.get('wave')} {it['id']:<28} {s.get('harness', '')} ({s.get('repo', '')}) — {it['title']}")
    else:
        print(json.dumps(rm.summary(), ensure_ascii=False))
        for it in rm.items:
            print(f"W{it.get('wave')} {it.get('status', ''):<11} {it.get('mode', ''):<7} {it['id']:<28} "
                  f"{(it.get('source') or {}).get('harness', '')}" + (f"  [{it['result']}]" if it.get("result") else ""))
    return 0


def cmd_absorb(args) -> int:
    from .roadmap import next_to_study

    ev = _evolution(args)
    if not args.source:
        nxt = next_to_study(ev.repo, n=1)
        if not nxt:
            print("every relevant harness in the registry has been studied")
            return 0
        args.source = nxt[0]["repo"]
    out = ev.absorb(args.source, focus=args.focus or "", top=args.top, run=args.run)
    print(f"{out['source']} ({out['license']}): {out['summary'][:300]}")
    for f in out["features"]:
        print(f"  {f.get('fit', 0):.2f}  {f['name']} → {f.get('target', '?')}: {f.get('value', '')[:120]}")
    print(f"roadmap: +{len(out['roadmap_added'])} {', '.join(out['roadmap_added'])}")
    if out.get("generation"):
        print(json.dumps(out["generation"], ensure_ascii=False))
    return 0


def cmd_evolution(args) -> int:
    import time as _time

    ev = _evolution(args)

    def on(e: dict) -> str:
        rt = e.get("runtime") or e.get("screen_runtime")
        return f"[{rt}]" if rt else ""

    for e in ev.ledger():
        if e["kind"] in ("candidate", "absorb_candidate"):
            mark = "MERGED" if e.get("merged") else ("ok" if e.get("accepted") else "rejected")
            print(f"g{e['generation']} {mark:<8} {e['branch']:<52} {on(e):<14} {e.get('reason', '')[:110]}")
        elif e["kind"] == "stack":
            mark = "MERGED" if e.get("merged") else ("ok" if e.get("accepted") else "rejected")
            print(f"g{e['generation']} {mark:<8} {e['branch']:<52} {on(e):<14} {e.get('reason', '')[:110]}")
        elif e["kind"] == "baseline":
            s = e["summary"]
            print(f"baseline {e['commit'][:10]} {on(e):<14} pass {s['pass_rate']:.0%}  partial {s['partial_mean']:.2f}  "
                  f"${s['cost_usd']}  {s['duration_mean_s']}s")
        elif e["kind"] == "limited":
            print(f"limit    {e['runtime']} ({e.get('step', '')}) until "
                  f"{_time.strftime('%Y-%m-%d %H:%M UTC', _time.gmtime(e['until']))}: {e.get('reason', '')[:100]}")
        elif e["kind"] == "absorb":
            print(f"absorb   {e['source']} ({e['license']}): {', '.join(e['features'][:6])}")
    if ev.runtimes:
        limits = ev.limits()
        print("runtimes: " + ", ".join(
            r + (f" (limited until {_time.strftime('%H:%M UTC', _time.gmtime(limits[r]))})" if r in limits else "")
            for r in ev.runtimes))
    return 0


def _robust_console() -> None:
    """Output never crashes on a console that cannot encode Cyrillic or ▲ (a Windows code page, a pipe, an ASCII
    locale): such characters are replaced instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None) -> int:
    _robust_console()
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="artel", description="Artel — a self-improving multi-agent harness")
    ap.add_argument("--config", help="extra YAML config merged over ~/.artel/artel.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="create a project from a goal and run it to completion")
    r.add_argument("goal")
    r.add_argument("--name")
    r.add_argument("--provider", choices=["anthropic", "openai_compat", "scripted"])
    r.add_argument("--model")
    r.add_argument("--parallel", type=int)
    r.add_argument("--max-tokens-total", type=int)
    r.add_argument("--web", action="store_true", help="also serve the dashboard")
    r.add_argument("--workdir", help="work directly in this directory (e.g. a git worktree) instead of shared/")
    r.add_argument("--ref", action="append", help="read-only source directory the team may study (repeatable)")
    r.add_argument("--seed", help="copy this directory into the deliverables directory before planning")
    r.add_argument("--runtime", type=_runtime_arg,
                   help="native | claude_code | codex")
    r.add_argument("--port", type=int, default=8765)
    r.set_defaults(fn=cmd_run)

    me = sub.add_parser("metrics", help="метрики приёмки проекта из журнала событий")
    me.add_argument("project")
    me.add_argument("--json", action="store_true")
    me.add_argument("--scenario", choices=["S", "L"], help="сверить с порогами сценария")
    me.set_defaults(fn=cmd_metrics)

    ac = sub.add_parser("acceptance", help="приёмка: сценарий S или L; независимая проверка проекта")
    acs = ac.add_subparsers(dest="action", required=True)
    ar = acs.add_parser("run", help="прогнать сценарий S или L")
    ar.add_argument("target", choices=["S", "L"], help="сценарий")
    ar.add_argument("--name")
    ar.add_argument("--provider", choices=["anthropic", "openai_compat", "scripted"])
    ar.add_argument("--runtime", type=_runtime_arg)
    av = acs.add_parser("verify", help="независимая проверка проекта на другом рантайме")
    av.add_argument("target", help="проект")
    av.add_argument("--provider", choices=["anthropic", "openai_compat", "scripted"])
    ac.set_defaults(fn=cmd_acceptance)

    rs = sub.add_parser("resume")
    rs.add_argument("project")
    rs.add_argument("--provider", choices=["anthropic", "openai_compat", "scripted"])
    rs.set_defaults(fn=cmd_resume)

    st = sub.add_parser("status")
    st.add_argument("project", nargs="?")
    st.set_defaults(fn=cmd_status)

    lg = sub.add_parser("log")
    lg.add_argument("project")
    lg.add_argument("--follow", "-f", action="store_true")
    lg.add_argument("--kinds")
    lg.set_defaults(fn=cmd_log)

    cm = sub.add_parser("comment")
    cm.add_argument("project")
    cm.add_argument("task")
    cm.add_argument("text")
    cm.set_defaults(fn=cmd_comment)

    w = sub.add_parser("web")
    w.add_argument("action", nargs="?", choices=["token"], help="token: about the remote-mode access token")
    w.add_argument("--new", action="store_true", help="with `token`: issue a new token, revoke all sessions")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--remote", action="store_true", help="bind beyond localhost; login with an access token")
    w.add_argument("--host", help="bind address in remote mode (default: config web.host, 0.0.0.0)")
    w.add_argument("--provider", choices=["anthropic", "openai_compat", "scripted"])
    w.set_defaults(fn=cmd_web)

    sk = sub.add_parser("skills")
    sk.add_argument("action", choices=["list", "view", "import", "curate", "pin", "unpin", "ledger", "rollback",
                                       "restore"])
    sk.add_argument("arg", nargs="?")
    sk.add_argument("--trust", choices=["builtin", "trusted", "community"])
    sk.add_argument("--include", default="*")
    sk.add_argument("--force", action="store_true")
    sk.set_defaults(fn=cmd_skills)

    def evo_common(p):
        p.add_argument("--repo", help="Artel git repo to evolve (default: this installation)")
        p.add_argument("--tasks", help="comma-separated benchmark task ids")
        p.add_argument("--parallel-bench", type=int, default=2)
        p.add_argument("--runtime", choices=["native", "claude_code", "codex"])
        p.add_argument("--provider", choices=["anthropic", "openai_compat", "scripted"])

    bn = sub.add_parser("bench", help="run the benchmark suite with hidden checks")
    evo_common(bn)
    bn.add_argument("--code", help="code revision directory to measure (default: this repo)")
    bn.add_argument("--label")
    bn.add_argument("--repeats", type=int, default=1)
    bn.set_defaults(fn=cmd_bench)

    dr = sub.add_parser("doctor", help="code health + trajectory diagnostics")
    dr.add_argument("--code")
    dr.add_argument("--repo")
    dr.add_argument("--home", help="directory whose projects to analyse (default: ARTEL_HOME)")
    dr.add_argument("--bench", help="suite.json to include")
    dr.add_argument("--json", help="also write the report as JSON")
    dr.add_argument("--no-tests", action="store_true")
    dr.set_defaults(fn=cmd_doctor)

    ev = sub.add_parser("evolve", help="recursive self-improvement: branches, measurement, selection")
    evo_common(ev)
    ev.add_argument("--generations", type=int, default=1)
    ev.add_argument("--supervise", action="store_true",
                    help="keep watching: a generation whose process died is resumed automatically (artel web does "
                         "this too unless evolve.auto_resume is false)")
    ev.add_argument("--candidates", type=int, default=2, help="hypotheses per generation (10, 20, 100…)")
    ev.add_argument("--parallel-candidates", type=int, default=2, help="candidates implemented at the same time")
    ev.add_argument("--screen-tasks", help="cheap benchmark subset every candidate runs first, e.g. adr-doc,csv-report")
    ev.add_argument("--finalists", type=int, help="how many screened candidates get the full benchmark")
    ev.add_argument("--budget", type=float, help="USD cap for one generation")
    ev.add_argument("--no-stack", action="store_true", help="merge only the single best candidate")
    ev.add_argument("--roadmap", type=int, metavar="N", help="implement the next N evolve items of roadmap.yaml")
    ev.add_argument("--wave", type=int, help="with --roadmap: only this wave")
    ev.add_argument("--items", metavar="IDS", help="implement exactly these roadmap items, e.g. C-agents-md,C-mcp-client")
    ev.add_argument("--cycle", type=int, metavar="K",
                    help="K turns of: study the next unstudied harness from the registry (findings join the "
                         "roadmap), then implement the next --roadmap items (default 2) in plan order")
    ev.add_argument("--study", type=int, default=1, help="with --cycle: harnesses studied per turn")
    ev.add_argument("--resume", action="store_true", help="finish the generation a crash or restart interrupted")
    ev.add_argument("--reopen", type=int, metavar="GEN",
                    help="void a generation's decisions (invalid measurement), keep its implementations, remeasure")
    ev.add_argument("--reason", help="with --reopen: why the measurement was invalid")
    ev.add_argument("--no-wait", action="store_true",
                    help="exit when the model provider hits a limit instead of waiting for the reset and resuming")
    ev.add_argument("--runtimes", help="subscriptions in order of preference, e.g. claude_code,codex: a limit on one "
                                       "moves the work to the next (overrides evolve.runtimes in the config)")
    ev.set_defaults(fn=cmd_evolve)

    pv = sub.add_parser("providers", help="subscriptions, API keys and runtimes; Codex device-code login")
    pv.add_argument("action", nargs="?", default="status",
                    choices=["status", "login", "logout", "install", "uninstall", "use", "models", "model", "disable",
                             "enable", "unlimit"],
                    help="use <runtime> [--role R] — who does the work; models <runtime> — what it can run; "
                         "model <runtime> <id> — pick one; disable|enable <runtime> — exclude from auto; "
                         "unlimit <runtime> — forget a recorded limit")
    pv.add_argument("name", nargs="?", default="codex")
    pv.add_argument("value", nargs="?", help="model id for `model`")
    pv.add_argument("--role", help="for `use`: set the runtime of one role only (empty runtime 'default' clears it)")
    pv.add_argument("--config")
    pv.set_defaults(fn=cmd_providers)

    rp = sub.add_parser("report", help="failure reports: list, show the text, send to the developers (GitHub issue)")
    rp.add_argument("action", nargs="?", default="list", choices=["list", "show", "send"])
    rp.add_argument("id", nargs="?")
    rp.add_argument("-n", type=int, default=20)
    rp.set_defaults(fn=cmd_report)

    wy = sub.add_parser("why", help="the diagnostician: why does it not work and what to do (on an available model)")
    wy.add_argument("problem", nargs="?", help="the error text (default: the project's stop reason)")
    wy.add_argument("--project")
    wy.add_argument("--task")
    wy.add_argument("--no-model", action="store_true", help="only the built-in known-cause rules")
    wy.add_argument("--config")
    wy.set_defaults(fn=cmd_why)

    tg = sub.add_parser("telegram", help="Telegram bot: control Artel and get notifications on the phone")
    tg.add_argument("action", nargs="?", default="run", choices=["run", "pair"])
    tg.add_argument("--config")
    tg.set_defaults(fn=cmd_telegram)

    upd = sub.add_parser("update", help="pull the improvements published since this installation (verified first)")
    upd.add_argument("--check", action="store_true", help="only show what is new")
    upd.add_argument("--rollback", action="store_true", help="return to the version before the last update")
    upd.add_argument("--no-verify", action="store_true", help="skip running the pillar tests on the new version")
    upd.add_argument("--config")
    upd.set_defaults(fn=cmd_update)

    rei = sub.add_parser("reinstall", help="переустановить Artel: свежая копия и окружение, данные сохраняются")
    rei.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение")
    rei.add_argument("--force", action="store_true", help="разрешить из исходного checkout (не установленной копии)")
    rei.set_defaults(fn=cmd_reinstall)

    rmp = sub.add_parser("roadmap", help="roadmap.yaml: status, next items, render docs/ROADMAP.md")
    rmp.add_argument("action", choices=["status", "next", "study", "render", "html"], nargs="?", default="status")
    rmp.add_argument("--repo")
    rmp.add_argument("-n", type=int, default=5)
    rmp.add_argument("--wave", type=int)
    rmp.add_argument("--mode", default="evolve", choices=["evolve", "design"])
    rmp.add_argument("--check", action="store_true", help="render: fail if docs/ROADMAP.md is stale")
    rmp.set_defaults(fn=cmd_roadmap)

    ab = sub.add_parser("absorb", help="analyse another harness and port its best features")
    evo_common(ab)
    ab.add_argument("source", nargs="?", help="git URL, owner/repo, or local path (default: the most relevant "
                                               "unstudied harness from the registry)")
    ab.add_argument("--focus")
    ab.add_argument("--top", type=int, default=3)
    ab.add_argument("--run", action="store_true",
                    help="implement the new items right away instead of waiting for their turn in the roadmap")
    ab.set_defaults(fn=cmd_absorb)

    el = sub.add_parser("evolution", help="show the experiment ledger")
    evo_common(el)
    el.set_defaults(fn=cmd_evolution)

    pl = sub.add_parser("plugins", help="plugin packages: install from git / pip / a path, enable, disable, remove")
    pl.add_argument("action", nargs="?", default="list",
                    choices=["list", "install", "show", "enable", "disable", "remove", "update"])
    pl.add_argument("target", nargs="?", help="install: git URL, pip spec or path; otherwise the plugin name "
                                               "(update all: 'all')")
    pl.add_argument("--yes", "-y", action="store_true", help="enable without asking (the manifest is still printed)")
    pl.add_argument("--enable", action="store_true", help="install: enable right after showing the manifest")
    pl.set_defaults(fn=cmd_plugins)

    rf = sub.add_parser("refine")
    rf.add_argument("action", choices=["history", "rollback"])
    rf.add_argument("project")
    rf.add_argument("edit", nargs="?")
    rf.set_defaults(fn=cmd_refine)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:  # Ctrl+C stops a command; a traceback would look like a crash
        print("\nОстановлено.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
