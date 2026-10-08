"""The diagnostician: when something does not work, a bot on whatever model is available explains why and what to do.

`diagnose()` collects the facts a person would otherwise dig out by hand — the error, the project's recent
failures and the task's history, provider and sandbox state, the embeddings server log, proxy settings — matches
them against known causes, and gives them to a read-only `doctor` agent on an available runtime (preferably not the
one that is failing). The agent may read Artel's logs and the project's files and must answer with
`diagnosis_report`: the cause, the evidence, who fixes it and the steps. With no model available, or when the
model does not answer, the known-cause rules answer on their own. Reports are kept in ARTEL_HOME/doctor/.

Used by: the "Почему?" buttons in the dashboard, `artel why`, and automatically when a task stops for a person.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

PROMPT = """You are Artel's diagnostician. Something in Artel does not work and a person wants to know why, in plain
Russian, and what to do. You get the error and the facts Artel collected; you may read the listed log files and
project files (read-only) to check. Rules:
- Name the actual cause, not the symptom. Quote the line of evidence that proves it.
- Say who fixes it: "artel" (a bug or setting inside Artel), "owner" (something on the person's machine or
  account), or "provider" (the model service: limits, outage).
- Give concrete steps the person can follow now (commands, buttons in the dashboard), shortest path first.
- If the facts are not enough, say what exactly is unknown and how to find out; never invent a cause.
- The known-cause hints below are rules of thumb: confirm or reject them from the evidence.
Finish by calling diagnosis_report exactly once. Write everything in Russian."""

# Known causes: (pattern over the error and the facts, cause, who fixes it, steps). Russian, shown as they are.
KNOWN = [
    (r"bwrap: .*(uid map|loopback|RTM_NEWADDR|Operation not permitted|Permission denied)|setting up uid map",
     "Песочница Codex (bubblewrap) не может запуститься на этой машине: система запрещает пользовательские "
     "пространства имён или настройку сети в них.", "owner",
     ["Обновите Artel: он сам переключит Codex в режим без песочницы, права и политика команд Artel продолжат действовать.",
      "Чтобы вернуть песочницу (Ubuntu 24.04+): sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"]),
    (r"no module named pytest", "В окружении Artel нет pytest, а проверки обновления его используют.", "owner",
     ["~/.artel/venv/bin/pip install pytest", "затем artel update"]),
    (r"llama-server не ответил|llama-server завершился", "Локальный сервер эмбеддингов не запустился или не ответил.",
     "owner", ["Посмотрите ~/.artel/tools/models/llama-server.log — последние строки называют причину.",
               "«Провайдеры» → «Эмбеддинги» → «Удалить» и «Установить» заново; без модели поиск идёт по словам."]),
    (r"usage limit|session limit|rate limit|hit your .*limit|\b429\b", "Подписка упёрлась в лимит провайдера.", "provider",
     ["Artel переключит работу на другой подключённый рантайм, если он есть («Провайдеры»).",
      "Или дождитесь сброса лимита — время сброса указано в ошибке."]),
    (r"not logged in|не подключ|login", "Рантайм не вошёл в аккаунт.", "owner",
     ["«Провайдеры» → карточка рантайма → «Войти»."]),
    (r"не установлен|not installed|command not found", "Нужная программа не установлена.", "owner",
     ["«Провайдеры» → «Установить» на карточке рантайма или инструмента."]),
    (r"no space left|disk full|ENOSPC", "Закончилось место на диске.", "owner",
     ["Освободите место (старые проекты в ~/.artel/projects, кэш ~/.cache) и нажмите «Продолжить»."]),
    (r"Failed to write file|Read-only file system", "Агенту запрещена запись в эту папку.", "artel",
     ["Проверьте права (fs-grants) роли: исполнитель пишет в свою папку и в shared/, проверяющие — никуда."]),
]


PROXY_SYMPTOM = r"127\.0\.0\.1|localhost|не ответил|connection refused|urlopen error|timed out|ProxyError"
PROXY_HINT = {"cause": "Задан системный HTTP-прокси, а ошибка похожа на обрыв локального соединения: запросы к 127.0.0.1 "
                       "могли уходить через прокси.", "who": "owner",
              "steps": ["Обновите Artel: соединения с 127.0.0.1 идут напрямую.",
                        "Либо добавьте в окружение: export no_proxy=127.0.0.1,localhost"]}


def known_causes(text: str, proxies: bool = False) -> list[dict]:
    out = []
    if proxies and re.search(PROXY_SYMPTOM, text or "", re.I):
        out.append(PROXY_HINT)
    for pattern, cause, who, steps in KNOWN:
        if re.search(pattern, text or "", re.I):
            out.append({"cause": cause, "who": who, "steps": steps})
    return out


def _tail(path: Path, n: int = 1500) -> str:
    try:
        return path.read_bytes()[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


def facts(config: dict, problem: str, project=None, task_id: Optional[str] = None) -> tuple[str, list[str]]:
    """A compact, factual brief for the diagnostician, and the files it may read."""
    from . import limits
    from .project import artel_home

    lines = [f"Проблема: {problem.strip()[:2000]}", "",
             f"Система: {platform.platform()} · Python {sys.version.split()[0]} · ARTEL_HOME={artel_home()}"]
    proxies = [k for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy") if os.environ.get(k)]
    lines.append("Прокси в окружении: " + (", ".join(proxies) + f" (no_proxy={os.environ.get('no_proxy') or os.environ.get('NO_PROXY') or '—'})"
                                           if proxies else "нет"))
    try:
        from .providers import claude_code_status, codex_status, embeddings_status, routing

        cx, cc = codex_status(config), claude_code_status(config)
        lines.append(f"Codex: установлен={cx.get('installed')} вход={cx.get('logged_in')} {cx.get('version', '')} "
                     f"песочница: {cx.get('sandbox') or '—'}")
        lines.append(f"Claude Code: установлен={cc.get('installed')} вход={cc.get('logged_in')} {cc.get('version', '')}")
        em = embeddings_status(config)
        lines.append(f"Эмбеддинги: режим={em.get('active')} установлены={em.get('installed')} ошибка={em.get('error') or '—'}")
        r = routing(config)
        lines.append(f"Рантайм по умолчанию: {r['runtime']} (сейчас {r['auto_resolves_to']}); по ролям: {r['runtime_by_role']}; "
                     f"отключены: {r['disabled_runtimes']}; лимиты: {r['limited'] or '—'}")
    except Exception as e:  # noqa: BLE001 - facts are best effort
        lines.append(f"(состояние провайдеров не прочитано: {e!r})")
    readable = []
    log = artel_home() / "tools" / "models" / "llama-server.log"
    if log.exists():
        readable.append(str(log))
        lines += ["", "Конец llama-server.log:", _tail(log, 800)]
    if project is not None:
        meta = project.meta
        lines += ["", f"Проект «{meta.get('name')}»: статус={meta.get('status')} "
                      f"причина остановки={meta.get('blocked_reason') or '—'}"]
        kinds = ("llm/error", "agent/end", "codex/sandbox", "policy/blocked", "provider/limited", "manager/escalated",
                 "manager/provider_switch", "codex/readonly_violation", "gate/result")
        recent = [e for e in project.log.read() if e["kind"] in kinds and (not task_id or e.get("task_id") in (task_id, None))]
        for e in recent[-25:]:
            detail = e.get("error") or e.get("reason") or e.get("rationale") or e.get("status") or ""
            if e["kind"] == "agent/end" and e.get("status") in ("terminal", "done"):
                continue
            lines.append(f"- {e['kind']} {e.get('actor', '')}: {str(detail)[:300]}")
        if task_id:
            t = project.board.get(task_id)
            if t:
                lines += ["", f"Задача «{t['title']}» ({t['status']}, попыток {t['attempts']}, исполнитель {t['assignee']})"]
                for c in project.board.comments(task_id)[-8:]:
                    lines.append(f"  {c.get('author')}: {str(c.get('body', ''))[:500]}")
        readable += [str(project.root / "events.jsonl")]
    return "\n".join(lines), readable


def choose_runtime(config: dict, exclude: Optional[str] = None) -> str:
    """An available runtime for the diagnostician, preferring one other than the failing one."""
    from . import limits
    from .runtimes import connected_runtimes, detect_runtime

    if config.get("provider") == "scripted":
        return "native"
    forced = (config.get("runtime_by_role") or {}).get("doctor")
    if forced and forced != "auto":
        return forced
    out = limits.load()
    usable = [r for r in connected_runtimes(config) if r not in (config.get("disabled_runtimes") or []) and r not in out]
    others = [r for r in usable if r != (exclude or "").split(":")[0]]
    return (others or usable or [detect_runtime(config)])[0]


def _store(report: dict) -> dict:
    from .project import artel_home

    d = artel_home() / "doctor"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "reports.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    return report


def diagnose(config: dict, problem: str, project=None, task_id: Optional[str] = None,
             exclude_runtime: Optional[str] = None, use_model: bool = True) -> dict:
    """Explain a failure: {cause, evidence, who, steps, confidence, by, hints}. Never raises."""
    from .project import Project, artel_home, deep_merge

    brief, readable = facts(config, problem, project, task_id)
    proxies = any(os.environ.get(k) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy"))
    # what the problem itself says comes first; causes seen only in the collected facts follow
    hints = known_causes(problem, proxies)
    hints += [h for h in known_causes(brief, False) if h not in hints]
    base = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "problem": problem[:2000],
            "project": getattr(project, "slug", None), "task_id": task_id, "hints": hints}
    rule = hints[0] if hints else None
    if use_model:
        rt = choose_runtime(config, exclude_runtime)
        if rt != "none":
            try:
                from .manager import new_kernel
                from .runtimes import run_agent
                from .tooling import ToolContext

                cfg = deep_merge(config, {"runtime_by_role": {"doctor": rt}})
                refs = tuple(Path(p).parent for p in readable) + ((project.root,) if project is not None else ())
                dproj = Project.create(f"diagnose: {problem[:120]}", cfg, home=artel_home() / "doctor",
                                       name=f"why-{base['id']}", refs=tuple(dict.fromkeys(refs)))
                kernel = new_kernel(cfg)
                try:
                    ctx = ToolContext(kernel, dproj, role="doctor", workspace=dproj.shared)
                    hint_text = "\n".join(f"- {h['cause']} (исправляет: {h['who']})" for h in hints) or "- нет"
                    out = run_agent(kernel, dproj, role="doctor", system=PROMPT, ctx=ctx, purpose="diagnose",
                                    first_user=f"{brief}\n\nПодсказки по известным причинам:\n{hint_text}\n\n"
                                               f"Файлы, которые можно прочитать: {', '.join(readable) or '—'}",
                                    max_turns=12)
                finally:
                    kernel.shutdown(reason="doctor")
                if out.name == "diagnosis_report":
                    return _store({**base, **out.payload, "by": rt})
                base["model_error"] = f"{rt}: {out.status} {out.error}"[:400]
            except Exception as e:  # noqa: BLE001 - the diagnostician must never become the next failure
                base["model_error"] = repr(e)[:400]
        else:
            base["model_error"] = "нет доступной модели"
    if rule:
        return _store({**base, "cause": rule["cause"], "who": rule["who"], "steps": rule["steps"],
                       "evidence": "совпадение с известной причиной", "confidence": 0.6, "by": "rules"})
    return _store({**base, "cause": "Причину по известным признакам определить не удалось.", "who": "unknown",
                   "steps": ["Откройте ленту проекта: последние ошибки показаны красным.",
                             "Подключите хотя бы один рантайм на странице «Провайдеры» — тогда диагност разберёт сбой моделью."],
                   "evidence": base.get("model_error", ""), "confidence": 0.1, "by": "rules"})


class Jobs:
    """Diagnoses started from the dashboard run in the background; the page polls their state."""

    def __init__(self):
        self.state: dict = {}
        self.lock = threading.Lock()

    def start(self, config: dict, problem: str, project=None, task_id: Optional[str] = None,
              exclude_runtime: Optional[str] = None) -> dict:
        jid = uuid.uuid4().hex[:10]
        with self.lock:
            self.state[jid] = {"id": jid, "phase": "running", "since": time.time()}

        def run():
            rep = diagnose(config, problem, project, task_id, exclude_runtime)
            self.state[jid] = {"id": jid, "phase": "done", "report": rep}

        threading.Thread(target=run, daemon=True, name=f"doctor-{jid}").start()
        return self.state[jid]

    def get(self, jid: str) -> dict:
        return self.state.get(jid) or {"phase": "unknown"}


def format_report(rep: dict) -> str:
    who = {"artel": "Artel", "owner": "владелец (на этой машине или в аккаунте)", "provider": "провайдер модели"}.get(
        rep.get("who"), "неизвестно")
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(rep.get("steps") or [], 1))
    by = "по правилам" if rep.get("by") == "rules" else f"модель: {rep.get('by')}"
    return (f"Причина: {rep.get('cause')}\nИсправляет: {who}\n" + (f"Что сделать:\n{steps}\n" if steps else "")
            + (f"Доказательство: {rep.get('evidence')}\n" if rep.get("evidence") else "") + f"({by})")


__all__ = ["diagnose", "known_causes", "facts", "choose_runtime", "Jobs", "format_report", "PROMPT"]
