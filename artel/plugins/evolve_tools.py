"""Terminal tools for the evolution roles."""

from __future__ import annotations

from ..kernel import ToolSpec
from ..tooling import ToolContext, ToolError

HYP = {"type": "object", "properties": {
    "title": {"type": "string"}, "rationale": {"type": "string", "description": "the evidence and the weakness"},
    "plan": {"type": "string", "description": "concrete code changes and tests"},
    "expected": {"type": "string", "description": "which metric moves and why"},
    "files": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "rationale", "plan", "expected"]}
FEAT = {"type": "object", "properties": {
    "name": {"type": "string"}, "description": {"type": "string"},
    "source_files": {"type": "array", "items": {"type": "string"}}, "value": {"type": "string"},
    "fit": {"type": "number"}, "target": {"type": "string", "description": "Artel module to change"},
    "port_plan": {"type": "string"}, "expected": {"type": "string"}},
    "required": ["name", "description", "value", "fit", "port_plan"]}


def propose_hypotheses(ctx: ToolContext, hypotheses: list) -> str:
    if not hypotheses:
        raise ToolError("propose at least one hypothesis")
    for h in hypotheses:
        missing = [k for k in HYP["required"] if not h.get(k)]
        if missing:
            raise ToolError(f"hypothesis {h.get('title', '?')!r} is missing {missing}")
    ctx.terminal = ("propose_hypotheses", {"hypotheses": hypotheses})
    return "accepted"


def absorb_report(ctx: ToolContext, summary: str, features: list) -> str:
    for f in features:
        missing = [k for k in FEAT["required"] if f.get(k) in (None, "")]
        if missing:
            raise ToolError(f"feature {f.get('name', '?')!r} is missing {missing}")
    ctx.terminal = ("absorb_report", {"summary": summary, "features": features})
    return "accepted"


def setup(kernel) -> None:
    S = {"type": "object", "additionalProperties": False}
    kernel.register_tool(ToolSpec(
        "propose_hypotheses", "Submit improvement hypotheses for Artel, each tied to diagnosed evidence.",
        {**S, "properties": {"hypotheses": {"type": "array", "items": HYP}}, "required": ["hypotheses"]},
        propose_hypotheses, roles=("evolver",), terminal=True))
    kernel.register_tool(ToolSpec(
        "absorb_report", "Submit the analysis of the source harness: features worth porting into Artel.",
        {**S, "properties": {"summary": {"type": "string"}, "features": {"type": "array", "items": FEAT}},
         "required": ["summary", "features"]}, absorb_report, roles=("absorber",), terminal=True))
