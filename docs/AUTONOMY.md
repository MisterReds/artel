# Автономная, прозрачная, независимая от провайдера система

Три цели, в порядке важности для архитектуры:

1. **Долгий горизонт.** Дорогая модель решает редко и по крошечному брифу, остальное — дешёвые модели.
2. **Автономия под контролем.** Из идеи система сама доводит дело до результата, но человек видит каждое решение и
   может вмешаться в любой момент.
3. **Независимость от провайдера.** Провайдер — сменный бэкенд. Ни одна часть системы, кроме адаптера, не знает, кто
   отвечает.

Документ продолжает docs/ORCHESTRATION.md (уровни моделей, бриф менеджера, бюджет). Для каждого элемента отмечено
состояние в Artel: **есть**, **частично**, **нет**.

---

## 1. Слои

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ L6 Интерфейсы: дашборд · Telegram · CLI · API      (видеть, вмешиваться)     │
├──────────────────────────────────────────────────────────────────────────────┤
│ L5 Steward: Intent, режим автономии, точки подтверждения, журнал решений,    │
│    статусы шагов, бюджет, команды вмешательства                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ L4 Оркестратор (детерминированный код, не модель): цикл, доска, зависимости, │
│    повторы, эскалации, приёмка                                               │
├──────────────────────────────────────────────────────────────────────────────┤
│ L3 Роли: Manager · Architect · Executor · Curator · Critic                   │
│    (промпты, схемы ввода/вывода — без знания провайдера)                     │
├──────────────────────────────────────────────────────────────────────────────┤
│ L2 Роутер: роль → Capability Profile → конкретная модель                     │
│    (качество ≥ порога, дешевле, доступна, в бюджете; fallback)               │
├──────────────────────────────────────────────────────────────────────────────┤
│ L1 Provider Adapter Layer: единый вызов, нормализованные usage/cost/latency, │
│    ошибки по классам (limit · auth · outage · bad_request)                   │
│    Anthropic · OpenAI · Google · OpenRouter · Ollama · vLLM · llama.cpp ·    │
│    агентные CLI (Claude Code, Codex, ACP) как адаптеры «исполнителя»         │
├──────────────────────────────────────────────────────────────────────────────┤
│ L0 Хранилища вне контекста: журнал событий (append-only), артефакты по       │
│    путям/хэшам, снимки для отката, rolling summary, кэш эмбеддингов          │
└──────────────────────────────────────────────────────────────────────────────┘
```

Правило зависимостей: слой видит только соседний снизу. Имя вендора встречается только в L1 и в конфиге.

| Слой | В Artel |
|---|---|
| L0 | **есть**: `events.jsonl` (только дописывается), `shared/`, снимки `snapshot.git`/worktree, кэш эмбеддингов; rolling summary — **частично** (сжатие контекста в своём цикле) |
| L1 | **частично**: `LLMResponse` нормализует ответы Anthropic/OpenAI-совместимых API (OpenRouter, Ollama, vLLM, llama.cpp через openai_compat); агентные CLI — рантаймы; классы ошибок — `failures.classify` |
| L2 | **частично**: `routing.py` (роль → уровень → модель) — но имена моделей зашиты в коде по умолчанию; Capability Profile — **нет** |
| L3 | **есть** (planner, manager, auditor, worker, reviewer, librarian, intake…) |
| L4 | **есть** (`Manager.run`) |
| L5 | **частично**: журнал, статусы задач, бюджет, политика команд; Intent с подтверждением, режимы автономии, журнал решений «зачем» — **нет** |
| L6 | **есть**: дашборд, Telegram, CLI |

---

## 2. Роли через capability

| Роль | Профиль | Решает | Не делает | В Artel |
|---|---|---|---|---|
| Manager | reasoning: high | Intent, декомпозиция, решения по эскалациям, приёмка | код, чтение сырых данных, рутина | planner + manager + auditor |
| Architect | reasoning: medium-high | план, интерфейсы, схемы задач | исполнение | planner при простом запросе (medium) |
| Executor | reasoning: medium/low, throughput: high | код, тексты, тесты, поиск | приёмку своей работы | worker, tester |
| Curator | reasoning: low, cost: min | сбор и сжатие контекста, бриф ≤800 токенов | решения | intake (контракт); бриф — кодом |
| Critic | reasoning: medium | проверка по чек-листу и гейтам | правку результатов | reviewer + гейты |
| Steward | reasoning: medium (часто — код без модели) | Intent-дрейф, бюджет, точки подтверждения, журнал | работу по задачам | **нет** как роли; части — guardian, budget, policy |

Steward — прежде всего код, а не модель: учёт, статусы, правила подтверждения детерминированы. Модель нужна ему
только для двух вопросов — «не ушли ли мы от Intent?» и «объясни это решение человеку».

---

## 3. Схемы

**Capability Profile** — требование роли, без вендора:

```json
{"id": "manager", "reasoning": "high", "max_cost_per_mtok_usd": 30, "min_context_tokens": 32000,
 "max_latency_s": 120, "needs": ["tool_use", "json_output"], "prefers": ["prompt_cache"],
 "fallback": ["architect", "executor"], "budget_class": "expensive"}
