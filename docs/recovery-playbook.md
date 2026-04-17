# Recovery Playbook

## Use This After A Crash

When context is lost, recover the project in a fixed order.

1. Read `README.md`.
2. Read `docs/architecture.md`.
3. Read `docs/development-principles.md`.
4. Read `docs/current-state.md`.
5. Read `artifacts/current/recovery_status.json`.
6. Confirm the current task against the actual root files.
7. Inspect the active shared truth files before making assumptions.

## Files To Check First

Always verify these before code changes that affect routing, confirmation, or promotion.

- `wagent_map_memory.json`
- `wagent_route_memory.json`
- `artifacts/current/recovery_status.json`
- `docs/current-state.md`
- `WAGENT_DESIGN.md`
- `/memories/repo/live-proof-findings.md`
- `/memories/repo/route-memory-strategy.md`

## Root Authority Check

Before editing, answer these questions explicitly:

1. Am I changing the root-level script or a stale duplicate?
2. Is the file part of live workflow, or just historical scaffolding?
3. Is the intended output shared truth, observation, or a temporary proof artifact?

If the answer to question 1 is unclear, stop and resolve that first.

## Safe Workflow For New Work

1. Identify the target frontier or failure mode.
2. Confirm the current shared map and route baseline.
3. Decide whether the task is scanner discovery, runner confirmation, or orchestrator policy.
4. Prefer candidate maps or archived proof outputs for uncertain experiments.
5. After success, write the lesson into durable docs or repo memory.

## Safe Workflow For Live Promotion Proofs

1. Keep the authoritative code in the root workflow.
2. Use fresh or intentionally chosen account pools when routing proofs depend on starting position.
3. Separate candidate discovery from durable promotion.
4. Verify that new shared-map edges and route hops match the observed confirmation.
5. Record the proof lesson in repo memory if it changes project policy.

## Crash Review Workflow

Use this when the task is not only to resume work, but also to audit whether the last run may have contributed to the crash.

1. Read `artifacts/current/recovery_status.json` first and identify the last active component.
2. Open the log, summary, and observation-memory paths referenced there before searching broadly.
3. Reconstruct the final live task from `task_kind`, target fields, and the most recent summary/log pair.
4. Compare that reconstructed task against `docs/current-state.md` to confirm whether the run was on the primary frontier, a secondary branch, or a proof-only detour.
5. Inspect the hot loop code in the referenced root script before resuming long runs.
6. Separate evidence into two categories:
	- repository evidence that identifies the last task and route plan
	- code or runtime evidence that could plausibly explain a crash or editor slowdown
7. If the crash review changes project policy, write the lesson back into docs or repo memory before closing the task.

The current default crash-review suspicion order is:

1. loop-time log volume
2. duplicated orchestrator child-log relay
3. unbounded or weakly bounded in-memory per-turn state

## Handoff Rule

A future model should be able to resume from repository state alone.

That means:

- docs hold the architecture and process
- `docs/current-state.md` holds the current human-readable objective and backlog
- `artifacts/current/recovery_status.json` holds the last known runtime task and script paths
- repo memory holds compact verified lessons
- root does not act as an unstructured dump of logs and JSON snapshots

If a task teaches something durable, write it down before closing the work.