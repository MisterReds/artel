"""Guardian: an asynchronous trajectory risk reviewer.

After a worker run, ``assess`` scores the run's event log with deterministic heuristics (no model call).
A ``high`` score switches the task's later runs to blocking review: ``check_command`` is then applied to
every shell command before it executes. System One may refine a borderline score, only when the
``decide.points.guardian`` point is enabled; any failure falls back silently to the heuristic result.

The two-stage idea (a non-blocking first-step reviewer whose high-risk verdict triggers blocking review of
future actions) and the risk categories (destructive actions, exfiltration, credential exposure) are adapted
from OpenAI Codex, codex-rs/prompts/templates/guardian/classifier_instructions.md.
Copyright OpenAI, licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional

HIGH = 0.7
SENSITIVE_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/var", "/root", "/dev", "/sys", "/proc")
_ROOTISH = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "${HOME}", "/home", "/home/*", "/etc", "/usr", "/var",
            "/bin", "/boot", "/lib", "/root", "/opt", "/srv", "/sbin"}
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
_DROP_SQL = re.compile(r"\bdrop\s+(table|database|schema)\b", re.I)
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{")
_DEV_OF = re.compile(r"\bof=/dev/(sd|nvme|hd|vd|xvd|mmcblk|disk|mapper|loop)")
_CURL_UPLOAD = re.compile(r"(^|\s)(-d|--data\S*|-F|--form|-T|--upload-file|--json)(\s|=|$)|\s-X\s*(POST|PUT)\b", re.I)
_WGET_UPLOAD = re.compile(r"--post-(file|data)", re.I)
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API secret key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\b\s*[=:]\s*['\"]?[A-Za-z0-9/+_-]{20,}"), "credential assignment"),
]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def check_command(command: str) -> tuple[float, str]:
    """Score one shell command: (0..1, reason). Destructive / exfiltrating commands score >= HIGH."""
    if not command or not command.strip():
        return 0.0, ""
    if _DROP_SQL.search(command):
        return 0.9, "destructive SQL (drop table/database)"
    if _FORK_BOMB.search(command):
        return 0.95, "fork bomb"
    if _DEV_OF.search(command):
        return 0.95, "dd/redirect writing to a block device"
    if re.search(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)", command):
        return 0.95, "redirect writing to a block device"
    best: tuple[float, str] = (0.0, "")

    def consider(score: float, reason: str) -> None:
        nonlocal best
        if score > best[0]:
            best = (score, reason)

    for seg in _SEGMENT_SPLIT.split(command):
        tok = _tokens(seg.strip())
        while tok and (tok[0] in ("sudo", "env", "nohup", "time") or re.match(r"\w+=", tok[0])):
            tok = tok[1:]
        if not tok:
            continue
        name = Path(tok[0]).name
        args = tok[1:]
        flags = [a for a in args if a.startswith("-")]
        operands = [a for a in args if not a.startswith("-")]
        if name == "rm":
            recursive = any(a.startswith("--recursive") or (not a.startswith("--") and "r" in a.lower()) for a in flags)
            if "--no-preserve-root" in flags or (recursive and any(o.rstrip("/") in {x.rstrip("/") for x in _ROOTISH} or o in _ROOTISH
                                                                   for o in operands)):
                consider(0.98, "recursive delete of a root/home/system path")
        elif name.startswith("mkfs"):
            consider(0.98, "filesystem creation (mkfs) wipes a device")
        elif name == "dd" and any(a.startswith("of=/dev/") for a in args):
            consider(0.95, "dd writing to a device")
        elif name in ("shred", "wipefs") and any(o.startswith("/dev/") for o in operands):
            consider(0.95, f"{name} on a device")
        elif name == "git" and "reset" in args and "--hard" in args:
            if "-C" in args and any(o in ("/", "~") for o in operands) or re.search(r"\bcd\s+(/|~)\s*(;|&&)", command):
                consider(0.9, "git reset --hard on a root directory")
            else:
                consider(0.4, "git reset --hard discards uncommitted work")
        elif name == "chmod" and any(f in ("-R", "-r") for f in flags) and any(o in _ROOTISH for o in operands):
            consider(0.85, "recursive chmod on a system path")
        elif name == "curl":
            if _CURL_UPLOAD.search(seg) and re.search(r"@|-T|--upload-file|-X", seg):
                consider(0.8, "network upload with curl (possible exfiltration)")
        elif name == "wget":
            if _WGET_UPLOAD.search(seg):
                consider(0.8, "network upload with wget (possible exfiltration)")
        elif name in ("nc", "ncat", "netcat", "socat"):
            consider(0.8, f"{name} opens a raw network channel (possible exfiltration)")
        elif name in ("scp", "rsync", "sftp") and any(re.match(r"^[\w.-]+@?[\w.-]+:", o) for o in operands):
            consider(0.75, f"{name} to a remote host (possible exfiltration)")
        # redirect/tee targets outside the project, e.g. `> /etc/passwd`
    for m in re.finditer(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*(/[^\s;|&]*)", command):
        if m.group(1).startswith(SENSITIVE_DIRS):
            consider(0.8, f"shell write to system path {m.group(1)}")
    return best


def _outside_write(path: str, workspace: Optional[Path]) -> Optional[tuple[float, str]]:
    p = Path(path)
    if not p.is_absolute() and ".." not in p.parts:
        return None
    try:
        target = p.resolve() if p.is_absolute() else (Path(workspace or ".") / p).resolve()
    except OSError:
        return None
    ws = Path(workspace).resolve() if workspace else None
    if ws and (target == ws or ws in target.parents):
        return None
    if str(target).startswith(SENSITIVE_DIRS):
        return 0.8, f"write to system path {target}"
    return 0.4, f"write outside the workspace: {target}"


def assess(events: list[dict], workspace: Optional[Path] = None) -> dict:
    """Risk of a run's event log: {risk: low|high, score: 0..1, reason}. Deterministic, no model call."""
    best, reason = 0.0, "no risky actions observed"

    def consider(score: float, why: str) -> None:
        nonlocal best, reason
        if score > best:
            best, reason = score, why

    for ev in events:
        kind = ev.get("kind")
        if kind == "tool/call":
            tool, inp = ev.get("tool"), ev.get("input") or {}
            if tool == "bash":
                s, why = check_command(str(inp.get("command", "")))
                if s:
                    consider(s, f"{why}: {str(inp.get('command', ''))[:120]}")
            elif tool in ("write_file", "edit_file") and inp.get("path"):
                hit = _outside_write(str(inp["path"]), workspace)
                if hit:
                    consider(*hit)
        elif kind == "tool/result" and not ev.get("is_error"):
            out = str(ev.get("output") or "")
            for rx, label in _SECRET_PATTERNS:
                if rx.search(out):
                    consider(0.75, f"secret in tool output ({label})")
                    break
    score = round(best, 2)
    return {"risk": "high" if score >= HIGH else "low", "score": score, "reason": reason}


