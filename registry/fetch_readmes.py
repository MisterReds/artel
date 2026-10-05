"""Fetch README + last commit date for registry entries via blobless shallow clone (no API)."""
import json, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

reg_path, out_dir, cats = sys.argv[1], Path(sys.argv[2]), set(sys.argv[3].split(","))
out_dir.mkdir(parents=True, exist_ok=True)
reg = json.load(open(reg_path))
ENV = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": "/root"}

def run(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, timeout=120, env=ENV)

def fetch(e):
    if e.get("status") != "alive" or e["category"] not in cats:
        return None
    slug = e["repo"].replace("/", "__")
    target = out_dir / f"{slug}.md"
    if target.exists():
        return e["repo"], json.loads((out_dir / f"{slug}.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix="rd-"))
    info = {"last_commit": None, "readme": None}
    try:
        r = run("git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout",
                f"https://github.com/{e['repo']}.git", str(tmp / "r"))
        if r.returncode:
            return e["repo"], info
        info["last_commit"] = run("git", "log", "-1", "--format=%cI", cwd=tmp / "r").stdout.strip()
        names = run("git", "ls-tree", "--name-only", "HEAD", cwd=tmp / "r").stdout.split()
        readme = next((n for n in names if n.lower() in ("readme.md", "readme.mdx", "readme.rst", "readme")), None)
        if readme:
            run("git", "checkout", "HEAD", "--", readme, cwd=tmp / "r")
            text = (tmp / "r" / readme).read_text(errors="replace")
            target.write_text(text[:200_000])
            info["readme"] = target.name
            info["readme_chars"] = len(text)
        info["top_level"] = names[:80]
    except Exception as exc:  # noqa: BLE001
        info["error"] = repr(exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    (out_dir / f"{slug}.json").write_text(json.dumps(info))
    return e["repo"], info

with ThreadPoolExecutor(32) as ex:
    results = dict(r for r in ex.map(fetch, reg) if r)
for e in reg:
    if e["repo"] in results:
        e.update({k: v for k, v in results[e["repo"]].items() if k in ("last_commit", "readme_chars")})
json.dump(reg, open(reg_path, "w"), ensure_ascii=False, indent=1)
print(len(results), "fetched;", sum(1 for v in results.values() if v.get("readme")), "with README")
