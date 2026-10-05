"""Build the harness registry from curated awesome lists (+ manual additions).

Usage: python registry/build_registry.py <lists_dir> > artel/data/harnesses.json
Each entry: repo, sources, sections, description, category.
"""
import json, re, sys
from pathlib import Path

LINK = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
SKIP_OWNERS = {"topics", "orgs", "sponsors", "features", "marketplace", "apps", "settings", "user-attachments", "search"}

# section/description keyword -> category, first match wins (order matters)
RULES = [
    ("list", r"awesome|curated list|ecosystem map"),
    ("orchestrator", r"orchestrat|parallel coding|swarm|kanban|loop runner|task runner|multi-agent|fleet|worktree"),
    ("sandbox", r"sandbox|execution substrate|microvm|container|firecracker|e2b|daytona"),
    ("protocol", r"protocol|mcp|a2a|agent contract|tool interface|acp\b"),
    ("eval", r"evaluat|benchmark|leaderboard|bench\b|testing harness|lm-eval"),
    ("observability", r"observab|tracing|telemetry|agentops|monitor|reliability op"),
    ("security", r"guardrail|security|govern|fuzz|red.?team|safety"),
    ("memory-skills", r"memory|context|working.state|skill|knowledge|rag\b"),
    ("devops", r"ci/cd|devops|pipeline platform|jenkins|github actions"),
    ("framework", r"framework|sdk|library|workflow framework|graph"),
    ("harness", r"harness|coding agent|agent runtime|personal assistant|reference harness|cli agent|terminal agent|autonomous"),
]

def classify(text):
    t = text.lower()
    for cat, pat in RULES:
        if re.search(pat, t):
            return cat
    return "other"

def clean(s):
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>|[*_`|]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -:—")

def main(lists_dir, extra=None):
    reg = {}
    for readme in sorted(Path(lists_dir).glob("*/README.md")):
        src = readme.parent.name.replace("_", "/", 1)
        section = ""
        for line in readme.read_text(errors="replace").splitlines():
            h = re.match(r"^#{2,4}\s+(.*)", line)
            if h:
                section = clean(h.group(1))
                continue
            for owner, repo in LINK.findall(line):
                repo = re.sub(r"(\.git|[.)#]+)$", "", repo)
                if owner.lower() in SKIP_OWNERS or not repo or f"{owner}/{repo}".lower() == src.lower():
                    continue
                key = f"{owner}/{repo}"
                e = reg.setdefault(key.lower(), {"repo": key, "sources": [], "sections": [], "descriptions": []})
                if src not in e["sources"]:
                    e["sources"].append(src)
                if section and section not in e["sections"]:
                    e["sections"].append(section)
                d = clean(line)[:300]
                if d and d not in e["descriptions"]:
                    e["descriptions"].append(d)
    if extra:
        for item in json.loads(Path(extra).read_text()):
            e = reg.setdefault(item["repo"].lower(), {"repo": item["repo"], "sources": [], "sections": [], "descriptions": []})
            e["sources"].append("manual")
            e["descriptions"].insert(0, item.get("description", ""))
            if item.get("category"):
                e["category_override"] = item["category"]
    out = []
    for e in reg.values():
        e["category"] = e.pop("category_override", None) or classify(" ".join(e["sections"][:2]) + " " + " ".join(e["descriptions"][:1]))
        e["description"] = e["descriptions"][0] if e["descriptions"] else ""
        del e["descriptions"]
        out.append(e)
    out.sort(key=lambda e: (-len(e["sources"]), e["repo"].lower()))
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
