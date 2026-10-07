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
        """Open items whose ``after`` dependencies are done: ``priority: core`` first (gaps against the main
        harnesses), then the lowest wave, in file order."""
        open_items = [it for it in self.items if it.get("mode") == mode and it.get("status") in ("planned", "partial")
                      and it["id"] not in skip and (wave is None or it.get("wave") == wave) and self.ready(it)]
        # what the main harnesses have and Artel lacks goes first (harnesses.yaml), then by wave; file order within
        open_items.sort(key=lambda it: (it.get("priority") != "core", it.get("wave", 99)))
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
    """Harnesses to study next: the most significant main harnesses whose code has not been read (harnesses.yaml),
    then the most relevant live ones from the registry.

    Already covered: anything in studied.yaml and every donor named in roadmap.yaml (studied by hand)."""
    if registry is None:
        import json

        registry = json.loads((Path(__file__).parent / "data" / "harnesses.json").read_text(encoding="utf-8"))
    rm = Roadmap.of_repo(repo)
    seen = {r["repo"].lower() for r in load_studied(repo)}
    # the main harnesses come first: by significance (harnesses.yaml), those whose code has not been read yet
    major = [h for h in load_harnesses(repo).get("harnesses", []) if h.get("studied") != "code"
             and h["repo"].lower() not in seen and h.get("origin") != "derivative"]
    major.sort(key=lambda h: significance(h)["total"], reverse=True)
    picked = [{"repo": h["repo"], "category": h.get("class", ""), "relevance": round(significance(h)["total"]),
               "description": h.get("origin_note", ""), "status": "alive"} for h in major][:n]
    if len(picked) >= n:
        return picked
    seen |= {str((it.get("source") or {}).get("repo", "")).lower() for it in rm.pillars + rm.items}
    pool = [h for h in registry if h.get("status") == "alive" and h.get("category") in STUDY_CATEGORIES
            and "/" in h.get("repo", "") and h["repo"].lower() not in seen]
    pool.sort(key=lambda h: (h.get("relevance", 0), h.get("last_commit") or ""), reverse=True)
    taken = {p["repo"].lower() for p in picked}
    return picked + [h for h in pool if h["repo"].lower() not in taken][:n - len(picked)]


# --- significance of harnesses (harnesses.yaml next to roadmap.yaml) ----------------------------

SIGNIFICANCE = (  # (key, weight, label, how it is measured) — documented in the HTML report
    ("adoption", 35, "Популярность", "звёзды GitHub по логарифмической шкале: 1 000 → 15, 10 000 → 22, 100 000 → 30, 400 000 → 35"),
    ("community", 10, "Сообщество", "форки — сколько людей строят на этом коде (логарифм; 60 000 форков = 10)"),
    ("curation", 10, "Признание кураторов", "в скольких из 10 awesome-списков по харнессам он есть (8 и больше = 10)"),
    ("activity", 10, "Живость", "последний коммит: до 30 дней — 10, до 120 — 5, старше — 0"),
    ("origin", 35, "Первоисточник", "породил механизмы, которые копируют остальные — 35; собственная школа — 18; "
                                     "производный (форк, клон, сборник) — 0. Единственный экспертный критерий, "
                                     "обоснование записано у каждого харнесса"),
)
STUDY = {"code": "код разобран", "readme": "прочитан README и документация", "none": "не изучен"}
TIERS = ((85, "core", "Ядро — главные харнессы"), (70, "major", "Значимые"), (0, "niche", "Нишевые и производные"))


def _log_scale(x: float, top: float) -> float:
    import math

    return 0.0 if not x or x <= 1 else min(1.0, math.log10(x) / math.log10(top))


def significance(h: dict, today: Optional[str] = None) -> dict:
    """Score 0..100 with its parts, from the metrics snapshot in harnesses.yaml. Openness is reported separately
    (it decides how deeply Artel can borrow, not how important a harness is)."""
    import datetime as dt

    m = h.get("metrics") or {}
    parts = {"adoption": 35 * _log_scale(m.get("stars") or 0, 400_000),
             "community": 10 * _log_scale(m.get("forks") or 0, 60_000),
             "curation": 10 * min(int(m.get("lists") or 0), 8) / 8}
    pushed = m.get("pushed")
    if pushed:
        ref = dt.date.fromisoformat(today) if today else dt.date.today()
        days = (ref - dt.date.fromisoformat(str(pushed)[:10])).days
        parts["activity"] = 10 if days <= 30 else 5 if days <= 120 else 0
    else:
        parts["activity"] = 0
    parts["origin"] = {"originator": 35, "school": 18, "derivative": 0}.get(h.get("origin", "derivative"), 0)
    total = round(sum(parts.values()), 1)
    tier = next(t for t in TIERS if total >= t[0])
    lic = str(m.get("license") or "").lower()
    openness = "open" if lic in ("mit", "apache-2.0", "bsd-3-clause", "bsd-2-clause", "isc") else \
        "closed" if lic in ("", "none", "proprietary") else "restricted"
    return {"total": total, "parts": {k: round(v, 1) for k, v in parts.items()}, "tier": tier[1],
            "tier_label": tier[2], "openness": openness}


def load_harnesses(repo: Path) -> dict:
    p = Path(repo) / "harnesses.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