def refine(kernel, project, result: dict, events: list[dict]) -> dict:
    """Optionally let System One escalate a borderline score. Silent no-op unless decide.points.guardian is on."""
    if not 0.3 <= result["score"] < HIGH:
        return result
    try:
        from .decide import decide, score

        calls = [f"{e.get('tool')}: {str(e.get('input'))[:200]}" for e in events if e.get("kind") == "tool/call"]
        ans = decide(kernel, project, "guardian",
                     {"trajectory": "\n".join(calls[-30:]), "heuristic_reason": result["reason"]},
                     {"risk": score("How dangerous is this agent trajectory (destructive, exfiltrating, leaking secrets)?",
                                    ["safe: routine work", "suspicious: unclear intent",
                                     "dangerous: likely harmful or irreversible"])})
        r = (ans or {}).get("risk")
        if r and float(r.get("confidence", 0)) >= 0.6 and round(float(r["score"])) >= 2:
            return {"risk": "high", "score": max(result["score"], HIGH), "reason": result["reason"] + " (System One: dangerous)"}
    except Exception:  # noqa: BLE001 - a failing System One must never stop the work
        pass
    return result


def latest_risk(events: list[dict]) -> dict[str, dict]:
    """task id -> the latest guardian/assessed payload."""
    out: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "guardian/assessed" and e.get("task_id"):
            out[e["task_id"]] = {"risk": e.get("risk"), "score": e.get("score"), "reason": e.get("reason")}
    return out


def blocking_error(command: str) -> Optional[str]:
    """Message to deny ``command`` under blocking review, or None when it may run."""
    s, why = check_command(command)
    if s >= HIGH:
        return f"command denied by guardian blocking review (task was assessed high risk): {why}; command: {command}"
    return None

