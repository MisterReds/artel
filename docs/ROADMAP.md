# Дорожная карта Artel

Что уже взято из каких харнессов, в каком объёме, и что дальше. Статусы: ✅ сделано и покрыто тестами ·
◐ сделано частично (упрощённая версия) · ⏭ в плане.

## Сколько харнессов изучено

| Глубина | Сколько | Что именно |
|---|---|---|
| Реестр | 1391 проект (1366 живых) | 10 awesome-списков + 42 ручных записи, включая 9 закрытых продуктов |
| Автоматический разбор README | 877 | 26 признаков, даты коммитов, заявленные бенчмарки |
| Ручное чтение README | ~45 | см. таблицу ниже |
| Разбор исходного кода | 9 | Hermes Agent, Prime Agent, DeepSeek Harness, OpenClaw, Agent Skills spec, anthropics/skills, openai/skills, so1 (open-alternative-jev), Jev-Style v3 (скорер и API) |

## Взято и работает

| Механизм | Источник | В Artel | Статус |
|---|---|---|---|
| Скиллы: авто-создание после задачи, правка, read-before-write, ownership | Hermes Agent | `skills.py`, библиотекарь в `manager.py` | ✅ |
| Журнал правок скиллов + откат, lifecycle active→stale→archived, guard по доверию | Hermes Agent | `skills.py` | ✅ |
| Импорт из хабов скиллов | anthropics/skills, openai/skills, Agent Skills spec | `artel skills import` | ✅ |
| Канбан на SQLite: DAG, CAS-захват, попытки | Hermes kanban | `board.py` | ✅ |
| Цикл до результата: continuation, лимит холостых ходов, backoff | Prime Agent | `agent.py`, `manager.py` | ✅ |
| Завершение решает проверка, а не агент (гейты) | Prime Agent autonomous | гейты + ревьюер | ✅ |
| Continual Harness: правки промптов/правил со снапшотами и откатом | Prime Agent `/refine` | `refine.py` | ✅ |
| Микроядро: сервисы, хуки, waterfall, плагины из конфига | DeepSeek Harness (Cordis) | `kernel.py` | ◐ упрощённо: нет обратимых эффектов и выгрузки плагинов на лету, менеджер и доска — ядро, а не плагины |
| Append-only журнал, «всё, что видит модель, записано» | DeepSeek Harness | `events.jsonl` | ✅ |
| Бот с душой: SOUL / IDENTITY / RULES / MEMORY + свой workspace | OpenClaw | `project.py` | ✅ |
| Аудит цели, недостающее → новые задачи | Hermes (root wake-up), LongHorizon-Harness | аудитор | ✅ |
| Портфель проектов, «требует внимания» | Gas Town, Munder Difflin, Agent Deck, Maestro | дашборд | ✅ |
| Аналитика агентов по моделям/ролям, $ на задачу, бюджетные алерты | agent-teams-ai, Agent Deck | `analytics.py` | ✅ |
| Путь задачи по этапам | AgentTeams (TeamHarness) | дашборд | ✅ |
| Внешний агент как рантайм (CLI + MCP-мост) | Scion, Munder Difflin, Omnigent | `runtimes/claude_code.py` | ✅ |
| Эволюция харнесса, проверяемая бенчмарком | auto-harness, Gear, HarnessX, reef | `evolve/` | ✅ |
| Скрытые проверки, которых агенты не видят | Terminal-Bench / Harbor | `evals/tasks` | ✅ |
| Ветки-кандидаты, отбор, теги поколений | Ouroboros (reviewed core evolution) | `evolve/engine.py` | ✅ |
| Абсорбция чужого харнесса | — (своё) | `artel absorb` | ✅ |
| System One решения в узких местах | TypeSafe Jev → so1 / Jev-Style v3 | `decide.py` | ✅ локально на Jev-Style 2B v3; ⏭ ускорить (сейчас 5 с/вызов на CPU) |
| Петля суждений: исправления → правила | kagemusha | refine → RULES.md | ◐ |

## Следующее, по приоритету

| # | Механизм | Источник | Зачем |
|---|---|---|---|
| 1 | Изоляция воркера: git worktree / контейнер на задачу | Archon, ClawTeam, Vibe Kanban, Gas Town | параллельные исполнители не мешают друг другу |
| 2 | Задача как машина состояний с исполняемыми гейтами переходов | StateM (95.3% Terminal-Bench 2.1), Archon | меньше срывов на длинных задачах |
| 3 | Свежий контекст на каждый шаг + проверенное состояние вне контекста | LongHorizon-Harness, Maestro Auto Run | долгие задачи без деградации контекста |
| 4 | Ревью плана до исполнения | Fusion, Archon | ошибки ловятся до траты денег |
| 5 | Ревьюер модели другого вендора | Omnigent (Polly) | меньше слепых пятен (уже настраивается через `models.reviewer`) |
| 6 | Полная модульность «всё — плагин»: менеджер, доска, планировщик — заменяемые сервисы, обратимые регистрации, hot-reload | DeepSeek Harness / Cordis | замена любого узла конфигом |
| 7 | Переписка агентов (mailbox), blackboard | Munder Difflin, Gas Town, ClawTeam | координация без менеджера |
| 8 | Merge-очередь с проверкой и бисекцией | Gas Town (Refinery) | безопасное слияние параллельной работы |
| 9 | Панель судей, adversarial-верификация, A/B скилла | revfactory/harness | качество проверки и скиллов |
| 10 | Witness/Deacon: патруль зависших агентов по всем проектам | Gas Town | авто-восстановление |
| 11 | Матрица прав агентов и людей | Chorus, QM | безопасность |
| 12 | Подписанные квитанции прогона | Bernstein | доказуемость результатов |
| 13 | Чат-поверхности (Telegram, Slack) | Hermes, QM, OpenClaw | управление с телефона |
| 14 | Мульти-рантайм воркеров: Codex, OpenCode, DeepSeek Harness | Omnigent, AgentTeams | выбор лучшего исполнителя под задачу |

Пункты 1–5 планируется прогонять через `artel absorb <источник> --run`: Artel сам переносит функцию в ветку, а бенчмарк решает, принять ли её.
