# Evennia Wagent

Wagent is an Evennia-based MUD exploration workflow for driving scanner and runner agents through a text world, capturing shared map knowledge, and coordinating local puzzle probing with durable route confirmation.

It is built around a simple division of labor: scanners discover and probe, runners confirm and promote stable truth. The workflow can use local Ollama models or OpenAI-compatible cloud APIs through the same configuration surface.

For a full first-run setup, start with [docs/quickstart.md](docs/quickstart.md).

## About

This repository contains two things:

- the live Wagent workflow at the repository root
- a bundled default Evennia game directory in `mygame/`

Wagent does depend on Evennia. The default setup installs Evennia from `requirements.txt`, so a fresh checkout can run without separately cloning the Evennia source repository. If you prefer a source checkout later, you can place the Evennia upstream repository at `vendor/evennia` and the bootstrap script will use it automatically.

The bundled `mygame/` directory is there to make this repository runnable out of the box. It is not a hard architectural requirement for the workflow itself. If a user already has an Evennia game directory, they can point the bootstrap helpers at that directory instead of using `mygame/`.

## Dependency Model

- Required dependency: `evennia`
- Default install path: install `evennia==4.5.0` from `requirements.txt`
- Optional advanced path: place an Evennia source checkout at `vendor/evennia` and `start_evennia.sh` will install it in editable mode
- Optional runtime path: use `WAGENT_GAME_DIR=/path/to/game ./start_evennia.sh` to bootstrap against an existing Evennia game directory

If you only want to run this project, you do not need to clone Evennia separately.

## Features

- Autonomous frontier expansion from incomplete shared map truth
- Separate scanner and runner roles instead of a single monolithic bot
- Shared map and route memory for confirmed navigation knowledge
- Local observation memory for scanner-side evidence that has not been promoted yet
- Provider-agnostic model calls with support for Ollama and OpenAI-compatible chat endpoints
- Portable repository-relative bootstrap via `start_evennia.sh`
- Public-repo-safe examples for environment variables and account-pool setup

## Core Value

This project is valuable when the shared map is incomplete.

If the map is already fully written, then a simple blind-traversal script can replay that route table and most of Wagent's value disappears. The core capability here is different: reach a frontier room, probe locally, discover a new candidate transition, and promote it into shared truth only after confirmation.

If you want to demonstrate that capability rather than just route replay, see [docs/autonomous-mapping-proof.md](docs/autonomous-mapping-proof.md).

For an isolated cold-start proof that ignores the repository baseline map and route files, run `./run_autonomous_mapping_proof.sh --account-pool-file wagent_account_pool.local.json ...`.

## Model Guidance

- Known-good baseline: `qwen2.5:7b` via Ollama
- Supported API shapes: Ollama generate endpoint and OpenAI-compatible chat completions
- Before adopting a new model, run `python model_sanity_check.py --print-response`
- A compatible model must follow short instructions, return strict JSON when asked, and emit short action strings instead of chatty prose

## Quick Start

1. Create a Python environment and install dependencies.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If installation fails while downloading `cryptography` or another large wheel, that is usually a network problem rather than an Evennia dependency problem. In that case, retry with a longer timeout and more retries:

```bash
python -m pip install --upgrade pip
python -m pip install --default-timeout=100 --retries 10 --resume-retries 20 -r requirements.txt
```

If your connection to the default PyPI host is unstable, use a closer mirror for the install step, for example:

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=100 --retries 10 --resume-retries 20 -r requirements.txt
```

Or use:

```bash
./start_evennia.sh
```

That script creates `venv/` if needed, installs Python dependencies including Evennia, then runs the initial Evennia bootstrap tasks inside `mygame/`.
It also creates the local Evennia runtime directories and a local `server/conf/secret_settings.py` stub when they are missing in a fresh checkout.

If you already have your own Evennia game directory, you can use it instead:

```bash
WAGENT_GAME_DIR=/path/to/your/game ./start_evennia.sh
```

2. Configure local credentials and model access.

- Review `.env.example`
- Keep real credentials in exported environment variables or `wagent_account_pool.local.json`
- Use `wagent_account_pool.example.json` only as a template

3. Start Evennia from the repository checkout.

```bash
cd mygame
evennia migrate
evennia collectstatic --noinput
evennia start
```

If `evennia migrate` prints a warning like `Your models in app(s) ... have changes that are not yet reflected in a migration`, treat it as non-blocking in this repository as long as:

- `evennia migrate` exits successfully
- `evennia check` reports no issues

Do not run `makemigrations` just to silence that warning unless you are intentionally changing Django/Evennia models.

4. Run the workflow from the repository root.

```bash
python bots.py
python scanner.py --target-room "corner of castle ruins"
python runner.py --target-room "corner of castle ruins"
```

In other words, the normal first-run path after `git clone` is:

```bash
git clone git@github.com:nickhuang99/evennia-wagent.git
cd evennia-wagent
./start_evennia.sh
cd mygame && ../venv/bin/evennia start
cd ..
source venv/bin/activate
python bots.py
```

For the full setup path, model-provider examples, and account-pool generation flow, see [docs/quickstart.md](docs/quickstart.md).

## Project Layout

- `scanner.py`, `runner.py`, and `bots.py` are the canonical workflow entrypoints
- top-level `*_runner.py` and `*_scanner.py` files are helper launchers for specific tasks
- `mygame/` is the bundled default Evennia game directory, not the source of truth for the live workflow
- `artifacts/` holds retained outputs, archive material, and recovery data

## Recovery And Handoff

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
- `docs/autonomous-mapping-proof.md`: what counts as real autonomous mapping, and how to demonstrate it honestly
- `run_autonomous_mapping_proof.sh`: isolated cold-start proof runner using empty shared map/route/experience files
- `model_sanity_check.py`: connectivity and JSON-output sanity check for a candidate model endpoint
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