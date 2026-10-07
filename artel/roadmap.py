"""The roadmap (roadmap.yaml) is the single source of truth for what Artel adopts, from where, and in what order.

Evolution follows it: ``artel evolve --roadmap N`` turns the next N ``mode: evolve`` items into hypotheses,
hands the donor harness's sources to the implementing team, and marks merged items done. ``design`` items
are structural and are implemented by hand; ``pillar`` entries are foundations guarded by tests/test_invariants.py.

Status updates edit the file in place (line-based) so comments and layout survive.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

import yaml

OPEN = ("planned", "partial", "in_progress")
ABSORB_WAVE = 4                       # items found by studying other harnesses queue after the hand-planned waves
STUDY_CATEGORIES = ("harness", "orchestrator", "memory-skills", "framework", "sandbox", "eval", "protocol")
STATUS_MARK = {"done": "готово", "partial": "частично", "in_progress": "в работе", "planned": "план",
               "blocked": "нужна помощь"}


class Roadmap:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.pillars: list[dict] = self.data.get("pillars", [])
        self.items: list[dict] = self.data.get("items", [])

    @classmethod
    def of_repo(cls, repo: Path) -> "Roadmap":
        return cls(Path(repo) / "roadmap.yaml")

    def get(self, item_id: str) -> dict:
        for it in self.pillars + self.items:
            if it["id"] == item_id:
                return it
        raise KeyError(item_id)

    # --- ordering -------------------------------------------------------
    def ready(self, it: dict) -> bool:
        return all(self.get(d).get("status") == "done" for d in it.get("after", []))

    def next_items(self, n: int, wave: Optional[int] = None, mode: str = "evolve",
                   skip: tuple = ()) -> list[dict]:
        """Open items of the lowest open wave first, in file order, whose ``after`` dependencies are done."""
        open_items = [it for it in self.items if it.get("mode") == mode and it.get("status") in ("planned", "partial")
                      and it["id"] not in skip and (wave is None or it.get("wave") == wave) and self.ready(it)]
        open_items.sort(key=lambda it: it.get("wave", 99))  # stable: file order within a wave
        return open_items[:n]

    # --- updates (in place, comment preserving) -------------------------
    def set_status(self, item_id: str, status: str, note: str = "") -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        start = next((i for i, ln in enumerate(lines) if re.match(rf"\s*- id: {re.escape(item_id)}\s*$", ln)), None)
        if start is None:
            raise KeyError(item_id)
        indent = " " * (len(lines[start]) - len(lines[start].lstrip()) + 2)
        end = start + 1
        while end < len(lines) and lines[end].startswith(indent):
            end += 1
        block = lines[start:end]
        block = [ln for ln in block if not ln.startswith(indent + "result:")]
        done = False
        for i, ln in enumerate(block):
            if ln.startswith(indent + "status:"):
                block[i] = f"{indent}status: {status}\n"
                done = True
        if not done:
            block.insert(1, f"{indent}status: {status}\n")
        if note:
            pos = next(i for i, ln in enumerate(block) if ln.startswith(indent + "status:")) + 1
            block.insert(pos, f"{indent}result: {yaml.safe_dump(note, default_style=chr(34), width=10_000).strip()}\n")
        self.path.write_text("".join(lines[:start] + block + lines[end:]), encoding="utf-8")
        self.__init__(self.path)

    def add_items(self, items: list[dict], header: str = "") -> list[str]:
        """Append new items at the end of the file (``items`` is the last section). Existing ids are skipped."""
        known = {it["id"] for it in self.pillars + self.items}
        new = [it for it in items if it["id"] not in known]
        if not new:
            return []
        text = self.path.read_text(encoding="utf-8").rstrip("\n") + "\n"
        if header:
            text += f"\n  # ---- {header} ----\n"
        for it in new:
            dumped = yaml.safe_dump([it], sort_keys=False, allow_unicode=True, width=110)
            text += "".join("  " + ln + "\n" for ln in dumped.splitlines())
        self.path.write_text(text, encoding="utf-8")
        self.__init__(self.path)
        return [it["id"] for it in new]

    def text_for_agents(self) -> str:
        """Compact roadmap for the absorber: pillars (protected) and every item with its donor and status."""
        rows = [f"- PILLAR {p['id']} ({(p.get('source') or {}).get('harness', '')}): {p['title']}" for p in self.pillars]
        rows += [f"- {it['id']} [{it.get('mode')}, {it.get('status')}] {it['title']} — from "
                 f"{(it.get('source') or {}).get('harness', '')}" for it in self.items]
        return "\n".join(rows)

    # --- donors ---------------------------------------------------------
    def fetch_source(self, it: dict, sources: Path) -> Optional[Path]:
        """Shallow-clone the donor repo (cached). Returns None when it cannot be fetched."""
        repo = (it.get("source") or {}).get("repo")
        if not repo:
            return None
        if Path(repo).expanduser().exists():
            return Path(repo).expanduser()
        dest = Path(sources) / repo.replace("/", "__")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = repo if "://" in repo else f"https://github.com/{repo}.git"
            p = subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(dest)], capture_output=True,
                               text=True, timeout=900, encoding="utf-8", errors="replace")
            if p.returncode != 0:
                return None
        return dest

    def to_hypothesis(self, it: dict, sources: Path):
        from .evolve.engine import Hypothesis, detect_license

        src = it.get("source") or {}
        path = self.fetch_source(it, sources)
        refs, note = [], f"Donor {src.get('harness', '?')} ({src.get('repo', '?')}) could not be fetched; work from the plan."
        if path:
            lic = detect_license(path)
            refs = [str(path)]
            where = ", ".join(str(path / p) for p in src.get("paths", [])) or str(path)
            note = (f"Donor: {src.get('harness')} — study first: {where}. License {lic}: " +
                    ("copying allowed with attribution; keep the notice in a comment."
                     if lic in ("MIT", "Apache-2.0", "BSD") else "reimplement the idea; do not copy code."))
        return Hypothesis(title=f"{it['id']}: {it['title']}", rationale=f"Roadmap item {it['id']} (wave {it.get('wave')}).",
                          plan=str(it.get("plan") or f"{it['title']}. Port it into {', '.join(it.get('target', [])) or 'Artel'}.").strip(), expected="; ".join(it.get("acceptance", [])),
                          files=list(it.get("target", [])), refs=refs, source=f"roadmap:{it['id']}",
                          license_note=note)

    # --- rendering ------------------------------------------------------
    def studied_markdown(self) -> list[str]:
        rows = load_studied(self.path.parent)
        if not rows:
            return []
        out = ["", f"## Изученные проекты — {len(rows)}", "",
               "Код прочитан агентом-аналитиком (`artel absorb` / `artel evolve --cycle`); найденное стало пунктами выше.",
               "", "| Проект | Лицензия | Найдено | В карту | Итог |", "|---|---|---|---|---|"]
        for r in rows:
            out.append(f"| `{r['repo']}` | {r.get('license', '')} | {r.get('found', 0)} | "
                       f"{', '.join(r.get('added', [])) or '—'} | {str(r.get('summary', ''))[:160]} |")
        return out

    def render_markdown(self) -> str:
        out = ["# Дорожная карта Artel", "",
               "Сгенерировано из `roadmap.yaml` командой `artel roadmap render`. Правки — только в yaml. "
               "Что уже взято и откуда — [ADOPTED.md](ADOPTED.md); опоры — [ARCHITECTURE.md](ARCHITECTURE.md).", "",
               "Режимы: **pillar** — опора, контракт закреплён `tests/test_invariants.py`; **design** — структурное "
               "изменение, делается вручную; **evolve** — реализует сам Artel (`artel evolve --roadmap`).", "",
               "## Опоры", "", "| Опора | Что | Источник | Статус |", "|---|---|---|---|"]
        for p in self.pillars:
            s = p.get("source") or {}
            out.append(f"| {p['id']} | {p['title']} | {s.get('harness', '')} | {STATUS_MARK.get(p.get('status'), p.get('status'))} |")
        waves = sorted({it.get("wave", 0) for it in self.items})
        for w in waves:
            items = [it for it in self.items if it.get("wave", 0) == w]
            done = sum(1 for it in items if it.get("status") == "done")
            label = " (найдено разбором проектов)" if w == ABSORB_WAVE else ""
            out += ["", f"## Волна {w}{label} — {done}/{len(items)} готово", "",
                    "| Пункт | Что | Откуда | Режим | Статус |", "|---|---|---|---|---|"]
            for it in items:
                s = it.get("source") or {}
                st = STATUS_MARK.get(it.get("status"), it.get("status"))
                if it.get("result"):
                    st += f" — {it['result']}"
                out.append(f"| {it['id']} | {it['title']} | {s.get('harness', '')} (`{s.get('repo', '')}`) | "
                           f"{it.get('mode')} | {st} |")
        total = len(self.items)
        done = sum(1 for it in self.items if it.get("status") == "done")
        out += self.studied_markdown()
        out += ["", f"Итого: {done}/{total} пунктов готово.", ""]
        return "\n".join(out)

    def summary(self) -> dict:
        from collections import Counter

        return {"pillars": len(self.pillars), "items": len(self.items),
                "by_status": dict(Counter(it.get("status") for it in self.items)),
                "by_mode": dict(Counter(it.get("mode") for it in self.items))}


# --- studied harnesses (studied.yaml next to roadmap.yaml) --------------------------------------

def load_studied(repo: Path) -> list[dict]:
    p = Path(repo) / "studied.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


def add_studied(repo: Path, row: dict) -> None:
    rows = [r for r in load_studied(repo) if r["repo"] != row["repo"]] + [row]
    (Path(repo) / "studied.yaml").write_text(
        "# Harnesses whose code Artel's absorber has read. Written by `artel absorb` / `artel evolve --cycle`.\n"
        + yaml.safe_dump(rows, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")


def next_to_study(repo: Path, registry: Optional[list] = None, n: int = 1) -> list[dict]:
    """Most relevant live harnesses from the registry that have not been studied yet.

    Already covered: anything in studied.yaml and every donor named in roadmap.yaml (studied by hand)."""
    if registry is None:
        import json

        registry = json.loads((Path(__file__).parent / "data" / "harnesses.json").read_text(encoding="utf-8"))
    rm = Roadmap.of_repo(repo)
    seen = {r["repo"].lower() for r in load_studied(repo)}
    seen |= {str((it.get("source") or {}).get("repo", "")).lower() for it in rm.pillars + rm.items}
    pool = [h for h in registry if h.get("status") == "alive" and h.get("category") in STUDY_CATEGORIES
            and "/" in h.get("repo", "") and h["repo"].lower() not in seen]
    pool.sort(key=lambda h: (h.get("relevance", 0), h.get("last_commit") or ""), reverse=True)
    return pool[:n]
