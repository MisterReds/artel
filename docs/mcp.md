# External MCP servers

Configure MCP servers once in `artel.yaml` (`~/.artel/artel.yaml` or the project's); every runtime gets them.

```yaml
mcp_servers:
  browser: {preset: playwright, allow: [worker]}      # presets: playwright, fetch, filesystem
  docs:
    type: http                                         # or stdio (default when `command` is set)
    url: https://example.com/mcp
    headers: {Authorization: "Bearer ${DOCS_TOKEN}"}
    allow: [worker, reviewer]
  mytool:
    command: my-mcp-server
    args: [--flag]
    env: {API_KEY: "${API_KEY}"}                       # ${VAR} and ${VAR:-default} from the environment
    allow: [worker]
    enabled: true
```

- `allow` lists the roles that see the server; without it no role does.
- Native runtime: tools appear as `mcp__<server>__<tool>`, are logged (`mcp/call`) and checked against the command
  policy (the rule prefix is matched against `mcp__server__tool {json args}`).
- Claude Code and Codex get the same servers in their own MCP config next to Artel's bridge.
- Playwright (preset, or any `@playwright/mcp` command) is always run with `--headless`; `--headed`,
  `--no-headless` and `--headless=false` are dropped, so a browser never opens on the owner's screen.
- A server that fails to start is skipped (see `kernel.failed["mcp:<name>"]`), never fatal.

## REA — разбор программ (reverse engineering)

REA (github.com/morluto/rea, MIT) подключается как **навык + CLI**, не как MCP-сервер. Его MCP-сервер объявляет 207
инструментов; их описания — 6,4 МБ, то есть сотни тысяч токенов в контексте каждого запуска воркера. Навык же
загружается, только когда задача его требует, а сам REA вызывается командой с ограничением вывода.

```sh
artel skills import morluto/rea --include reverse-engineer-anything   # навык в библиотеку Artel
```

Воркеру нужен Node.js 22+ (REA запускается через `npx -y rea-agents@6.5.0 …`). Сразу работают статический разбор
JavaScript/Electron, .NET и сайтов; нативные бинарники — с Ghidra (`GHIDRA_INSTALL_DIR`), Hopper или IDA; Android —
с JADX и JDK. Вывод ограничивайте: `--format toon --token-limit 4000`.

Для исходных кодов REA не нужен (так говорит и его навык): репозиторий читается обычными инструментами. Использовать
только на своём ПО или ПО, на анализ которого есть разрешение.
