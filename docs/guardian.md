# Guardian

An asynchronous trajectory risk reviewer (`artel/guardian.py`). The two-stage idea and risk categories are
adapted from OpenAI Codex (`codex-rs/prompts/templates/guardian/classifier_instructions.md`, Apache-2.0).

- After every worker run the Manager scores the run's `tool/call` and `tool/result` events and logs
  `guardian/assessed` (`task_id`, `run_id`, `risk` = `low|high`, `score` 0..1, `reason`).
- Signals: destructive shell commands (`rm -rf /`, `mkfs`, `dd of=/dev/..`, `DROP TABLE`), network exfiltration
  (`curl -d @file`, `nc`, `scp` to a remote), writes to system paths or outside the workspace, secrets in tool
  output (private keys, cloud/API tokens). Score >= 0.7 is `high`.
- The assessment is deterministic and makes no model call.
- **High risk** adds a board comment and switches the task's later runs to blocking review: each `bash` command
  is checked before it runs, and a destructive or exfiltrating one is denied (`guardian/denied` event). Safe
  commands still run. **Low risk** changes nothing. The state is rebuilt from the log on resume.
- The dashboard task card shows a red "риск" badge whose tooltip is the reason; the task dialog shows the score.
