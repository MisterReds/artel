# Целевая архитектура Artel: гексагональное ядро, FSM, амплификация, переписывание

Продолжение docs/ORCHESTRATION.md (уровни моделей, бриф менеджера) и docs/AUTONOMY.md (Intent, автономия,
прозрачность, независимость от провайдера). Здесь — **как это построить**: модули, парадигмы, порты, автомат
состояний, контракты, правила, план. Раздел «Предложение о переписывании» опирается на замеры текущего кода.

## 0. Усиленный Intent этой работы

| | Исходно | Усилено |
|---|---|---|
| Altitude | «спроектируй архитектуру» | проекты доходят до результата уровня ведущего специалиста без присмотра, лимит дорогой модели не кончается, человек в любой момент понимает и меняет ход |
| Depth | схемы и правила | доказательства на данных: тот же набор задач проходит на двух провайдерах и локальной модели; выигрыш амплификации измерен A/B на отложенных задачах внешним Critic'ом; часы до лимита — из реального прогона |
| Range | архитектура ПО | распределённые системы (event sourcing, FSM), SRE (бюджеты как SLO), экономика (маршрутизация по цене), безопасность (vault, политики), HCI (поверхность управления), методология исследований (A/B, отложенные данные) |
| Готово, когда | документ написан | каждый модуль ниже существует, изолированно тестируем, а приёмочный прогон даёт метрики §13 |

## 1. Слои

```text
apps (L6)      web · telegram · cli                         — только порты оболочки
shell          Orchestrator(FSM) · Router · BudgetTracker · Journal · Steward · MemoryStore ·
               SessionStore · AmbitionAmplifier · ToolRunner — классы с lifecycle, DI через конструктор
core           compile · curate · render · amplify · extract_delta · classify · estimate_cost ·
               select_model · next_state — чистые функции
contracts      Intent · TaskContract · ExecutorResult · … — неизменяемые данные (dataclass frozen / TypedDict)
ports          LLMPort · AgentPort · MemoryPort · ToolPort · ClockPort · ConfigPort · EventPort
adapters       llm/{anthropic, openai_compat} · agent/{claude_code, codex, acp} · memory/{jsonl, sqlite} ·
               tools/{shell_sandbox, mcp} · config/{yaml_ro} · clock/{system, fake} · events/{jsonl}
```

Зависимости направлены внутрь: apps → shell → core/contracts; shell → ports; adapters → ports. Ядро не импортирует
ничего, кроме contracts. Имя вендора встречается только в `adapters/` и в конфиге.

## 2. Карта модулей и парадигм

| Модуль | Парадигма | Ответственность (одна причина для изменения) |
|---|---|---|
| `contracts` | схема | форма данных между модулями |
| `core.compile` | функция | Intent + факты → Manager Input ≤800 токенов |
| `core.curate` | функция | события/артефакты → факты ≤7 + refs (без модели); с моделью — через Curator |
| `core.render` | функция | контракт + Capability Profile → промпт под класс модели (reasoning / coder / critic / formatter) |
| `core.amplify` | функция (+ cheap-вызов через порт) | сырой запрос → Amplified Task Contract по правилам Altitude/Depth/Range |
| `core.extract_delta` | функция | новые события → дельта для rolling summary |
| `core.classify` | функция | ошибка → limit · auth · outage · bad_request · work |
| `core.estimate_cost` | функция | usage × карточка модели → деньги |
| `core.select_model` | функция | (каталог, профиль, бюджет, лимиты, история качества) → модель |
| `core.next_state` | функция | (состояние, событие) → (состояние, эффекты) — сердце FSM |
| `shell.Orchestrator` | класс + FSM | исполняет эффекты `next_state`, ничего не решает сам |
| `shell.Router` | класс | держит каталог и лимиты, вызывает `select_model` |
| `shell.BudgetTracker` | класс | счётчики по профилям, проектам, переписыванию, амплификации |
| `shell.Journal` | класс | Decision Log поверх EventPort; статусы шагов |
| `shell.Steward` | класс | дрейф от Intent, сигналы «заплатки», точки подтверждения, бюджеты |
| `shell.MemoryStore` | класс | rolling summary + delta, ссылки на артефакты |
| `shell.SessionStore` | класс | состояние FSM и доски для возобновления |
| `shell.AmbitionAmplifier` | класс | вызывает `core.amplify` на cheap-профиле, учитывает стоимость |
| `shell.ToolRunner` | класс | инструменты через ToolPort под политикой и песочницей |

