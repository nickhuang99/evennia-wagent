# Current State

Read this after `README.md` and `docs/recovery-playbook.md` when resuming after a crash.

## Active Goal

The current live goal is still frontier expansion with a strict success bar.

- Do not count activity as success.
- Do not report progress just because new observations were collected.
- Only treat the work as a frontier success when a genuinely new room name is promoted into `wagent_map_memory.json` and, when applicable, `wagent_route_memory.json`.
- Use deterministic blind transit to reach the confirmed staging room first; only switch into local probing after the target room is reached.

## Current Authoritative Baseline

The confirmed shared map still has 12 rooms.

Important confirmed routes and exits already in shared truth include:

- dark cell -> underground passages via the root-covered wall recipe
- underground passages -> cliff by the coast via `climb the chain`
- cliff by the coast -> the old bridge via `old bridge`
- the old bridge -> ruined gatehouse via `east`
- ruined gatehouse -> corner of castle ruins via `castle corner`
- corner of castle ruins -> overgrown courtyard via `courtyard`
- overgrown courtyard -> the ruined temple via `ruined temple`
- the ruined temple -> antechamber via `stairs down`

This means recent work improved confirmed transit and confirmed exits, but did not yet produce a new room name beyond the current baseline.

## Last Recovered Frontier Focus

The latest active frontier branches before recovery were:

1. `corner of castle ruins` -> `obelisk` / `ghostly apparition`
2. `cliff by the coast` -> `old well`

Observed status of those branches:

- The obelisk branch is the higher-value frontier. It can now be reached reliably and has produced repeated obelisk text variations and apparition-related scan targets.
- The old well branch has been probed heavily with object-oriented commands and has not yielded a new room or new durable exit.

## Resume Roadmap

If work resumes after a crash, use this order unless new evidence overrides it:

1. Confirm that the active goal is still new-room discovery in shared truth, not just new observations.
2. Resume from the obelisk and apparition frontier first.
3. Use the old well branch only as a secondary check, not as the default continuation path.
4. Before launching another long run, inspect the most recent runtime log and summary referenced by `recovery_status.json`.
5. If the run that crashed was long-lived, audit log-heavy loop paths before restarting the same proof.
6. Do not resume from `mygame/` duplicate bot scripts; only the root workflow is authoritative.

## Resource / Crash Notes

The repository does not contain proof that the prior VS Code crash was caused by these scripts.

What current evidence does show:

- `artifacts/current/` is only about 2 MB in total.
- The largest live files are append-only logs, not giant JSON state files.
- The biggest active log is the obelisk action scanner log at roughly 600 KB and about 8k lines.
- Current observation-memory and orchestrator-summary JSON files are small, generally under about 10 KB.

Practical interpretation:

- There is no evidence here of a runaway large-memory artifact.
- The clearest pressure risk is repeated loop logging across several live `.log` files.
- That could make VS Code feel heavier if many large logs are open or constantly refreshed, but the current artifact sizes alone do not prove they caused the editor crash.
- Runtime logs now rotate by default in the root scripts. Current defaults are `WAGENT_LOG_MAX_BYTES=524288` and `WAGENT_LOG_BACKUP_COUNT=4`.
- The previously unbounded in-memory interaction history in runner and scanner is now capped by the existing `HISTORY_MAX_LEN` setting.

## Recovery Mechanism

Use the chain below after a restart:

1. `README.md`
2. `docs/recovery-playbook.md`
3. `docs/current-state.md`
4. `artifacts/current/recovery_status.json`
5. shared truth files
6. repo memory notes
7. active logs and summaries referenced by `recovery_status.json`

The `recovery_status.json` file is the runtime pointer layer. It should tell you:

- which script last ran
- whether it was runner, scanner, or orchestrator work
- what target room and scan target were active
- where the current active log, summary, and observation-memory files live

## Crash Review Inputs

When the recovery task includes crash review, the first-pass inputs should be:

1. `artifacts/current/recovery_status.json`
2. the log and summary referenced there
3. the active root script named there
4. this file's roadmap and current gaps

Current high-priority review areas:

1. per-turn environment and queue logging in runner and scanner
2. orchestrator child-log relay that duplicates important child lines into the orchestrator log
3. any long-lived per-turn state that can still grow during extended runs

## Current Gaps

- We can recover the task and mechanism well, but we still do not have hard evidence for the editor crash root cause.
- Log volume is now bounded by rotation, but logging is still verbose inside tight loops.
- We still need to review whether some loop-time `INFO` lines should become `DEBUG` or be sampled.

## Review Backlog

1. Resume from the obelisk and apparition branch first, not the old well branch.
2. Review bridge traversal behavior and loop handling where repeated old-bridge churn appears in logs.
3. Review whether repeated environment and queue dumps should stay at `INFO` in long frontier runs.
4. Keep updating this file when the active frontier, blockers, or review priority changes.