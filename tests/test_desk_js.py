"""Desktop notifications: the pure parts of artel/web/static/desk.js run under node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "artel/web/static/desk.js"
node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

PORTFOLIO = [
    {"slug": "cafe", "name": "Сайт кофейни", "status": "running",
     "attention": [{"task_id": "t3", "reason": "нужен человек", "question": "Какой домен использовать?"},
                   {"task_id": "t4", "reason": "заблокирована", "question": ""}]},
    {"slug": "bot", "name": "Бот записи", "status": "done", "stage": "done", "attention": []},
]


def js(expr: str, **data):
    src = ("global.window={};require(%s);const D=window.ArtelDesk;const P=%s;console.log(JSON.stringify(%s))"
           % (json.dumps(str(JS)), json.dumps(data.get("portfolio", PORTFOLIO)), expr))
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@node
def test_new_question_and_done_project_become_notifications_with_the_agreed_text():
    items = js("D.fresh(P, [])")
    ask = next(i for i in items if i["kind"] == "ask")
    assert ask["title"] == "Artel: нужен ваш ответ"
    assert ask["body"] == "Сайт кофейни: Какой домен использовать?"
    assert (ask["slug"], ask["task_id"]) == ("cafe", "t3")
    done = next(i for i in items if i["kind"] == "done")
    assert done["title"] == "Artel: готово" and done["body"] == "Бот записи" and done["slug"] == "bot"
    assert len(items) == 2  # a blocked task nobody must answer is not a question to the owner


@node
def test_items_already_shown_are_not_repeated():
    ids = [i["id"] for i in js("D.fresh(P, [])")]
    assert js("D.fresh(P, %s)" % json.dumps(ids)) == []
    assert [i["kind"] for i in js("D.fresh(P, %s)" % json.dumps(ids[:1]))] == [js("D.fresh(P, [])")[1]["kind"]]


@node
def test_body_is_at_most_100_characters():
    long = [{"slug": "p", "name": "Длинный проект", "status": "running",
             "attention": [{"task_id": "t1", "reason": "нужен человек", "question": "в" * 300}]}]
    body = js("D.fresh(P, [])[0].body", portfolio=long)
    assert len(body) <= 100 and body.startswith("Длинный проект: в") and body.endswith("…")


@node
def test_remembered_ids_are_capped_keeping_the_newest():
    out = js("D.remember(%s, ['x','y'], 3)" % json.dumps(["a", "b", "c"]))
    assert out == ["c", "x", "y"]
    assert js("D.remember(['a'], ['a'], 5)") == ["a"]


@node
def test_badge_counts_questions_to_the_owner():
    assert js("D.badgeCount(P)") == 2
    assert js("D.badgeCount([])") == 0


@node
def test_a_question_asked_again_notifies_again():
    def ask(asked, question="Какой домен?"):
        return [{"slug": "cafe", "name": "Кафе", "status": "running",
                 "attention": [{"task_id": "t3", "reason": "нужен человек", "question": question, "asked": asked}]}]

    first = js("D.fresh(P, [])", portfolio=ask(4))
    assert len(first) == 1
    shown = json.dumps([i["id"] for i in first])
    assert js("D.fresh(P, %s)" % shown, portfolio=ask(4)) == []  # same ask: quiet
    again = js("D.fresh(P, %s)" % shown, portfolio=ask(9))  # same text, new ask: notifies
    assert len(again) == 1 and again[0]["id"] != first[0]["id"]
    # without an ask id the question text tells asks apart
    assert js("D.fresh(P, %s)" % shown, portfolio=ask(None, "Другой вопрос?")) != []


@node
def test_shown_ids_are_read_from_storage_each_time():
    stored = {"v": json.dumps(["a"])}
    src = ("global.window={};require(%s);const D=window.ArtelDesk;"
           "const st={v:%s};const get=()=>st.v;const r=[];"
           "r.push(D.loadShown(get()));st.v=JSON.stringify(['a','b']);r.push(D.loadShown(get()));"
           "st.v='not json';r.push(D.loadShown(get()));st.v='{\"x\":1}';r.push(D.loadShown(get()));r.push(D.loadShown(null));"
           "console.log(JSON.stringify(r))" % (json.dumps(str(JS)), json.dumps(stored["v"])))
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [["a"], ["a", "b"], [], [], []]
