# Дорогая модель — только менеджер

Цель: проекты, которые идут часами и днями и не упираются в лимиты дорогой модели. Дорогая модель (Opus) вызывается
редко и с крошечным контекстом; весь объём работы, истории и рутины — на средней (Sonnet) и дешёвой (Haiku).

## Роли и модели

| Роль (в Artel) | Уровень | Claude Code | Что делает | Чего не делает |
|---|---|---|---|---|
| Manager: `planner`, `manager`, `auditor` | expensive | opus | цель → задачи; решение по застрявшей задаче; финальная приёмка | не пишет код, не читает логи и файлы целиком, не тестирует |
| Architect: план внутри `planner` | expensive → medium при деградации | opus/sonnet | интерфейсы, схемы задач | — |
| Executor: `worker`, `tester` | medium | sonnet | код, тексты, тесты, поиск | не принимает свою же работу |
| Critic: `reviewer` | medium | sonnet | проверка по критериям и автоматическим проверкам | не пишет в результаты |
| Curator: `librarian`, `refiner`, `doctor`, сборщик брифа | cheap | haiku | навыки, сжатие, диагностика, бриф для менеджера | — |

Уровни и имена моделей задаются в `artel.yaml` (`tiers:`); роль, у персоны которой указана своя модель, остаётся на ней.

```yaml
tiers:
  roles: {planner: expensive, manager: expensive, auditor: expensive, worker: medium, reviewer: medium,
          librarian: cheap, refiner: cheap, doctor: cheap}
  models:
    claude_code: {cheap: haiku, medium: sonnet, expensive: opus}
    native: {cheap: <id модели>, medium: <id модели>, expensive: <id модели>}   # имена провайдера — только здесь
    codex: {}                     # пусто — модель аккаунта Codex
  expensive_budget_tokens: 600000 # на проект
  degrade_at: 0.9                 # доля бюджета, после которой менеджер работает на medium
```

## Схемы

**Manager Input** — всё, что видит дорогая модель (≤ 3200 символов ≈ 800 токенов, константа относительно длины
проекта; `Manager.manager_brief`, `Manager.audit_brief`):

```json
{"goal": "≤800 символов", "constraints": ["…"], "success_criteria": ["…"],
 "facts": ["≤7 фактов: вердикты ревью, результаты проверок, счётчики доски"],
 "refs": ["shared/путь", "task:t-…", "run:42"], "open_questions": ["…"],
 "budget": {"expensive_used": 120000, "expensive_limit": 600000}}
```

**Task Contract** — задача на доске (`board.add_task`): узкий вход, критерии, проверяемые командой критерии и
исполнитель.

```json
{"id": "t-…", "title": "…", "body": "точный вход", "criteria": ["проверяемые"], "checks": ["команды-гейты"],
 "assignee": "воркер", "depends_on": ["t-…"], "output": "task_submit{summary, evidence}", "max_turns": 40}
```

**Executor Result** — только структура, через терминальный инструмент `task_submit`:

```json
{"summary": "что сделано", "evidence": "команды и их вывод, пути", "files": ["shared/…"]}
```

**Escalation Request** — застрявшая задача (`blocked`) или отказ ревью дважды → менеджеру уходит бриф и приходит
одно решение `replan_decision`:

```json
{"task": "t-…", "reason": "critic_failed | conflict | architecture | risk", "brief": "Manager Input",
 "options": ["retry", "reassign", "split", "cancel", "escalate"],
 "answer": {"action": "split", "rationale": "…", "subtasks": ["Task Contract…"]}}
```

## Кто что решает

| Событие | Кто | Дорогая модель? |
|---|---|---|
| новая цель → задачи | planner | да, один раз |
| выполнение задачи | worker | нет |
| проверка задачи | reviewer + гейты | нет |
| отказ ревью, повтор | worker с отзывом ревьюера | нет |
| задача застряла (ошибки, отказы, конфликт) | manager | да, по брифу |
| все задачи готовы | auditor | да, по брифу, ≤2 точечные проверки |
| сбой окружения | doctor | нет (cheap) |
| навыки после задачи | librarian | нет (cheap) |

## Оркестратор (псевдокод; в коде — `Manager.run` и `artel/routing.py`)

```text
plan = run(role=planner, tier=tier_for(planner), input=goal + skills_index)      # expensive, один раз
loop until goal done or stop:
    for task in ready: run(worker, tier=medium, input=TaskContract(task))       # параллельно
    for task in review: run(reviewer, tier=medium, input=task + gates)
    for task in blocked: run(manager, tier=tier_for(manager), input=manager_brief(task))   # ≤800 токенов
    if no open work: run(auditor, tier=tier_for(auditor), input=audit_brief())           # ≤800 токенов
    after each run: routing.record(project, tier, usage)                         # бюджет по уровням
tier_for(role):
    tier = roles[role]
    if tier == expensive and expensive_used ≥ degrade_at × budget: tier = medium  # работа продолжается
```

## Бюджет, эскалация, деградация

- **Бюджет дорогой модели** — `tiers.expensive_budget_tokens` на проект; учитывается по событиям каждого запуска
  (`project.meta.tiers`, `tier/usage`).
- **Деградация, не остановка.** На `degrade_at` (90%) менеджер, планировщик и аудитор переходят на medium; проект
  идёт дальше. Каждый такой запуск отмечен `degraded: true`.
- **Эскалация к менеджеру** — только застрявшая задача: исчерпаны повторы, повторные отказы ревью, конфликт слияния.
  Обычный отказ ревью возвращается исполнителю без менеджера.
- **Лимит провайдера** — задача ждёт сброса или уходит на другой подключённый рантайм; аудит ждёт так же
  (растущая пауза, затем человек).
- **Повторы** — исполнитель повторяет с отзывом ревьюера; после исчерпания — бриф менеджеру.

## Метрики (`routing.efficiency`, «Воркеры» в режиме разработчика)

- доля токенов дорогой модели и доля её вызовов (ориентир — единицы процентов вызовов);
- токены на принятую задачу;
- эскалаций на задачу;
- успех: принятые / закрытые задачи;
- часов работы до первого лимита провайдера (или всего, если лимита не было);
- число запусков в деградации.

## Что сделано и что дальше

Сделано (MVP):
1. уровни моделей по ролям для Claude Code, Codex (по именам из конфига) и своего цикла; заменяют старые `cheap_roles`;
2. бюджет дорогой модели на проект и деградация на medium вместо остановки;
3. вход менеджера постоянного размера: бриф застрявшей задачи и бриф приёмки (вердикты ревью, проверки, ссылки) вместо
   всей доски, всех запусков и результатов целиком; аудитор не тестирует сам, а решает по уже собранным доказательствам;
4. метрики эффективности по проектам.

Дальше:
1. **Task Contract из сырого запроса дешёвой моделью** до планирования (уточнение цели, критерии, ограничения) —
   чтобы планировщик получал готовый контракт, а не прозу.
2. **Curator на дешёвой модели** для брифа, когда детерминированной выжимки мало (конфликт требований).
3. **Rolling summary для долгих сессий воркера** в своём цикле (в Claude Code и Codex сжатие делает сам CLI).
4. **Эскалация исполнителя cheap → medium** для задач, которые дешёвая модель дважды провалила.
5. **Метрика «часов до лимита» в реальном многочасовом прогоне** на подписке — главный приёмочный тест.