```

**Provider Adapter / Model Card** — что умеет конкретная модель (в конфиге, не в коде):

```json
{"provider": "anthropic", "kind": "api | agent_cli | local", "model": "<id модели у провайдера>",
 "reasoning": "high", "context_tokens": 200000, "cost_in_per_mtok": 15, "cost_out_per_mtok": 75,
 "latency_class": "slow", "capabilities": ["tool_use", "json_output", "prompt_cache"],
 "auth": {"env": "ANTHROPIC_API_KEY"} , "limits": {"rpm": 50}, "available": true}
```

**Intent** — зачем всё это (подтверждается человеком):

```json
{"id": "intent-1", "idea": "сырой текст пользователя", "intent": "зачем это нужно, 1–2 предложения",
 "success_criteria": ["проверяемые"], "constraints": ["…"], "out_of_scope": ["…"], "assumptions": ["…"],
 "open_questions": ["…"], "risks": ["что может пойти не так"],
 "autonomy": "suggest | act_and_show | ask_at_forks | full", "budget": {"usd": 20, "expensive_tokens": 600000},
 "confirmed_by": "owner | null", "version": 1}
```

**Manager Input** (≤800 токенов, собирает Curator/код):

```json
{"intent_ref": "intent-1@v1", "goal": "…", "constraints": ["…"], "success_criteria": ["…"],
 "facts": ["≤7"], "refs": ["shared/…", "task:t-…", "run:42", "sha256:…"], "open_questions": ["…"],
 "decision_needed": "plan | escalation | acceptance", "budget": {"expensive_left": 480000, "usd_left": 14.2}}
```

**Task Contract**:

```json
{"id": "t-…", "intent_ref": "intent-1@v1", "title": "…", "input": "точный вход и ссылки",
 "criteria": ["…"], "checks": ["команды"], "output_schema": "executor_result.v1", "max_tokens": 40000,
 "capability": "executor", "reversible": true, "depends_on": ["t-…"]}
```

**Executor Result** (только JSON):

```json
{"task": "t-…", "status": "done | blocked | failed", "summary": "…", "evidence": ["команда → вывод (обрезан)"],
 "artifacts": [{"path": "shared/…", "sha256": "…"}], "confidence": 0.8, "blocked_on": null}
```

**Escalation Request**:

```json
{"task": "t-…", "kind": "critic_failed | conflict | architecture | risk | irreversible | budget | uncertainty",
 "to": "manager | owner", "brief": "Manager Input", "options": ["retry", "reassign", "split", "cancel", "ask_owner"],
 "deadline": null}
