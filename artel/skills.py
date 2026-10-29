"""Skill library in the open Agent Skills format, with Hermes-style self-maintenance.

Layout under ``<home>/skills``:

    <category>/<name>/SKILL.md (+ references/ scripts/ templates/ assets/)
    .usage.json        sidecar telemetry + provenance (never stored in SKILL.md)
    .ledger.jsonl      one entry per mutation with before/after manifests
    .blobs/<sha256>    content-addressed file contents for rollback
    .archive/<name>/   archived skills (never deleted, always recoverable)

Ownership: agents may only modify skills they created (``origin == "agent"``)
and that are not pinned. Hub and user skills are read-only to agents.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-))*[a-z0-9]$|^[a-z0-9]$")
SUPPORT_DIRS = ("references", "scripts", "templates", "assets")
INDEX_DESC_CHARS = 160

# Trust tiers (Hermes skills_guard): which scan verdicts each tier may install.
TRUSTED_SOURCES = ("anthropics/skills", "openai/skills", "huggingface/skills", "NVIDIA/skills", "agentskills/agentskills")
INSTALL_POLICY = {
    "builtin": {"safe": True, "caution": True, "dangerous": True},
    "trusted": {"safe": True, "caution": True, "dangerous": False},
    "community": {"safe": True, "caution": False, "dangerous": False},
}

DANGEROUS_PATTERNS = [
    (r"rm\s+-rf\s+(/|~|\$HOME)(\s|$)", "recursive delete of root/home"),
    (r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", "pipe remote script to shell"),
    (r"base64\s+(-d|--decode)[^\n]*\|\s*(ba|z)?sh", "decode-and-execute"),
    (r"(nc|ncat|netcat)\s+[^\n]*-e\s", "reverse shell"),
    (r"/dev/tcp/", "raw TCP redirection"),
    (r"\.ssh/(id_|authorized_keys)", "touches SSH keys"),
    (r"(printenv|env)\s*\|\s*(curl|wget|nc)", "environment exfiltration"),
    (r"(ANTHROPIC|OPENAI|AWS|GITHUB)_[A-Z_]*KEY[^\n]*(curl|requests\.post|fetch\()", "credential exfiltration"),
    (r"ignore (all )?(previous|prior) instructions", "prompt injection phrase"),
]
CAUTION_PATTERNS = [
    (r"\bsudo\b", "sudo"),
    (r"chmod\s+(-R\s+)?777", "world-writable permissions"),
    (r"shell\s*=\s*True", "subprocess with shell=True"),
    (r"\beval\s*\(", "eval"),
    (r"\bexec\s*\(", "exec"),
    (r"(curl|wget)\s+-[^\n]*(-d|--data|-F)\s", "outbound data upload"),
    (r"requests\.(post|put)\(", "outbound HTTP write"),
    (r"[​-‏‪-‮⁦-⁩﻿]", "invisible/bidi unicode"),
    (r"(\.claude|\.codex|\.artel)/(settings|config|skills)", "modifies agent configuration"),
]


class SkillError(Exception):
    pass


def parse_skill(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with YAML frontmatter (---)")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillError("unterminated frontmatter")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError("frontmatter must be a mapping")
    return meta, parts[2]


def validate_skill(name: str, text: str) -> list[str]:
    """Agent Skills spec + authoring bar. Returns warnings; raises on hard errors."""
    meta, body = parse_skill(text)
    if meta.get("name") != name:
        raise SkillError(f"frontmatter name {meta.get('name')!r} must equal the skill directory name {name!r}")
    if not NAME_RE.match(name) or len(name) > 64:
        raise SkillError("name: 1-64 chars, lowercase a-z 0-9 and single hyphens, no leading/trailing hyphen")
    desc = str(meta.get("description") or "").strip()
    if not desc:
        raise SkillError("description is required")
    if len(desc) > 1024:
        raise SkillError("description must be <= 1024 characters")
    warnings = []
    if len(desc) > INDEX_DESC_CHARS:
        warnings.append(f"description is {len(desc)} chars; the prompt index shows only {INDEX_DESC_CHARS}")
    lines = body.strip().splitlines()
    if len(lines) > 400:
        warnings.append(f"SKILL.md body is {len(lines)} lines; move depth into references/")
    lower = body.lower()
    for section in ("when to use", "procedure", "pitfalls", "verification"):
        if section not in lower:
            warnings.append(f"missing recommended section: {section}")
    return warnings


def scan_text(text: str) -> list[dict]:
    findings = []
    for pattern, label in DANGEROUS_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            findings.append({"severity": "dangerous", "rule": label, "match": m.group(0)[:80]})
    for pattern, label in CAUTION_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            findings.append({"severity": "caution", "rule": label, "match": m.group(0)[:80]})
    return findings


def scan_skill_dir(path: Path) -> dict:
    findings = []
    for f in sorted(path.rglob("*")):
        if f.is_file() and f.stat().st_size < 2_000_000:
            try:
                text = f.read_text(errors="replace", encoding="utf-8")
            except OSError:
                continue
            for fd in scan_text(text):
                findings.append({**fd, "file": f.relative_to(path).as_posix()})
    verdict = "safe"
    if any(f["severity"] == "caution" for f in findings):
        verdict = "caution"
    if any(f["severity"] == "dangerous" for f in findings):
        verdict = "dangerous"
    return {"verdict": verdict, "findings": findings}


class SkillStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".blobs").mkdir(exist_ok=True)
        (self.root / ".archive").mkdir(exist_ok=True)
        self._lock = threading.RLock()

    # --- usage sidecar --------------------------------------------------
    @property
    def _usage_path(self) -> Path:
        return self.root / ".usage.json"

    def usage(self) -> dict:
        p = self._usage_path
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _save_usage(self, data: dict) -> None:
        tmp = self._usage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._usage_path)

    def _touch(self, name: str, **updates: Any) -> dict:
        with self._lock:
            data = self.usage()
            rec = data.setdefault(name, {"origin": "user", "trust": "builtin", "pinned": False, "state": "active",
                                         "use_count": 0, "view_count": 0, "patch_count": 0,
                                         "created_at": time.time(), "last_used": None})
            for k, v in updates.items():
                if k.endswith("_count") and v == "+1":
                    rec[k] = rec.get(k, 0) + 1
                else:
                    rec[k] = v
            self._save_usage(data)
            return rec

    def record_use(self, name: str, kind: str = "view") -> None:
        self._touch(name, **{f"{kind}_count": "+1", "last_used": time.time(), "state": "active"})

    def meta(self, name: str) -> dict:
        return self.usage().get(name, {"origin": "user", "pinned": False, "state": "active"})

    # --- lookup ---------------------------------------------------------
    def find(self, name: str) -> Optional[Path]:
        for p in self.root.glob("*/*/SKILL.md"):
            if p.parent.name == name and not p.parent.parent.name.startswith("."):
                return p.parent
        return None

    def list(self, query: str = "") -> list[dict]:
        out = []
        q = query.lower().strip()
        usage = self.usage()
        for p in sorted(self.root.glob("*/*/SKILL.md")):
            if p.parent.parent.name.startswith("."):
                continue
            try:
                meta, _ = parse_skill(p.read_text(encoding="utf-8"))
            except SkillError:
                continue
            name = p.parent.name
            desc = str(meta.get("description", "")).strip()
            if q and q not in name and q not in desc.lower():
                continue
            u = usage.get(name, {})
            out.append({"name": name, "category": p.parent.parent.name, "description": desc,
                        "origin": u.get("origin", "user"), "pinned": u.get("pinned", False),
                        "state": u.get("state", "active"), "use_count": u.get("use_count", 0)})
        return out

    def relevant(self, query: str, config: Optional[dict] = None, top: int = 40) -> list[dict]:
        """Active skills most relevant to ``query`` (hybrid lexical + embeddings, artel/retrieval.py); pinned skills
        always come first."""
        from .retrieval import rank

        rows = [s for s in self.list() if s["state"] != "archived"]
        pinned = [s for s in rows if s["pinned"]]
        rest = [s for s in rows if not s["pinned"]]
        hits = rank(query, [f"{s['name'].replace('-', ' ')}: {s['description']}" for s in rest], config,
                    titles=[s["name"] for s in rest], top=top)
        return pinned + [rest[i] for i, _, _ in hits]

    def index(self, limit: int = 120, query: str = "", config: Optional[dict] = None) -> str:
        """Compact skill index for system prompts (name: description). With ``query``, a large library is cut to
        the skills relevant to it instead of the first ``limit`` by name."""
        rows = self.list()
        if query and len(rows) > limit // 3:
            rows = self.relevant(query, config, top=limit // 3) or rows
        lines = []
        for s in rows[:limit]:
            if s["state"] == "archived":
                continue
            desc = s["description"].replace("\n", " ")
            if len(desc) > INDEX_DESC_CHARS:
                desc = desc[: INDEX_DESC_CHARS - 1] + "…"
            lines.append(f"- {s['name']}: {desc}")
        return "\n".join(lines)

    def view(self, name: str, file_path: Optional[str] = None) -> str:
        d = self.find(name)
        if d is None:
            raise SkillError(f"skill {name!r} not found")
        if file_path:
            target = (d / file_path).resolve()
            if d.resolve() not in target.parents:
                raise SkillError("file_path escapes the skill directory")
            if not target.is_file():
                raise SkillError(f"{file_path} not found in {name}")
            text = target.read_text(errors="replace", encoding="utf-8")
        else:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            extras = [f.relative_to(d).as_posix() for f in sorted(d.rglob("*")) if f.is_file() and f.name != "SKILL.md"]
            if extras:
                text += "\n\n<!-- support files (load with skill_view file_path=...) -->\n" + "\n".join(
                    f"- {e}" for e in extras)
        self.record_use(name, "view")
        return text

    # --- ledger ---------------------------------------------------------
    def _blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        bp = self.root / ".blobs" / sha
        if not bp.exists():
            bp.write_bytes(data)
        return sha

    def _manifest(self, d: Optional[Path]) -> dict:
        if d is None or not d.exists():
            return {}
        return {f.relative_to(d).as_posix(): self._blob(f.read_bytes()) for f in sorted(d.rglob("*")) if f.is_file()}

    def _ledger(self, name: str, action: str, actor: str, before: dict, after: dict, location: str,
                reason: str = "") -> str:
        entry_id = uuid.uuid4().hex[:12]
        entry = {"id": entry_id, "ts": time.time(), "skill": name, "action": action, "actor": actor,
                 "location": location, "before": before, "after": after, "reason": reason}
        with (self.root / ".ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry_id

    def ledger(self, name: Optional[str] = None) -> list[dict]:
        p = self.root / ".ledger.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [r for r in rows if name is None or r["skill"] == name]

    def rollback(self, entry_id: str, actor: str = "user") -> str:
        """Restore a skill directory to its ``before`` manifest of the given ledger entry."""
        entry = next((e for e in self.ledger() if e["id"] == entry_id), None)
        if entry is None:
            raise SkillError(f"ledger entry {entry_id} not found")
        d = self.root / entry["location"]
        before_now = self._manifest(d)
        if d.exists():
            shutil.rmtree(d)
        if entry["before"]:
            for rel, sha in entry["before"].items():
                f = d / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes((self.root / ".blobs" / sha).read_bytes())
        return self._ledger(entry["skill"], f"rollback:{entry_id}", actor, before_now, entry["before"],
                            entry["location"])

    # --- mutations ------------------------------------------------------
    def _check_writable(self, name: str, actor: str) -> None:
        if actor == "user":
            return
        m = self.meta(name)
        if m.get("pinned"):
            raise SkillError(f"skill {name!r} is pinned; only the user may change it")
        if m.get("origin") != "agent":
            raise SkillError(f"skill {name!r} is {m.get('origin')}-owned and read-only to agents; "
                             "create a new class-level skill or report the problem instead")

    def create(self, name: str, content: str, category: str = "general", actor: str = "agent",
               reason: str = "") -> dict:
        with self._lock:
            if self.find(name):
                raise SkillError(f"skill {name!r} already exists; patch it instead")
            warnings = validate_skill(name, content)
            if actor != "user":
                scan = scan_text(content)
                if any(f["severity"] == "dangerous" for f in scan):
                    raise SkillError(f"skill content failed the safety scan: {scan}")
            category = re.sub(r"[^a-z0-9-]", "-", category.lower()) or "general"
            d = self.root / category / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(content, encoding="utf-8")
            self._touch(name, origin="agent" if actor != "user" else "user", trust="builtin", state="active",
                        created_by=actor, created_at=time.time())
            entry = self._ledger(name, "create", actor, {}, self._manifest(d), d.relative_to(self.root).as_posix(), reason)
        return {"name": name, "path": str(d), "warnings": warnings, "ledger": entry}

    def patch(self, name: str, old: str, new: str, actor: str = "agent", file_path: str = "SKILL.md",
              reason: str = "") -> dict:
        with self._lock:
            d = self.find(name)
            if d is None:
                raise SkillError(f"skill {name!r} not found")
            self._check_writable(name, actor)
            target = (d / file_path).resolve()
            if d.resolve() not in target.parents:
                raise SkillError("file_path escapes the skill directory")
            text = target.read_text(encoding="utf-8")
            count = text.count(old)
            if count != 1:
                raise SkillError(f"old text must match exactly once (matched {count} times)")
            updated = text.replace(old, new)
            warnings = validate_skill(name, updated) if file_path == "SKILL.md" else []
            before = self._manifest(d)
            target.write_text(updated, encoding="utf-8")
            self._touch(name, patch_count="+1", last_used=time.time())
            entry = self._ledger(name, "patch", actor, before, self._manifest(d), d.relative_to(self.root).as_posix(),
                                 reason)
        return {"name": name, "warnings": warnings, "ledger": entry}

    def write_file(self, name: str, file_path: str, content: str, actor: str = "agent", reason: str = "") -> dict:
        with self._lock:
            d = self.find(name)
            if d is None:
                raise SkillError(f"skill {name!r} not found")
            self._check_writable(name, actor)
            rel = Path(file_path)
            if rel.parts[0] not in SUPPORT_DIRS and str(rel) != "SKILL.md":
                raise SkillError(f"support files go under {', '.join(SUPPORT_DIRS)}/")
            target = (d / rel).resolve()
            if d.resolve() not in target.parents:
                raise SkillError("file_path escapes the skill directory")
            if str(rel) == "SKILL.md":
                validate_skill(name, content)
            before = self._manifest(d)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._touch(name, patch_count="+1", last_used=time.time())
            entry = self._ledger(name, "write_file", actor, before, self._manifest(d), d.relative_to(self.root).as_posix(),
                                 reason)
        return {"name": name, "file": str(rel), "ledger": entry}

    def archive(self, name: str, actor: str = "agent", reason: str = "") -> dict:
        with self._lock:
            d = self.find(name)
            if d is None:
                raise SkillError(f"skill {name!r} not found")
            self._check_writable(name, actor)
            dest = self.root / ".archive" / d.parent.name / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(d), str(dest))
            self._touch(name, state="archived")
            entry = self._ledger(name, "archive", actor, {}, {}, dest.relative_to(self.root).as_posix(), reason)
        return {"name": name, "archived_to": str(dest), "ledger": entry}

    def restore(self, name: str) -> dict:
        for p in (self.root / ".archive").glob(f"*/{name}"):
            dest = self.root / p.parent.name / name
            shutil.move(str(p), str(dest))
            self._touch(name, state="active", last_used=time.time())
            return {"name": name, "restored_to": str(dest)}
        raise SkillError(f"archived skill {name!r} not found")

    def pin(self, name: str, pinned: bool = True) -> None:
        if self.find(name) is None:
            raise SkillError(f"skill {name!r} not found")
        self._touch(name, pinned=pinned)

    # --- curator (deterministic lifecycle) ------------------------------
    def curate(self, stale_after_days: float = 30, archive_after_days: float = 90, now: Optional[float] = None) -> list[dict]:
        now = now or time.time()
        actions = []
        for s in self.list():
            m = self.meta(s["name"])
            if m.get("origin") != "agent" or m.get("pinned"):
                continue
            last = m.get("last_used") or m.get("created_at") or now
            idle_days = (now - last) / 86400
            if idle_days >= archive_after_days:
                self.archive(s["name"], actor="curator", reason=f"unused for {idle_days:.0f} days")
                actions.append({"skill": s["name"], "action": "archived"})
            elif idle_days >= stale_after_days and m.get("state") != "stale":
                self._touch(s["name"], state="stale")
                actions.append({"skill": s["name"], "action": "stale"})
        return actions

    # --- hub import -----------------------------------------------------
    def import_from(self, source: str, trust: Optional[str] = None, include: str = "*", force: bool = False,
                    category: Optional[str] = None) -> dict:
        """Install every skill found under a local path or git URL (``owner/repo`` is GitHub shorthand)."""
        tmp = None
        src_path = Path(source).expanduser()
        label = source
        if not src_path.exists():
            url = source if "://" in source or source.startswith("git@") else f"https://github.com/{source}.git"
            tmp = Path(tempfile.mkdtemp(prefix="artel-hub-"))
            subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(tmp / "repo")], check=True,
                           capture_output=True, timeout=600)
            src_path = tmp / "repo"
            label = source.removeprefix("https://github.com/").removesuffix(".git")
        if trust is None:
            trust = "trusted" if any(label.endswith(t) or t in label for t in TRUSTED_SOURCES) else "community"
        installed, skipped = [], []
        try:
            for skill_md in sorted(src_path.rglob("SKILL.md")):
                d = skill_md.parent
                name = d.name
                if not fnmatch.fnmatch(name, include):
                    continue
                try:
                    meta, _ = parse_skill(skill_md.read_text(encoding="utf-8"))
                except SkillError as exc:
                    skipped.append({"skill": name, "reason": str(exc)})
                    continue
                name = str(meta.get("name") or name)
                if not NAME_RE.match(name):
                    skipped.append({"skill": name, "reason": "invalid name"})
                    continue
                scan = scan_skill_dir(d)
                if not force and not INSTALL_POLICY[trust][scan["verdict"]]:
                    skipped.append({"skill": name, "reason": f"guard: {scan['verdict']} for {trust} source",
                                    "findings": scan["findings"][:5]})
                    continue
                existing = self.find(name)
                if existing and self.meta(name).get("origin") not in (None, f"hub:{label}"):
                    skipped.append({"skill": name, "reason": "name collides with a local skill"})
                    continue
                cat = category or slugify_category(label)
                dest = existing or (self.root / cat / name)
                before = self._manifest(dest)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(d, dest, ignore=shutil.ignore_patterns(".git"))
                self._touch(name, origin=f"hub:{label}", trust=trust, state="active", scan=scan["verdict"],
                            installed_at=time.time())
                self._ledger(name, "hub_install", "user", before, self._manifest(dest),
                             dest.relative_to(self.root).as_posix(), f"from {label}")
                installed.append(name)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        return {"source": label, "trust": trust, "installed": installed, "skipped": skipped}


def slugify_category(label: str) -> str:
    return "hub-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
