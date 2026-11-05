<p align="center"><img src="https://raw.githubusercontent.com/MisterReds/arteliq/master/docs/brand/arteliq-logo.svg" alt="Arteliq" height="72"></p>

**Arteliq** is a self-improving multi-agent harness: you give it a goal, it plans the project, hires a team of AI
workers, reviews every task, and keeps going until an independent audit confirms the goal is met.

**Arteliq** — многоагентный харнесс: цель → план → команда ИИ-воркеров → ревью каждой задачи → аудит результата.
Работает автономно, на любых провайдерах и подписках (Claude Code, Codex, API, локальные модели).

<p align="center"><img src="https://raw.githubusercontent.com/MisterReds/arteliq/master/docs/img/app-project-light.webp" alt="Arteliq dashboard: project page" width="760"></p>

```bash
pip install arteliq
arteliq run "Make a CLI tool that converts CSV to JSON, with tests and a README" --web
# dashboard: http://127.0.0.1:8765
arteliq app        # the dashboard in its own app window
```

- Autonomy first: the owner is asked only when a decision is genuinely theirs.
- Verified results: discriminating checks per task, a reviewer, and a completion audit.
- Any provider: Claude Code and Codex subscriptions, Anthropic and OpenAI-compatible APIs, local models.
- Desktop app window, phone access and a Telegram bot.
- Safety: an OS sandbox for agent commands, a command policy, protected project files.

Full documentation (in Russian): https://github.com/MisterReds/arteliq
