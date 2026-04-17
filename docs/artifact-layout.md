# Artifact Layout

## Goal

The repository root should remain readable. Historical run products still matter, but they should not be mixed with live entrypoints and shared truth.

## Root-Level Files That Are Expected To Stay

These categories may stay at the top level.

- active scripts and wrappers
- active shared memory files such as `wagent_map_memory.json` and `wagent_route_memory.json`
- safe example config files such as `.env.example` and `wagent_account_pool.example.json`
- top-level handoff documentation

## Files That Should Move Out Of Root Once Historical

Move these under `artifacts/archive/` after the run is no longer active.

- one-off proof logs
- orchestrator snapshots for completed experiments
- temporary candidate maps for retired proofs
- per-run observation, experience, and run-memory bundles tied to a named experiment
- obsolete account-pool snapshots created for a specific demo or proof only

## Archive Structure

Use the following folders.

- `artifacts/archive/legacy-run-bundles/`: named experiment bundles and associated JSON snapshots
- `artifacts/archive/manual-logs/`: manual runner proof logs
- `artifacts/archive/orchestrator-logs/`: older orchestrator logs that are no longer live references
- `artifacts/archive/account-pools/`: retired pool snapshots tied to specific demos

## Classification Rule

Keep an artifact in root only if at least one of these is true.

1. A root script uses it by default.
2. It is the current shared truth file.
3. It is the current active config or pool for live work.
4. The current task is still actively reading or writing it.

Otherwise, archive it.

Local account pools and local secrets are an exception: keep them untracked even when active.

## Documentation Rule

Do not rely on raw logs as the durable handoff layer.

- logs preserve evidence
- docs preserve meaning
- repo memory preserves compact verified lessons

For active crash recovery, `artifacts/current/recovery_status.json` is the machine-readable runtime snapshot that points back to the relevant active logs and summaries.

Active runtime logs now rotate by default. Use `WAGENT_LOG_MAX_BYTES` and `WAGENT_LOG_BACKUP_COUNT` to tune the cap when a proof needs longer retention.

When an archived artifact matters, summarize why it matters in docs or repo memory.