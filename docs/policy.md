# Command policy (`policy.yaml`)

Before a command runs, Artel checks it against a policy: the `bash` tool, the Codex runtime and the Claude Code
runtime all consult it. Design adapted from OpenAI Codex `codex-rs/execpolicy` (Apache-2.0).

## Files

Built-in defaults, then `ARTEL_HOME/policy.yaml` (default `~/.artel/policy.yaml`), then `<project dir>/policy.yaml`.
A rule with the same `id` in a later file replaces the earlier one. When several rules match, the strictest wins:
`forbidden` > `prompt` > `allow`. Files are re-read when their mtime changes; evaluation is cached otherwise.

## Format

```yaml
rules:
  - id: no-terraform-destroy
    prefix: terraform destroy        # command-token prefix, or:  regex: '...'
    decision: forbidden              # allow | prompt | forbidden
    justification: "Use plan and ask a human."
    match: ["terraform destroy -auto-approve"]   # must match, else the policy fails to load
    not_match: ["terraform plan"]                # must NOT match
```

`prefix` is matched against every simple command the line runs: segments of a compound command (`;`, `&&`, `||`,
`|`, newlines), commands inside `$( )`, backticks, `( )`, `<( )` and `{ }` (also inside double quotes), the script of
`sh -c` / `bash -c` / `eval`, with leading `VAR=value` assignments and wrappers (`sudo`, `env`, `nice`, `timeout`,
`nohup`, `command`, `exec`…) removed. `regex` is searched in the command as written, in a normalised copy (quotes
removed, `/./` and `//` collapsed) and in each simple command. Exactly one of the two is required. A rule whose examples
fail, an unknown decision or an invalid regex is a load error (`PolicyError`): `bash` then fails with a clear tool
error and runs nothing, and the Claude Code hook blocks every shell command until the file is fixed.

The policy is a guard rail, not a boundary: a shell can always hide a command (variables, here-documents, a script
written to disk and run). What a command can reach is limited by the OS sandbox (`sandbox.mode`).

## Decisions

- `forbidden`: the command is not run; the model gets a tool error with the rule id, justification and command.
- `prompt`: the command is not run; the task ends as `task_block` with `needs_human=1`, the command and
  justification in the reason/evidence.
- `allow`: runs as usual (also the result when nothing matches).

## Defaults

`rm -rf /` / `~` (forbidden), `curl|wget ... | sh` (forbidden), `git push --force` to main/master (forbidden),
credential files `~/.ssh/id_*`, `.aws/credentials`, `.env*` (prompt).

## Runtimes

- builtin `bash`: checked before execution.
- Claude Code: a `PreToolUse` hook for `Bash` (`python -m artel.policy`, exit 2) blocks the command; the event stream
  is also checked and a `prompt` verdict ends the run as needs_human.
- Codex: has no pre-execution hook, so the command is seen when it starts (`item.started`) and the process is
  terminated immediately. This is best-effort: a very fast command may already have run.

## Protected configuration

The built-in rule `protected-config` forbids commands that write `policy.yaml`, `artel.yaml` or the skills library
(`ARTEL_HOME` and the project): redirection, `tee`, `cp`/`install`/`ln` onto them, `mv`/`rm`/`sed -i`, also through
`$ARTEL_HOME`, `~/.artel` or `../`. Reading stays allowed. The same paths are outside every write grant, refused by
the file tools and read-only inside the OS sandbox.
