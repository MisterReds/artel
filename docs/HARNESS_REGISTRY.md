# Реестр харнессов

Полный реестр: `artel/data/harnesses.json` (в дашборде — раздел «Реестр харнессов»). Скрипты сборки: `registry/`.

## Методология

1. **Источники.** 10 курируемых awesome-списков, клонированы целиком:
   `mahonzhan/awesome-agent-harness`, `yenanjing/awesome-harness-engineering`, `andyrewlee/awesome-agent-orchestrators`, `Picrew/…`, `AmariahAK/…`, `shyringo/…`, `chasekek/…`, `AutoJunjie/awesome-agent-harness`, `ai-boost/awesome-harness-engineering`, `lobehub/awesome-rsi`. Плюс 42 ручные записи: проекты, которых нет в списках (Prime Agent, StateM, LongHorizon-Harness, auto-harness, Goose, Roo Code, Letta, DSPy, Harbor и др.), и 9 закрытых продуктов (OpenAI Dots, Grok Bot, Cursor, Devin, Factory, Amp, Manus, Claude Managed Agents, Jules).
2. **Извлечение.** Каждая ссылка на GitHub из списков → запись с разделом и описанием. Дубли склеены, учитывается, в скольких списках проект упомянут.
3. **Проверка существования.** `git ls-remote` по каждому репозиторию.
4. **Уровень C (автоматически).** Для всех харнессов, оркестраторов, фреймворков и проектов памяти/скиллов скачаны README и дата последнего коммита (blobless shallow clone). Извлечена матрица из 26 признаков (kanban, DAG задач, верификатор, цикл до результата, самоулучшение, скиллы, персоны, многопроектность, best-of-N, переписка агентов, бюджеты и др.) и заявленные результаты на бенчмарках. Рейтинг релевантности для Artel — взвешенная сумма признаков плюс число списков.
5. **Уровень B (вручную).** Прочитаны выжимки README ~40 самых релевантных проектов.
6. **Уровень A (код).** Разобраны исходники Hermes Agent, Prime Agent, DeepSeek Harness, OpenClaw, Agent Skills spec, anthropics/skills, openai/skills (см. `ANALYSIS.md`).

GitHub API (звёзды) в среде недоступен, поэтому популярность оценивается числом списков, а не звёздами.

## Цифры

| | |
|---|---|
| Всего проектов | 1391 |
| Существуют | 1366 |
| Удалены / переименованы | 16 |
| Закрытые продукты | 9 |
| Оркестраторы | 344 |
| Память и скиллы | 208 |
| Харнессы | 194 |
| Фреймворки | 150 |
| Протоколы | 136 |
| Бенчмарки и оценка | 136 |
| Песочницы | 74 |
| README проанализировано автоматически | 877 |
| Коммиты после июля 2026 | 717 из 877 |

Частота признаков среди 877 README: скиллы 558, верификатор 516, плагины 446, память 425, песочница 420, MCP 410, resume/durable 394, дашборд 387, цикл до результата 203, DAG задач 146, самоулучшение 112, переписка агентов 84, **kanban 66, многопроектность 61, best-of-N 9**.

## Что взято и что ещё нужно взять

Статус: ✅ есть в Artel · 🆕 сделано в этой итерации · ⏭ следующий этап.