## 3. Порты и адаптеры

| Порт | Контракт | Адаптеры |
|---|---|---|
| LLMPort | `call(req) → resp`; `stream(req) → iter[event]`; `count_tokens(req) → int`; `estimate_cost(usage) → usd`; `classify(err) → ErrorClass`; `card() → ModelCard` | anthropic, openai_compat (OpenAI, OpenRouter, Ollama, vLLM, llama.cpp), scripted (тесты) |
| AgentPort | `run(TaskContract, workspace, tools) → ExecutorResult`; `card()`; `classify(err)` — целый агент как исполнитель | claude_code, codex, acp |
| MemoryPort | `put(blob) → ref`; `get(ref)`; `summary()`; `apply_delta(delta)` | jsonl+files, sqlite |
| EventPort | `append(event) → seq`; `read(after) → iter` (только дописывание) | jsonl |
| ToolPort | `run(name, args, grants) → ToolResult` | shell_sandbox (bwrap/seatbelt), mcp |
| ClockPort | `now()`, `sleep(s)` | system, fake (тесты) |
| ConfigPort | `get(path)` — **только чтение**, читается при каждом решении | yaml_ro (artel.yaml, policy.yaml) |

`NormRequest = {messages, tools, output_schema, max_tokens, cache_hint}`;
`NormResponse = {content, tool_calls, usage{in, out, cached}, latency_ms, model, stop}` — одинаковы для всех
адаптеров. Вендорские возможности (кэш, thinking, computer use) — поля `card.capabilities`, не требования.

AgentPort нужен отдельно: Claude Code и Codex — не модели, а агенты со своим циклом. Отличие честное: LLMPort —
«один вызов модели», AgentPort — «выполни контракт».

## 4. Автомат состояний

```text
          idea
Idle ──────────► Refining ──► Amplifying ──► AwaitingApproval ──approve──► Planning ──► Executing
                    ▲              ▲              │ edit Intent              ▲    │          │
                    │              └──────────────┘                          │    ▼          ▼
                    │                                                         │  Verifying ──fail×2──► Escalated
                    │                                                         │    │ pass               │ decision
                    │                                                         │    ▼                    ▼
                    │                                                         │  Done ◄── accept ── (Planning|Executing)
  any ──pause──► Paused ──resume──► (previous)                                │
  any ──rollback(step)──► RolledBack ──► Executing                            │
  Steward: patches≥N | «спроектировал бы иначе» ──► RewriteProposed ──approve──► Rewriting ──► Planning
                                                         └─reject──► (previous)
```

Таблица переходов (`core.next_state`), выдержка:

| Из | Событие | В | Эффекты |
|---|---|---|---|
| Idle | `idea(text)` | Refining | curator.draft_intent |
| Refining | `intent_drafted` | Amplifying | amplifier.amplify |
| Amplifying | `amplified` | AwaitingApproval (или Planning в режиме full без open_questions) | journal.propose |
| AwaitingApproval | `approve` / `edit(intent)` | Planning / Refining | — |
| Planning | `plan_ready` | Executing | board.load |
| Executing | `task_done` | Verifying | critic.check (другая модель/провайдер) |
| Verifying | `fail` (2-й раз) | Escalated | curator.brief → manager.decide |
| * | `pause` / `resume` | Paused / предыдущее | новые шаги не стартуют |
| * | `rollback(step)` | RolledBack | snapshot.restore, зависимые → очередь |
| * | `patch_signal` / `rewrite_signal` | RewriteProposed | manager.rewrite_proposal; ждать человека |
| RewriteProposed | `approve` | Rewriting | рождается под-FSM с Rewrite Task Contracts |
| * | `budget_hit(expensive)` | то же | router.degrade |
| * | `budget_hit(money)` | Paused | причина в журнале |

Вмешательство человека — тоже событие. FSM — единственное место, где меняется поток.

## 5. Роли через Capability Profile

