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
STATUS_MARK = {"done": "готово", "partial": "частично", "in_progress": "в работе", "planned": "план"}


class Roadmap:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = yaml.safe_load(self.path.read_text()) or {}
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
        lines = self.path.read_text().splitlines(keepends=True)
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
        self.path.write_text("".join(lines[:start] + block + lines[end:]))
        self.__init__(self.path)

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
                               text=True, timeout=900)
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
            out += ["", f"## Волна {w} — {done}/{len(items)} готово", "",
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
        out += ["", f"Итого: {done}/{total} пунктов готово.", ""]
        return "\n".join(out)

    def summary(self) -> dict:
        from collections import Counter

        return {"pillars": len(self.pillars), "items": len(self.items),
                "by_status": dict(Counter(it.get("status") for it in self.items)),
                "by_mode": dict(Counter(it.get("mode") for it in self.items))}
