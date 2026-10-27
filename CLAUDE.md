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

## Engineering rules (every change, main session and subagents)

Adapted in our own words from Robert C. Martin's ub-mode (github.com/unclebob/ub-mode) and our own incidents.

1. **Failing test first.** Before changing code, write the test that reproduces the defect or pins the new
   behaviour, run it, and see it fail for the expected reason. The change is accepted only with that evidence:
   the failing output before, the passing output after. "Not verified on the old code" is a rejection.
2. **Tests check behaviour.** A test calls the code as its user does and checks what that user sees. A test that
   would still pass if the functions under test returned nothing (or a constant) is deleted or rewritten.
3. **Root causes only.** A guard, retry, sleep, longer timeout or loosened assertion that hides a failure is not a
   fix. Reproduce, find the cause, fix the cause.
4. **Attack the premise.** When two fixes that share one premise fail the same check, stop: write the premise in
   one sentence, test it directly, and only then try again. (Our cases: "the work is bad" when the environment was
   broken — `rg`, proxy, sandbox; three replans each.)
5. **Smallest sufficient change; subtract before adding.** One decision lives in one place. Remove complexity
   before adding any. New requirements get the design they would have had from the start, delivered in pieces.
6. **Boundaries.** Parse and validate outside data once where it enters (contracts); trust it inside. Make illegal
   states unrepresentable. Keep logic in pure functions.
7. **Shared state.** If two actors do not need the same mutable thing, give each its own; if they do, one writer,
   enforced by structure (lock, single owner). Operations are idempotent: twice, or after a crash midway, ends the
   same.
8. **Small verified units.** Finish one unit, check it, commit it (why, what, how proven), then the next. A branch
   reaches master only with green CI on all six jobs.
9. **Quality gates on changed code** (when the tools are present; skip a missing tool and say so): CRAP < 10 per
   changed function (radon complexity + coverage), no duplicate blocks (pylint duplicate-code), mutation run
   (mutmut) on changed files with no surviving mutants left unexplained.

## Project rules

- Answers to the owner in Russian; code, comments and commits in English.
- Never put keys or tokens in chat, files or command lines; environment variables only.
- No model identifiers in commits or files except the commit attribution trailer.
- Before merging to master: the full test suite (`python3 -m pytest -q -x`) passes.
- Terminology: workers are «воркеры»; «бот» is only the Telegram bot.
