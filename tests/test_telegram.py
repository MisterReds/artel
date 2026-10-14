import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from artel.project import DEFAULT_CONFIG, deep_merge


class FakeTelegram:
    """Bot API stand-in: queued updates for getUpdates, every sendMessage recorded."""

    def __init__(self):
        self.updates, self.sent, self.uid = [], [], 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                if method == "getUpdates":
                    res = [u for u in fake.updates if u["update_id"] >= body.get("offset", 0)]
                elif method == "sendMessage":
                    fake.sent.append(body)
                    res = {"message_id": len(fake.sent)}
                else:
                    res = True
                data = json.dumps({"ok": True, "result": res}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def msg(self, chat, text):
        self.uid += 1
        self.updates.append({"update_id": self.uid, "message": {"chat": {"id": chat}, "text": text}})

    def press(self, chat, data):
        self.uid += 1
        self.updates.append({"update_id": self.uid, "callback_query": {"id": "q", "data": data,
                                                                       "message": {"chat": {"id": chat}}}})

    def texts(self, chat):
        return [m["text"] for m in self.sent if m["chat_id"] == chat]


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARTEL_TELEGRAM_TOKEN", "123:test")
    from artel.telegram import TelegramBot
    from artel.web.server import Runner

    tg = FakeTelegram()
    runner = Runner(deep_merge(DEFAULT_CONFIG, {"provider": "scripted", "backoff_base_s": 0.05}))
    b = TelegramBot(runner, api_base=tg.url)
    return b, tg, runner


def wait_for(cond, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_nothing_is_shown_to_an_unpaired_chat_and_pairing_is_one_time(bot):
    b, tg, runner = bot
    code = b.store.new_code()
    tg.msg(1, "/status")
    tg.msg(1, "/pair 000000" if code != "000000" else "/pair 111111")
    b.poll_once(timeout=0)
    assert all("Проектов" not in t for t in tg.texts(1)) and "не привязан" in tg.texts(1)[0]
    assert "не подошёл" in tg.texts(1)[1]
    tg.msg(1, f"/pair {code}")
    tg.msg(2, f"/pair {code}")  # the same code cannot pair a second chat
    b.poll_once(timeout=0)
    assert "привязан к Artel" in tg.texts(1)[-1] and "не подошёл" in tg.texts(2)[-1]
    assert b.store.data["paired"] == [1]


def test_goal_from_the_phone_runs_and_notifies(bot):
    b, tg, runner = bot
    b.store.data["paired"] = [7]
    tg.msg(7, "Напиши отчёт о продажах")
    b.poll_once(timeout=0)
    assert "Начать новую цель?" in tg.texts(7)[-1]
    tg.press(7, "go")
    b.poll_once(timeout=0)
    assert "Начал" in tg.texts(7)[-1]
    slug = b.listing[7][0]
    assert wait_for(lambda: slug not in runner.active)
    b.notify_once()
    assert any("Цель достигнута" in t for t in tg.texts(7))
    tg.msg(7, "/projects")
    tg.msg(7, "/p 1")
    b.poll_once(timeout=0)
    assert "готово" in tg.texts(7)[-2] and "Статус: готово" in tg.texts(7)[-1]


def test_waiting_project_is_announced_and_answered_from_the_phone(bot, monkeypatch):
    import artel.runtimes as rt

    b, tg, runner = bot
    b.store.data["paired"] = [9]
    monkeypatch.setattr(rt, "detect_runtime", lambda config, ttl=120.0: "none")
    runner.config["provider"] = "anthropic"
    slug = runner.start("blocked goal", None)
    assert wait_for(lambda: slug not in runner.active)
    b.notify_once()
    assert any("Нужен человек" in t and "Подключения" in t for t in tg.texts(9))
    b.notify_once()  # no duplicate notification
    assert sum("Нужен человек" in t for t in tg.texts(9)) == 1
