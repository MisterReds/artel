# Skip refiner on clean runs; gate librarian on cheap tasks

## Change (artel/manager.py)
- `Manager.run_failures`: never-reset counter incremented on rejections, blocks, run-ended-without-completion, and tool-call `-> ERROR` digest lines.
- Final `job_refine` runs only if `run_failures > 0`; otherwise logs `harness/refine_skipped`.
- `job_skill_review` (librarian) runs only if tool_calls >= `review_after_tool_calls` AND (the task had a rejection/tool error, or tool_calls >= 2x threshold).

## Tests (tests/test_e2e.py, scripted provider)
- Clean run: no refiner/librarian `agent/start`, `harness/refine_skipped` logged.
- Failure run (`review_failures: 1`): refiner still runs.
- Librarian threshold gating on clean runs (T=4 skipped, T=3/1 run); runs below 2x after a rejection.
- Existing tests that relied on always-refine now inject a failure explicitly.

## Verification
- `python -m pytest -q -x -p no:cacheprovider`: 26 passed (includes shared/test_summary.py).
- CLI smoke: `artel run GOAL --name smoke --seed <dir> --provider scripted --workdir <dir>` -> status done, `refine_skipped` logged.
- git diff touches only artel/manager.py and tests/test_e2e.py.
