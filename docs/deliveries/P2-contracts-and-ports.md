# Delivery 2: contracts and ports

Source: docs/SPEC.md §6 П2, docs/TARGET_ARCHITECTURE.md §3 (ports) and §6 (schemas), SPEC §10 defects б and в.
Expensive-token budget: 40k (stop and escalate above 48k).

## Input

- Schemas of TARGET_ARCHITECTURE §6: Intent, ManagerInput, TaskContract, AmplifiedTaskContract, ExecutorResult,
  EscalationRequest, DecisionLogEntry, CapabilityProfile, RewriteProposal, RewriteTaskContract, RangeResearchTask.
- Port contracts of TARGET_ARCHITECTURE §3: LLMPort, AgentPort, MemoryPort, EventPort, ToolPort, ClockPort,
  ConfigPort; NormRequest / NormResponse.
- Defects б (no token usage) and в (events without required fields crash readers).

## Output

- `artel/contracts/`: one frozen dataclass per schema plus NormRequest, NormResponse, Usage, ToolResult,
  ModelCard, ErrorClass, Event. Each has `from_dict(d)` that validates (required fields, types, enums, ranges)
  and raises `ContractError` naming the field, and `to_dict()`; `from_dict(x.to_dict()) == x`.
  `Usage{in, out, cached}` is required in NormResponse and ExecutorResult (defect б).
  `Event{seq, ts, kind, data}` validates `data` against a per-kind required-field table for the kinds the
  current journal writes (defect в).
- `artel/ports/`: `typing.Protocol` per port with the exact methods of §3 and docstrings stating the contract.
- `artel/ports/fakes.py`: in-memory fakes for every port (scripted LLM, scripted agent, dict memory, list event
  log, recording tool runner, fake clock, dict config), each satisfying its Protocol at runtime.
- `tests/test_contracts.py`, `tests/test_ports.py`.

## Constraints

- `contracts` and `ports` import only the standard library and each other (`ports` → `contracts`, never back).
  No network, file system, environment, `artel.*` outside these two packages, `adapters`, vendor or model names.
- No module-level mutable state. Python ≥ 3.10.
- Existing code is not changed or wired to the new packages in this delivery (that is П3–П8).
- No model identifiers anywhere.

## Acceptance

1. Unit tests: every schema round-trips; every required field missing → `ContractError` naming it; every enum
   rejects an unknown value; NormResponse and ExecutorResult without usage are rejected.
2. Every fake passes `isinstance(fake, Port)` with `runtime_checkable` Protocols and a behaviour test
   (EventPort append-only and ordered; ClockPort fake advances only on `sleep`; ConfigPort read-only).
3. Import check test: an `ast` scan of `artel/contracts` and `artel/ports` finds no import outside the standard
   library and these two packages (archcheck proper is П5).
4. Full suite `python3 -m pytest -q -x` green; critic review before merge; merge to master only with the
   owner's consent.
