# Delivery 3: AgentPort — one agent loop, thin codecs

Source: docs/SPEC.md §6 П3 and §10 (defects а, б, в); docs/TARGET_ARCHITECTURE.md §3 (AgentPort);
docs/AUDIT-2026-10.md §3. Expensive-token budget: 60k (stop and escalate above 72k).

Two stages, each merged separately (owner's consent, green CI on all six jobs).

## Stage A — structure, behaviour unchanged

Input: artel/runtimes/claude_code.py (381 lines), artel/runtimes/codex.py (471), artel/runtimes/__init__.py,
their tests (test_codex, test_grants*, test_mcp_*, test_security, test_role_gating, test_failover, test_isolation).

Output:
- `artel/adapters/agent/loop.py` — everything both CLIs share, written once: the Artel tool bridge (MCP proxy),
  process start (agent_env, process group, StderrTail, timeout kill), line reading, mirroring of normalised
  events into the journal, terminal-tool handling with the usage grace, resume attempts, policy gate on command
  events, limit detection, the result object `run_agent` callers use today.
- `artel/adapters/agent/claude_code.py`, `artel/adapters/agent/codex.py` — codecs, ≤ 200 lines each: settings,
  env, args, `parse(line) -> list[AgentEvent]`, `classify(text) -> ErrorClass`, model catalogue, sandbox probe.
  A codec does no IO except building args and its own probes.
- `AgentEvent` (kinds: session, text, tool_call, tool_result, command, usage, end, error) in `artel/contracts`;
  `AgentPort` adapters in `artel/adapters/agent/__init__.py` satisfy `artel.ports.AgentPort`.
- `artel/runtimes/claude_code.py` and `codex.py` deleted; every importer (cli.py, providers.py,
  plugins/runtimes.py, tests) imports from the adapters.

Constraints: no behaviour change visible in tests or the journal; secrets by variable name only; sandbox,
grants and judge roles unchanged; codecs import nothing from `loop.py` and no `artel.*` outside contracts/ports
and their own helpers.

Acceptance: all existing runtime tests pass unchanged except import paths; a codec test per CLI parses a recorded
stream (fixture lines from tests/fakes) into the expected AgentEvent list; `wc -l` of each codec ≤ 200.

## Stage B — hardening (audit and baseline defects)

1. Defect в: every `llm/response` the loop writes carries `stop_reason`; journal writes of agent events are
   validated against `artel.contracts.Event` (invalid → the run logs `contract/violation` and continues, never
   writes a malformed event). `artel log` reads events through the contract and never crashes on old journals.
2. Defect б: `ExecutorResult`-style usage from every run; a codec that reports none sets `usage_missing`,
   cached tokens counted the same way in both codecs and the native provider.
3. Defect а: agents' shells resolve `python` to the user's `python3` (a shim directory on PATH, created once under
   ARTEL_HOME), for native bash, task checks and both CLIs.
4. Sandbox for native bash and task checks on by default where a backend exists (`mode: auto`); reads of
   `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.gnupg` and ARTEL_HOME outside the project are denied; when no backend
   exists the dashboard and `artel providers` say so once. Config `sandbox.mode: off` keeps the old behaviour.
5. Agents cannot write `policy.yaml`, `artel.yaml` or the skills library: those paths are outside every write
   grant and the policy forbids commands that target them.
6. Tool output is capped (head + tail, spill to a file in the project) before it enters history, for the bridge
   and native tools alike; untrusted tool output is wrapped in delimiters whose closing tag is escaped inside.
7. Native loop: a response cut by `max_tokens` never executes its tool calls (the model is asked to continue);
   tool-call JSON that does not parse is returned to the model as an error, never passed to the tool.

Acceptance: one test per item that fails before the change; full suite; green CI on six jobs; a live smoke on the
owner's machine (one task on each subscription) before merge of stage B.
