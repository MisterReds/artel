import re, sys
from pathlib import Path
KEY = re.compile(r"kanban|board|dag|verif|review|audit|judge|loop|until|self[- ]improv|evolv|learn|skill|soul|persona|role|project|dashboard|best[- ]of|parallel|mailbox|message|budget|resume|checkpoint|worktree|manager|orchestr|supervis|heartbeat|retry|fail|bench|%", re.I)
def digest(repo, rd, n=2600):
    p = Path(rd) / (repo.replace("/", "__") + ".md")
    if not p.exists(): return f"## {repo}\n(no readme)\n"
    lines = [l.strip() for l in p.read_text(errors="replace").splitlines()]
    lines = [re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)", r"\1", l) for l in lines]
    lines = [l for l in lines if l and not l.startswith(("[!", "```", "|--", "---")) and len(l) > 3]
    head = lines[:12]
    keys = [l for l in lines[12:] if KEY.search(l) and len(l) < 400]
    out, size = [], 0
    for l in head + ["…"] + keys:
        if size > n: break
        out.append(l); size += len(l)
    return f"## {repo}\n" + "\n".join(out) + "\n"
for r in sys.argv[2:]:
    print(digest(r, sys.argv[1]))
