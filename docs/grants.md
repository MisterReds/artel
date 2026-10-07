# Path grants

Every file tool resolves its path through `ToolContext.resolve`, which checks the path against the role's grants
(`artel/tooling.py`, `effective_grants`). The project page shows the effective grants ("Доступ к путям").

## Defaults

- **write**: the role's workspace and the project `shared/` directory.
- **read**: everything writable, plus the project root and the `--ref` directories.
- **refs** are always read-only, even if listed under `write`.
- Roles `planner`, `reviewer`, `auditor`, `manager`, `evolver`, `absorber` get no write grants.

## Overrides

Add a `grants` mapping to the project config or to the project meta (`project.json`). It only adds paths; a write
grant also grants read. Relative paths are relative to the project root.

```yaml
grants:
  read: [../datasets]
  write: [../out]
  roles:
    worker:
      write: [../scratch]
```

`roles.<role>` entries apply only to that role.

## Runtime mapping

- Built-in tools: `ToolContext.resolve(path, write=...)` for every read, write and edit.
- Codex: the sandbox is `read-only` when there are no write grants; otherwise one `--add-dir` per write grant
  (never refs).
- Claude Code: `--add-dir` for every read and write grant; without write grants the write tools are
  listed in `--disallowedTools`.

## Denial

A refused access returns a tool error that names the grant:

```
write to '/etc/x' denied: not in write grants ['<workspace>', '<shared>']; use a workspace-relative path or shared/...
```
