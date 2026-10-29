"""Wording for people: the dashboard's user mode shows Russian sentences instead of system strings.

Pure functions only. The raw data stays untouched in the dev mode; the server applies these to API payloads when
the page asks for ``mode=user``.
"""

from __future__ import annotations

import re
from typing import Optional

ROLES = {"researcher": "исследователь", "engineer": "инженер", "reviewer": "ревьюер", "auditor": "аудитор",
         "planner": "планировщик", "manager": "менеджер", "tester": "тестировщик", "librarian": "библиотекарь",
         "worker": "исполнитель", "architect": "архитектор", "writer": "автор", "designer": "дизайнер",
         "analyst": "аналитик", "developer": "разработчик", "refiner": "улучшатель", "evolver": "эволюционер"}

# whole strings the system and the scripted/test models produce
PHRASES = {
    "Every requirement of the goal is met.": "Все требования цели выполнены.",
    "All criteria verified against the files.": "Все критерии проверены по файлам.",
    "Deliverables written and listed.": "Результаты записаны и перечислены.",
    "Add more detail to the deliverable.": "Добавьте деталей в результат.",
    "Research and outline": "Исследование и план",
    "Build the deliverable": "Сборка результата",
    "Integrate and verify": "Сборка и проверка",
    "Write a changelog": "Написать список изменений",
    "A changelog is missing.": "Не хватает списка изменений.",
}

# beginnings of system comments: (pattern, replacement); the rest of the line is kept
PREFIXES = [
    (re.compile(r"^ACCEPTED \(not merged yet\): "), "Принято ревьюером, но ещё не влито: "),
    (re.compile(r"^ACCEPTED: "), "Принято ревьюером: "),
    (re.compile(r"^REJECTED: "), "Отклонено ревьюером: "),
    (re.compile(r"^GOAL MET: "), "Аудитор: цель достигнута. "),
    (re.compile(r"^BLOCKED: "), "Исполнитель остановился: "),
]
# fragments inside longer strings
FRAGMENTS = [
    (re.compile(r"\breviewer ACCEPTED\b"), "ревьюер принял"),
    (re.compile(r"\breviewer REJECTED\b"), "ревьюер отклонил"),
    (re.compile(r"\bauditor GOAL MET\b"), "аудитор: цель достигнута"),
    (re.compile(r"\bauditor GOAL NOT MET\b"), "аудитор: цель не достигнута"),
]


# manager comments that hand a task to the owner
ASKS = re.compile(r"Нужна настройка|Перепланирование|Менеджер передал")


def role_ru(role: Optional[str]) -> Optional[str]:
    """A role name in Russian; names we do not know (the planner invents roles) stay as they are."""
    if not isinstance(role, str):
        return role
    return ROLES.get(role.strip().lower(), role)


def ru(text):
    """Russian wording for one system string; anything else (the owner's words, model prose) is returned as is."""
    if not isinstance(text, str) or not text:
        return text
    if text in PHRASES:
        return PHRASES[text]
    for pat, rep in PREFIXES:
        if pat.match(text):
            rest = pat.sub("", text, count=1)
            return rep + PHRASES.get(rest, rest)
    for pat, rep in FRAGMENTS:
        text = pat.sub(rep, text)
    return text


def block_question(comments: list[dict]) -> str:
    """What the stuck worker asks the owner: the reason of its latest block, without the evidence dump.
    Empty once a human has answered after it."""
    for c in reversed(comments or []):
        if c.get("author") == "human":
            return ""
        body = (c.get("body") or "").strip()
        if body.startswith("BLOCKED:"):
            return body[len("BLOCKED:"):].split("\nEvidence:", 1)[0].strip()
        if c.get("author") == "manager" and ASKS.match(body):
            return body
    return ""


def _title(tasks: dict, tid) -> str:
    t = tasks.get(tid) or {}
    return f"«{t.get('title') or 'задача'}»"


def _sentence(e: dict, tasks: dict) -> Optional[tuple[str, str, Optional[str]]]:
    """(text, tone, task_id) for an event a person cares about, else None."""
    k, tid = e["kind"], e.get("task_id")
    if k == "board/task_moved":
        to = e.get("to")
        who = (tasks.get(tid) or {}).get("assignee") or role_ru(e.get("actor")) or ""  # the manager only moves cards
        if to == "running":
            return f"{who} приступает к {_title(tasks, tid)}".strip(), "info", tid
        if to == "review":
            return f"{_title(tasks, tid)} отправлена на проверку" + (f" · {who}" if who else ""), "info", tid
        if to == "blocked":
            if (tasks.get(tid) or {}).get("needs_human"):
                return f"Нужен ваш ответ по задаче {_title(tasks, tid)}", "bad", tid
            return f"Задача {_title(tasks, tid)} заблокирована", "bad", tid
    elif k == "review/verdict":
        if e.get("passed"):
            return f"Ревьюер принял {_title(tasks, tid)}", "ok", tid
        return f"Ревьюер вернул {_title(tasks, tid)} на доработку", "bad", tid
    elif k == "audit/verdict":
        return ("Аудитор: цель достигнута", "ok", None) if e.get("complete") else \
            ("Аудитор нашёл, чего не хватает: проект продолжается", "info", None)
    elif k == "board/comment" and e.get("author") == "human":
        return f"Вы ответили по задаче {_title(tasks, tid)}", "info", tid
    elif k == "owner/stop":
        return "Вы поставили проект на паузу", "info", None
    elif k == "owner/resume":
        return "Проект продолжен", "info", None
    elif k == "project/status" and e.get("status") == "done":
        return "Проект завершён", "ok", None
    return None


def happenings(events: list[dict], tasks: dict, limit: int = 7) -> list[dict]:
    """The last `limit` things that happened, as short Russian sentences (oldest first)."""
    out: list[dict] = []
    for e in events:
        s = _sentence(e, tasks)
        if s and not (out and out[-1]["text"] == s[0]):
            out.append({"ts": e["ts"], "text": s[0], "tone": s[1], "task_id": s[2]})
    return out[-limit:]


def _tr_task(t: dict) -> None:
    for f in ("title", "result"):
        if f in t:
            t[f] = ru(t[f])
    for c in t.get("comments") or []:
        if c.get("author") != "human":
            c["body"] = ru(c.get("body"))
            c["author"] = role_ru(c.get("author"))
    for s in t.get("timeline") or []:
        s["actor"] = role_ru(s.get("actor"))


def localize_stats(s: dict) -> dict:
    """Portfolio card / project stats, in place."""
    for w in s.get("workers") or []:
        w["role"] = role_ru(w.get("role"))
    for a in s.get("attention") or []:
        for f in ("title", "question", "reason"):
            if f in a:
                a[f] = ru(a[f])
    le = s.get("last_event")
    if le:
        le["text"] = ru(le["text"])
    return s


def localize_view(view: dict) -> dict:
    """A project view (project_view), in place."""
    for w in view.get("workers") or []:
        w["role"] = role_ru(w.get("role"))
    for a in view.get("active") or []:
        a["role"] = role_ru(a.get("role"))
    for t in (view.get("board") or {}).get("tasks") or []:
        _tr_task(t)
    if view.get("stats"):
        localize_stats(view["stats"])
    return view
