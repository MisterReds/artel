"""artel — command line.

    artel run "<goal>" [--name N] [--provider P] [--web]   create a project and drive it to completion
    artel resume <project>                                  continue a stopped / budget-paused project
    artel status [<project>]                                projects, or one board
    artel log <project> [--follow] [--kinds a,b]            the event trajectory
    artel comment <project> <task> "<text>"                 message a task as the human (unblocks escalations)
    artel web [--port 8765]                                 live dashboard
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
from .project import Project, list_projects, load_config

ICONS = {"todo": "·", "ready": "○", "running": "▶", "review": "◎", "done": "✓", "blocked": "✗", "cancelled": "–"}


def _cfg(args) -> dict:
    cfg = load_config(getattr(args, "config", None))
    if getattr(args, "provider", None):
        cfg["provider"] = args.provider
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
    print(f"{project.slug}  [{m['status']}]  {m['name']}")
    print(f"  tokens in/out {u['input_tokens']}/{u['output_tokens']}  requests {u['requests']}  "
          f"audits {m.get('audit_rounds', 0)}")
    for p in project.personas():
        print(f"  @{p.name} — {p.role}")
    for t in project.board.list():
        flag = " [needs human]" if t["needs_human"] else ""
        print(f"  {ICONS.get(t['status'], '?')} {t['status']:<9} {t['id']}  {t['assignee'] or '-':<10} "
              f"{t['title']}{flag}  (attempts {t['attempts']})")


def _event_line(e: dict) -> str:
    k = e["kind"]
    t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
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
        print(f"dashboard: http://127.0.0.1:{args.port}")
    holder: dict = {}

    def target():
        holder["result"] = run_goal(args.goal, cfg, name=args.name, stop_event=stop)

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
    project = Project.open(args.project, load_config())
    kinds = set(args.kinds.split(",")) if args.kinds else None
    after = 0
    while True:
        for e in project.log.read(after=after, kinds=kinds):
            print(_event_line(e))
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
    from .web.server import serve

    serve(port=args.port, config=_cfg(args))
    return 0


def cmd_skills(args) -> int:
    store = new_kernel(load_config()).get("skills")
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
            print(f"{e['id']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))}  {e['skill']:<36} "
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


def main(argv=None) -> int:
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
    w.add_argument("--port", type=int, default=8765)
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

    rf = sub.add_parser("refine")
    rf.add_argument("action", choices=["history", "rollback"])
    rf.add_argument("project")
    rf.add_argument("edit", nargs="?")
    rf.set_defaults(fn=cmd_refine)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
