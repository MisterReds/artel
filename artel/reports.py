"""Failure reports: every failure Artel meets becomes a short text report — what broke, the diagnostician's cause and
steps, where, on which version — kept in ARTEL_HOME/reports/ and sent where it helps:

- to the owner: Telegram (when the bot is paired), as soon as it is recorded;
- to the developers: an issue in Artel's own GitHub repository (label `artel-report`) through the GitHub CLI the
  installer signed in with. By button ("Отправить разработчикам") by default; `reports.github: auto` sends every new
  kind of failure. The same failure (same fingerprint) is a comment on the existing issue, not a new one, so
  evolution sees how often each failure happens.

Nothing secret leaves the machine: API keys, tokens, bearer headers, e-mails and the user's home path are removed
before a report is stored or sent, and the person can read the exact text first.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

DEFAULTS = {"telegram": True, "github": "ask"}  # github: ask | auto | off
LABEL = "artel-report"

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN[^-\n]*PRIVATE KEY-----[\s\S]*?(?:-----END[^-\n]*-----|$)"), "<закрытый ключ>"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{12,}"), "<ключ>"),
    (re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"), "<токен GitHub>"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}"), "<токен Slack>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), "<ключ Google>"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "<токен Hugging Face>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<ключ AWS>"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), "<токен Telegram>"),
    (re.compile(r"(?i)\b(bearer|basic)(\s{1,16})[A-Za-z0-9._~+/=\-]{8,}"), r"\1\2<скрыто>"),
    (re.compile(r"(?i)(x-artel-token[\"']?\s{0,8}[:=]\s{0,8}[\"']?)[0-9a-f]{16,}"), r"\1<скрыто>"),
    # NAME=value, "api_key": "value", 'token': '…' — a name that ends in key/token/secret/password, any quoting
    (re.compile(r"""(?ix)
        ((?:key|token|secret|passw(?:or)?d|passwd|credentials?)["']?\s{0,8}[:=]\s{0,8})
        (?:"[^"\n]*"?|'[^'\n]*'?|[^\s,;}\]"']+)"""), r"\1<скрыто>"),
    (re.compile(r"""(?i)\b(passw(?:or)?d|passwd|secret)(\s{1,16})(?:"[^"\n]*"|'[^'\n]*')"""), r"\1\2<скрыто>"),
    (re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}\.[\w.-]{1,253}"), "<e-mail>"),
]


def settings(config: dict) -> dict:
    return {**DEFAULTS, **(config.get("reports") or {})}


def redact(text: str) -> str:
    out = str(text or "")
    for pattern, repl in SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    home = str(Path.home())
    if home and home not in ("/", "/root"):
        out = out.replace(home, "~")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if len(user) >= 3:
        out = re.sub(rf"(?<![\w.]){re.escape(user)}(?![\w])", "<user>", out)
    return out


def redact_deep(value):
    """Secret patterns masked in every string value of nested dicts/lists (keys untouched); paths stay as they are."""
    if isinstance(value, str):
        for pattern, repl in SECRET_PATTERNS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_deep(v) for v in value]
    return value


def fingerprint(kind: str, text: str) -> str:
    """The same failure on another day or machine: numbers, paths, ids and times removed before hashing."""
    norm = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{7,}\b|\d+", "#", str(text or "").lower())
    norm = re.sub(r"(/[\w.\-<>~]+)+", "/…", norm)
    norm = re.sub(r"\s+", " ", norm).strip()[:400]
    return hashlib.sha256(f"{kind}|{norm}".encode()).hexdigest()[:12]


def _dir() -> Path:
    from .project import artel_home

    d = artel_home() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def version() -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=10)
        return p.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_lock = threading.Lock()
_listeners: list = []  # callables(report) — e.g. the Telegram bot


