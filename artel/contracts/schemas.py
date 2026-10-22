"""The schemas of the target architecture as frozen dataclasses with validating from_dict and to_dict."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Optional

from . import _base as b
from .core import Usage

AUTONOMY = ("suggest", "act_and_show", "ask_at_forks", "full")
DECISIONS = ("plan", "escalation", "acceptance", "rewrite")
RESULT_STATUS = ("done", "blocked", "failed")
ESCALATION_KINDS = ("critic_failed", "conflict", "architecture", "risk", "rewrite", "irreversible", "budget",
                    "uncertainty")
ESCALATION_TO = ("manager", "owner")
DECISION_STATUS = ("planned", "running", "done", "failed", "rolled_back", "rewritten")
REASONING = ("low", "medium", "medium-high", "high")
INDEPENDENT_OF = ("executor",)
REWRITE_TRIGGERS = ("wrong_architecture", "patches", "steward", "before_big_block", "would_design_differently")
KEEPS = ("intent", "contracts", "behaviour_tests", "journal", "lessons")
PARADIGMS = ("function", "class", "schema", "fsm", "adapter")
CRITICS = ("independent",)
MAX_FACTS = 7


class _Plain:
    """to_dict shared by all schemas; Python field `in_` is serialised as `in`."""

    def to_dict(self) -> dict:
        return {("in" if f.name == "in_" else f.name): b.to_plain(getattr(self, f.name)) for f in fields(self)}


def _head(d: Any, path: str, cls):
    path = b.check_path(path)
    d = b.obj(d, path)
    b.reject_unknown(d, cls, path)
    return d, path


@dataclass(frozen=True)
class IntentBudget(_Plain):
    usd: float
    expensive_tokens: int
    rewrite_usd: float
    amplify_usd: float

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.number(d, "usd", p), b.integer(d, "expensive_tokens", p), b.number(d, "rewrite_usd", p),
                   b.number(d, "amplify_usd", p))


@dataclass(frozen=True)
class Intent(_Plain):
    id: str
    version: int
    idea: str
    intent: str
    goal_behind_goal: str
    success_criteria: tuple
    constraints: tuple
    out_of_scope: tuple
    assumptions: tuple
    open_questions: tuple
    risks: tuple
    autonomy: str
    budget: IntentBudget
    confirmed_by: Optional[str]

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "id", p, nonempty=True), b.integer(d, "version", p, 1), b.string(d, "idea", p),
                   b.string(d, "intent", p), b.string(d, "goal_behind_goal", p),
                   b.strings(d, "success_criteria", p), b.strings(d, "constraints", p),
                   b.strings(d, "out_of_scope", p), b.strings(d, "assumptions", p),
                   b.strings(d, "open_questions", p), b.strings(d, "risks", p),
                   b.enum(d, "autonomy", AUTONOMY, p), b.nested(d, "budget", IntentBudget, p),
                   b.string(d, "confirmed_by", p, nullable=True))


@dataclass(frozen=True)
class ManagerBudget(_Plain):
    expensive_left: int
    usd_left: float

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.integer(d, "expensive_left", p), b.number(d, "usd_left", p))


@dataclass(frozen=True)
class ManagerInput(_Plain):
    intent_ref: str
    decision_needed: str
    goal: str
    constraints: tuple
    success_criteria: tuple
    facts: tuple
    refs: tuple
    open_questions: tuple
    budget: ManagerBudget

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "intent_ref", p), b.enum(d, "decision_needed", DECISIONS, p),
                   b.string(d, "goal", p), b.strings(d, "constraints", p), b.strings(d, "success_criteria", p),
                   b.strings(d, "facts", p, maximum=MAX_FACTS), b.strings(d, "refs", p),
                   b.strings(d, "open_questions", p), b.nested(d, "budget", ManagerBudget, p))


@dataclass(frozen=True)
class TaskContract(_Plain):
    id: str
    intent_ref: str
    title: str
    input: str
    refs: tuple
    criteria: tuple
    checks: tuple
    output_schema: str
    max_tokens: int
    capability: str
    reversible: bool
    depends_on: tuple

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "id", p, nonempty=True), b.string(d, "intent_ref", p), b.string(d, "title", p),
                   b.string(d, "input", p), b.strings(d, "refs", p), b.strings(d, "criteria", p),
                   b.strings(d, "checks", p), b.string(d, "output_schema", p), b.integer(d, "max_tokens", p),
                   b.string(d, "capability", p, nonempty=True), b.boolean(d, "reversible", p),
                   b.strings(d, "depends_on", p))


@dataclass(frozen=True)
class RangeResearchTask(_Plain):
    discipline: str
    question: str
    method: str
    raw_inputs: tuple
    output_schema: str
    critic: str
    max_tokens: int

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "discipline", p), b.string(d, "question", p), b.string(d, "method", p),
                   b.strings(d, "raw_inputs", p), b.string(d, "output_schema", p),
                   b.enum(d, "critic", CRITICS, p), b.integer(d, "max_tokens", p))


@dataclass(frozen=True)
class Altitude(_Plain):
    goal_behind: str
    done_means: str

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "goal_behind", p), b.string(d, "done_means", p))


@dataclass(frozen=True)
class Depth(_Plain):
    raw_sources: tuple
    methods: tuple
    tools: tuple
    independent_evidence: tuple

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.strings(d, "raw_sources", p), b.strings(d, "methods", p), b.strings(d, "tools", p),
                   b.strings(d, "independent_evidence", p))


@dataclass(frozen=True)
class RangeEntry(_Plain):
    discipline: str
    research_task: RangeResearchTask

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "discipline", p), b.nested(d, "research_task", RangeResearchTask, p))


@dataclass(frozen=True)
class AmplifiedTaskContract(_Plain):
    base: TaskContract
    altitude: Altitude
    depth: Depth
    range: tuple
    what_changed: tuple
    why: str
    cost_usd: float

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.nested(d, "base", TaskContract, p), b.nested(d, "altitude", Altitude, p),
                   b.nested(d, "depth", Depth, p), b.nested_list(d, "range", RangeEntry, p),
                   b.strings(d, "what_changed", p), b.string(d, "why", p), b.number(d, "cost_usd", p))


@dataclass(frozen=True)
class ArtifactRef(_Plain):
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "path", p, nonempty=True), b.string(d, "sha256", p))


@dataclass(frozen=True)
class ExecutorResult(_Plain):
    task: str
    status: str
    summary: str
    evidence: tuple
    artifacts: tuple
    confidence: float
    blocked_on: Optional[str]
    usage: Usage

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        status = b.enum(d, "status", RESULT_STATUS, p)
        blocked_on = b.string(d, "blocked_on", p, nullable=True)
        if status == "blocked" and not blocked_on:
            raise b.ContractError(f"{p}blocked_on", "must be a non-empty string when status is 'blocked'")
        if status != "blocked" and blocked_on is not None:
            raise b.ContractError(f"{p}blocked_on", "must be null unless status is 'blocked'")
        return cls(b.string(d, "task", p, nonempty=True), status,
                   b.string(d, "summary", p), b.strings(d, "evidence", p),
                   b.nested_list(d, "artifacts", ArtifactRef, p), b.number(d, "confidence", p, 0.0, 1.0),
                   blocked_on, b.nested(d, "usage", Usage, p))


@dataclass(frozen=True)
class EscalationRequest(_Plain):
    task: str
    kind: str
    to: str
    brief: ManagerInput
    options: tuple
    recommendation: str

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "task", p), b.enum(d, "kind", ESCALATION_KINDS, p), b.enum(d, "to", ESCALATION_TO, p),
                   b.nested(d, "brief", ManagerInput, p), b.strings(d, "options", p),
                   b.string(d, "recommendation", p))


@dataclass(frozen=True)
class Tokens(_Plain):
    in_: int
    out: int

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.integer(d, "in", p), b.integer(d, "out", p))


@dataclass(frozen=True)
class DecisionLogEntry(_Plain):
    seq: int
    ts: str
    actor: str
    capability: str
    provider: str
    model: str
    decision: str
    why: str
    alternatives: tuple
    next: str
    inputs: tuple
    tokens: Tokens
    cost_usd: float
    latency_ms: int
    reversible: bool
    status: str
    intent_ref: str

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.integer(d, "seq", p), b.string(d, "ts", p), b.string(d, "actor", p),
                   b.string(d, "capability", p), b.string(d, "provider", p), b.string(d, "model", p),
                   b.string(d, "decision", p), b.string(d, "why", p), b.strings(d, "alternatives", p),
                   b.string(d, "next", p), b.strings(d, "inputs", p), b.nested(d, "tokens", Tokens, p),
                   b.number(d, "cost_usd", p), b.integer(d, "latency_ms", p), b.boolean(d, "reversible", p),
                   b.enum(d, "status", DECISION_STATUS, p), b.string(d, "intent_ref", p))


@dataclass(frozen=True)
class CapabilityProfile(_Plain):
    id: str
    reasoning: str
    max_cost_per_mtok_usd: float
    max_latency_s: float
    min_context_tokens: int
    needs: tuple
    prefers: tuple
    fallback: tuple
    independent_of: Optional[str]

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "id", p, nonempty=True), b.enum(d, "reasoning", REASONING, p),
                   b.number(d, "max_cost_per_mtok_usd", p), b.number(d, "max_latency_s", p),
                   b.integer(d, "min_context_tokens", p), b.strings(d, "needs", p), b.strings(d, "prefers", p),
                   b.strings(d, "fallback", p), b.enum(d, "independent_of", INDEPENDENT_OF, p, nullable=True))


@dataclass(frozen=True)
class RewriteModule(_Plain):
    name: str
    paradigm: str
    contract: str
    behaviour_tests: tuple

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.string(d, "name", p, nonempty=True), b.enum(d, "paradigm", PARADIGMS, p),
                   b.string(d, "contract", p), b.strings(d, "behaviour_tests", p))


@dataclass(frozen=True)
class RewriteBudget(_Plain):
    usd: float
    expensive_tokens: int

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.number(d, "usd", p), b.integer(d, "expensive_tokens", p))


@dataclass(frozen=True)
class RewriteProposal(_Plain):
    id: str
    trigger: str
    what_is_wrong: tuple
    why_patching_is_worse: tuple
    target: str
    keeps: tuple
    modules: tuple
    parallel_groups: tuple
    budget: RewriteBudget
    risks: tuple
    rollback: str

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        keeps = b.strings(d, "keeps", p)
        for i, k in enumerate(keeps):
            if k not in KEEPS:
                raise b.ContractError(f"{p}keeps[{i}]", f"must be one of {sorted(KEEPS)}, got {k!r}")
        parsed = b.string_groups(d, "parallel_groups", p)
        return cls(b.string(d, "id", p, nonempty=True), b.enum(d, "trigger", REWRITE_TRIGGERS, p),
                   b.strings(d, "what_is_wrong", p), b.strings(d, "why_patching_is_worse", p),
                   b.string(d, "target", p), keeps, b.nested_list(d, "modules", RewriteModule, p), parsed,
                   b.nested(d, "budget", RewriteBudget, p), b.strings(d, "risks", p), b.string(d, "rollback", p))


@dataclass(frozen=True)
class RewriteTaskContract(_Plain):
    base: TaskContract
    module: str
    paradigm: str
    public_interface: str
    behaviour_tests: tuple
    known_problems: tuple
    must_not_read: tuple

    @classmethod
    def from_dict(cls, d, path=""):
        d, p = _head(d, path, cls)
        return cls(b.nested(d, "base", TaskContract, p), b.string(d, "module", p, nonempty=True),
                   b.enum(d, "paradigm", PARADIGMS, p), b.string(d, "public_interface", p),
                   b.strings(d, "behaviour_tests", p), b.strings(d, "known_problems", p),
                   b.strings(d, "must_not_read", p))
