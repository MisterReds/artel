# System One: как подключить свою модель к Artel

System One — быстрые типизированные решения в узких местах харнесса: модель не пишет текст, а отвечает на заранее
заданные вопросы с вероятностями. Artel своей модели не содержит; без System One каждая точка решения берёт значение
по умолчанию, и Artel работает как обычно. Подключённая System One делает работу дешевле и быстрее там, где сейчас
решает дорогая модель или жёсткое правило.

## Контракт

Запрос: `state` (контекст, строки) и `questions` (именованные вопросы трёх типов).

```json
{
  "state": {"task": "Написать fizzbuzz.py\n…", "criteria": "тесты проходят"},
  "questions": {
    "difficulty": {"type": "score", "instructions": "How hard is this task for an experienced engineer?",
                   "criteria": ["trivial: a few obvious edits", "routine: standard work, some care",
                                "hard: subtle logic, debugging or design needed"]},
    "go": {"type": "noul", "instructions": "Does this trajectory contain something worth saving as a skill?",
           "criteria": {"true": "a non-obvious technique…", "false": "routine work…"}},
    "action": {"type": "choice", "instructions": "What will most likely get this stuck task done?",
               "criteria": {"retry": "…", "guidance": "…", "reassign": "…", "human": "…"}}
  }
}
```

Ответ: `answers` с ключами вопросов; `model` и `usage` по желанию (`usage.cost` в USD учитывается в стоимости проекта).

| Тип | Ответ |
|---|---|
| `noul` (да/нет) | `{"type": "noul", "noul": 0.93}` — вероятность «да» |
| `choice` (один из вариантов) | `{"type": "choice", "choice": "retry", "confidence": 0.81, "probabilities": {"retry": 0.81, "human": 0.12, …}}` — только ключи из `criteria` |
| `score` (шкала) | `{"type": "score", "score": 1.0, "confidence": 0.9}` — индекс по шкале `criteria` (0 … n-1) |

Ошибка, таймаут или неполный ответ не ломают работу: точка решения берёт значение по умолчанию, в журнале
появляется `decide/error`.

## Два способа подключения

**HTTP-сервис** — в `~/.artel/artel.yaml`:
```yaml
decide:
  backend: http
  url: http://127.0.0.1:8099/decide      # POST {state, questions} -> {answers, model, usage}
  api_key_env: TEAM_S1_KEY               # необязательно: имя переменной окружения с ключом (Bearer)
  timeout_s: 10
  points: {difficulty: true, skill_review: true, refine: true, replan_shortcut: true}
```

**Плагин Artel** — пакет с модулем, который регистрирует сервис `system_one`:
```python
PLUGIN = {"name": "team_s1", "version": "1", "provides": ["service:system_one"]}

class TeamSystemOne:
    name = "team-s1"
    def ask(self, state: dict, questions: dict) -> dict:
        ...  # вернуть answers по контракту выше

def setup(kernel):
    kernel.provide("system_one", TeamSystemOne())
```
и в конфиге: `plugins: [team_s1]`, `decide: {points: {…: true}}`.

## Точки решения

| Точка | Вопрос | Что делает Artel с ответом | По умолчанию (без System One) |
|---|---|---|---|
| `difficulty` | score: насколько трудна задача | глубина рассуждения исполнителя low / medium / high (при уверенности ≥ 0.6) | глубина по умолчанию рантайма |
| `skill_review` | noul: есть ли в прогоне что сохранить как скилл | при «да» < 0.25 библиотекарь не запускается | библиотекарь по правилам |
| `refine` | noul: помогло бы новое правило проекта | при «да» < 0.25 правка правил не запускается | правка по правилам |
| `replan_shortcut` | choice: retry / guidance / reassign / human для застрявшей задачи | при уверенности ≥ 0.8 в retry или human — сразу, без вызова менеджера | решает менеджер (большая модель) |

Пороги — `decide.thresholds`. Каждое решение пишется в журнал проекта (`decide/answer`: точка, ответы, задержка,
модель), так что пользу можно измерить бенчмарком эволюции: с System One и без неё.

## Что важно для модели

- Латентность: точки стоят на пути каждой задачи; цель — десятки–сотни миллисекунд.
- Калибровка: Artel сравнивает вероятности с порогами, поэтому вероятности должны значить то, что значат.
- Только варианты из `criteria`: выдуманный ключ в `choice` считается неполным ответом.
