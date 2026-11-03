from artel.prompts import PLANNER


def test_planner_asks_for_task_titles_in_the_language_of_the_goal():
    assert "titles" in PLANNER and "language of the goal" in PLANNER.split("How to plan:")[1].split("Call submit_plan")[0]