| Механизм | Где лучше всего реализован | Статус |
|---|---|---|
| Цель → проект → канбан → команда ботов | Hermes kanban, Fusion, agent-teams-ai, Chorus | ✅ |
| Бот с душой и своим workspace | OpenClaw, Hermes profiles, Hive (Queen-персоны), Munder Difflin | ✅ |
| Цикл до результата, continuation, no-progress backoff | Prime Agent, LoopX (heartbeat, +17% LHTB), LongHorizon-Harness | ✅ |
| Независимый ревьюер + shell-гейты | kodo (архитектор-верификатор поймал 9 раундов багов), Chorus, StateM | ✅ |
| Аудит цели, недостающая работа → новые задачи | LongHorizon (read-only auditor), Hermes root wake-up | ✅ |
| Скиллы: авто-создание, ledger, откат, guard, lifecycle | Hermes, ECC (293 скилла + continuous learning) | ✅ |
| Refine харнесса со снапшотами | Prime Continual Harness, revfactory `/harness:evolve` | ✅ |
| Плагинное ядро, append-only лог | DeepSeek Harness | ✅ |
| **Портфель всех проектов, этап каждого, «требует внимания»** | Gas Town (rigs), Munder Difflin (floors), Agent Deck, Maestro | 🆕 |
| **Аналитика агентов по проектам: модели/роли/боты, $ и токены на задачу** | agent-teams-ai (аналитика + бюджеты), Agent Deck (cost dashboard) | 🆕 |
| **Путь каждой задачи по этапам с временем и исполнителем** | AgentTeams (TeamHarness task state history), Fusion | 🆕 |
| **Бюджетные алерты 80% / 100%** | agent-teams-ai | 🆕 (в портфеле) |
| **Реестр всех харнессов в дашборде** | — | 🆕 |
| Самоулучшение, **проверяемое бенчмарком**: правка харнесса принимается, только если eval не просел | auto-harness (τ-bench 0.56→0.78), Gear (Terminal-Bench 52.9→84.3%), HarnessX (GAIA 62→84%), reef | ⏭ приоритет 1 |
| Изоляция воркера: git worktree / контейнер на задачу | Archon, ClawTeam, Vibe Kanban, Cline Kanban, Gas Town | ⏭ приоритет 1 |
| Ревью моделью другого вендора | Omnigent (Polly) | ⏭ (уже настраивается через `models.reviewer`) |
| Ревью плана до исполнения (план → ревью → исполнение → ревью по шагам) | Fusion, Archon | ⏭ |
| Задача как машина состояний с исполняемыми гейтами переходов | StateM (95.3% Terminal-Bench 2.1), Archon YAML-воркфлоу, Bernstein | ⏭ |
| Свежий контекст на каждый шаг + проверенное состояние вне контекста | LongHorizon-Harness, Maestro Auto Run | ⏭ частично (повтор = свежий контекст) |
| Почта между агентами, blackboard | Munder Difflin, Gas Town, ClawTeam, AgentTeams | ⏭ (сейчас — комментарии к задаче) |
| Merge-очередь с проверкой и бисекцией | Gas Town (Refinery) | ⏭ |
| Панель судей, adversarial-верификация, loop-until-dry | revfactory/harness v2 | ⏭ |
| A/B скилла: с ним и без | revfactory/harness | ⏭ |
| Подписанные квитанции прогона | Bernstein | ⏭ |
| Матрица прав агентов и людей | Chorus, QM | ⏭ |
| Witness/Deacon: патруль зависших агентов по всем проектам | Gas Town | ⏭ (сейчас — heartbeat + recover) |
| Петля суждений: исправления → принципы → постоянные правила | kagemusha (преемник multi-agent-shogun) | ✅ частично (refine → RULES.md) |
| Чат-поверхности (Telegram, Slack) | Hermes, QM, OpenClaw | ⏭ |
| Мульти-рантайм: воркер = Claude Code / Codex / OpenCode / DSH | Munder Difflin, Omnigent, AgentTeams, Scion | ⏭ |

