"""User-mode wording: system strings in Russian, the «Что происходит» sentences, the block question."""
from artel.humanize import block_question, happenings, localize_stats, localize_view, role_ru, ru


def test_ru_translates_known_verdicts_and_keeps_unknown_text():
    assert ru("Every requirement of the goal is met.") == "Все требования цели выполнены."
    assert ru("ACCEPTED: All criteria verified against the files.").startswith("Принято ревьюером: ")
    assert ru("reviewer ACCEPTED: ok") == "ревьюер принял: ok"
    assert ru("auditor GOAL MET") == "аудитор: цель достигнута"
    assert ru("Integrate and verify") == "Сборка и проверка"
    assert ru("Своё русское название") == "Своё русское название"
    assert ru("some unknown english") == "some unknown english"
    assert ru(None) is None


def test_role_names_and_at_sign():
    assert [role_ru(r) for r in ("researcher", "engineer", "reviewer", "auditor", "Planner")] == \
        ["исследователь", "инженер", "ревьюер", "аудитор", "планировщик"]
    assert role_ru("кондитер") == "кондитер"


def test_block_question_takes_the_reason_not_the_evidence():
    comments = [{"author": "Ada", "body": "BLOCKED: Need the bank login\nEvidence:\nls: nothing"},
                {"author": "manager", "body": "note"}]
    assert block_question(comments) == "Need the bank login"
    assert block_question([{"author": "manager", "body": "Нужна настройка, повтор не поможет: CLI не найден"}]) \
        == "Нужна настройка, повтор не поможет: CLI не найден"
    assert block_question([]) == ""
    # a human answer ends the question
    assert block_question([{"author": "Ada", "body": "BLOCKED: x"}, {"author": "human", "body": "ok"}]) == ""


EV = [
    {"seq": 1, "ts": 100, "kind": "board/task_created", "task_id": "t1", "title": "Research"},
    {"seq": 2, "ts": 101, "kind": "board/task_moved", "task_id": "t1", "frm": "ready", "to": "running", "actor": "Ada"},
    {"seq": 3, "ts": 102, "kind": "tool/call", "tool": "bash", "input": {}},
    {"seq": 4, "ts": 103, "kind": "board/task_moved", "task_id": "t1", "frm": "running", "to": "review", "actor": "Ada"},
    {"seq": 5, "ts": 104, "kind": "review/verdict", "task_id": "t1", "passed": True, "feedback": "ok"},
    {"seq": 6, "ts": 105, "kind": "board/task_moved", "task_id": "t2", "frm": "running", "to": "blocked", "actor": "Linus"},
    {"seq": 7, "ts": 106, "kind": "review/verdict", "task_id": "t1", "passed": False, "feedback": "no"},
    {"seq": 8, "ts": 107, "kind": "audit/verdict", "complete": True, "summary": "Every requirement of the goal is met."},
]
TASKS = {"t1": {"title": "Сверить выписки", "needs_human": 0, "assignee": "Ada"},
         "t2": {"title": "Войти в банк", "needs_human": 1, "assignee": "Linus"}}


def test_happenings_are_human_sentences_without_raw_events():
    out = happenings(EV, TASKS)
    texts = [h["text"] for h in out]
    assert "Ada приступает к «Сверить выписки»" in texts
    assert "«Сверить выписки» отправлена на проверку · Ada" in texts
    assert "Ревьюер принял «Сверить выписки»" in texts
    assert "Ревьюер вернул «Сверить выписки» на доработку" in texts
    assert "Нужен ваш ответ по задаче «Войти в банк»" in texts
    assert texts[-1] == "Аудитор: цель достигнута"
    assert not any("tool" in t or "bash" in t for t in texts)
    assert [h["ts"] for h in out] == sorted(h["ts"] for h in out)
    blocked = [h for h in out if h["text"].startswith("Нужен ваш ответ")][0]
    assert blocked["tone"] == "bad" and blocked["task_id"] == "t2"


def test_happenings_limit_keeps_the_newest():
    out = happenings(EV, TASKS, limit=2)
    assert [h["ts"] for h in out] == [106, 107]


def test_localize_view_translates_roles_titles_and_verdicts_but_not_human_text():
    view = {"workers": [{"name": "Ada", "role": "researcher"}],
            "board": {"tasks": [{"title": "Research and outline", "result": "Every requirement of the goal is met.",
                                 "comments": [{"author": "reviewer", "body": "ACCEPTED: fine"},
                                              {"author": "human", "body": "ACCEPTED: my own words"}]}]},
            "stats": {"attention": [{"reason": "нужен человек", "question": "Need access"}],
                      "last_event": {"text": "reviewer ACCEPTED"}, "workers": [{"role": "engineer"}]}}
    localize_view(view)
    assert view["workers"][0]["role"] == "исследователь"
    t = view["board"]["tasks"][0]
    assert t["title"] == "Исследование и план" and t["result"] == "Все требования цели выполнены."
    assert t["comments"][0]["body"] == "Принято ревьюером: fine" and t["comments"][0]["author"] == "ревьюер"
    assert t["comments"][1]["body"] == "ACCEPTED: my own words" and t["comments"][1]["author"] == "human"
    assert view["stats"]["last_event"]["text"] == "ревьюер принял"
    assert view["stats"]["workers"][0]["role"] == "инженер"


def test_localize_stats_for_portfolio_cards():
    s = {"workers": [{"role": "auditor"}], "attention": [{"question": "Every requirement of the goal is met."}],
         "last_event": None}
    localize_stats(s)
    assert s["workers"][0]["role"] == "аудитор" and s["attention"][0]["question"].startswith("Все требования")
