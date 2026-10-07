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

## Волна 0 — 9/10 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| D-self-update | Self-update from the dashboard and CLI, verified by the pillar tests before switching, rollback | Artel (`MisterReds/artel`) | design | готово |
| D-brand | Brand identity: A-frame mark, copper palette, bundled fonts, role icons, terminal colours | Artel (`MisterReds/artel`) | design | готово |
| D-codex-runtime | Codex runtime (ChatGPT subscription) with device-code login | Codex / Hermes Agent (`openai/codex`) | design | готово |
| D-providers | Провайдеры page: subscriptions, keys (set/unset only), local model, per-role routing, CLIs installed on demand without Node.js | Artel (`MisterReds/artel`) | design | готово |
| D-installers | Installers for Linux/macOS and Windows that install only what Artel needs | Artel (`MisterReds/artel`) | design | готово |
| D-failure-classes | No crash, no endless loop: auto executor, setup errors to a person, limits wait, replan cap | Artel (`MisterReds/artel`) | design | готово |
| D-evolution-ops | Evolution operations: adopt mode for roadmap items, resume after restart, pause on provider limits, journal on GitHub | Artel (`MisterReds/artel`) | design | готово |
| D-provider-failover | Provider failover on limits, disabling runtimes, model lists from the CLIs, Claude and Codex sign-in from the dashboard, live agents panel | Artel (`MisterReds/artel`) | design | готово |
| D-semantic-retrieval | Meaning-based search, local and private: EmbeddingGemma 2 via llama.cpp, hybrid with BM25, skills by relevance, `search` tool | Google EmbeddingGemma 2 (`google/embeddinggemma-2`) | design | готово |
| D-multimodal-retrieval | Multimodal memory: images, audio and video in deliverables and across projects; fully local RAG with Gemma 4 | Google EmbeddingGemma 2 (`google/embeddinggemma-2`) | design | план |

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

## Волна 2 — 7/10 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| W2-worktree-isolation | Worker isolation — git worktree (or container) per task, merge on acceptance | Archon / Gas Town / Qwen Code (`QwenLM/qwen-code`) | design | готово — feat/isolation-sandbox: opt-in `isolation.worktrees` — a git worktree per task (artel/worktree.py), pre-review merge of the base, merge into shared/ on acceptance, discard on START OVER, conflicts go back to the worker as a task comment (never overwritten); containers not done |
| W2-execpolicy | Command policy rules allow/prompt/forbidden with match/not_match examples validated at load | Codex (`openai/codex`) | evolve | готово — evolve/gen-4 (merged by hand after a conflict with the System One change) |
| W2-guardian | Asynchronous trajectory risk reviewer; high risk switches later actions to blocking review | Codex (`openai/codex`) | evolve | план — gen 5 implement: implementation produced no changes |
| W2-sandbox | bubblewrap sandbox for worker bash | Codex (`openai/codex`) | design | готово — feat/isolation-sandbox: opt-in `sandbox.mode` (artel/sandbox.py) — bubblewrap on Linux (verified for real), sandbox-exec profile on macOS (built, not run on a Mac), unsandboxed fallback with a note or `require: true` refusal; gates sandboxed on `sandbox.gates` |
| W2-fs-grants | Explicit read/write grants per path | OpenScience (`synthetic-sciences/openscience`) | evolve | план — gen 5 implement: implementation produced no changes |
| W2-cost-down | Bring cost back down — wave 1 raised the benchmark cost from $4.57 to $5.99 (+31%) | Claude Code / Codex (`anthropics/claude-code`) | evolve | готово — evolve/gen-4 (merged by hand after a conflict with the System One change) |
| W2-ci | CI on Linux, macOS and Windows — tests, installer and a scripted first run on every push | OpenHands / Codex (`openai/codex`) | design | частично — .github/workflows/ci.yml: ubuntu/macos/windows x Python 3.10/3.13 run the full suite, install.sh / install.ps1 into a temporary home cloning the checkout, then scripts/ci_smoke.py (scripted first run ends done, dashboard answers /api/ready). Code made portable (UTF-8 I/O, POSIX shell via Git Bash on Windows, process-tree stop, POSIX paths in journal keys, restart without execv on Windows). Open: a green run on macOS/Windows is not yet observed; blocking merges on CI needs branch protection, and the evolution publish gate is not wired to it. |
| W2-remote-and-notify | Use from the phone — authenticated remote dashboard and notifications (Telegram / webhook) | Hermes Agent / OpenClaw (`NousResearch/hermes-agent`) | design | готово — Telegram bot (control, answers, notifications, one-time pairing) and `artel web --remote`: token login (scrypt hash only, rotatable), server-side HttpOnly SameSite=Strict sessions, per-IP rate limit, HTTPS recipes (Caddy, Tailscale) with an insecure-connection banner; installable PWA (manifest, icons, shell-only service worker) |
| W2-timezone | User time zone for every time shown (dashboard, CLI, notifications), e.g. Asia/Sakhalin | Artel (`MisterReds/artel`) | evolve | готово — evolve/gen-4 (merged by hand after a conflict with the System One change) |
| W2-evolve-two-subscriptions | Evolution across two subscriptions (Claude Code and Codex) so one provider's limit does not stop it | Artel (`MisterReds/artel`) | design | готово — evolve.runtimes [claude_code, codex]: a limit moves implementation and benchmark to the next runtime, pause only when all are limited; baselines per (commit, runtime) (tests/test_evolve_runtimes.py) |

