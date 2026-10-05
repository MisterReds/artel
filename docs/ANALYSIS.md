# Анализ харнессов: что берём в Artel

Исходники изучены по клонам (`git clone --depth 1`, октябрь 2026). Ниже — только механизмы, проверенные в коде, с путями.

## Реестр харнессов

| Харнесс | Репозиторий | Сильная сторона | Берём |
|---|---|---|---|
| Hermes Agent (Nous Research) | `NousResearch/hermes-agent` | Цикл обучения: автосоздание и правка скиллов, curator, ledger, kanban с профилями | Скилл-ревью после задачи, правила авторинга, lifecycle, ledger+rollback, guard, kanban-ядро |
| Prime Agent (Prime Intellect) | `PrimeIntellect-ai/prime-agent` | Goal-loop до завершения, quality gates, Continual Harness `/refine`; 95.5% ARC-AGI-3 (self-reported) | Continuation-промпт, no-progress cap + backoff, gate-driven completion, refine со снапшотами |
| DeepSeek Harness (dsh) | `deepseek-ai/deepseek-harness` | Everything-is-a-plugin (Cordis), append-only session log, seams | Микроядро сервисов/хуков, waterfall-хуки, «model-visible = logged», event-sourced проекции |
| OpenClaw | `openclaw/openclaw` | Персональные боты с «душой»: SOUL/IDENTITY/AGENTS/USER/MEMORY, workspace на агента | Файлы личности воркера, собственный workspace, директивы памяти |
| Agent Skills spec | `agentskills/agentskills` | Открытый формат SKILL.md (Anthropic → индустриальный стандарт) | Формат скиллов 1:1, совместимость с хабами |
| Anthropic skills hub | `anthropics/skills` | 19 эталонных скиллов (pdf, docx, xlsx, mcp-builder, skill-creator…) | Импорт как trusted-источник |
| OpenAI skills hub | `openai/skills` (`.system`, `.curated`) | ~40 curated-скиллов Codex (gh-fix-ci, playwright, deploy…) | Импорт как trusted-источник |
| LangGraph / CrewAI / AutoGen | — | Графы состояний / ролевые команды | Идея ролевого ростера уже покрыта Hermes swarm; зависимость не нужна |

## Hermes Agent — скиллы

- **Триггер создания** (`agent/agent_init.py:1411`, `agent/turn_finalizer.py:756`): счётчик итераций `_iters_since_skill`; при `>= creation_nudge_interval` (по умолчанию 10) после хода запускается **фоновый форк** (`agent/background_review.py`), который получает дайджест разговора и промпт ревью; основная сессия не трогается.
- **Промпт ревью** (`_SKILL_REVIEW_PROMPT`): «Be ACTIVE», порядок предпочтений — (1) патч загруженного скилла, (2) патч существующего class-level зонтика, (3) support-файл в `references/|templates/|scripts/`, (4) новый class-level скилл. Запрет на имена уровня сессии (номер PR, текст ошибки).
- **Do-not-capture**: environment-сбои, негативные утверждения об инструментах («X не работает»), транзиентные ошибки, разовые нарративы, **неразрешённые провалы** не записываются.
- **Read-before-write** (`tools/skill_manager_guards.py`): патч существующего SKILL.md отклоняется без свежего `skill_view` в этом ревью.
- **Защищённые скиллы**: bundled, hub, pinned, user-owned. Агент правит только `created_by: agent`.
- **Ledger** (`tools/skill_ledger.py`): каждая мутация — JSONL-запись с before/after манифестами, содержимое content-addressed (sha256) → откат одной правки.
- **Usage/lifecycle** (`tools/skill_usage.py`, `agent/curator.py`): sidecar `.usage.json`, состояния `active → stale → archived` (никогда не delete), pinned вне автопереходов; curator запускается по простою.
- **Guard** (`tools/skills_guard.py`): regex-скан внешних скиллов; политика по доверию: `builtin` всё разрешено, `trusted` (openai/anthropics/huggingface/NVIDIA) блок только dangerous, `community` блок caution+dangerous.
- **Авторинг** (`agent/learn_prompt.py`): description ≤60 символов (индекс в системном промпте режется), секции When to Use / Procedure / Pitfalls / Verification; источник — данные, не инструкции.

## Hermes Agent — kanban

