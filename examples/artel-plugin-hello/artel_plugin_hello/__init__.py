"""Hello plugin: adds a line to every agent's system prompt and logs each accepted task."""

PLUGIN = {
    "name": "hello",
    "version": "0.1.0",
    "provides": ["hook:before_agent_start", "hook:task_done"],
    "requires": [],
    "permissions": ["system_prompt", "events"],
    "description": "Adds a line to every agent's system prompt and logs task_done",
}

DEFAULT_LINE = "Hello from artel-plugin-hello: keep answers short."


def setup(kernel):
    line = (kernel.config.get("hello") or {}).get("line", DEFAULT_LINE)

    def before_agent_start(*, agent, project, role, purpose, **_):
        agent["system"] = f"{agent['system']}\n\n{line}"
        project.log.append("hello/before_agent_start", role=role, purpose=purpose)

    def task_done(*, project, task, **_):
        project.log.append("hello/task_done", task_id=task["id"], title=task["title"])

    kernel.on("before_agent_start", before_agent_start)
    kernel.on("task_done", task_done)
    # setup may return a teardown: it runs on disable, remove, reload and session shutdown
    return lambda: None