def record(kind: str, title: str, text: str, diagnosis: Optional[dict] = None, project: Optional[str] = None,
           task_id: Optional[str] = None, config: Optional[dict] = None) -> dict:
    """Store a failure report (redacted) and tell the listeners; sends to GitHub when `reports.github: auto`."""
    rep = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "kind": kind, "title": redact(title)[:200],
           "text": redact(text)[-6000:], "fingerprint": fingerprint(kind, title + "\n" + (text or "")[:400]),
           "project": project, "task_id": task_id, "version": version(),
           "system": f"{platform.system()} {platform.release()} · Python {sys.version.split()[0]}"}
    if diagnosis:
        rep["diagnosis"] = {k: (redact(v) if isinstance(v, str) else [redact(x) for x in v] if isinstance(v, list) else v)
                            for k, v in diagnosis.items() if k in ("cause", "who", "steps", "evidence", "by", "confidence")}
    with _lock:
        with (_dir() / "reports.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rep, ensure_ascii=False) + "\n")
    for fn in list(_listeners):
        try:
            fn(rep)
        except Exception:  # noqa: BLE001 - a listener never breaks recording
            pass
    if config is not None and settings(config)["github"] == "auto":
        threading.Thread(target=lambda: send_github(rep["id"]), daemon=True, name="report-github").start()
    return rep


def load(limit: int = 200) -> list[dict]:
    p = _dir() / "reports.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    sent = _sent()
    for r in rows:
        r["sent"] = sent.get(r["fingerprint"])
    return rows[-limit:][::-1]


def get(rid: str) -> Optional[dict]:
    return next((r for r in load(10_000) if r["id"] == rid), None)


def _sent() -> dict:
    p = _dir() / "sent.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _mark_sent(fp: str, info: dict) -> None:
    with _lock:
        data = _sent()
        data[fp] = info
        (_dir() / "sent.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def markdown(rep: dict) -> str:
    d = rep.get("diagnosis") or {}
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(d.get("steps") or [], 1))
    parts = [f"**{rep['title']}**", "",
             f"- вид: `{rep['kind']}` · версия Artel: `{rep['version']}` · {rep['system']}",
             f"- отпечаток: `{rep['fingerprint']}` · время: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(rep['ts']))}"]
    if d:
        parts += ["", "### Диагност", f"Причина: {d.get('cause', '')}", f"Исправляет: {d.get('who', '')}",
                  steps, f"Доказательство: {d.get('evidence', '')}" if d.get("evidence") else "",
                  f"_({'правила' if d.get('by') == 'rules' else 'модель ' + str(d.get('by'))})_"]
    parts += ["", "### Подробности", "```", rep["text"][-4000:], "```",
              "", "_Отчёт создан Artel автоматически; ключи, токены и пути пользователя вырезаны._"]
    return "\n".join(p for p in parts if p is not None)


def github_repo() -> Optional[str]:
    """owner/name of the repository this installation came from (its origin)."""
    root = Path(__file__).resolve().parents[1]
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def github_ready() -> dict:
    gh = shutil.which("gh")
    repo = github_repo()
    if not gh:
        return {"ok": False, "reason": "нет GitHub CLI (gh) — установщик ставит его для доступа к репозиторию"}
    if not repo:
        return {"ok": False, "reason": "установка не из GitHub-репозитория"}
    p = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=20)
    if p.returncode != 0:
        return {"ok": False, "reason": "GitHub CLI не вошёл в аккаунт: gh auth login"}
    return {"ok": True, "repo": repo}


def _gh(*args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run([shutil.which("gh") or "gh", *args], capture_output=True, text=True, timeout=60,
                          input=input_text, encoding="utf-8", errors="replace")


def send_github(rid: str) -> dict:
    """Open an issue for this failure in Artel's repository, or comment on the open one with the same fingerprint."""
    rep = get(rid)
    if not rep:
        return {"ok": False, "error": "нет такого отчёта"}
    ready = github_ready()
    if not ready["ok"]:
        return {"ok": False, "error": ready["reason"]}
    repo, body = ready["repo"], markdown(rep)
    found = _gh("issue", "list", "-R", repo, "--state", "open", "--label", LABEL, "--search",
                f"{rep['fingerprint']} in:body", "--json", "number,url", "--limit", "1")
    try:
        existing = (json.loads(found.stdout) or [None])[0] if found.returncode == 0 else None
    except ValueError:
        existing = None
    if existing:
        p = _gh("issue", "comment", str(existing["number"]), "-R", repo, "--body-file", "-", input_text=body)
        url = existing["url"]
    else:
        _gh("label", "create", LABEL, "-R", repo, "--color", "B5522B", "--description",
            "failure reported by an Artel installation", "--force")
        p = _gh("issue", "create", "-R", repo, "--title", f"[сбой] {rep['title'][:120]}", "--label", LABEL,
                "--body-file", "-", input_text=body)
        url = p.stdout.strip().splitlines()[-1] if p.returncode == 0 and p.stdout.strip() else ""
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout).strip()[-400:]}
    _mark_sent(rep["fingerprint"], {"url": url, "at": time.time(), "comment": bool(existing)})
    return {"ok": True, "url": url, "comment": bool(existing)}


def telegram_text(rep: dict) -> str:
    d = rep.get("diagnosis") or {}
    lines = [f"⚠️ Сбой: {rep['title']}"]
    if d.get("cause"):
        lines.append(f"Причина: {d['cause']}")
    if d.get("steps"):
        lines.append("Что сделать: " + d["steps"][0])
    lines.append("Подробно: дашборд → «Сбои»")
    return "\n".join(lines)[:1500]


__all__ = ["record", "load", "get", "redact", "fingerprint", "markdown", "send_github", "github_ready",
           "telegram_text", "settings"]
