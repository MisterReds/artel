"""Keyword feature matrix over fetched READMEs + relevance ranking for Artel."""
import json, re, sys
from pathlib import Path

FEATURES = {
    "kanban": r"\bkanban\b|task board|\bboard\b.*\btask",
    "task_dag": r"\bdag\b|dependency graph|depends[_ ]on|blocked[_ ]?by",
    "worktree": r"worktree",
    "sandbox": r"sandbox|docker|container|micro-?vm|firecracker",
    "skills": r"skill\.md|agent skills|\bskills?\b",
    "self_improve": r"self[- ]improv|self[- ]evolv|learns? from|continual learning|auto[- ]?improv|evolv(e|ing) (its|the) (harness|prompt)",
    "memory": r"\bmemory\b",
    "verifier": r"verif(y|ier|ication)|\breviewer\b|\baudit|\bjudge\b|\bcritic\b|quality gate",
    "goal_loop": r"until (it'?s )?(done|complete|finished)|ralph|long[- ]running|long[- ]horizon|autonomous (mode|loop)|keeps? (going|working)",
    "persona": r"\bsoul\b|persona|identity\.md|\broles?\b",
    "multi_project": r"multi[- ]project|multiple projects|portfolio|across projects|projects? dashboard",
    "dashboard": r"dashboard|web ui|\bgui\b|desktop app",
    "observability": r"opentelemetry|\botel\b|tracing|langfuse|observab",
    "budget": r"\bbudget|cost (control|tracking|limit)|spend",
    "mcp": r"\bmcp\b|model context protocol",
    "agent_messaging": r"\ba2a\b|agent[- ]to[- ]agent|mailbox|inter[- ]agent|message each other",
    "human_loop": r"human[- ]in[- ]the[- ]loop|approval|approve",
    "durable_resume": r"checkpoint|resum(e|able)|durable|rewind|crash[- ]recover",
    "best_of_n": r"best[- ]of[- ]n|parallel attempts|multiple candidates|tournament|pass@k",
    "spec_driven": r"spec[- ]driven|\bprd\b|specification",
    "plugins": r"\bplugins?\b|extensions?\b",
    "benchmark": r"swe-?bench|terminal-?bench|arc-?agi|\bgaia\b|tau-?bench",
    "scheduling": r"\bcron\b|schedul|heartbeat",
    "git_pr": r"pull request|\bopen(s)? (a )?prs?\b",
    "chat_surfaces": r"telegram|slack|discord|whatsapp",
    "multi_model": r"openrouter|ollama|multi[- ]provider|any (model|provider)|provider[- ]agnostic",
}
RELEVANT = {"kanban": 3, "task_dag": 2, "verifier": 2, "goal_loop": 3, "self_improve": 3, "skills": 1, "persona": 1,
            "multi_project": 3, "dashboard": 1, "best_of_n": 2, "agent_messaging": 2, "durable_resume": 1,
            "observability": 1, "budget": 1}

def main(reg_path, readme_dir):
    reg = json.load(open(reg_path))
    for e in reg:
        p = Path(readme_dir) / (e["repo"].replace("/", "__") + ".md")
        if not p.exists():
            continue
        text = p.read_text(errors="replace").lower()
        feats = [f for f, pat in FEATURES.items() if re.search(pat, text)]
        e["features"] = feats
        claims = sorted(set(m.group(0) for m in re.finditer(
            r"(swe-?bench[\w -]{0,20}|terminal-?bench[\w .-]{0,12}|arc-?agi[\w -]{0,6}|gaia)[^.\n]{0,40}?\d{1,3}(\.\d)?\s?%", text)))[:3]
        if claims:
            e["benchmark_claims"] = claims
        e["relevance"] = sum(RELEVANT.get(f, 0) for f in feats) + 2 * len(e["sources"])
    json.dump(reg, open(reg_path, "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main(*sys.argv[1:3])