| Роль | reasoning | cost | latency | context | Особое |
|---|---|---|---|---|---|
| Manager | high | high | any | ≥32k | вход ≤800 токенов, не пишет код |
| Architect | medium-high | medium | any | ≥64k | — |
| AmbitionAmplifier | low | min | fast | ≥16k | только перед Manager |
| Executor | medium/low | low | fast | ≥64k | throughput |
| Curator | low | min | fast | ≥128k | читает сырое, отдаёт ≤800 токенов |
| Critic | medium | medium | any | ≥64k | **другая модель или провайдер**, чем у Executor |
| Steward | medium (чаще — код) | low | fast | ≥16k | — |
| Range-субагенты | low/medium | low | any | узкий | одна дисциплина = один субагент + свой Critic |

Range-субагент получает задачу исследования, а не роль: не «пиши как шорт-селлер», а «найди три допущения, при
ложности которых результат рушится; для каждого — проверку на данных».

## 6. Схемы

```json
{"Intent": {"id": "", "version": 1, "idea": "", "intent": "", "goal_behind_goal": "", "success_criteria": [""],
  "constraints": [""], "out_of_scope": [""], "assumptions": [""], "open_questions": [""], "risks": [""],
  "autonomy": "suggest|act_and_show|ask_at_forks|full", "budget": {"usd": 0, "expensive_tokens": 0,
  "rewrite_usd": 0, "amplify_usd": 0}, "confirmed_by": null},

 "ManagerInput": {"intent_ref": "", "decision_needed": "plan|escalation|acceptance|rewrite", "goal": "",
  "constraints": [""], "success_criteria": [""], "facts": ["<=7"], "refs": ["path|sha256|task|run"],
  "open_questions": [""], "budget": {"expensive_left": 0, "usd_left": 0}},

 "TaskContract": {"id": "", "intent_ref": "", "title": "", "input": "", "refs": [""], "criteria": [""],
  "checks": [""], "output_schema": "ExecutorResult.v1", "max_tokens": 0, "capability": "executor",
  "reversible": true, "depends_on": [""]},

 "AmplifiedTaskContract": {"base": "TaskContract", "altitude": {"goal_behind": "", "done_means": ""},
  "depth": {"raw_sources": [""], "methods": [""], "tools": [""], "independent_evidence": [""]},
  "range": [{"discipline": "", "research_task": "RangeResearchTask"}], "what_changed": [""],
  "why": "", "cost_usd": 0},

 "ExecutorResult": {"task": "", "status": "done|blocked|failed", "summary": "", "evidence": [""],
  "artifacts": [{"path": "", "sha256": ""}], "confidence": 0.0, "blocked_on": null},

 "EscalationRequest": {"task": "", "kind": "critic_failed|conflict|architecture|risk|rewrite|irreversible|budget|uncertainty",
  "to": "manager|owner", "brief": "ManagerInput", "options": [""], "recommendation": ""},

 "DecisionLogEntry": {"seq": 0, "ts": "", "actor": "", "capability": "", "provider": "", "model": "",
  "decision": "", "why": "", "alternatives": [""], "next": "", "inputs": ["sha256"], "tokens": {"in": 0, "out": 0},
  "cost_usd": 0, "latency_ms": 0, "reversible": true,
  "status": "planned|running|done|failed|rolled_back|rewritten", "intent_ref": ""},

 "CapabilityProfile": {"id": "", "reasoning": "low|medium|medium-high|high", "max_cost_per_mtok_usd": 0,
  "max_latency_s": 0, "min_context_tokens": 0, "needs": [""], "prefers": [""], "fallback": [""],
  "independent_of": "executor|null"},

 "RewriteProposal": {"id": "", "trigger": "wrong_architecture|patches|steward|before_big_block|would_design_differently",
  "what_is_wrong": [""], "why_patching_is_worse": [""], "target": "", "keeps": ["intent", "contracts",
  "behaviour_tests", "journal", "lessons"], "modules": [{"name": "", "paradigm": "function|class|schema|fsm|adapter",
  "contract": "", "behaviour_tests": [""]}], "parallel_groups": [[""]], "budget": {"usd": 0, "expensive_tokens": 0},
  "risks": [""], "rollback": ""},

 "RewriteTaskContract": {"base": "TaskContract", "module": "", "paradigm": "", "public_interface": "",
  "behaviour_tests": [""], "known_problems": [""], "must_not_read": ["old module source"]},

 "RangeResearchTask": {"discipline": "", "question": "", "method": "", "raw_inputs": [""],
  "output_schema": "", "critic": "independent", "max_tokens": 0}}
```

