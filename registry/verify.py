"""Mark each registry entry alive/dead via `git ls-remote` (no GitHub API needed)."""
import json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

path = sys.argv[1]
reg = json.load(open(path))

def check(e):
    if e["repo"].startswith("closed:"):
        return "closed"
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", f"https://github.com/{e['repo']}.git"],
                           capture_output=True, text=True, timeout=30,
                           env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"})
        return "alive" if r.returncode == 0 else "dead"
    except subprocess.TimeoutExpired:
        return "timeout"

with ThreadPoolExecutor(48) as ex:
    for e, s in zip(reg, ex.map(check, reg)):
        e["status"] = s
json.dump(reg, open(path, "w"), ensure_ascii=False, indent=1)
from collections import Counter
print(Counter(e["status"] for e in reg))