```

**Decision Log Entry** — одна строка «что, зачем, кто, во что обошлось»:

```json
{"seq": 812, "ts": "…", "actor": "manager", "capability": "manager", "provider": "anthropic",
 "model": "…", "decision": "split t-7 into 3 tasks", "why": "two critic failures on one criterion",
 "alternatives": ["retry with guidance"], "next": "t-7a starts after t-6", "inputs": ["brief:sha256…"],
 "tokens": {"in": 640, "out": 210}, "cost_usd": 0.02, "latency_s": 6.1, "reversible": true,
 "status": "planned | running | done | failed | rolled_back", "intent_ref": "intent-1@v1"}
```

---

## 4. Роутинг

### Кто что решает

| Ситуация | Решает | Модель/профиль | Человек |
|---|---|---|---|
| идея → Intent | Curator (черновик) → Manager (если сложно) | low → high | **подтверждает** |
| Intent → план | Architect/Manager | medium-high / high | видит; в `ask_at_forks` подтверждает развилки |
| задача → исполнение | Executor | medium/low | — |
| проверка | Critic + гейты | medium | — |
| отказ Critic'а | Executor повторяет с отзывом | — | — |
| второй отказ, конфликт, развилка, риск | Manager по брифу | high | при `ask_at_forks` |
| необратимое, деньги, публикация, внешний вызов | **человек** | — | **обязательно** |
| дрейф от Intent | Steward → Manager | medium → high | при изменении цели |
| приёмка | Manager по брифу | high | видит итог |

### Какая модель под задачу

```text
select(profile, budget):
  candidates = [m for m in catalog if m.available and meets(m, profile) and not limited(m)]
  candidates = [m for m in candidates if within_budget(m, profile, budget)]
  if not candidates: candidates = fallback(profile)     # профиль ниже → любой доступный провайдер
  return min(candidates, key=(cost_per_task_estimate, latency, -quality_history))
```

- `meets`: уровень reasoning ≥ требуемого, окно ≥ нужного, есть нужные capability.
- `quality_history`: доля принятых с первой попытки задач по этой модели (Artel уже считает это в аналитике).
- Модель, упёршаяся в лимит, исключается до сброса (`limits.py`, **есть**).

---

## 5. Протокол автономии

1. **Идея → Intent.** Curator делает черновик Intent (у Artel это intake, **есть**); Manager уточняет только при
   сложности high. Intent показывается человеку.
2. **Подтверждение.** Режим `suggest` и любой Intent с `open_questions` ждут ответа. В остальных режимах работа
   начинается сразу, а Intent остаётся видимым и редактируемым.
3. **Самопроверка на каждой развилке** — пять вопросов, ответы идут в журнал решений: «что делаю», «зачем (какой
   критерий Intent)», «как пойму, что готово», «что может пойти не так», «нужно ли спросить».
4. **Работа без человека** — по режиму:

| Режим | Без вопросов | Спрашивает |
|---|---|---|
| `suggest` | ничего | каждый план и каждое действие |
| `act_and_show` | всё обратимое | необратимое |
| `ask_at_forks` | исполнение и проверку | архитектурные развилки, смену плана, эскалации |
| `full` | всё обратимое, включая развилки | необратимое, деньги, публикации, внешние вызовы, смену Intent |

5. **Остановка с вопросом** — только по триггерам: необратимое действие; конфликт с Intent; бюджет превышен;
   уверенность ниже порога (`confidence < 0.6` у Manager); цель меняется. Вопрос — один, с вариантами и рекомендацией.
   Пока ответа нет, всё, что от вопроса не зависит, продолжает работать.

---

## 6. Протокол прозрачности

- **Журнал решений** — каждый вызов Manager, каждая эскалация, каждая смена плана: Decision Log Entry. Рутина
  исполнителей пишется событиями, но в сводку для человека попадает агрегатом.
- **Статусы шагов**: запланирован, выполняется, готов, провален, откачен. Для задач доска уже это показывает (**есть**,
  кроме «откачен»).
- **Панель состояния** (одна страница): Intent и его версия, план с прогрессом, бюджет (дорогой/всего, $), открытые
  вопросы, последние 10 решений с «зачем» и «что дальше», текущий режим автономии.
- **Объяснение по запросу**: «Почему?» у любого решения разворачивает запись журнала. Если нужно — дешёвая модель
  пересказывает её простыми словами (как диагност сейчас, **есть**).
- **Воспроизводимость**: запись хранит хэш входного брифа, модель, параметры. Повторный прогон того же брифа
  возможен и сравним.

---

## 7. Протокол управляемости

| Команда | Действие | Состояние в Artel |
|---|---|---|
| пауза / продолжить | идущие шаги доделываются, новые не стартуют | **есть** как стоп/продолжить; мягкой паузы **нет** |
| сменить Intent | новая версия Intent; Steward помечает задачи, не служащие новой версии; Manager перепланирует дельту | **нет** |
| отменить задачу | задача и зависимые → отменены, worktree сброшен | **частично** (отмена через менеджера) |
| откатить шаг | результаты задачи откатываются к снимку до неё; зависимые возвращаются в очередь | **частично**: снимки и discard есть, кнопки нет |
| режим автономии | вступает со следующего решения | **нет** |
| бюджет | новые лимиты вступают сразу | **частично** (конфиг) |
| вмешаться в план | правка/добавление задачи, комментарий исполнителю | **есть** (комментарий снимает блок) |

Каждая команда сама пишется в журнал решений с автором «owner».

---

## 8. Протокол провайдер-агностичности

**Новый провайдер** = один адаптер + записи в каталоге моделей:

```text
class Adapter:
    def call(request: NormRequest) -> NormResponse      # messages, tools, schema, max_tokens
    def classify(error) -> limit | auth | outage | bad_request
    def usage(raw) -> {input_tokens, output_tokens, cached_tokens}
    def cost(usage, card) -> usd
