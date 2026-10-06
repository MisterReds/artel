"""Agent runtimes as plugins. Add one by registering ``runtime:<name>`` (same contract as ``run_loop``)."""

from __future__ import annotations

PLUGIN = {"name": "artel.plugins.runtimes", "version": "1", "provides": ["service:runtime:native",
                                                                       "service:runtime:claude_code",
                                                                       "service:runtime:codex",
                                                                       "service:runtime:acp"]}


def _native(kernel, project, **kw):
    from ..agent import run_loop

    return run_loop(kernel, project, **kw)


def _claude_code(kernel, project, **kw):
    from ..runtimes.claude_code import run_claude_code

    return run_claude_code(kernel, project, **kw)


def _codex(kernel, project, **kw):
    from ..runtimes.codex import run_codex

    return run_codex(kernel, project, **kw)


def _acp(kernel, project, **kw):
    from ..runtimes.acp import run_acp

    return run_acp(kernel, project, **kw)


def setup(kernel) -> None:
    kernel.register_runtime("native", _native)
    kernel.register_runtime("claude_code", _claude_code)
    kernel.register_runtime("codex", _codex)
    kernel.register_runtime("acp", _acp)