## Волна 3 — 3/12 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| W3-acp-runtime | ACP runtime plugin — any ACP agent (Claude Code, Codex, Gemini, OpenHands) as a worker | OpenHands (`OpenHands/OpenHands`) | design | готово — feat/acp-runtime — runtime `acp` (artel/runtimes/acp.py): ACP v1 client over stdio with presets chosen per role (`acp:gemini`, `acp:claude`…); Artel's MCP proxy passed in session/new; session/update mirrored to the event log; permissions and fs/* by role policy; resume in the same session; tests with a stand-in ACP agent; handshake checked against the real Gemini CLI and the Claude ACP adapter. |
| W3-dsh-runtime | DeepSeek Harness as a runtime plugin, so dsh plugins work inside Artel workers | DeepSeek Harness (`deepseek-ai/deepseek-harness`) | design | готово — Runtime `dsh` (artel/runtimes/dsh.py) on the official Python SDK; dsh plugins (profile packages and patch layers) load unchanged, Artel tools over dsh-mcp-client; verified for real (boot, MCP tool discovery, missing-key classification, no key used) and end to end against a stand-in SDK (tests/test_dsh.py) |
| W3-durable | Durable turns and idempotent run receipts — resume mid-turn after a crash | Pi / OpenScience (`badlogic/pi-mono`) | design | план |
| W3-plugin-packages | Plugin packages (install from git/pip), lifecycle events before_agent_start / session_shutdown | Pi (`badlogic/pi-mono`) | design | готово — feat/plugin-packages: artel plugins install|list|show|enable|disable|update|remove (git, pip, path; ARTEL_HOME/plugins + plugins.json; static manifest, installed disabled), lifecycle events project_start / before_agent_start / agent_end / task_done / project_end / session_shutdown, failing plugins disabled with the error, dashboard section, examples/artel-plugin-hello |
| W3-state-machine | Task as a state machine with executable transition gates | StateM (`henryqin1997/statem`) | evolve | план |
| W3-fresh-context-steps | Fresh context per step, verified state outside the context | LongHorizon-Harness (`AMAP-ML/LongHorizon-Harness`) | evolve | план |
| W3-plan-review | Plan review before execution; built-in Explore / Plan / Verification subagents | Fusion / Claude Code (`lintsinghua/claude-code-book`) | evolve | план |
| W3-two-phase-memory | Two-phase memory — per-run extraction with secret redaction, then consolidation | Codex (`openai/codex`) | evolve | план |
| W3-tree-budget | Budget for the whole agent tree with threshold reminders | Codex (`openai/codex`) | evolve | план |
| W3-mailbox | Agent mailbox and teams | Claude Code / Gas Town (`lintsinghua/claude-code-book`) | evolve | план |
| W3-automations | Automations — schedule and event triggers | OpenHands / Qwen Code (`QwenLM/qwen-code`) | evolve | план |
| W3-system-one | Our own System One from the partner team, connected through the System One contract (docs/SYSTEM_ONE.md) | partner team (`pending`) | design | план |

## Волна 4 (найдено разбором проектов) — 0/14 готово

