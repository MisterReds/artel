# Artel

Мультиагентный харнесс: цель → проект → канбан → команда ботов с «душой» → менеджер, который не останавливается, пока аудит не подтвердит, что цель достигнута. Скиллы создаются и улучшаются сами. Каждое действие пишется в журнал и видно в дашборде.

Откуда взят каждый механизм:
- [`docs/HARNESS_REGISTRY.md`](docs/HARNESS_REGISTRY.md) — полный реестр (1391 проект из 10 awesome-списков + ручные дополнения, проверены на существование, 877 README разобраны автоматически, ~40 вручную) и таблица «что взято / что следующее».
- [`docs/ANALYSIS.md`](docs/ANALYSIS.md) — разбор исходного кода Hermes Agent, Prime Agent, DeepSeek Harness, OpenClaw и хабов скиллов.

## Быстрый старт

```bash
pip install -e .
export ANTHROPIC_API_KEY=...            # или `ant auth login`
artel run "Сделай CLI-утилиту на Python: CSV → JSON, с тестами и README" --web
# дашборд: http://127.0.0.1:8765
```

Без ключа, детерминированно (проверка всей механики): `artel run "любая цель" --provider scripted`.

DeepSeek / OpenRouter / Ollama: в `~/.artel/artel.yaml`
```yaml
provider: openai_compat
openai_compat: {base_url: https://api.deepseek.com/v1, api_key_env: DEEPSEEK_API_KEY}
models: {default: deepseek-chat}
```

## Как это работает

```
цель ──► Architect ──► submit_plan: ростер ботов + DAG задач с критериями и гейтами
                         │
             ┌───────────▼────────────── Manager (цикл до результата) ───────────────┐
             │ todo ─► ready ─► running ─► review ─► done                            │
             │                   │  worker: агентный цикл, свой workspace, SOUL/RULES/MEMORY
             │                   │  конец хода без task_submit → continuation-промпт  │
             │                   │  3 хода без действий → stalled → backoff 10s·2ⁿ    │
             │                   └► review: shell-гейты + независимый ревьюер         │
             │ отказ / N неудач → blocked → replan: retry | reassign | split | cancel | escalate
             │ всё done → аудит цели → не достигнута? → новые задачи → снова в работу │
             └───────────────────────────────────────────────────────────────────────┘
после принятой задачи (≥5 tool calls) → Librarian: создать/исправить скилл (ledger, откат)
серия неудач и финал → Refiner: правки prompt_notes / RULES.md со снапшотами
```

| Модуль | Назначение |
|---|---|
| `artel/kernel.py` | Микроядро: сервисы, хуки, waterfall (`tool/execute`, `llm/request`), реестр тулов, `events.jsonl` |
| `artel/board.py` | Канбан на SQLite: DAG, CAS-claim, попытки (`runs`), комментарии-blackboard |
| `artel/project.py` | Проект, конфиг, персоны воркеров (`SOUL.md`, `IDENTITY.md`, `RULES.md`, `MEMORY.md`, `workspace/`) |
| `artel/agent.py` | Общий агентный цикл: терминальные тулы, continuation, no-progress cap, бюджеты, лог каждого запроса |
| `artel/manager.py` | Планирование, диспетчеризация, ревью, перепланирование, аудит, обучение, refine, восстановление после рестарта |
| `artel/skills.py` | Библиотека в формате Agent Skills: валидация, guard по уровням доверия, ledger + rollback, lifecycle, импорт из хабов |
| `artel/refine.py` | Continual Harness: малые правки с доказательствами, журнал, откат |
| `artel/plugins/*` | Провайдеры (Anthropic SDK, OpenAI-compatible, scripted), тулы (shell/файлы/память, скиллы, доска) |
| `artel/web/` | Дашборд: проекты, команда, доска, живая лента событий (SSE), файлы результата, сообщения исполнителю |

### Гарантии завершения

- Задача закрывается только через `task_submit` с доказательствами; правка кода без запуска — отказ.
- `done` ставит ревьюер после прохождения гейтов, не воркер.
- Корневая цель закрывается только аудитом; неполнота превращается в новые задачи.
- Остановка возможна только по: аудиту, бюджету (`budgets.max_total_tokens`, `max_hours`), команде человека, или когда все оставшиеся пути ждут человека (`escalate`). `artel comment <проект> <задача> "..."` снимает такой блок, `artel resume` продолжает.

### Скиллы

```bash
artel skills import anthropics/skills          # trusted: guard блокирует только dangerous
artel skills import openai/skills
artel skills import some/repo                  # community: блокирует caution и dangerous
artel skills list | view <name> | ledger [<name>] | rollback <entry> | pin <name> | curate
```
Агенты правят только свои (`origin: agent`), незакреплённые скиллы; правка требует свежего `skill_view` (read-before-write). Неиспользуемые скиллы: `active → stale (30 дн) → archived (90 дн)`, удаления нет.

### Расширение (плагины)

```python
# mypkg/approval.py ; в artel.yaml: plugins: [mypkg.approval]
def setup(kernel):
    def gate(ctx, next_):
        if ctx["name"] == "bash" and "rm -rf" in ctx["args"]["command"]:
            return "denied by policy", True
        return next_()
    kernel.on("tool/execute", gate, priority=10)
```

## Дашборд: много проектов одновременно

`artel web` → http://127.0.0.1:8765. Сервер ведёт любое число проектов параллельно; у каждого своя ссылка `#/p/<slug>`.

- **Портфель** — все проекты: этап (планирование → исполнение → ревью → аудит → готово), сегментный прогресс по статусам задач, кто из ботов чем занят, последнее событие, стоимость, бюджетные алерты; сводка «требует внимания» по всем проектам.
- **Проект** — шкала этапов, команда со статистикой, канбан, живая лента событий, путь каждой задачи по этапам с временем и исполнителем, доказательства, запуски, сообщение исполнителю, файлы результата.
- **Агенты** — как справляются модели, роли и конкретные боты во всех проектах: принято, с первой попытки, отказы ревью, доля срывов, токены и $ на принятую задачу.
- **Реестр харнессов** — 1391 проект с фильтрами по типу, признакам и поиском.
- **Скиллы** — общая библиотека и история правок каждого скилла.

## Команды

```
artel run "<цель>" [--web] [--provider P] [--model M] [--parallel N] [--max-tokens-total T]
artel resume <проект>          artel status [<проект>]        artel log <проект> -f
artel comment <проект> <задача> "<текст>"                      artel web
artel refine history|rollback <проект> [<id>]
```

## Тесты

`pytest -q` — 13 тестов: портфель и аналитика агентов по нескольким проектам, полный цикл цель→done, отказ ревью, stall→continuation→retry, раунды аудита, разблокировка человеком и resume, откат refine, CAS-claim под конкуренцией, циклы DAG, guard, lifecycle, импорт реальных хабов Anthropic/OpenAI.

## Состояние и дальнейшие шаги

Работает и покрыто тестами на scripted-провайдере. Запрос к Claude проверен на фейковом эндпоинте (формат, beta-заголовок fallbacks, сохранение thinking-блоков). Прогон с реальной моделью не выполнялся — в окружении разработки нет ключа.

Следующее: изоляция воркеров (git worktree / контейнер на воркера), компакция длинных прогонов, бенчмарк-раннер (SWE-bench / ARC-AGI-3), семантический поиск скиллов, mailbox между воркерами, параллельные варианты решения с выбором лучшего (best-of-N).
