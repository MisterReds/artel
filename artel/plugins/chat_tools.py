"""Tools of the concierge chat (artel/chat.py): read-only views of the portfolio and the terminal chat_reply.

Every view returns project data wrapped as untrusted text. Actions are not tools: the concierge lists them in
chat_reply and Artel runs the ones on its short whitelist.
"""

from __future__ import annotations

from ..kernel import ToolSpec
from ..tooling import ToolContext, ToolError

PLUGIN = {"name": "artel.plugins.chat_tools", "version": "1", "provides": ["tool:chat_reply"]}
ROLES = ("concierge",)


def _wrap(source: str, data) -> str:
    from ..chat import _safe_json

    return f'<untrusted source="{source}">{_safe_json(data)}</untrusted>'


def _runner(ctx: ToolContext):
    return ctx.state["chat_runner"]


def _view(fn):
    def tool(ctx: ToolContext, **kw) -> str:
        try:
            return _wrap(fn.__name__, fn(_runner(ctx), **kw))
        except (KeyError, FileNotFoundError) as exc:
            raise ToolError(f"not found: {exc}") from exc
    return tool


def chat_reply(ctx: ToolContext, reply: str, actions: list = None, refs: list = None) -> str:
    if not str(reply).strip():
        raise ToolError("reply must not be empty")
    ctx.terminal = ("chat_reply", {"reply": str(reply), "actions": actions or [], "refs": refs or []})
    return "chat_reply accepted"


def setup(kernel) -> None:
    from .. import chat

    S = {"type": "object"}
    slug = {"slug": {"type": "string"}}

    def reg(name, desc, props, required, fn):
        kernel.register_tool(ToolSpec(name, desc, {**S, "properties": props, "required": required}, _view(fn),
                                      roles=ROLES))

    reg("portfolio", "All projects, compact: status, stage, done/total, what waits for the owner, spend.", {}, [],
        lambda r: chat.portfolio_summary(r))
    reg("project_brief", "One project: summary, its tasks (id, status, last comment) and the result's state.",
        slug, ["slug"], lambda r, slug: chat.project_brief(r, slug))
    reg("task_lookup", "One task: body, result, last comments.", {**slug, "task_id": {"type": "string"}},
        ["slug", "task_id"], lambda r, slug, task_id: chat.task_lookup(r, slug, task_id))
    reg("result_status", "Whether the project's result is runnable and running, its address.", slug, ["slug"],
        lambda r, slug: chat.result_status(r, slug))
    reg("spend", "Spend per project and the provider limits in force.", {}, [], lambda r: chat.spend_summary(r))
    action = {"type": "object", "properties": {
        "type": {"type": "string", "enum": sorted(chat.ACTIONS)}, "project": {"type": "string"},
        "task_id": {"type": "string"}, "text": {"type": "string"}, "goal": {"type": "string"},
        "why": {"type": "string"}}, "required": ["type"]}
    ref = {"type": "object", "properties": {"type": {"type": "string", "enum": ["project", "task", "file"]},
                                            "slug": {"type": "string"}, "id": {"type": "string"},
                                            "label": {"type": "string"}}, "required": ["type", "slug"]}
    kernel.register_tool(ToolSpec(
        "chat_reply", "Deliver the answer to the owner (Russian), the actions to run (only if the owner asked) and "
        "the projects/tasks you mention.",
        {**S, "properties": {"reply": {"type": "string"}, "actions": {"type": "array", "items": action},
                             "refs": {"type": "array", "items": ref}}, "required": ["reply"]},
        chat_reply, roles=ROLES, terminal=True))