## 7. Роутинг

**Кто что решает** — таблица из docs/AUTONOMY.md §4, плюс:

- переписывание предлагает Manager, разрешает человек, исполняют Executor'ы;
- усиление делает AmbitionAmplifier (cheap) до того, как задачу увидит Manager;
- Critic обязан быть независим от Executor'а.

**Какая модель** — `select_model(catalog, profile, budget, limits, quality)`:

1. отфильтровать доступные и не упёршиеся в лимит модели, удовлетворяющие профилю (для Critic — другую модель или
   провайдер, чем у исполнителя задачи);
2. отбросить выходящие за бюджет;
3. если пусто — профиль из `fallback` (деградация), затем любой провайдер;
4. выбрать минимум по `(ожидаемая стоимость задачи, латентность, −доля принятых с первой попытки)`.

## 8. Протоколы

**Автономия, прозрачность, управляемость, провайдеры** — docs/AUTONOMY.md §5–§8. Дополнения:

- **Амплификация.**
  - Запускается на каждый сырой запрос и на каждую крупную задачу плана, только на cheap-профиле.
  - Отвечает на четыре вопроса (что сделал бы ведущий специалист, какие сырые данные, какие методы и инструменты,
    какие независимые доказательства).
  - Выдаёт Amplified Task Contract с полями `what_changed` и `why`. Человек видит, что усилено.
  - Ограничение: глубина, не объём. Число задач плана не растёт больше чем в 1,5 раза.
- **Переписывание.**
  - Steward считает признаки: заплатки на одном модуле, повторные эскалации, растущий модуль, ответ «нет» на вопрос
    «спроектировал бы так же?».
  - Manager по брифу ≤800 токенов пишет Rewrite Proposal; человек подтверждает.
  - Curator собирает под каждый модуль отдельный контекст: контракт, поведение, тесты, известные проблемы, без
    старого исходника.
  - Executor'ы переписывают независимые модули параллельно; Critic другого провайдера проверяет по тестам поведения.
  - Старый модуль удаляется только после того, как новый прошёл те же тесты.
  - Если и новая конструкция неверна — стоп, эскалация, новое решение; латать нельзя.
- **Внешние действия** (push, issue, сообщение в Telegram, внешний API):
  - чтение источника от закладки, а не от окна;
  - ошибка источника — `unreadable`, а не «ничего нового»;
  - перед действием — живая перепроверка статуса;
  - «сделано» только при `ok: true` и идентификаторе от внешней системы, иначе `maybe_done`;
  - ledger и закладки обновляются после подтверждения;
  - политики читаются из ConfigPort при каждом решении;
  - секреты — только ссылкой на переменную окружения или vault.

## 9. Псевдокод

**Ядро (чистые функции)**

```python
def amplify(raw: str, facts: Facts, llm_cheap: Call) -> AmplifiedTaskContract:
    q = render(AMPLIFY_QUESTIONS, raw=raw, facts=facts)          # 4 вопроса ambition
    out = llm_cheap(q, schema=AmplifiedTaskContract)              # единственный побочный вызов — через переданную функцию
    return clamp_scope(out, max_growth=1.5)                       # глубина, не объём

def compile_manager_input(intent, facts, refs, budget) -> ManagerInput:
    return ManagerInput(goal=intent.intent, success_criteria=intent.success_criteria[:7],
                        facts=top_k(facts, 7), refs=refs[:20], budget=budget).fit(max_tokens=800)

def next_state(state, event) -> tuple[State, list[Effect]]:
    return TRANSITIONS[(state.name, event.kind)](state, event)    # таблица §4; неизвестная пара → ошибка

def select_model(catalog, profile, budget, limits, quality) -> ModelCard: ...   # §7
```

**Оболочка (классы с DI)**

```python
class Orchestrator:
    def __init__(self, ports: Ports, router: Router, budget: BudgetTracker, journal: Journal,
                 steward: Steward, memory: MemoryStore, amplifier: AmbitionAmplifier): ...
    def handle(self, event):                                     # единственная точка входа
        state, effects = next_state(self.session.state, event)
        for e in effects: self.run_effect(e)                     # каждый эффект → журнал
        self.session.save(state)

class Router:
    def model_for(self, role, task=None) -> ModelCard:
        p = self.profiles[role]
        if self.budget.near_limit(p): p = self.profiles[p.fallback[0]]
        return select_model(self.catalog, p, self.budget.left(), self.limits, self.quality)
```