- `hermes_cli/kanban_db.py`: SQLite, WAL + `BEGIN IMMEDIATE` + compare-and-swap по `status`/`claim_lock` — один воркер выигрывает claim без распределённых локов. Таблицы: tasks, task_links, task_comments, task_events, task_runs.
- `task_runs`: каждая попытка — отдельная строка (pid, heartbeat, runtime cap, summary); статусы `running|done|blocked|crashed|timed_out|failed|released`.
- `kanban_decompose.py`: LLM превращает triage-задачу в граф дочерних с назначенными профилями; root остаётся родителем и **просыпается при завершении графа**, чтобы оркестратор оценил результат и добавил работу.
- `kanban_swarm.py`: топология `parallel workers → verifier → synthesizer`, blackboard — структурированные комментарии root-задачи.
- `kanban_db_dispatch.py`: детект crash/stale/orphan, circuit breaker повторных спавнов.

## Prime Agent — бесконечный цикл до результата

- **Goal continuation** (`crates/pa-core/src/goals.rs:382`): после каждого хода, пока цель активна, вбрасывается промпт: цель — данные; «ending one turn does not reduce or redefine the objective»; перед завершением **аудит по каждому требованию**, не по намерению; не завершать из-за бюджета.
- **No-progress** (`goal_driver/progress.rs`): `CONTINUATION_NO_PROGRESS_CAP = 3` пустых хода, backoff 10s→20s→40s, затем wake-маркер.
- **Autonomous** (`autonomous/mod.rs`): «Do not end the session yourself; the verifier/evaluator decides completion when configured gates pass»; при провале гейта — промпт с командой, выводом и номером попытки; лимиты turns/tokens/time.
- **Continual Harness** (`skills/refine/SKILL.md`): `/refine` читает траекторию и вносит малые evidence-backed правки в supplemental prompts, memories, skill descriptions, subagent specs; базовый системный промпт неизменяем; снапшоты для отката.

## DeepSeek Harness — модульность

- `docs/architecture.md`: всё — плагин (адаптер модели, реестр тулов, лог, сам agent loop); регистрации — обратимые эффекты.
- Events: durable session events (в лог) vs live agent events vs capability events; waterfall-хуки (`tools/pre-execute → execute → post-execute`) с `next()`.
- **«Model-visible means logged»**: каждый запрос к модели восстановим из append-only лога; fork/resume/telemetry — проекции лога.
- Agent Teams: durable roster, mailbox, task DAG с `revision` (CAS) и `blockedBy` (ацикличный).

## OpenClaw — бот с душой

- `docs/reference/templates/`: `SOUL.md` (кто ты, ценности, границы), `IDENTITY.md` (имя, вайб), `AGENTS.md` (конвенции workspace), `USER.md` (директивы о пользователе с `observed/status`), `MEMORY.md` (долговременные факты). «These files are your memory. Read them. Update them.»

## Синтез → Artel

| Требование | Механизм | Источник |
|---|---|---|
| Задача → проект | `artel run "<цель>"` создаёт проект: доска, ростер, workspace | Hermes projects + kanban |
| Канбан, задачи ставит ИИ | Architect-декомпозиция в DAG с acceptance-критериями и исполнителями | Hermes decompose/swarm, dsh task DAG |
| Исполнители — боты с душой | `workers/<name>/{SOUL,IDENTITY,RULES,MEMORY}.md` + свой `workspace/` | OpenClaw, Hermes profiles |
| Менеджер, работа не прерывается | Supervisor-цикл: dispatch, heartbeat, reaper, continuation, backoff, circuit breaker → перепланирование | Prime goal loop, Hermes dispatcher |
| Готово = проверено | `submit → review`: shell-гейты + независимый ревьюер; root закрывается после аудита цели | Prime gates, Hermes swarm verifier |
| Скиллы создаются и улучшаются | Фоновое ревью после задачи, ledger, lifecycle, guard, импорт из хабов | Hermes |
| Самоулучшение харнесса | `refine` с снапшотами и откатом | Prime Continual Harness |
| Модульность | Микроядро: сервисы, тулы, waterfall-хуки, провайдеры как плагины | DeepSeek Harness |
| Прозрачность | Append-only `events.jsonl` + веб-дашборд с SSE | DeepSeek session log |