```

Оркестратор, промпты и схемы не меняются. Агентные CLI (Claude Code, Codex, ACP) — тот же интерфейс уровнем выше:
вход — Task Contract, выход — Executor Result.

**Fallback между провайдерами**: limit или outage → модель исключается до сброса → роутер берёт следующую по тому же
профилю у другого провайдера → нет такой → профиль ниже (деградация) → всё недоступно → задача ждёт сброса, остальные
идут дальше. Каждый переход — запись в журнале. Для подписок Artel это уже работает между рантаймами (**есть**:
auto-переключение при лимите).

**Нормализация метрик**: usage, стоимость, латентность, класс ошибки приводит адаптер; журнал и аналитика видят одну
схему.

**Вендорские фичи** (кэш промпта, extended thinking, computer use) — опциональные capability в карточке модели. Без
них система работает, с ними — дешевле или лучше.

**Локальные модели** — обычные карточки (`kind: local`, `cost: 0`): если карточка удовлетворяет профилю Executor,
Curator или Critic, роутер выберет её без изменений оркестратора.

---

## 9. Оркестратор (псевдокод)

```text
run(idea, mode, budget):
    intent = curator.draft_intent(idea)                      # cheap
    if intent.complexity == high: intent = manager.refine(brief(intent))      # expensive, ≤800 токенов
    steward.publish(intent); if needs_confirmation(intent, mode): wait_owner(intent)
    plan = architect_or_manager.plan(brief(intent))          # профиль по сложности
    board.load(plan); log.decision(plan, why=…, next=…)

    while not intent.done:
        steward.apply_owner_commands()                       # пауза, Intent v2, откат, режим, бюджет
        if steward.paused: wait; continue
        for t in board.ready():
            if t.irreversible and not approved(t): steward.ask_owner(t); continue
            spawn(executor(t.contract), model=router.select("executor", budget))
        for t in board.review(): spawn(critic(t, gates), model=router.select("critic", budget))
        for e in escalations():                              # 2-й отказ, конфликт, развилка, риск
            if mode requires owner for e.kind: steward.ask_owner(e)
            else: decide(manager, curator.brief(e))          # Manager Input ≤800 токенов
        if board.all_done(): accept(manager, curator.audit_brief(intent))
        steward.check_drift(intent); steward.check_budget(); memory.roll_summary()   # O(1) контекст
        after each call: log.decision_or_event(normalized usage, cost, latency, model, why)

