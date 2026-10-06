# Дорожная карта Artel

Сгенерировано из `roadmap.yaml` командой `artel roadmap render`. Правки — только в yaml. Что уже взято и откуда — [ADOPTED.md](ADOPTED.md); опоры — [ARCHITECTURE.md](ARCHITECTURE.md).

Режимы: **pillar** — опора, контракт закреплён `tests/test_invariants.py`; **design** — структурное изменение, делается вручную; **evolve** — реализует сам Artel (`artel evolve --roadmap`).

## Опоры

| Опора | Что | Источник | Статус |
|---|---|---|---|
| P1-modularity | Everything is a plugin — manifests, reversible effects, hot reload, swappable runtimes | DeepSeek Harness | готово |
| P2-skills | Self-improving skill library — create/patch after hard tasks, read-before-write, ledger, lifecycle, guard | Hermes Agent | готово |
| P3-goal-loop | Work until done — continuation, no-progress backoff, evidence, reviewer and auditor decide | Prime Agent | готово |
| P4-souls | Bots with souls — SOUL/IDENTITY/RULES/MEMORY and a private workspace | OpenClaw | готово |
| P5-transparency | Append-only event log; every model and tool call recorded; live dashboard | DeepSeek Harness | готово |
| P6-kanban | Kanban DAG with compare-and-swap claims | Hermes Agent | готово |
| P7-measured-evolution | Evolution through branches, tests, hidden-check benchmark, population screening and stacking | auto-harness / Gear / Ouroboros | готово |

## Волна 0 — 7/7 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| D-self-update | Self-update from the dashboard and CLI, verified by the pillar tests before switching, rollback | Artel (`MisterReds/artel`) | design | готово |
| D-brand | Brand identity: A-frame mark, copper palette, bundled fonts, role icons, terminal colours | Artel (`MisterReds/artel`) | design | готово |
| D-codex-runtime | Codex runtime (ChatGPT subscription) with device-code login | Codex / Hermes Agent (`openai/codex`) | design | готово |
| D-providers | Провайдеры page: subscriptions, keys (set/unset only), local model, per-role routing, CLIs installed on demand without Node.js | Artel (`MisterReds/artel`) | design | готово |
| D-installers | Installers for Linux/macOS and Windows that install only what Artel needs | Artel (`MisterReds/artel`) | design | готово |
| D-failure-classes | No crash, no endless loop: auto executor, setup errors to a person, limits wait, replan cap | Artel (`MisterReds/artel`) | design | готово |
| D-evolution-ops | Evolution operations: adopt mode for roadmap items, resume after restart, pause on provider limits, journal on GitHub | Artel (`MisterReds/artel`) | design | готово |

## Волна 1 — 8/8 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| W1-loop-detector | Loop detector — identical tool calls in a row and repeated text stop the run with a corrective note | Qwen Code (`QwenLM/qwen-code`) | evolve | готово — evolve/gen-2 (evolve/g2-1-w1-loop-detector-loop-detector-i) |
| W1-evidence-window | Evidence window keeps head and tail, cuts the middle | Qwen Code (`QwenLM/qwen-code`) | evolve | готово — evolve/gen-2 (evolve/g2-2-w1-evidence-window-evidence-wind) |
| W1-output-spill | Oversized tool output spills to a file with a preview and the path | OpenCode (`anomalyco/opencode`) | evolve | готово — evolve/gen-3 (evolve/g3-1-w1-output-spill-oversized-tool-o) |
| W1-read-cache | Stale-edit guard — refuse edits to files changed since the agent last read them | Qwen Code (`QwenLM/qwen-code`) | evolve | готово — evolve/gen-2 (evolve/g2-4-w1-read-cache-stale-edit-guard-r) |
| W1-fuzzy-edit | Fuzzy edit application (whitespace-normalized, then block-anchor match) | OpenCode (`anomalyco/opencode`) | evolve | готово — evolve/gen-3 (evolve/g3-2-w1-fuzzy-edit-fuzzy-edit-applica) |
| W1-post-write-diagnostics | Diagnostics right after a write (py_compile / configured linters) | OpenCode (`anomalyco/opencode`) | evolve | готово — evolve/gen-3 (evolve/g3-3-w1-post-write-diagnostics-diagno) |
| W1-compaction | Context compaction for the native runtime — clear old tool outputs, then summarize | Claude Code / OpenCode (`anomalyco/opencode`) | evolve | готово — evolve/gen-3 (evolve/g3-4-w1-compaction-context-compaction) |
| W1-snapshots | Shadow-git snapshot per run; rejected attempt can be reverted | OpenCode (`anomalyco/opencode`) | evolve | готово — evolve/gen-3 (evolve/g3-5-w1-snapshots-shadow-git-snapshot) |

