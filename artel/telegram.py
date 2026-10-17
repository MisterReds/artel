"""Artel in Telegram: control and notifications from the phone.

The bot runs inside the dashboard process (`artel web`) or alone (`artel telegram`) and talks to the same Runner,
so a goal started from the phone is a normal project visible everywhere.

Security: the bot token comes only from the environment (ARTEL_TELEGRAM_TOKEN) and is never stored. Nobody gets
any data until their chat is paired: `/pair CODE` with a one-time code (valid 15 minutes) shown in the terminal and
on the dashboard's "Подключения" page. Paired chat ids are kept in ARTEL_HOME/telegram.json.

Commands: /status · /projects · /p N · /new <цель> (or just send the goal text) · /answer N <ответ> · /stop N ·
/resume N · /update · /unpair · /help. Notifications: a project is done, needs a person, failed or ran out of
budget; an update of Artel is available.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

TOKEN_ENV = "ARTEL_TELEGRAM_TOKEN"
API = "https://api.telegram.org"
CODE_TTL_S = 15 * 60
NOTIFY = {"done": "✅ Цель достигнута", "waiting_human": "🙋 Нужен человек", "error": "⚠️ Ошибка",
          "paused_budget": "⏸ Закончился бюджет"}
STATUS_RU = {"done": "готово", "running": "в работе", "planning": "планирование", "waiting_human": "ждёт человека",
             "error": "ошибка", "stopped": "остановлен", "paused_budget": "нет бюджета", "needs_review": "на проверке"}
HELP = ("Artel на связи.\n"
        "/status — сводка\n/projects — проекты\n/p N — проект подробно\n"
        "/new цель — начать (или просто пришлите текст цели)\n/answer N текст — ответить проекту, который ждёт вас\n"
        "/stop N · /resume N — остановить / продолжить\n/update — обновление Artel\n/unpair — отвязать этот чат")


def token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def _store_path() -> Path:
    from .project import artel_home

    return artel_home() / "telegram.json"


class Store:
    """Paired chats, the pending pairing code, the update offset and what was already notified."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _store_path()
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}
        self.data.setdefault("paired", [])
        self.data.setdefault("offset", 0)
        self.data.setdefault("notified", {})

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
            tmp.replace(self.path)

    def new_code(self) -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self.data["pair"] = {"code": code, "expires": time.time() + CODE_TTL_S}
        self.save()
        return code

    def pair(self, chat_id: int, code: str) -> bool:
        p = self.data.get("pair") or {}
        ok = bool(p) and time.time() < p.get("expires", 0) and secrets.compare_digest(p.get("code", ""), code.strip())
        if ok:
            if chat_id not in self.data["paired"]:
                self.data["paired"].append(chat_id)
            self.data.pop("pair", None)  # single use
            self.save()
        return ok


