"""Agent runtimes. ``native`` is Artel's own loop over the Messages API; ``claude_code``
drives the Claude Code CLI headless and exposes Artel's tools to it over MCP."""

from __future__ import annotations


def runtime_for(config: dict, role: str) -> str:
    return (config.get("runtime_by_role") or {}).get(role) or config.get("runtime", "native")


def run_agent(kernel, project, *, role: str, **kw):
    """Same contract as :func:`artel.agent.run_loop`, dispatched to the runtime plugin configured for the role."""
    name = runtime_for(kernel.config, role)
    fn = kernel.get(f"runtime:{name}")
    if fn is None:
        raise RuntimeError(f"no runtime plugin registered as {name!r}; loaded: "
                           f"{sorted(k[8:] for k in kernel.services if k.startswith('runtime:'))}")
    return fn(kernel, project, role=role, **kw)