### Харнессы — топ-15 из 194

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) | 6 | 2026-09-28 | task_dag, worktree, sandbox, skills, memory, goal_loop, persona |
| [HKUDS/OpenHarness](https://github.com/HKUDS/OpenHarness) | 7 | 2026-06-04 | worktree, sandbox, skills, memory, verifier, persona, observability |
| [neosigmaai/auto-harness](https://github.com/neosigmaai/auto-harness) | 7 | 2026-09-16 | sandbox, self_improve, verifier, budget, benchmark |
| [Hmbown/CodeWhale](https://github.com/Hmbown/CodeWhale) | 6 | 2026-10-04 | sandbox, skills, goal_loop, persona, dashboard, mcp, human_loop |
| [emo-xiaoyu/harness-mix](https://github.com/emo-xiaoyu/harness-mix) | 1 | 2026-10-02 | kanban, task_dag, worktree, skills, verifier, goal_loop, persona |
| [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | 8 | 2026-10-03 | dashboard, plugins, chat_surfaces |
| [Chachamaru127/claude-code-harness](https://github.com/Chachamaru127/claude-code-harness) | 5 | 2026-09-06 | task_dag, worktree, skills, memory, verifier, persona, human_loop |
| [AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness) | 4 | 2026-08-20 | skills, verifier, goal_loop, persona, dashboard, mcp, human_loop |
| [SAIL-Research-Lab/cheetahclaws](https://github.com/SAIL-Research-Lab/cheetahclaws) | 1 | 2026-09-21 | task_dag, worktree, sandbox, skills, memory, verifier, goal_loop |
| [earendil-works/pi](https://github.com/earendil-works/pi) | 6 | 2026-10-05 | sandbox, skills, verifier, durable_resume, plugins, scheduling, chat_surfaces |
| [microsoft/Webwright](https://github.com/microsoft/Webwright) | 4 | 2026-10-04 | skills, verifier, goal_loop, dashboard, budget, plugins, chat_surfaces |
| [nullclaw/nullclaw](https://github.com/nullclaw/nullclaw) | 1 | 2026-10-05 | task_dag, sandbox, skills, memory, verifier, goal_loop, persona |
| [PrimeIntellect-ai/prime-agent](https://github.com/PrimeIntellect-ai/prime-agent) | 1 | 2026-10-04 | worktree, sandbox, skills, self_improve, verifier, goal_loop, persona |
| [sfw/loom](https://github.com/sfw/loom) | 1 | 2026-07-06 | task_dag, sandbox, self_improve, memory, verifier, persona, multi_project |
| [openai/codex](https://github.com/openai/codex) | 7 | 2026-10-04 | dashboard |

### Оркестраторы — топ-15 из 344

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [affaan-m/ECC](https://github.com/affaan-m/ECC) | 6 | 2026-10-01 | task_dag, worktree, sandbox, skills, self_improve, memory, verifier |
| [aden-hive/hive](https://github.com/aden-hive/hive) | 7 | 2026-09-13 | task_dag, sandbox, skills, memory, verifier, goal_loop, persona |
| [bytedance/deer-flow](https://github.com/bytedance/deer-flow) | 7 | 2026-10-05 | sandbox, skills, memory, verifier, goal_loop, persona, dashboard |
| [Yeachan-Heo/oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) | 6 | 2026-10-03 | worktree, skills, self_improve, verifier, goal_loop, persona, observability |
| [agentscope-ai/AgentTeams](https://github.com/agentscope-ai/AgentTeams) | 4 | 2026-10-04 | task_dag, sandbox, skills, self_improve, memory, verifier, goal_loop |
| [Runfusion/Fusion](https://github.com/Runfusion/Fusion) | 1 | 2026-10-04 | kanban, task_dag, worktree, sandbox, skills, self_improve, memory |
| [thClaws/thClaws](https://github.com/thClaws/thClaws) | 6 | 2026-09-25 | worktree, sandbox, skills, memory, verifier, goal_loop, persona |
| [chaitanyagiri/munder-difflin](https://github.com/chaitanyagiri/munder-difflin) | 5 | 2026-10-05 | kanban, worktree, skills, memory, goal_loop, persona, dashboard |
| [ruvnet/ruflo](https://github.com/ruvnet/ruflo) | 6 | 2026-10-04 | sandbox, skills, self_improve, memory, verifier, dashboard, observability |
| [affaan-m/everything-claude-code](https://github.com/affaan-m/everything-claude-code) | 3 | 2026-10-01 | task_dag, worktree, sandbox, skills, self_improve, memory, verifier |
| [huangruiteng/loopx](https://github.com/huangruiteng/loopx) | 2 | 2026-10-05 | kanban, skills, self_improve, memory, verifier, goal_loop, persona |
| [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) | 8 | 2026-10-04 | sandbox, skills, memory, goal_loop, observability, mcp, human_loop |
| [GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion) | 7 | 2026-10-04 | task_dag, worktree, sandbox, skills, memory, persona, dashboard |
| [yc-software/qm](https://github.com/yc-software/qm) | 7 | 2026-10-04 | task_dag, sandbox, skills, memory, verifier, persona, dashboard |
| [Chorus-AIDLC/Chorus](https://github.com/Chorus-AIDLC/Chorus) | 6 | 2026-10-04 | kanban, task_dag, sandbox, verifier, persona, observability, mcp |

### Фреймворки — топ-15 из 150

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [holny/Agent-Harness-Develop-Book](https://github.com/holny/Agent-Harness-Develop-Book) | 1 | 2026-09-24 | kanban, task_dag, worktree, sandbox, skills, self_improve, memory |
| [tmgthb/Autonomous-Agents](https://github.com/tmgthb/Autonomous-Agents) | 1 | 2026-06-24 | task_dag, sandbox, skills, self_improve, memory, verifier, goal_loop |
| [agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw) | 2 | 2026-09-30 | sandbox, skills, self_improve, memory, verifier, goal_loop, persona |
| [Habitat-Thinking/ai-literacy-superpowers](https://github.com/Habitat-Thinking/ai-literacy-superpowers) | 1 | 2026-09-20 | worktree, sandbox, skills, memory, verifier, goal_loop, persona |
| [sd0xdev/sd0x-harness](https://github.com/sd0xdev/sd0x-harness) | 1 | 2026-10-03 | sandbox, skills, self_improve, memory, verifier, goal_loop, persona |
| [inngest/utah](https://github.com/inngest/utah) | 5 | 2026-07-06 | skills, memory, persona, dashboard, observability, budget, durable_resume |
| [blogminhquy/javis-os](https://github.com/blogminhquy/javis-os) | 1 | 2026-10-05 | kanban, task_dag, sandbox, skills, memory, verifier, goal_loop |
| [clawboo/clawboo](https://github.com/clawboo/clawboo) | 1 | 2026-09-28 | kanban, worktree, skills, self_improve, memory, verifier, dashboard |
| [genai-io/san](https://github.com/genai-io/san) | 1 | 2026-10-01 | sandbox, skills, self_improve, memory, goal_loop, persona, dashboard |
| [Stanshy/AgentHub](https://github.com/Stanshy/AgentHub) | 1 | 2026-04-09 | kanban, sandbox, skills, memory, verifier, persona, multi_project |
| [deepklarity/harness-kit](https://github.com/deepklarity/harness-kit) | 2 | 2026-07-16 | kanban, task_dag, sandbox, skills, verifier, dashboard, budget |
| [CodeSoul-co/Hypha](https://github.com/CodeSoul-co/Hypha) | 1 | 2026-09-04 | task_dag, sandbox, skills, memory, verifier, goal_loop, persona |
| [hardness1020/learn-agent-architecture](https://github.com/hardness1020/learn-agent-architecture) | 1 | 2026-09-22 | worktree, sandbox, skills, self_improve, memory, verifier, goal_loop |
| [openbkn-ai/bkn-foundry](https://github.com/openbkn-ai/bkn-foundry) | 1 | 2026-10-05 | sandbox, skills, verifier, goal_loop, persona, dashboard, observability |
| [SuperClaude-Org/SuperClaude_Framework](https://github.com/SuperClaude-Org/SuperClaude_Framework) | 4 | 2026-09-27 | skills, memory, verifier, persona, mcp, spec_driven, plugins |

### Память и скиллы — топ-15 из 208

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 8 | 2026-10-05 | sandbox, skills, self_improve, memory, verifier, persona, mcp |
| [muratcankoylan/Agent-Skills-for-Context-Engineering](https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering) | 4 | 2026-10-01 | sandbox, skills, self_improve, memory, verifier, goal_loop, persona |
| [EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS) | 4 | 2026-10-01 | task_dag, skills, self_improve, memory, goal_loop, persona, dashboard |
| [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files) | 4 | 2026-10-01 | task_dag, worktree, sandbox, skills, memory, verifier, goal_loop |
| [jaylfc/taOS](https://github.com/jaylfc/taOS) | 1 | 2026-10-01 | kanban, task_dag, sandbox, skills, memory, verifier, goal_loop |
| [davidkimai/Context-Engineering](https://github.com/davidkimai/Context-Engineering) | 4 | 2026-02-26 | task_dag, self_improve, memory, verifier, goal_loop, budget, chat_surfaces |
| [jrenaldi79/harness-engineering](https://github.com/jrenaldi79/harness-engineering) | 1 | 2026-03-26 | worktree, skills, self_improve, verifier, goal_loop, persona, multi_project |
| [withkynam/vibecode-pro-max-kit](https://github.com/withkynam/vibecode-pro-max-kit) | 1 | 2026-06-21 | task_dag, worktree, sandbox, skills, self_improve, memory, verifier |
| [NevaMind-AI/memU](https://github.com/NevaMind-AI/memU) | 5 | 2026-09-21 | skills, self_improve, memory, verifier, persona, durable_resume, plugins |
| [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) | 4 | 2026-10-04 | task_dag, worktree, sandbox, skills, memory, verifier, multi_project |
| [osaurus-ai/osaurus](https://github.com/osaurus-ai/osaurus) | 4 | 2026-10-05 | sandbox, skills, memory, verifier, goal_loop, persona, dashboard |
| [multica-ai/multica](https://github.com/multica-ai/multica) | 6 | 2026-10-03 | worktree, sandbox, skills, verifier, persona, dashboard, scheduling |
| [gastownhall/beads](https://github.com/gastownhall/beads) | 4 | 2026-10-04 | skills, memory, verifier, goal_loop, persona, dashboard, mcp |
| [holaboss-ai/holaOS](https://github.com/holaboss-ai/holaOS) | 5 | 2026-08-21 | skills, memory, verifier, persona, dashboard, mcp, human_loop |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | 5 | 2026-10-01 | sandbox, skills, memory, verifier, persona, dashboard, budget |

### Бенчмарки и оценка — топ-15 из 136

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [china-qijizhifeng/agentic-harness-engineering](https://github.com/china-qijizhifeng/agentic-harness-engineering) | 6 |  |  |
| [claw-eval/claw-eval](https://github.com/claw-eval/claw-eval) | 6 |  |  |
| [stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness) | 6 |  |  |
| [AgentOps-AI/agentops](https://github.com/AgentOps-AI/agentops) | 5 |  |  |
| [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | 5 |  |  |
| [awslabs/agentcore-samples](https://github.com/awslabs/agentcore-samples) | 5 |  |  |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | 5 |  |  |
| [confident-ai/deepeval](https://github.com/confident-ai/deepeval) | 5 |  |  |
| [EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) | 5 |  |  |
| [future-agi/future-agi](https://github.com/future-agi/future-agi) | 5 |  |  |
| [Helicone/helicone](https://github.com/Helicone/helicone) | 5 |  |  |
| [openlit/openlit](https://github.com/openlit/openlit) | 5 |  |  |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | 5 |  |  |
| [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) | 5 |  |  |
| [teaql/teaql-agent-kit](https://github.com/teaql/teaql-agent-kit) | 5 |  |  |

### Песочницы — топ-15 из 74

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [openclaw/openclaw](https://github.com/openclaw/openclaw) | 8 |  |  |
| [browser-use/browser-harness](https://github.com/browser-use/browser-harness) | 6 |  |  |
| [esengine/DeepSeek-Reasonix](https://github.com/esengine/DeepSeek-Reasonix) | 6 |  |  |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | 6 |  |  |
| [HKUDS/DeepCode](https://github.com/HKUDS/DeepCode) | 6 |  |  |
| [nearai/ironclaw](https://github.com/nearai/ironclaw) | 6 |  |  |
| [xai-org/grok-build](https://github.com/xai-org/grok-build) | 6 |  |  |
| [agent-infra/sandbox](https://github.com/agent-infra/sandbox) | 5 |  |  |
| [daytonaio/daytona](https://github.com/daytonaio/daytona) | 5 |  |  |
| [e2b-dev/E2B](https://github.com/e2b-dev/E2B) | 5 |  |  |
| [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) | 5 |  |  |
| [mattolson/agent-sandbox](https://github.com/mattolson/agent-sandbox) | 5 |  |  |
| [microsoft/agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit) | 5 |  |  |
| [mksglu/context-mode](https://github.com/mksglu/context-mode) | 5 |  |  |
| [nanocoai/nanoclaw](https://github.com/nanocoai/nanoclaw) | 5 |  |  |

### Протоколы — топ-15 из 136

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [HKUDS/nanobot](https://github.com/HKUDS/nanobot) | 7 |  |  |
| [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack) | 6 |  |  |
| [github/spec-kit](https://github.com/github/spec-kit) | 6 |  |  |
| [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) | 6 |  |  |
| [aaif-goose/goose](https://github.com/aaif-goose/goose) | 5 |  |  |
| [agentgateway/agentgateway](https://github.com/agentgateway/agentgateway) | 5 |  |  |
| [charmbracelet/crush](https://github.com/charmbracelet/crush) | 5 |  |  |
| [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | 5 |  |  |
| [clawdotnet/openclaw.net](https://github.com/clawdotnet/openclaw.net) | 5 |  |  |
| [cuga-project/cuga-agent](https://github.com/cuga-project/cuga-agent) | 5 |  |  |
| [Gentleman-Programming/engram](https://github.com/Gentleman-Programming/engram) | 5 |  |  |
| [github/copilot-cli](https://github.com/github/copilot-cli) | 5 |  |  |
| [google-labs-code/design.md](https://github.com/google-labs-code/design.md) | 5 |  |  |
| [matevip/mateclaw](https://github.com/matevip/mateclaw) | 5 |  |  |
| [MemPalace/mempalace](https://github.com/MemPalace/mempalace) | 5 |  |  |

### Безопасность — топ-15 из 37

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [Infisical/agent-vault](https://github.com/Infisical/agent-vault) | 6 |  |  |
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | 6 |  |  |
| [sd0xdev/sd0x-dev-flow](https://github.com/sd0xdev/sd0x-dev-flow) | 5 |  |  |
| [SponsioLabs/Sponsio](https://github.com/SponsioLabs/Sponsio) | 5 |  |  |
| [aliasrobotics/cai](https://github.com/aliasrobotics/cai) | 4 |  |  |
| [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) | 4 |  |  |
| [emcie-co/parlant](https://github.com/emcie-co/parlant) | 4 |  |  |
| [frumu-ai/tandem](https://github.com/frumu-ai/tandem) | 4 |  |  |
| [Kong/kong](https://github.com/Kong/kong) | 4 |  |  |
| [NVIDIA/SkillSpector](https://github.com/NVIDIA/SkillSpector) | 4 |  |  |
| [Portkey-AI/gateway](https://github.com/Portkey-AI/gateway) | 4 |  |  |
| [TracecatHQ/tracecat](https://github.com/TracecatHQ/tracecat) | 4 |  |  |
| [ucsandman/DashClaw](https://github.com/ucsandman/DashClaw) | 4 |  |  |
| [Yuan-lab-LLM/ClawManager](https://github.com/Yuan-lab-LLM/ClawManager) | 4 |  |  |
| [0sec-labs/0](https://github.com/0sec-labs/0) | 1 |  |  |

### Наблюдаемость — топ-15 из 23

| Проект | Списков | Коммит | Признаки |
|---|---|---|---|
| [peteromallet/desloppify](https://github.com/peteromallet/desloppify) | 7 |  |  |
| [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | 6 |  |  |
| [comet-ml/opik](https://github.com/comet-ml/opik) | 5 |  |  |
| [langfuse/langfuse](https://github.com/langfuse/langfuse) | 5 |  |  |
| [traceloop/openllmetry](https://github.com/traceloop/openllmetry) | 5 |  |  |
| [volcengine/OpenViking](https://github.com/volcengine/OpenViking) | 5 |  |  |
| [walkinglabs/learn-harness-engineering](https://github.com/walkinglabs/learn-harness-engineering) | 5 |  |  |
| [Arize-ai/openinference](https://github.com/Arize-ai/openinference) | 4 |  |  |
| [cordum-io/cordum](https://github.com/cordum-io/cordum) | 4 |  |  |
| [latitude-dev/latitude-llm](https://github.com/latitude-dev/latitude-llm) | 4 |  |  |
| [raga-ai-hub/RagaAI-Catalyst](https://github.com/raga-ai-hub/RagaAI-Catalyst) | 4 |  |  |
| [SafeAI-Lab-X/ClawKeeper](https://github.com/SafeAI-Lab-X/ClawKeeper) | 4 |  |  |
| [Yuyz0112/claude-code-reverse](https://github.com/Yuyz0112/claude-code-reverse) | 4 |  |  |
| [cybernetix-lab/moss-harness](https://github.com/cybernetix-lab/moss-harness) | 1 |  |  |
| [evilmartians/agent-prism](https://github.com/evilmartians/agent-prism) | 1 |  |  |

### Закрытые продукты

- **amp** — Sourcegraph Amp: agentic coding with subagents/oracle
- **claude-managed-agents** — Anthropic Managed Agents: hosted loop + per-session sandbox, outcomes graded against rubrics
- **cursor** — Cursor agents / background agents
- **devin** — Cognition Devin: autonomous SWE agent with own environment
- **factory-droids** — Factory Droids: enterprise coding agents
- **grok-bot** — Grok Bot: multiple persistent bots with own computers, separate responsibilities, permission requests
- **jules** — Google Jules: async coding agent
- **manus** — Manus: general autonomous agent with cloud sandbox
- **openai-dots** — OpenAI Dots: always-on persistent agent coworkers with own cloud computer; ChatGPT Space for teams (DevDay 2026)

### Заявленные результаты на бенчмарках (из README, не проверены)

- [AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness): terminal-bench 2.1 · 24%
- [bitrouter/bitrouter](https://github.com/bitrouter/bitrouter): terminal-bench 2.1**, bitrouter cut api cost by **40.9%
- [Darwin-Agent/HarnessX](https://github.com/Darwin-Agent/HarnessX): gaia accuracy rises from 62%
- [facebookresearch/cca-swebench](https://github.com/facebookresearch/cca-swebench): swe-bench-pro score: **71%
- [ikamensh/kodo](https://github.com/ikamensh/kodo): swe-bench verified: kodo 57%
- [mini-harness/mini-harness](https://github.com/mini-harness/mini-harness): swe-bench verified | 401 / 500 | **80.2%
- [OpenAutoCoder/Agentless](https://github.com/OpenAutoCoder/Agentless): swe-bench lite with 82 fixes (27.3%
- [razzant/ouroboros](https://github.com/razzant/ouroboros): gaia | claude sonnet-5 | 129/165, 78.2%
- [rsi-gear/gear](https://github.com/rsi-gear/gear): terminal-bench 2.1 | 83.2%; terminal-bench 2.1 | 83.8%; terminal-bench 2.1 | 87.4%
- [yohey-w/multi-agent-shogun](https://github.com/yohey-w/multi-agent-shogun): swe-bench 79.6%