| Пункт | Что | Откуда | Режим | Статус |
|---|---|---|---|---|
| A-ecc-paired-promotion | Free hypotheses win only on paired repeats: minimum samples, mean delta and per-task win rate | ECC (`affaan-m/ECC`) | design | план |
| A-ecc-run-heartbeat-watchdog | Silent-run watchdog: a CLI worker with no stream events for N minutes is killed and retried as 'hung' | ECC (`affaan-m/ECC`) | evolve | план |
| A-fusion-file-scope-lease | File-scope leases: tasks declare the paths they write; overlapping tasks never run at the same time | Fusion (`Runfusion/Fusion`) | evolve | план |
| A-fusion-novelty-loop-window | Loop detector catches alternating loops: low-novelty window over recent tool-call fingerprints | Fusion (`Runfusion/Fusion`) | evolve | план |
| A-fusion-external-obstacle-classes | Failure classes for network and host problems: transient network retries free, full disk goes to a person | Fusion (`Runfusion/Fusion`) | evolve | план |
| A-fusion-review-dispute | Workers can dispute a review finding; the next reviewer must rule on each dispute | Fusion (`Runfusion/Fusion`) | evolve | план |
| A-deerflow-typed-criteria | Machine-checkable acceptance criteria (file exists / non-empty / json-valid, tests_passed:<cmd>) decided in code | DeerFlow (`bytedance/deer-flow`) | evolve | план |
| A-hive-chat-reply | Answer a waiting task from the phone: Telegram reply routed back to the task by an HMAC reference | Hive (`aden-hive/hive`) | design | план |
| A-hive-escalation-filter | Filter spurious 'ask a human' blocks: a cheap check sends permission-seeking back to work | Hive (`aden-hive/hive`) | evolve | план |
| A-agentteams-secret-free-env | Workers never see provider secrets: tool and CLI environments are scrubbed of keys and tokens | AgentTeams (`agentscope-ai/AgentTeams`) | evolve | план |
| A-omc-clarify-gate | Clarify vague goals before planning: ambiguity score, up to three questions to the owner | oh-my-claudecode (`Yeachan-Heo/oh-my-claudecode`) | evolve | план |
| A-omc-review-tiers | Review effort scaled to the change: light review for small diffs, full review for large or sensitive ones | oh-my-claudecode (`Yeachan-Heo/oh-my-claudecode`) | evolve | план |
| A-omc-secret-redaction | Redact secrets from the event log, tool results and notifications | oh-my-claudecode (`Yeachan-Heo/oh-my-claudecode`) | evolve | план |
| A-munder-work-tokens | Count cache reads separately: budgets on work tokens, costs with cache prices | Munder Difflin (`chaitanyagiri/munder-difflin`) | evolve | план |

## Изученные проекты — 7

Код прочитан агентом-аналитиком (`artel absorb` / `artel evolve --cycle`); найденное стало пунктами выше.

| Проект | Лицензия | Найдено | В карту | Итог |
|---|---|---|---|---|
| `affaan-m/ECC` | MIT | 7 | A-ecc-paired-promotion, A-ecc-run-heartbeat-watchdog | Plugin/skill bundle for Claude Code plus an alpha Rust control plane (ecc2): SQLite sessions, daemon, worktrees, deterministic risk scoring, notifications with  |
| `Runfusion/Fusion` | MIT | 9 | A-fusion-file-scope-lease, A-fusion-novelty-loop-window, A-fusion-external-obstacle-classes, A-fusion-review-dispute | TypeScript kanban 'software factory': PROMPT.md plans with file scope, worktree per task, plan/execute/review gates, stuck-task detector, external-obstacle clas |
| `bytedance/deer-flow` | MIT | 4 | A-deerflow-typed-criteria | LangGraph super-agent: lead agent, sub-agents with acceptance criteria checked in code, sandboxes, skills, memory backends, goals evaluated after each run, doct |
| `aden-hive/hive` | Apache-2.0 | 5 | A-hive-chat-reply, A-hive-escalation-filter | Python colony runtime: a Queen agent loop clones itself into workers, tracker ledger, crash-safe park/resume, Sentinel out-of-band escalation over Telegram/Slac |
| `agentscope-ai/AgentTeams` | Apache-2.0 | 4 | A-agentteams-secret-free-env | Kubernetes-native manager/worker platform: Matrix rooms for human visibility, MinIO shared files, Higress gateway holding credentials, TeamHarness task transiti |
| `Yeachan-Heo/oh-my-claudecode` | MIT | 7 | A-omc-clarify-gate, A-omc-review-tiers, A-omc-secret-redaction | Claude Code plugin: team pipeline plan/prd/exec/verify/fix, deep-interview ambiguity gate, verification tiers, notifications with a reply listener and token red |
| `chaitanyagiri/munder-difflin` | MIT | 6 | A-munder-work-tokens | Electron desktop app wrapping many terminal agent CLIs as an office: git-backed hive mailbox with a single committer, markdown memory, runaway circuit breaker ( |

Итого: 27/54 пунктов готово.
