"""Agent runtimes. ``native`` is Artel's own loop over the Messages API; ``claude_code``
drives the Claude Code CLI headless and exposes Artel's tools to it over MCP."""

from __future__ import annotations


def runtime_for(config: dict, role: str) -> str:
    return (config.get("runtime_by_role") or {}).get(role) or config.get("runtime", "native")


def run_agent(kernel, project, *, role: str, **kw):
    """Same contract as :func:`artel.agent.run_loop`, dispatched by the configured runtime."""
    if runtime_for(kernel.config, role) == "claude_code":
        from .claude_code import run_claude_code

        return run_claude_code(kernel, project, role=role, **kw)
    from ..agent import run_loop

    return run_loop(kernel, project, role=role, **kw)