## Волна 2 — 4/10 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| W2-worktree-isolation | Worker isolation — git worktree (or container) per task, merge on acceptance | Archon / Gas Town / Qwen Code (`QwenLM/qwen-code`) | design | план |
| W2-execpolicy | Command policy rules allow/prompt/forbidden with match/not_match examples validated at load | Codex (`openai/codex`) | evolve | готово — evolve/gen-4 (merged by hand after a conflict with the System One change) |
| W2-guardian | Asynchronous trajectory risk reviewer; high risk switches later actions to blocking review | Codex (`openai/codex`) | evolve | план — gen 4: conflicted in artel/plugins/builtin_tools.py with the adopted items; retried on the new main |
| W2-sandbox | bubblewrap sandbox for worker bash | Codex (`openai/codex`) | design | план |
| W2-fs-grants | Explicit read/write grants per path | OpenScience (`synthetic-sciences/openscience`) | evolve | план — gen 4: conflicted in artel/plugins/builtin_tools.py with the adopted items; retried on the new main |
| W2-cost-down | Bring cost back down — wave 1 raised the benchmark cost from $4.57 to $5.99 (+31%) | Claude Code / Codex (`anthropics/claude-code`) | evolve | готово — evolve/gen-4 (merged by hand after a conflict with the System One change) |
| W2-ci | CI on Linux, macOS and Windows — tests, installer and a scripted first run on every push | OpenHands / Codex (`openai/codex`) | design | план |
| W2-remote-and-notify | Use from the phone — authenticated remote dashboard and notifications (Telegram / webhook) | Hermes Agent / OpenClaw (`NousResearch/hermes-agent`) | design | план |
| W2-timezone | User time zone for every time shown (dashboard, CLI, notifications), e.g. Asia/Sakhalin | Artel (`MisterReds/artel`) | evolve | готово — evolve/gen-4 (merged by hand after a conflict with the System One change) |
| W2-evolve-two-subscriptions | Evolution across two subscriptions (Claude Code and Codex) so one provider's limit does not stop it | Artel (`MisterReds/artel`) | design | готово — evolve.runtimes [claude_code, codex]: a limit moves implementation and benchmark to the next runtime, pause only when all are limited; baselines per (commit, runtime) (tests/test_evolve_runtimes.py) |

## Волна 3 — 0/12 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| W3-acp-runtime | ACP runtime plugin — any ACP agent (Claude Code, Codex, Gemini, OpenHands) as a worker | OpenHands (`OpenHands/OpenHands`) | design | план |
| W3-dsh-runtime | DeepSeek Harness as a runtime plugin, so dsh plugins work inside Artel workers | DeepSeek Harness (`deepseek-ai/deepseek-harness`) | design | план |
| W3-durable | Durable turns and idempotent run receipts — resume mid-turn after a crash | Pi / OpenScience (`badlogic/pi-mono`) | design | план |
| W3-plugin-packages | Plugin packages (install from git/pip), lifecycle events before_agent_start / session_shutdown | Pi (`badlogic/pi-mono`) | design | план |
| W3-state-machine | Task as a state machine with executable transition gates | StateM (`henryqin1997/statem`) | evolve | план |
| W3-fresh-context-steps | Fresh context per step, verified state outside the context | LongHorizon-Harness (`AMAP-ML/LongHorizon-Harness`) | evolve | план |
| W3-plan-review | Plan review before execution; built-in Explore / Plan / Verification subagents | Fusion / Claude Code (`lintsinghua/claude-code-book`) | evolve | план |
| W3-two-phase-memory | Two-phase memory — per-run extraction with secret redaction, then consolidation | Codex (`openai/codex`) | evolve | план |
| W3-tree-budget | Budget for the whole agent tree with threshold reminders | Codex (`openai/codex`) | evolve | план |
| W3-mailbox | Agent mailbox and teams | Claude Code / Gas Town (`lintsinghua/claude-code-book`) | evolve | план |
| W3-automations | Automations — schedule and event triggers | OpenHands / Qwen Code (`QwenLM/qwen-code`) | evolve | план |
| W3-system-one | Our own System One from the partner team, connected through the System One contract (docs/SYSTEM_ONE.md) | partner team (`pending`) | design | план |

Итого: 19/37 пунктов готово.
