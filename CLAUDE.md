# Working on Artel: every token where it counts

The same rule Artel follows at runtime (docs/ORCHESTRATION.md) applies to agents developing it: the strongest model
decides, cheaper subagents do the reading, searching, routine editing and checking. The main session keeps its
context small and spends it on decisions.

## Roles (definitions in .claude/agents/)

| Subagent | Model | Use for | Returns |
|---|---|---|---|
| `prompt-refiner` | haiku | a vague, long or multi-part request → Task Contract before work starts | JSON contract |
| `scout` | haiku | any "where is / how does X" search; before reading code yourself | ≤25 lines `path:line — fact` |
| `compressor` | haiku | long logs, test output, docs, transcripts | ≤12 facts with sources |
| `executor` | sonnet | a well-specified change (files, behaviour, checks) | JSON: files, checks, deviations |
| `critic` | sonnet | review of a diff before merge | JSON findings |

Without these definitions loaded, use the general-purpose agent with `model: haiku` / `model: sonnet` and paste the
same instructions.

## Protocol for the main session

1. **Refine first** when a request is ambiguous or large: `prompt-refiner` → contract; work from the contract.
2. **Never read to search.** Locating code, configs or docs goes to `scout`; read only the lines it points to.
3. **Never read long output.** Test logs, tool dumps, large JSON go to `compressor` or are cut at the source
   (`tail`, `--token-limit`, `grep -c`).
4. **Delegate the routine.** Mechanical or well-specified edits go to `executor` with a Task Contract: exact files,
   behaviour, checks, output format. Keep design and security decisions in the main session.
5. **Review before merge.** Non-trivial diffs go to `critic`; fix confirmed findings.
6. **Run independent subagents in parallel**, in one message.
7. **Pass references, not content**: paths, line ranges, commit ids, commands — never whole files.
8. **Ask for JSON or fixed-size answers** and say the size limit in the prompt.
9. **Do not repeat work**: a subagent's result is the answer; do not re-derive it.

## Project rules

- Answers to the owner in Russian; code, comments and commits in English.
- Never put keys or tokens in chat, files or command lines; environment variables only.
- No model identifiers in commits or files except the commit attribution trailer.
- Before merging to master: the full test suite (`python3 -m pytest -q -x`) passes.
- Terminology: workers are «воркеры»; «бот» is only the Telegram bot.
