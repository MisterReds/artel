"""Projects, worker personas and configuration.

A goal becomes a project directory:

    <home>/projects/<slug>/
        project.json          goal, status, budgets, usage
        events.jsonl          append-only trajectory (everything the system did)
        board.db              kanban task DAG
        shared/               deliverables every worker can read and write
        workers/<name>/       one "bot with a soul" per executor (OpenClaw layout)
            SOUL.md           who the worker is, values, voice
            IDENTITY.md       name, role, model, effort
            RULES.md          operating rules (refined over time)
            MEMORY.md         durable notes the worker keeps for itself
            workspace/        private scratch area
        harness/              continual-harness state (prompt notes + snapshots)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .board import Board
from .kernel import EventLog

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "anthropic",            # anthropic | openai_compat | scripted
    "openai_compat": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
    "models": {                          # per role; a worker persona may override
        "default": "claude-opus-5-5",
    },
    "effort": {
        "planner": "high", "manager": "high", "auditor": "medium", "worker": "high",
        "reviewer": "medium", "librarian": "low", "refiner": "low",
    },
    "roles": {                           # cost control for coordination roles (all overridable)
        "cheap_model": "claude-sonnet-5-5",   # used for cheap_roles on the anthropic provider when
        "cheap_roles": ["librarian", "refiner", "auditor"],  # `models` has no explicit entry for the role
        "skip_on_easy": True,            # skip the goal audit when the whole run was small and clean
        "easy_max_tasks": 2,             # easy run: at most this many tasks...
        "easy_max_tool_calls": 15,       # ...at most this many worker tool calls in total...
    },                                   # ...every task accepted on its first attempt, no failures at all
    "fallbacks": True,                   # server-side refusal fallbacks on Claude
    "max_tokens": 16000,
    "budgets": {
        "max_parallel": 3,               # concurrent worker runs
        "max_turns_per_run": 40,         # model requests per worker run
        "max_attempts": 4,               # runs per task before the circuit breaker replans it
        "max_replans": 3,                # replans of one task before it goes to a person (no endless circling)
        "max_total_tokens": 0,           # 0 = unlimited
        "max_hours": 0,                  # 0 = unlimited
        "max_audit_rounds": 0,           # 0 = unlimited goal audits
    },
    "skills": {
        "review_after_tool_calls": 5,    # Hermes: learn after a non-trivial task
        "stale_after_days": 30,
        "archive_after_days": 90,
    },
    "tool_timeout_s": 300,
    "backoff_base_s": 10,                # no-progress retry backoff: 10s, 20s, 40s … (cap 600s)
    "heartbeat_stale_s": 900,
    "plugins": [],
    "providers": {                       # named OpenAI-compatible endpoints usable as `provider` or per role
    },
    "provider_by_role": {},              # e.g. {librarian: local}
    "decide": {                          # System One (fast typed decisions); none is shipped — see docs/SYSTEM_ONE.md
        "backend": "none",               # none | http (or a plugin providing the `system_one` service)
        "url": "", "api_key_env": "",
        "points": {"skill_review": False, "refine": False, "replan_shortcut": False, "difficulty": False},
        "thresholds": {"skip_learning_below": 0.25, "shortcut_min": 0.8},
    },
    "isolation": {"worktrees": False},   # true: each task works in its own git worktree, merged on acceptance
    "sandbox": {                         # OS sandbox for agents' bash (artel/sandbox.py); off = as before
        "mode": "off",                   # off | auto | bwrap | seatbelt
        "network": True, "writable": [], "temp": True, "require": False, "gates": False,
    },
    "gate_timeout_s": 900,              # shell gates often run a whole test suite; tool calls keep tool_timeout_s
    "runtime": "auto",                   # auto | native | claude_code | codex (auto: whatever is connected)
    "runtime_by_role": {},               # e.g. {reviewer: native}
    "claude_code": {"model": "sonnet", "permission_mode": "bypassPermissions", "max_resumes": 3,
                    "timeout_s": 2400},
}


def artel_home() -> Path:
    return Path(os.environ.get("ARTEL_HOME", Path.home() / ".artel"))


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Optional[Path] = None) -> dict:
    cfg = DEFAULT_CONFIG
    for candidate in [artel_home() / "artel.yaml", path]:
        if candidate and Path(candidate).exists():
            cfg = deep_merge(cfg, yaml.safe_load(Path(candidate).read_text()) or {})
    return cfg


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w]+", "-", text.lower(), flags=re.UNICODE).strip("-")
    return (s[:limit].strip("-") or "project")


@dataclass
class Persona:
    name: str
    role: str
    soul: str = ""
    rules: list = field(default_factory=list)
    skills: list = field(default_factory=list)   # skill names to preload into the prompt
    model: Optional[str] = None
    effort: Optional[str] = None

    @property
    def slug(self) -> str:
        return slugify(self.name, 32)


class Project:
    def __init__(self, root: Path, config: dict):
        self.root = Path(root)
        self.config = config
        self.slug = self.root.name
        self.log = EventLog(self.root / "events.jsonl")
        self.board = Board(self.root / "board.db", log=self.log)
        meta = json.loads((self.root / "project.json").read_text()) if (self.root / "project.json").exists() else {}
        # workdir: the team works directly in an external directory (e.g. a git worktree) instead of shared/
        self.shared = Path(meta["workdir"]) if meta.get("workdir") else self.root / "shared"
        self.shared.mkdir(parents=True, exist_ok=True)
        # refs: read-only source directories the team may study (e.g. another harness to absorb from)
        self.refs = [Path(r) for r in meta.get("refs", [])]
        (self.root / "workers").mkdir(exist_ok=True)
        (self.root / "harness" / "snapshots").mkdir(parents=True, exist_ok=True)
        self._meta_lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------
    @classmethod
    def create(cls, goal: str, config: dict, home: Optional[Path] = None, name: Optional[str] = None,
               workdir: Optional[Path] = None, refs: tuple = (), seed: Optional[Path] = None) -> "Project":
        base = (home or artel_home()) / "projects"
        slug = slugify(name or goal)
        root = base / slug
        n = 2
        while root.exists():
            root = base / f"{slug}-{n}"
            n += 1
        root.mkdir(parents=True)
        meta = {"goal": goal, "name": name or goal[:80], "status": "planning", "created_at": time.time(),
                "usage": {"input_tokens": 0, "output_tokens": 0, "requests": 0}, "audit_rounds": 0,
                "root_task": None}
        if workdir:
            meta["workdir"] = str(Path(workdir).resolve())
        if refs:
            meta["refs"] = [str(Path(r).resolve()) for r in refs]
        (root / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        project = cls(root, config)
        if seed:
            import shutil

            shutil.copytree(seed, project.shared, dirs_exist_ok=True)
        project.log.append("project/created", goal=goal, slug=project.slug, workdir=meta.get("workdir"),
                           refs=meta.get("refs", []), seeded=bool(seed))
        return project

    @classmethod
    def open(cls, slug_or_path: str, config: dict, home: Optional[Path] = None) -> "Project":
        p = Path(slug_or_path)
        if not (p / "project.json").exists():
            p = (home or artel_home()) / "projects" / slug_or_path
        if not (p / "project.json").exists():
            raise FileNotFoundError(f"no project at {slug_or_path}")
        return cls(p, config)

    # --- metadata -------------------------------------------------------
    @property
    def meta(self) -> dict:
        return json.loads((self.root / "project.json").read_text())

    def _write_meta(self, m: dict) -> None:
        tmp = self.root / f".project.json.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2))
        tmp.replace(self.root / "project.json")

    def set_meta(self, **fields: Any) -> dict:
        with self._meta_lock:
            m = self.meta
            m.update(fields)
            self._write_meta(m)
        if "status" in fields:
            self.log.append("project/status", status=fields["status"])
        return m

    def add_usage(self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0) -> dict:
        with self._meta_lock:
            m = self.meta
            u = m["usage"]
            u["input_tokens"] += input_tokens
            u["output_tokens"] += output_tokens
            u["requests"] += 1
            u["cost_usd"] = round(u.get("cost_usd", 0.0) + (cost_usd or 0.0), 6)
            self._write_meta(m)
        return u

    @property
    def goal(self) -> str:
        return self.meta["goal"]

    # --- workers --------------------------------------------------------
    def worker_dir(self, name: str) -> Path:
        return self.root / "workers" / slugify(name, 32)

    def hire(self, persona: Persona) -> Path:
        d = self.worker_dir(persona.name)
        (d / "workspace").mkdir(parents=True, exist_ok=True)
        (d / "SOUL.md").write_text(
            f"# SOUL — {persona.name}\n\n{persona.soul.strip() or 'A careful, relentless professional.'}\n\n"
            "## Core truths\n"
            "- Be genuinely useful, not performatively useful. Do the work, then show the evidence.\n"
            "- Be resourceful before declaring a blocker: read, search, try, measure.\n"
            "- Earn trust through competence: never claim what you did not verify.\n"
            "- These files are your memory. Read them, keep MEMORY.md current.\n")
        (d / "IDENTITY.md").write_text(yaml.safe_dump(
            {"name": persona.name, "role": persona.role, "model": persona.model, "effort": persona.effort,
             "skills": persona.skills}, allow_unicode=True, sort_keys=False))
        rules = persona.rules or []
        (d / "RULES.md").write_text("# RULES\n\n" + "".join(f"- {r}\n" for r in rules))
        if not (d / "MEMORY.md").exists():
            (d / "MEMORY.md").write_text("# MEMORY\n\n")
        self.log.append("worker/hired", worker=persona.name, role=persona.role, soul=persona.soul,
                        rules=rules, skills=persona.skills, model=persona.model)
        return d

    def persona(self, name: str) -> Optional[Persona]:
        d = self.worker_dir(name)
        if not (d / "IDENTITY.md").exists():
            return None
        ident = yaml.safe_load((d / "IDENTITY.md").read_text()) or {}
        soul = (d / "SOUL.md").read_text()
        rules = [ln[2:] for ln in (d / "RULES.md").read_text().splitlines() if ln.startswith("- ")]
        return Persona(name=ident.get("name", name), role=ident.get("role", ""), soul=soul, rules=rules,
                       skills=ident.get("skills") or [], model=ident.get("model"), effort=ident.get("effort"))

    def personas(self) -> list[Persona]:
        out = []
        for d in sorted((self.root / "workers").iterdir()):
            p = self.persona(d.name)
            if p:
                out.append(p)
        return out

    def worker_files(self, name: str) -> dict[str, str]:
        d = self.worker_dir(name)
        return {f: (d / f).read_text() for f in ("SOUL.md", "IDENTITY.md", "RULES.md", "MEMORY.md") if (d / f).exists()}

    # --- harness notes (continual harness) ------------------------------
    @property
    def prompt_notes_path(self) -> Path:
        return self.root / "harness" / "prompt_notes.md"

    def prompt_notes(self) -> str:
        p = self.prompt_notes_path
        return p.read_text() if p.exists() else ""

    def model_for(self, role: str, persona: Optional[Persona] = None) -> str:
        if persona and persona.model:
            return persona.model
        models = self.config["models"]
        roles = self.config.get("roles") or {}
        if role not in models and role in roles.get("cheap_roles", ()) and roles.get("cheap_model") \
                and self.config.get("provider", "anthropic") == "anthropic":
            return roles["cheap_model"]  # only where the model name is valid: other providers keep their default
        return models.get(role, models["default"])

    def effort_for(self, role: str, persona: Optional[Persona] = None) -> str:
        if persona and persona.effort:
            return persona.effort
        return self.config["effort"].get(role, "high")


def list_projects(home: Optional[Path] = None) -> list[dict]:
    base = (home or artel_home()) / "projects"
    out = []
    if base.exists():
        for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            mf = d / "project.json"
            if mf.exists():
                m = json.loads(mf.read_text())
                out.append({"slug": d.name, **{k: m.get(k) for k in ("goal", "name", "status", "created_at", "usage")}})
    return out


def persona_from_dict(d: dict) -> Persona:
    return Persona(name=d["name"], role=d.get("role", ""), soul=d.get("soul", ""), rules=list(d.get("rules", [])),
                   skills=list(d.get("skills", [])), model=d.get("model"), effort=d.get("effort"))


__all__ = ["Project", "Persona", "load_config", "artel_home", "list_projects", "persona_from_dict", "asdict"]