**FSM оркестратора**

```text
Amplifying: contract = amplifier.run(intent)          → journal(what_changed, why, cost) → AwaitingApproval
Executing:  for t in ready: agent(router.model_for("executor", t)).run(t) → Verifying
Verifying:  critic = router.model_for("critic", independent_of=t.model); fail×2 → Escalated(curator.brief(t))
Escalated:  manager(router.model_for("manager")).decide(brief) → Planning|Executing|RewriteProposed|ask owner
RewriteProposed: manager.rewrite_proposal(curator.brief(signals)) → wait owner
Rewriting:  for group in proposal.parallel_groups: parallel(executor.rewrite(RewriteTaskContract(m)))
            critic(other provider).check(behaviour_tests) → swap module → Planning
```

## 10. Правила архитектуры

Можно: чистая функция для преобразования; класс для состояния с lifecycle; frozen-данные для контрактов; FSM для
потока; адаптер для внешнего мира; DI через конструктор; композиция.

Нельзя:
- глобальные синглтоны и изменяемое состояние на уровне модуля;
- импорт адаптера из ядра;
- имя вендора вне `adapters/` и конфига;
- наследование для повторного использования данных;
- класс, отвечающий больше чем за одну причину изменения;
- поток управления вне FSM;
- чтение конфига из глубины логики вместо передачи значения;
- оценка своей работы той же моделью.

## 11. Политики

Эскалация, бюджет, деградация, точки подтверждения и cross-provider fallback описаны в docs/AUTONOMY.md §10.
Дополнения:

- **Переписывание** — свой бюджет (`budget.rewrite_usd`); крупное (больше одного модуля или публичного интерфейса)
  требует подтверждения.
- **Амплификация** — свой бюджет (`budget.amplify_usd`), только cheap-профиль; при исчерпании задача идёт без
  усиления, это отмечается в журнале.
- **Расходы за запуск** ограничены; при достижении лимита — пауза с причиной, без падения.

## 12. Метрики

- доля токенов дорогого профиля;
- токены на решённую задачу;
- escalation rate;
- success rate;
- часы до лимита;
- доля автономных шагов;
- вмешательства человека;
- стоимость задачи по провайдерам;
- доля переписанного кода;
- стоимость переписывания;
- стоимость амплификации;
- **прирост качества от амплификации** — A/B на отложенном наборе задач: исходный и усиленный контракт, одна и та же
  модель-исполнитель. Оценивает независимый Critic другого провайдера по заранее заданным критериям. Метрика —
  разница доли принятых и оценки глубины. Без этого замера амплификация — гипотеза, а не улучшение.

## 13. Предложение о переписывании (требует подтверждения)

Замеры текущего кода (59 модулей, 17,2 тыс. строк; собраны дешёвыми субагентами по исходникам):

| Признак | Факт |
|---|---|
| Скрытые циклы зависимостей | 24 цикла в 6 группах; 124 из 194 связей — импорты внутри функций, без них пакет не импортируется. Главный узел: agent ↔ analytics ↔ runtimes ↔ providers ↔ 5 модулей рантаймов ↔ mcp_client |
| God-объекты | `Evolution` 64 метода / 1224 строки; `Manager` 47 / 937; `make_handler` — замыкание на 438 строк с 23 методами; `run_codex` 184 и `run_claude_code` 188 строк |
| Дублирование | четыре рантайма агентных CLI (349–738 строк каждый) по отдельности повторяют мост, зеркало событий, продолжение, политику, лимиты |
| Глобальное состояние | 19 изменяемых объектов уровня модуля: кэши без блокировок (`_auto_cache`, `_sandbox_mode`, `analytics._cache`, `PROVIDERS`), синглтон `DSH_GUI`, растущий без очистки `reports._listeners` |
| Вендор в логике | провайдер по умолчанию «anthropic» повторён в 5 местах; ветвления по имени рантайма в providers, analytics, cli, tools; таблица цен и имена моделей в коде |
| Конфиг в глубине логики | providers 38 чтений, acp 35, codex 34, dsh 29 |
| Цена заплаток | каждая новая возможность (MCP, уровни моделей, intake) трогала 4–6 файлов рантаймов; три последних регресса — на этих швах (откат токена при переносе ветки, подмена прокси через `-m`, цикл аудита) |

