"""Continual Harness: small, evidence-backed edits to supplemental harness state.

Editable state: the project's ``harness/prompt_notes.md`` and each worker's
``RULES.md``. Base prompts are immutable. Every edit snapshots the target first
and is listed in ``harness/refinements.jsonl``; ``rollback`` restores a snapshot.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

MAX_EDIT_CHARS = 2000


class Refiner:
    def _target(self, project, target: str, worker: str) -> Path:
        if target == "prompt_notes":
            return project.prompt_notes_path
        if target == "worker_rules":
            if not worker or project.persona(worker) is None:
                raise ValueError(f"unknown worker {worker!r}")
            return project.worker_dir(worker) / "RULES.md"
        raise ValueError("target must be prompt_notes or worker_rules")

    def _journal(self, project) -> Path:
        return project.root / "harness" / "refinements.jsonl"

    def apply(self, project, *, target: str, content: str, evidence: str, worker: str = "", op: str = "append",
              actor: str = "refiner") -> dict:
        if not evidence.strip():
            raise ValueError("every harness edit needs evidence from the trajectory")
        if len(content) > MAX_EDIT_CHARS:
            raise ValueError(f"edits are small by design (<= {MAX_EDIT_CHARS} chars)")
        path = self._target(project, target, worker)
        edit_id = uuid.uuid4().hex[:10]
        snap = project.root / "harness" / "snapshots" / f"{edit_id}.bak"
        existed = path.exists()
        if existed:
            shutil.copy2(path, snap)
        path.parent.mkdir(parents=True, exist_ok=True)
        if op == "replace":
            text = content if target != "worker_rules" or content.startswith("# RULES") else "# RULES\n\n" + content
            path.write_text(text.rstrip() + "\n")
        else:
            line = content.strip()
            if target == "worker_rules" and not line.startswith("- "):
                line = "- " + line
            prev = path.read_text() if existed else ""
            if prev and not prev.endswith("\n"):
                prev += "\n"
            path.write_text(prev + line + "\n")
        entry = {"id": edit_id, "ts": time.time(), "actor": actor, "target": target, "worker": worker, "op": op,
                 "content": content, "evidence": evidence, "path": str(path.relative_to(project.root)),
                 "snapshot": str(snap.relative_to(project.root)) if existed else None}
        with self._journal(project).open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        project.log.append("harness/edited", **entry)
        return {"id": edit_id, "path": entry["path"]}

    def history(self, project) -> list[dict]:
        p = self._journal(project)
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []

    def rollback(self, project, edit_id: str) -> dict:
        entry = next((e for e in self.history(project) if e["id"] == edit_id), None)
        if entry is None:
            raise ValueError(f"no harness edit {edit_id}")
        path = project.root / entry["path"]
        if entry["snapshot"]:
            shutil.copy2(project.root / entry["snapshot"], path)
        elif path.exists():
            path.unlink()
        project.log.append("harness/rolled_back", id=edit_id, path=entry["path"])
        return {"rolled_back": edit_id, "path": entry["path"]}


def trajectory_digest(project, limit: int = 400) -> str:
    """Failures, verdicts, replans and continuations — the evidence a refiner needs."""
    kinds = {"run/finished", "board/comment", "agent/continuation", "manager/decision", "review/verdict",
             "audit/verdict", "llm/error", "gate/result", "agent/end"}
    lines = []
    for ev in project.log.read(kinds=kinds)[-limit:]:
        k = ev["kind"]
        if k == "agent/end" and ev.get("status") == "terminal":
            continue
        body = {x: ev[x] for x in ev if x not in ("seq", "ts", "kind")}
        lines.append(f"{k}: {json.dumps(body, ensure_ascii=False)[:500]}")
    return "\n".join(lines) or "(no notable events)"


def setup_refiner(kernel) -> Optional[Refiner]:
    r = Refiner()
    kernel.provide("refine", r)
    return r
