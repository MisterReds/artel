# Acceptance results registry

One row per acceptance run. Raw results live on the branch named in "Source"; values are copied verbatim,
never recomputed or filled in. `null` means the run produced no value.

| Run | Scenario | Version | Status | Source |
|---|---|---|---|---|
| S-baseline | S | 917c66a | interrupted: ended `waiting_human` after 0.26 h, goal audit not run, `verify: null` | `baseline/S` @ f459260, `bench/acceptance/results/S-baseline.json` |

## S-baseline (reference point "before" for the rewrite)

Checks against `bench/acceptance/S.yaml`, verbatim from S-baseline.json:

| Metric | Value | Limit | ok |
|---|---|---|---|
| expensive_token_share_max | null | 0.05 | null |
| expensive_call_share_max | 0.161 | 0.05 | false |
| autonomy_min | 0.964 | 0.95 | true |
| owner_interventions_max | 0 | 1 | true |
| no_limit_stop | false | true | true |

Other recorded values: duration_h 0.26, ended waiting_human, tasks_done 1, success_rate 0.333, steps 56,
owner_asks 2, tokens_total 0, tokens_by_runtime {codex: 0}, cost_usd null, verdict null.

Break point: two tasks (t-709c1ae1 CSV import, t-ada719df HTML report) escalated after 3 replans without
progress; their checks call `python`, absent from PATH on the owner's machine (exit 127). The project never
reached done, so neither the goal audit nor independent verification ran. Token counters are unreliable for
this run (tokens_total 0), so U3 is not measured by it.

Defects found by this run are tracked in docs/SPEC.md §10.
