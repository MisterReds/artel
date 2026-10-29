"""``tool_install``: an agent asks Artel for a missing program instead of asking the owner (artel/provision.py)."""

from __future__ import annotations

from .. import provision
from ..kernel import ToolSpec
from ..tooling import ToolContext, ToolError

PLUGIN = {"name": "artel.plugins.provision_tools", "version": "1", "provides": ["tool:tool_install"]}
ROLES = ("worker", "tester", "manager")


def tool_install(ctx: ToolContext, name: str, reason: str) -> str:
    name, reason = str(name or "").strip(), str(reason or "").strip()
    if not provision.valid_name(name):
        raise ToolError(f"invalid tool name {name!r}: use the program's name, lower case (rg, jq, pandoc)")
    if not reason:
        raise ToolError("say why the tool is needed: it is written to the decision log")
    cfg = ctx.kernel.config
    if not (cfg.get("provision") or {}).get("auto", True):
        return f"not installed: automatic installs are disabled (provision.auto: false); {name} was not installed"
    task_id = (ctx.task or {}).get("id")
    path = provision.ensure(name, why=reason, cfg=cfg,
                            log=lambda kind, **p: ctx.project.log.append(kind, actor=ctx.actor, task_id=task_id, **p))
    if path:
        return path
    return f"not installed: {name}: {provision.last_reason(name) or 'no install route'}"


def setup(kernel):
    kernel.register_tool(ToolSpec(
        "tool_install", "Make a missing program available (ripgrep `rg`, `jq`, `gh`, `uv`, pandoc ...). Artel installs it "
        "itself: a checksum-verified release binary, else conda-forge, else a system package. Returns the program's path, "
        "or why it could not be installed; then use the path or run the program by name. `reason` is logged.",
        {"type": "object", "additionalProperties": False,
         "properties": {"name": {"type": "string"}, "reason": {"type": "string"}}, "required": ["name", "reason"]},
        tool_install, roles=ROLES))