```json
{"id": "rw-1", "trigger": "would_design_differently",
 "what_is_wrong": ["поток управления размазан между Manager.run, рантаймами и web-сервером; нет явного автомата",
                   "агентные CLI не за одним портом: 4 копии одного цикла",
                   "циклы зависимостей скрыты ленивыми импортами",
                   "вендор и конфиг — в логике, глобальные кэши — без владельца"],
 "why_patching_is_worse": ["Intent, режимы автономии, журнал решений и откат требуют одного места, где меняется поток; заплатки добавят пятое место",
                           "каждая новая возможность снова трогает 4–6 файлов"],
 "target": "§1–§3: contracts + core (функции) + shell (классы, DI) + FSM + ports/adapters",
 "keeps": ["Intent/контракты", "board (SQLite) как адаптер MemoryPort", "журнал событий", "навыки", "политика команд",
           "песочница", "worktree-изоляция", "399 тестов поведения", "веб-интерфейс как приложение"],
 "modules": "§14",
 "parallel_groups": [["contracts", "ports"],
                     ["core.select_model+catalog", "core.next_state", "core.compile/curate", "adapters.agent (общий цикл + кодеки claude_code/codex/acp)"],
                     ["shell.Orchestrator", "shell.Journal", "shell.Steward", "shell.BudgetTracker"],
                     ["apps.web (маршруты над shell)", "apps.cli", "apps.telegram"]],
 "budget": {"expensive_tokens": 150000, "note": "Manager только формулирует контракты и принимает; код пишут Executor'ы, проверяет Critic другого провайдера"},
 "risks": ["регрессы в живых CLI, которые тестовые фейки не ловят → по каждому CLI живой smoke-прогон перед заменой",
           "эволюция завязана на Manager → заморозить и переписать отдельным предложением"],
 "rollback": "каждая группа — своя ветка; старый путь остаётся до прохождения новым тех же тестов"}
```

Без переписывания отпадают: DeepSeek Harness как рантайм (по прежнему решению), заглушка System One.

## 14. MVP-план по модулям и парадигмам

Стратегия — «душитель»: новое ядро растёт рядом, старые части отключаются по одной, каждый шаг проходит все тесты
поведения.

| # | Модуль | Парадигма | Из чего | Готово, когда |
|---|---|---|---|---|
| 1 | `contracts` | схема | поля из board, tool-схем, routing | все схемы §6 как frozen-данные, валидация |
| 2 | `ports` | интерфейсы | — | 7 портов §3, fake-адаптеры для тестов |
| 3 | `core.select_model` + каталог в `artel.yaml` | функция + конфиг | routing.py, limits.py, PRICES | ни одного имени модели в коде; тест: та же роль на двух каталогах |
| 4 | `adapters.agent` | адаптер (общий цикл + кодек на CLI) | runtimes/claude_code, codex, acp | 3 кодека по ≤150 строк; живой smoke на Claude Code и Codex |
| 5 | `adapters.llm` | адаптер | plugins/llm_providers | Anthropic + openai_compat; локальная модель проходит профиль Executor |
| 6 | `core.next_state` | функция (FSM) | Manager.run, schedule, on_review | таблица §4; 100% переходов покрыты тестами |
| 7 | `shell.Orchestrator` | класс | Manager | Manager удалён; проект идёт через FSM |
| 8 | `shell.Journal` + панель решений | класс + UI | события | каждое решение: зачем → что дальше |
| 9 | Intent + `AmbitionAmplifier` | функция + класс | intake | подтверждение, версии, A/B-замер амплификации |
| 10 | `shell.Steward` | класс | budget, guardian, policy | режимы автономии, точки подтверждения, сигнал «переписать» |
| 11 | `apps.web` | приложение | web/server.py | маршруты по файлам, нет замыкания на 438 строк |
| 12 | приёмочный прогон | — | — | многочасовой проект; все метрики §12 из данных |

Шаги 3–6 независимы и идут параллельными субагентами после шагов 1–2.