router.select(role, budget):
    profile = profiles[role]
    if budget.expensive_used ≥ 0.9 × budget.expensive and profile.budget_class == expensive:
        profile = profiles[profile.fallback[0]]              # деградация, не остановка
    return choose(catalog, profile)                          # §4, с cross-provider fallback
```

---

## 10. Политики

- **Эскалация к Manager** — только при втором провале Critic'а, конфликте требований, архитектурной развилке, риске
  выше порога, уверенности ниже порога.
- **Эскалация к человеку** — точки обязательного подтверждения и триггеры §5.
- **Точки обязательного подтверждения** (во всех режимах): удаление и перезапись вне рабочих папок; `git push`,
  публикации, релизы; платежи и платные API сверх бюджета; письма, сообщения и иные внешние вызовы от имени
  человека; изменение Intent. Технически это политика команд (**есть**, `policy.yaml` с решением `prompt`) плюс
  список «внешних» инструментов MCP.
- **Бюджет**: деньги на проект; токены дорогого профиля; лимит на задачу (max_tokens контракта). На 90% дорогого
  бюджета — деградация (**есть**); на 100% денег — пауза с вопросом.
- **Деградация**: high → medium-high → medium; качество может просесть, Critic и гейты остаются. Работа не
  останавливается.
- **Cross-provider fallback**: §8.

---

## 11. Метрики

| Метрика | Как | В Artel |
|---|---|---|
| доля токенов дорогого профиля | tokens(expensive) / tokens(all) | **есть** |
| доля вызовов дорогого профиля | calls(expensive) / calls(all) | **есть** |
| токены на решённую задачу | tokens / accepted tasks | **есть** |
| escalation rate | эскалации / задачи | **есть** (решения менеджера) |
| success rate | принятые / закрытые | **есть** |
| часы до лимита | время до первого limit | **есть** |
| доля автономных шагов | шаги без вопроса человеку / все шаги | **нет** |
| вмешательства человека | команды owner за проект | **нет** |
| стоимость задачи по провайдерам | cost по provider / accepted tasks | **частично** (по моделям) |

---

## 12. MVP-план встраивания в Artel

Порядок — по отдаче на единицу работы:

1. **Каталог моделей и Capability Profile** — снять имена вендоров с кода (`routing.py`). Профили ролей и карточки
   моделей лежат в `artel.yaml`, роутер выбирает по §4. Существующие рантаймы становятся адаптерами каталога.
2. **Intent с подтверждением** — intake уже делает черновик. Добавить версию, подтверждение в дашборде и Telegram,
   «Изменить Intent». Режим `suggest` ждёт подтверждения.
3. **Журнал решений** — Decision Log Entry для каждого вызова Manager и каждой команды человека; панель «последние
   решения: зачем → что дальше».
4. **Режимы автономии и точки подтверждения** — четыре режима; необратимые действия через `policy: prompt` и
   список внешних инструментов.
5. **Управление** — мягкая пауза, откат задачи по снимку (кнопка), отмена с зависимыми, бюджет из дашборда.
6. **Cross-provider fallback по профилю** — сейчас переключение идёт между рантаймами. Сделать его по каталогу,
   включая локальные модели в роли Executor и Curator.
7. **Steward-проверка дрейфа** — дешёвая модель раз в N решений сравнивает план с Intent.
8. **Приёмочный прогон** — многочасовой проект на подписке с замером всех метрик §11. Без него цели не доказаны.

Каждый шаг — отдельная поставка с тестами. Проверка «провайдер не важен»: тот же проект проходит на двух разных
провайдерах и на локальной модели в роли Executor.
