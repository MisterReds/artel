"""Guardian: a cheap trajectory risk reviewer and a blocking shell-command check.

After every worker run ``assess`` scores the run's tool trajectory with heuristics only (no LLM): destructive
commands, writes outside the workspace, network exfiltration, secrets in output. A high-risk run switches the
task's later runs to blocking review: every shell command goes through ``review_command`` before it executes.

The two-step shape (non-blocking trajectory reviewer first, blocking reviewer on future actions once risk is high)
and the risk taxonomy (low / medium / high / critical, "do not call a path outside the workspace high on its own",
"a specific rm -rf is not automatically high") are adapted from the Codex guardian
codex-rs/prompts/templates/guardian/classifier_instructions.md -- Apache-2.0, Copyright (c) OpenAI.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

HIGH, MEDIUM = 0.7, 0.4
CATEGORIES = ("destructive", "outside_write", "exfiltration", "secret")

_WIPE = r"(?:/|~|\$HOME|\*|\.\.?|/\*|~/\*)"
# (category, pattern, score, reason); the strongest hit wins, each further category adds a little
COMMAND_RULES: list[tuple[str, re.Pattern, float, str]] = [(c, re.compile(p, re.I), s, r) for c, p, s, r in [
    ("destructive", rf"\brm\s+(?:-\w*\s+)*-\w*[rR]\w*\s+(?:-\w+\s+)*(?:--no-preserve-root\s+)?{_WIPE}(?:\s|$)", 0.9,
     "recursive delete of root/home/wildcard"),
    ("destructive", r"\brm\s+(?:-\w+\s+)*(?:-\w*[rR]\w*\s+(?:-\w+\s+)*-\w*f|-\w*f\w*\s+(?:-\w+\s+)*-\w*[rR]|"
                    r"-\w*(?:rf|fr)\w*|--recursive\b.*--force|--force\b.*--recursive)", 0.5, "recursive force delete (rm -rf)"),
    ("destructive", r"\bmkfs(?:\.\w+)?\b", 0.95, "formats a filesystem (mkfs)"),
    ("destructive", r"\bdd\b[^|;&]*\bof=/dev/", 0.95, "dd writes to a block device"),
    ("destructive", r">\s*/dev/(?:sd|nvme|hd|vd)\w*", 0.95, "redirect onto a block device"),
    ("destructive", r"\bgit\s+reset\s+--hard\b", 0.5, "git reset --hard discards work"),
    ("destructive", r"\bgit\s+push\b[^;&|]*(?:--force\b|--force-with-lease\b|\s-f\b)", 0.65, "forced git push"),
    ("destructive", r"\bgit\s+clean\s+-\w*[fdx]", 0.5, "git clean deletes untracked files"),
    ("destructive", r"\bchmod\s+(?:-\w+\s+)*-\w*R\w*\s+\S+\s+(?:/|~|\$HOME)(?:\s|$)", 0.8, "recursive chmod on root/home"),
    ("destructive", r"\bchmod\s+(?:-\w+\s+)*-\w*R", 0.4, "recursive chmod"),
    ("destructive", r"\bchown\s+(?:-\w+\s+)*-\w*R", 0.4, "recursive chown"),
    ("destructive", r":\(\)\s*\{\s*:\s*\|\s*:", 0.9, "fork bomb"),
    ("destructive", r"\b(?:shutdown|reboot|halt|poweroff)\b", 0.7, "powers off the machine"),
    ("destructive", r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", 0.7, "pipes a download into a shell"),
    ("exfiltration", r"\b(?:curl|wget)\b[^;&|]*(?:\s-d\b|--data\S*|\s-F\b|--form\b|\s-T\b|--upload-file|-X\s*(?:POST|PUT))"
                     r"[^;&|]*(?:@|\$\(|`|/etc/|\.ssh|\.env|\.aws|id_rsa|passwd|credentials)", 0.9,
     "sends local files/secrets over the network"),
    ("exfiltration", r"\b(?:curl|wget)\b[^;&|]*(?:\s-d\b|--data\S*|\s-F\b|--form\b|\s-T\b|--upload-file|-X\s*(?:POST|PUT))",
     0.6, "uploads data with curl/wget"),
    ("exfiltration", r"\b(?:nc|ncat|netcat|socat)\b\s+\S", 0.6, "raw network connection (nc/socat)"),
    ("exfiltration", r"\b(?:scp|rsync|sftp)\b[^;&|]*\s\S+@?[\w.-]+:\S*", 0.6, "copies files to a remote host"),
    ("exfiltration", r"(?:cat|tar|base64)\b[^;&|]*(?:\.ssh|\.aws|\.env\b|id_rsa|/etc/shadow|\.netrc)[^;&|]*\|\s*"
                    r"(?:curl|wget|nc|ncat|socat)\b", 0.95, "pipes credentials to the network"),
    ("outside_write", r"(?:>>?|\btee\s+(?:-a\s+)?)\s*(?:/etc|/usr|/bin|/sbin|/boot|/lib|/root|/var|~/\.\w+|\$HOME/\.\w+)\S*",
     0.8, "writes into a system or dot path"),
]]

SECRET_RULES: list[tuple[re.Pattern, float, str]] = [(re.compile(p), s, r) for p, s, r in [
    (r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----", 0.85, "private key block in output"),
    (r"\bAKIA[0-9A-Z]{16}\b", 0.75, "AWS access key id in output"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", 0.75, "GitHub token in output"),
    (r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_-]{24,}", 0.75, "API key (sk-...) in output"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{20,}", 0.75, "Slack token in output"),
    (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{30,}", 0.6, "bearer token in output"),
    (r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?[A-Za-z0-9_\-/+=]{20,}", 0.55, "credential assignment in output"),
]]

_REDIRECT = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*(/[^\s;&|<>'\"]+)")


def _scan_command(command: str) -> list[tuple[str, float, str]]:
    return [(cat, s, why) for cat, rx, s, why in COMMAND_RULES if rx.search(command)]


def _outside_paths(command: str, roots: Iterable[str]) -> list[str]:
    roots = [r.rstrip("/") for r in roots if r]
    bad = []
    for m in _REDIRECT.finditer(command):
        p = m.group(1)
        if p.startswith("/dev/null") or p.startswith("/tmp/") or any(p == r or p.startswith(r + "/") for r in roots):
            continue
        bad.append(p)
    return bad


def _finish(hits: list[tuple[str, float, str]]) -> dict:
    if not hits:
        return {"risk": "low", "score": 0.0, "reasons": []}
    top = max(s for _, s, _ in hits)
    score = min(1.0, round(top + 0.05 * (len({c for c, _, _ in hits}) - 1), 2))
    reasons = list(dict.fromkeys(f"{why}" for _, s, why in sorted(hits, key=lambda h: -h[1])))[:6]
    return {"risk": "high" if score >= HIGH else "medium" if score >= MEDIUM else "low", "score": score,
            "reasons": reasons}


def assess(events: Iterable[dict], roots: Iterable[str] = (), kernel: Any = None, project: Any = None) -> dict:
    """Score a run's trajectory -> ``{risk: low|medium|high, score, reasons}``.

    ``events`` are ``{"tool", "input", "output", "is_error"}`` dicts in call order; ``roots`` are the directories
    the run may write to (workspace, shared). Pure heuristics; the optional System One refinement only runs for
    a medium result and only when the ``guardian`` decision point is enabled.
    """
    hits: list[tuple[str, float, str]] = []
    roots = list(roots)
    for ev in events:
        args = ev.get("input") or {}
        if not isinstance(args, dict):
            args = {"command": str(args)}
        if ev.get("tool") == "bash":
            cmd = str(args.get("command", ""))
            hits += _scan_command(cmd)
            hits += [("outside_write", 0.4, f"writes outside the workspace: {p}") for p in _outside_paths(cmd, roots)]
        elif ev.get("tool") in ("write_file", "edit_file"):
            p = str(args.get("path", ""))
            if p.startswith("/") and not any(p == r.rstrip("/") or p.startswith(r.rstrip("/") + "/") for r in roots if r):
                sysdir = re.match(r"/(?:etc|usr|bin|sbin|boot|lib|root|var)(?:/|$)", p)
                hits.append(("outside_write", 0.8 if sysdir else 0.4, f"writes outside the workspace: {p}"))
        text = f"{ev.get('output') or ''}\n{args.get('content') or args.get('new_string') or ''}"
        for rx, s, why in SECRET_RULES:
            if rx.search(text):
                hits.append(("secret", s, why))
    res = _finish(hits)
    if res["risk"] == "medium" and kernel is not None and project is not None:
        res = _refine(res, kernel, project)
    return res


def _refine(res: dict, kernel, project) -> dict:
    """Optional System One second opinion on a medium result; any failure keeps the heuristic answer."""
    try:
        from .decide import decide, noul

        ans = decide(kernel, project, "guardian", {"findings": "; ".join(res["reasons"])},
                     {"risky": noul("Could an agent that did this go on to cause irreversible damage, leak secrets "
                                    "or export data to an untrusted destination?", "yes, likely", "no, routine work")})
        p = float(((ans or {}).get("risky") or {}).get("noul", 0.5))
    except Exception:  # noqa: BLE001 - the reviewer must never break a run
        return res
    if p >= 0.8:
        return {**res, "risk": "high", "score": max(res["score"], HIGH), "reasons": res["reasons"] + ["System One: risky"]}
    return res


def review_command(command: str, roots: Iterable[str] = (), kernel: Any = None, project: Any = None) -> dict:
    """Blocking check of one shell command -> ``{allow: bool, reason, risk, score}``. Denies high risk only."""
    hits = _scan_command(command)
    hits += [("outside_write", 0.4, f"writes outside the workspace: {p}") for p in _outside_paths(command, roots)]
    res = _finish(hits)
    if res["risk"] == "medium" and kernel is not None and project is not None:
        res = _refine(res, kernel, project)
    allow = res["risk"] != "high"
    return {"allow": allow, "risk": res["risk"], "score": res["score"],
            "reason": "; ".join(res["reasons"]) if res["reasons"] else "no risk found"}


def trace_events(out_digest: Optional[list] = None) -> list[dict]:  # pragma: no cover - convenience for runtimes
    """Fallback when a runtime keeps no structured trace: parse ``TOOL name({json}) -> ...`` digest lines."""
    evs = []
    for line in out_digest or []:
        m = re.match(r"TOOL (\w+)\((.*?)\) -> (ERROR )?(.*)$", line, re.S)
        if not m:
            continue
        try:
            inp = json.loads(m.group(2))
        except ValueError:
            inp = {"command": m.group(2)} if m.group(1) == "bash" else {}
        evs.append({"tool": m.group(1), "input": inp, "output": m.group(4), "is_error": bool(m.group(3))})
    return evs
