"""artel — command line.

    artel run "<goal>" [--name N] [--provider P] [--web]   create a project and drive it to completion
    artel resume <project>                                  continue a stopped / budget-paused project
    artel status [<project>]                                projects, or one board
    artel log <project> [--follow] [--kinds a,b]            the event trajectory
    artel comment <project> <task> "<text>"                 message a task as the human (unblocks escalations)
    artel web [--port 8765] [--remote [--host H]]           live dashboard (remote: login, docs/REMOTE.md)
    artel web token [--new]                                 remote-mode access token (--new rotates it)
    artel skills list|view|import|curate|pin|unpin|ledger|rollback|restore
    artel refine history|rollback <project> [<edit>]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from .manager import human_comment, new_kernel, resume, run_goal
from .tz import fmt_ts
from .project import Project, list_projects, load_config

ICONS = {"todo": "·", "ready": "○", "running": "▶", "review": "◎", "done": "✓", "blocked": "✗", "cancelled": "–"}


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


def cmd_run(args) -> int:
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
    project, status = holder["result"]
    print_board(project)
    print(f"\nproject: {project.root}\ndeliverables: {project.shared}\nstatus: {status}")
    return 0 if status == "done" else 2


def cmd_resume(args) -> int:
    stop = threading.Event()
    holder: dict = {}
    th = threading.Thread(target=lambda: holder.update(r=resume(args.project, _cfg(args), stop_event=stop)))
    th.start()
    try:
        while th.is_alive():
            th.join(1)
    except KeyboardInterrupt:
        stop.set()
        th.join()
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
    bench_suite = json.loads(Path(args.bench).read_text()) if args.bench else None
    rep = diagnose.doctor(code, [p.parent for p in projects], bench=bench_suite, run_tests=not args.no_tests)
    md = diagnose.render_markdown(rep)
    print(md)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if not rep["health"] or rep["health"]["ok"] else 1


def cmd_evolve(args) -> int:
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
    if args.roadmap:
        for _ in range(args.generations):
            s = ev.roadmap_generation(args.roadmap, wave=args.wave, **opts)
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
    if args.action in ("install", "uninstall"):
        from .tools import install, uninstall

        name = "claude" if args.name in ("claude", "claude_code") else "codex"
        res = install(name) if args.action == "install" else uninstall(name)
        if not res.get("ok"):
            print(res.get("error", "failed"))
        return 0 if res.get("ok") else 1
    if args.action == "login" and args.name == "codex":
        c = codex_settings(cfg)  # the CLI prints the link and the one-time code, then waits for the browser
        return subprocess.call([c["binary"], "login", "--device-auth"], env=codex_env(c))
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
    s1 = o["system_one"]
    print(f"  {'System One':<14} {('подключена: ' + s1['name']) if s1['connected'] else 'не подключена':<34} "
          f"{'точки: ' + ', '.join(s1['points']) if s1['points'] else ''}")
    r = o["routing"]
    print(f"  рантайм по умолчанию: {r['runtime']}" + "".join(f"; {k}: {v}" for k, v in r["runtime_by_role"].items()))
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


def cmd_roadmap(args) -> int:
    from .roadmap import Roadmap

    repo = _repo(args)
    rm = Roadmap.of_repo(repo)
    if args.action == "render":
        md = rm.render_markdown()
        target = repo / "docs" / "ROADMAP.md"
        if args.check:
            ok = target.exists() and target.read_text() == md
            print("docs/ROADMAP.md is up to date" if ok else "docs/ROADMAP.md is stale: run `artel roadmap render`")
            return 0 if ok else 1
        target.write_text(md)
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["local"]:
        print("the local System One model was removed; connect a System One as described in docs/SYSTEM_ONE.md")
        return 2
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
    r.add_argument("--runtime", choices=["native", "claude_code", "codex"])
    r.add_argument("--port", type=int, default=8765)
    r.set_defaults(fn=cmd_run)

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
    ev.add_argument("--candidates", type=int, default=2, help="hypotheses per generation (10, 20, 100…)")
    ev.add_argument("--parallel-candidates", type=int, default=2, help="candidates implemented at the same time")
    ev.add_argument("--screen-tasks", help="cheap benchmark subset every candidate runs first, e.g. adr-doc,csv-report")
    ev.add_argument("--finalists", type=int, help="how many screened candidates get the full benchmark")
    ev.add_argument("--budget", type=float, help="USD cap for one generation")
    ev.add_argument("--no-stack", action="store_true", help="merge only the single best candidate")
    ev.add_argument("--roadmap", type=int, metavar="N", help="implement the next N evolve items of roadmap.yaml")
    ev.add_argument("--wave", type=int, help="with --roadmap: only this wave")
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
    pv.add_argument("action", nargs="?", default="status", choices=["status", "login", "logout", "install", "uninstall"])
    pv.add_argument("name", nargs="?", default="codex", choices=["codex", "claude", "claude_code"])
    pv.add_argument("--config")
    pv.set_defaults(fn=cmd_providers)

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

    rmp = sub.add_parser("roadmap", help="roadmap.yaml: status, next items, render docs/ROADMAP.md")
    rmp.add_argument("action", choices=["status", "next", "study", "render"], nargs="?", default="status")
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

    rf = sub.add_parser("refine")
    rf.add_argument("action", choices=["history", "rollback"])
    rf.add_argument("project")
    rf.add_argument("edit", nargs="?")
    rf.set_defaults(fn=cmd_refine)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
