# Evennia Wagent

Wagent is an Evennia-based MUD exploration workflow with separate scanner and runner roles, shared map memory, and pluggable local or cloud LLM backends.

If you are opening this repository for the first time, start with `docs/quickstart.md`.

If you are resuming after a crash or taking over from another model, continue with the recovery-oriented sections below.

## Public Bootstrap

This repository tracks the Wagent workflow plus the Evennia game directory in `mygame/`.

- Default install path: `pip install -r requirements.txt`
- Optional advanced path: add the Evennia upstream repository at `vendor/evennia` as a submodule or editable checkout
- Local secrets stay in `.env`, exported environment variables, or `wagent_account_pool.local.json`
- The checked-in `wagent_account_pool.example.json` is only a template

The bootstrap helper `start_evennia.sh` is repository-relative and safe to run from any checkout location.

## Source Of Truth

The active workflow is rooted at the repository top level.

- Active entrypoints: `runner.py`, `scanner.py`, `bots.py`
- Active helper launchers: top-level `*_runner.py` and `*_scanner.py` wrappers only
- Non-authoritative copies: anything under `mygame/` unless the workflow is explicitly migrated there
- Legacy duplicate workflow scripts are archived under `artifacts/archive/legacy-mygame-workflow/`

Do not restore or edit the archived legacy workflow copies under `artifacts/archive/legacy-mygame-workflow/`. The live system currently uses the root-level versions.

## System Shape

The architecture is a two-layer exploration system.

- `scanner.py` discovers, probes, and records partial evidence
- `runner.py` confirms and promotes durable shared truth
- `bots.py` orchestrates scanner/runner handoff instead of collapsing both roles into one process

The most important design rules are:

1. Shared map and shared route memory are runner-owned truth.
2. Scanner output should be treated as observation or experience until runner confirmation.
3. Blind transit is only for reaching confirmed staging rooms, not for post-frontier exploration.
4. Parallel scanners may disagree; runner confirmation is the arbitration step.
5. Multi-step puzzle progress may be assembled from multiple partial runs before final confirmation.

## What Must Stay Stable

Keep these invariants unless there is an explicit architectural migration:

- Root scripts remain authoritative.
- `wagent_map_memory.json` is the main confirmed shared map.
- `wagent_route_memory.json` is the main confirmed shared route layer.
- Scanner-side observation memory is an unverified bus, not durable truth.
- Room-specific hacks should be the exception; prefer policy in data and orchestration.

## First Recovery Steps

If the editor crashed or context is lost, recover in this order:

1. Read `README.md`.
2. Read `docs/recovery-playbook.md`.
3. Read `docs/current-state.md` for the live objective, blockers, lessons, and next-review backlog.
4. Read `artifacts/current/recovery_status.json` to identify the last active script, task, targets, and runtime artifact paths.
5. Confirm the active root files you intend to touch.
6. Inspect the current shared truth files before changing code:
   - `wagent_map_memory.json`
   - `wagent_route_memory.json`
7. Check repository memory notes under `/memories/repo/` if the task is related to live proofs or routing behavior.

## Recovery Guarantee

This repository should be recoverable by a different model from repository state alone.

If the recovery chain is current, a replacement model should be able to do all of the following without chat history:

1. Recover the active goal and current frontier roadmap.
2. Identify the last active script, task type, targets, and runtime artifacts.
3. Continue the work from the correct frontier instead of re-exploring old branches blindly.
4. Review the relevant logs and summaries to reconstruct the last task before the crash.
5. Audit the most likely crash-risk code paths before resuming long runs.

If any of those are no longer true, update `docs/current-state.md`, `docs/recovery-playbook.md`, and `artifacts/current/recovery_status.json` before closing work.

## Documentation Map

- `docs/quickstart.md`: first-run setup, credentials, model provider configuration, and launch commands
- `docs/architecture.md`: current system model and workflow
- `docs/development-principles.md`: what to do and what not to do when modifying the system
- `WAGENT_DESIGN.md`: design note, workflow intent, and SOP for scanner/runner/orchestrator frontier expansion
- `docs/current-state.md`: current objective, known gaps, lessons, and review backlog
- `docs/recovery-playbook.md`: crash recovery and handoff procedure
- `docs/artifact-layout.md`: where logs, summaries, configs, and historical artifacts belong
- `artifacts/README.md`: archive intent and retention model

## Current Repository Policy

The root directory should contain only:

- active scripts
- active shared memory files
- current utility/config files that are genuinely part of the workflow
- example configuration files that are safe to commit
- top-level handoff documentation

Historical experiment bundles, one-off proof logs, and legacy run snapshots should be moved under `artifacts/archive/` when they are no longer active.

## Short Do / Don't

Do:

- modify root scripts when fixing live behavior
- preserve the scanner/runner split
- treat scanner findings as candidate evidence first
- verify map changes against runner confirmation
- update `docs/current-state.md` and `artifacts/current/recovery_status.json` expectations when the active recovery flow changes
- keep runtime log growth bounded with the built-in rotation settings before opening long-lived logs in the editor
- keep durable reasoning in Git-tracked docs, not only in chat

Do not:

- assume `mygame/` copies are live
- let blind transit drive unconfirmed frontier exploration
- let scanner promotion silently overwrite shared truth by default
- keep adding root-level ad hoc logs and snapshots without classification
- replace deterministic parser fallbacks with model-only guesses in fragile rooms