class TelegramBot:
    def __init__(self, runner, tok: Optional[str] = None, api_base: Optional[str] = None, store: Optional[Store] = None):
        self.runner = runner
        self.tok = tok or token()
        self.base = (api_base or (runner.config.get("telegram") or {}).get("api_base") or API).rstrip("/")
        self.store = store or Store()
        self.client = httpx.Client(timeout=40)
        self.listing: dict[int, list[str]] = {}   # chat -> project slugs as last listed (for "N" references)
        self.pending_goal: dict[int, str] = {}
        self.stop_event = threading.Event()
        self.failed_pairs: dict[int, int] = {}
        self.started_at = time.time()

    # --- Telegram API ---------------------------------------------------
    def call(self, method: str, **params) -> dict:
        r = self.client.post(f"{self.base}/bot{self.tok}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description', r.status_code)}")
        return data.get("result")

    def send(self, chat_id: int, text: str, buttons: Optional[list] = None) -> None:
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self.call("sendMessage", **params)
        except Exception:  # noqa: BLE001 - a lost message must not stop the bot
            pass

    # --- loop -----------------------------------------------------------
    def poll_once(self, timeout: int = 25) -> int:
        updates = self.call("getUpdates", offset=self.store.data["offset"], timeout=timeout,
                            allowed_updates=["message", "callback_query"])
        for u in updates or []:
            self.store.data["offset"] = u["update_id"] + 1
            try:
                self.handle(u)
            except Exception as exc:  # noqa: BLE001
                chat = ((u.get("message") or {}).get("chat") or {}).get("id")
                if chat in self.store.data["paired"]:
                    self.send(chat, f"Не получилось: {exc}")
        if updates:
            self.store.save()
        return len(updates or [])

    def run(self) -> None:
        threading.Thread(target=self._notify_loop, daemon=True, name="telegram-notify").start()
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                backoff = 1.0
            except Exception:  # noqa: BLE001 - network hiccups: retry with backoff
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def on_report(self, rep: dict) -> None:
        """A failure report was recorded: tell the owner at once (reports.telegram, on by default)."""
        from . import reports

        if not reports.settings(self.runner.config).get("telegram", True):
            return
        for chat in list(self.store.data["paired"]):
            self.send(chat, reports.telegram_text(rep))

    def start(self) -> "TelegramBot":
        from . import reports

        reports._listeners.append(self.on_report)
        if not self.store.data["paired"] and not self.store.data.get("pair"):
            self.store.new_code()
        threading.Thread(target=self.run, daemon=True, name="telegram").start()
        return self

    # --- handling -------------------------------------------------------
    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = cq["message"]["chat"]["id"]
            try:
                self.call("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:  # noqa: BLE001
                pass
            if chat in self.store.data["paired"]:
                self.on_button(chat, cq.get("data", ""))
            return
        msg = u.get("message") or {}
        chat, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
        if chat is None or not text:
            return
        if chat not in self.store.data["paired"]:
            return self.unpaired(chat, text)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        handler = {"/start": self.c_help, "/help": self.c_help, "/status": self.c_status, "/projects": self.c_projects,
                   "/p": self.c_project, "/new": self.c_new, "/answer": self.c_answer, "/stop": self.c_stop,
                   "/resume": self.c_resume, "/update": self.c_update, "/unpair": self.c_unpair}.get(cmd)
        if handler:
            handler(chat, arg.strip())
        elif not text.startswith("/"):
            self.pending_goal[chat] = text
            self.send(chat, f"Начать новую цель?\n\n{text[:500]}", [[{"text": "▶️ Начать", "callback_data": "go"},
                                                                    {"text": "Отмена", "callback_data": "cancel"}]])
        else:
            self.send(chat, HELP)

    def unpaired(self, chat: int, text: str) -> None:
        if self.failed_pairs.get(chat, 0) >= 5:
            return  # stop answering a chat that keeps guessing
        cmd, _, code = text.partition(" ")
        if cmd.split("@")[0].lower() == "/pair" and code:
            if self.store.pair(chat, code):
                self.send(chat, "Чат привязан к Artel.\n\n" + HELP)
            else:
                self.failed_pairs[chat] = self.failed_pairs.get(chat, 0) + 1
                self.send(chat, "Код не подошёл или устарел. Новый код: дашборд → Подключения → Telegram, "
                                "или `artel telegram pair` в терминале.")
        else:
            self.send(chat, "Этот чат не привязан. Отправьте /pair КОД — код в дашборде (Подключения → Telegram) "
                            "или в терминале: `artel telegram pair`.")

    # --- commands -------------------------------------------------------
    def _portfolio(self) -> list[dict]:
        from .analytics import portfolio

        return portfolio(self.runner.config, live=set(self.runner.active))

    def _pick(self, chat: int, arg: str) -> Optional[str]:
        arg = arg.strip().split(" ")[0] if arg else ""
        slugs = self.listing.get(chat) or [p["slug"] for p in self._portfolio()]
        if arg.isdigit() and 1 <= int(arg) <= len(slugs):
            return slugs[int(arg) - 1]
        return next((s for s in slugs if s == arg or s.startswith(arg)), None) if arg else None

    def c_help(self, chat, arg):
        self.send(chat, HELP)

    def c_status(self, chat, arg):
        ps = self._portfolio()
        live = sum(1 for p in ps if p.get("live"))
        need = [p for p in ps if p.get("status") == "waiting_human" or p.get("attention")]
        done = sum(1 for p in ps if p.get("status") == "done")
        lines = [f"Проектов: {len(ps)} · идут сейчас: {live} · готово: {done} · ждут вас: {len(need)}"]
        from .runtimes import detect_runtime

        rt = detect_runtime(self.runner.config)
        lines.append("Исполнитель: " + ({"none": "не подключён — откройте «Подключения»"}.get(rt, rt)))
        self.send(chat, "\n".join(lines))
        if need:
            self.c_projects(chat, "need")

    def c_projects(self, chat, arg):
        ps = self._portfolio()
        if arg == "need":
            ps = [p for p in ps if p.get("status") == "waiting_human" or p.get("attention")]
        ps = ps[:15]
        self.listing[chat] = [p["slug"] for p in ps]
        if not ps:
            return self.send(chat, "Проектов пока нет. Пришлите текст цели — я начну.")
        rows = [f"{i}. {p.get('name') or p['slug']} — {STATUS_RU.get(p.get('status'), p.get('status'))}"
                f"{' ▶' if p.get('live') else ''} · {p.get('tasks_done', 0)}/{p.get('tasks_total', 0)}"
                for i, p in enumerate(ps, 1)]
        self.send(chat, "\n".join(rows) + "\n\nПодробно: /p N")

    def c_project(self, chat, arg):
        slug = self._pick(chat, arg)
        if not slug:
            return self.send(chat, "Какой проект? /projects, затем /p N")
        from .project import Project
        from .tz import fmt_ts

        pr = Project.open(slug, self.runner.config)
        m = pr.meta
        tasks = [t for t in pr.board.list() if t["kind"] != "goal"]
        lines = [f"{m.get('name')}", f"Статус: {STATUS_RU.get(m.get('status'), m.get('status'))}"
                 f"{' (идёт)' if slug in self.runner.active else ''}",
                 f"Цель: {pr.goal[:300]}"]
        if m.get("blocked_reason"):
            lines.append(f"Причина остановки: {m['blocked_reason'][:400]}")
        mark = {"done": "✅", "running": "▶", "review": "🔍", "blocked": "⛔", "ready": "•", "todo": "·"}
        for t in tasks[:20]:
            lines.append(f"{mark.get(t['status'], '·')} {t['title']}" + (" — ждёт вас" if t["needs_human"] else ""))
        cost = (m.get("usage") or {}).get("cost_usd")
        lines.append(f"Обновлён: {fmt_ts(m.get('updated_at') or time.time(), config=self.runner.config)}"
                     + (f" · ${cost:.2f}" if cost else ""))
        buttons = []
        if slug in self.runner.active:
            buttons.append({"text": "⏹ Стоп", "callback_data": f"stop:{slug}"})
        elif m.get("status") != "done":
            buttons.append({"text": "▶️ Продолжить", "callback_data": f"resume:{slug}"})
        self.send(chat, "\n".join(lines), [buttons] if buttons else None)

    def c_new(self, chat, arg):
        if not arg:
            return self.send(chat, "Напишите цель: /new сделать ...  (или просто пришлите текст)")
        self._start_goal(chat, arg)

    def _start_goal(self, chat: int, goal: str) -> None:
        from .runtimes import NO_EXECUTOR, detect_runtime

        slug = self.runner.start(goal, None)
        note = "" if detect_runtime(self.runner.config) != "none" else f"\n\n⚠️ {NO_EXECUTOR}"
        self.listing[chat] = [slug] + [s for s in self.listing.get(chat, []) if s != slug]
        self.send(chat, f"Начал: {goal[:200]}\nСообщу, когда будет результат или понадобится ваше решение. "
                        f"Ход работы: /p 1{note}")

    def c_answer(self, chat, arg):
        n, _, text = arg.partition(" ")
        slug = self._pick(chat, n)
        if not slug or not text.strip():
            return self.send(chat, "Формат: /answer N ваш ответ")
        from .manager import human_comment
        from .project import Project

        pr = Project.open(slug, self.runner.config)
        waiting = [t for t in pr.board.list() if t["needs_human"]] or \
                  [t for t in pr.board.list(status=["blocked"])]
        if not waiting:
            return self.send(chat, "Этот проект сейчас не ждёт ответа.")
        for t in waiting:
            human_comment(pr, t["id"], text.strip())
        self.runner.resume(slug, via="telegram", after_answer=True)
        self.send(chat, f"Ответ передан ({len(waiting)} задач), проект продолжает работу.")

    def c_stop(self, chat, arg):
        slug = self._pick(chat, arg)
        self.send(chat, "Остановлен." if slug and self.runner.stop(slug, via="telegram") else "Этот проект сейчас не идёт.")

    def c_resume(self, chat, arg):
        slug = self._pick(chat, arg)
        if not slug:
            return self.send(chat, "Какой проект? /projects, затем /resume N")
        self.runner.resume(slug, via="telegram")
        self.send(chat, "Продолжаю.")

    def c_update(self, chat, arg):
        st = self.runner.update_status(fetch=True)
        if not st.get("available"):
            return self.send(chat, f"Обновлений нет ({st.get('reason') or 'установлена последняя версия'}).")
        news = "\n".join(f"• {r['id']} — {r['title']}" for r in st.get("roadmap_done", [])[:8])
        self.send(chat, f"Доступно обновление: {st['behind']} изменений." + (f"\nНовое:\n{news}" if news else ""),
                  [[{"text": "⬆️ Обновить", "callback_data": "update"}]] if st.get("can_update") else None)

    def c_unpair(self, chat, arg):
        self.store.data["paired"] = [c for c in self.store.data["paired"] if c != chat]
        self.store.save()
        self.send(chat, "Чат отвязан.")

    def on_button(self, chat: int, data: str) -> None:
        if data == "go" and chat in self.pending_goal:
            self._start_goal(chat, self.pending_goal.pop(chat))
        elif data == "cancel":
            self.pending_goal.pop(chat, None)
            self.send(chat, "Хорошо, не начинаю.")
        elif data.startswith("stop:"):
            self.send(chat, "Остановлен." if self.runner.stop(data[5:], via="telegram") else "Уже не идёт.")
        elif data.startswith("resume:"):
            self.runner.resume(data[7:], via="telegram")
            self.send(chat, "Продолжаю.")
        elif data == "update":
            out = self.runner.update_apply()
            self.send(chat, "Обновлено, Artel перезапустится, когда проекты закончатся." if out.get("ok")
                      else f"Обновление не установлено: {out.get('error', '')[:500]}")

    # --- notifications --------------------------------------------------
    def notify_once(self) -> int:
        paired = list(self.store.data["paired"])
        if not paired:
            return 0
        sent = 0
        seen = self.store.data["notified"]
        for p in self._portfolio():
            st, slug = p.get("status"), p["slug"]
            if seen.get(slug) == st:
                continue
            first_sight = slug not in seen
            seen[slug] = st
            if first_sight and st == "done" and p.get("created_at", 0) < self.started_at:
                continue  # do not announce old finished projects when the bot starts
            if st in NOTIFY:
                reason = ""
                if st in ("waiting_human", "error"):
                    from .project import Project

                    meta = Project.open(slug, self.runner.config).meta
                    reason = meta.get("blocked_reason") or ""
                text = f"{NOTIFY[st]}: {p.get('name') or slug}" + (f"\n{reason[:400]}" if reason else "")
                for chat in paired:
                    self.listing[chat] = [slug] + [s for s in self.listing.get(chat, []) if s != slug]
                    self.send(chat, text + ("\nОтветить: /answer 1 текст" if st == "waiting_human" else "\nПодробно: /p 1"))
                sent += 1
        upd = getattr(self.runner.updater, "_last", None) or {}
        if upd.get("available") and seen.get("_update") != upd.get("remote"):
            seen["_update"] = upd.get("remote")
            for chat in paired:
                self.send(chat, f"⬆️ Доступно обновление Artel: {upd.get('behind')} изменений. /update")
            sent += 1
        self.store.save()
        return sent

    def _notify_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.notify_once()
            except Exception:  # noqa: BLE001
                pass
            self.stop_event.wait(15)


def status(config: dict) -> dict:
    st = Store()
    pair = st.data.get("pair") or {}
    return {"token_set": bool(token()), "token_env": TOKEN_ENV, "paired": len(st.data["paired"]),
            "code": pair.get("code") if pair and time.time() < pair.get("expires", 0) else None}
