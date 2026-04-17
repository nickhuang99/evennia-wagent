# Quickstart

## What This Repo Contains

This repository tracks the Wagent workflow and includes a bundled default Evennia game directory at `mygame/`.

Wagent does depend on Evennia, but it does not vendor the Evennia engine by default. The default bootstrap path installs `evennia` from `requirements.txt` so a fresh checkout can work without separately cloning the Evennia source repository.

If you prefer a vendored Evennia source checkout later, place the upstream repository at `vendor/evennia` as a git submodule or editable checkout. `start_evennia.sh` will install it automatically when that directory exists.

The bundled `mygame/` directory is for the default runnable setup. If you already have an Evennia game directory, you can reuse it instead of the bundled one.

## Dependency Summary

- Required runtime dependency: `evennia`
- Default install source: `requirements.txt`
- Optional advanced source checkout: `vendor/evennia`
- Optional game directory override: `WAGENT_GAME_DIR=/path/to/game`

If your goal is only to run this project, the normal path is to clone this repository and install dependencies here. A separate Evennia git checkout is optional, not required.

## 1. Create a Python Environment

From the repository root:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Or just run:

```bash
./start_evennia.sh
```

That script creates `venv/` if needed, installs Evennia and the other Python dependencies, and runs the initial Evennia bootstrap commands.

If you already have your own Evennia game directory, use:

```bash
WAGENT_GAME_DIR=/path/to/your/game ./start_evennia.sh
```

For a minimal first run after checkout:

```bash
git clone git@github.com:nickhuang99/evennia-wagent.git
cd evennia-wagent
./start_evennia.sh
cd mygame
../venv/bin/evennia start
cd ..
source venv/bin/activate
python bots.py
```

## 2. Bootstrap Evennia

From `mygame/`:

```bash
evennia migrate
evennia collectstatic --noinput
evennia start
```

The bundled `mygame/` directory is the default Evennia game directory shipped with this repository. The live Wagent workflow remains in the repository root and can also target another Evennia game directory when you set `WAGENT_GAME_DIR`.

## 3. Configure Credentials

Do not commit real credentials.

Use one of these options locally:

1. Export `EVENNIA_USER` and `EVENNIA_PASS`.
2. Create `wagent_account_pool.local.json` from `wagent_account_pool.example.json`.
3. Generate a pool file with:

```bash
python provision_account_pool.py --pool-file wagent_account_pool.local.json --count 2
```

If you are not using the bundled `mygame/`, add `--game-dir /path/to/your/game` or export `WAGENT_GAME_DIR` first.

`wagent_account_pool.local.json` is gitignored on purpose.

## 4. Configure A Model Provider

The workflow is not restricted to Ollama.

### Minimum Model Requirements

Any replacement model should satisfy these baseline requirements:

- Follows short instruction prompts reliably without adding long meta commentary
- Returns strict JSON when asked, without markdown code fences
- Produces short action strings instead of essays
- Handles noisy room text and parser feedback without collapsing into refusal or generic chat behavior
- Responds consistently within the configured timeout window, which defaults to 30 seconds

If a model fails those basics, the workflow will degrade quickly even if the raw language quality looks good.

### Compatibility Status

- Validated baseline: `qwen2.5:7b` via Ollama
- Configured fallback candidates: `qwen2.5:3b`, `llama3.2:3b`
- Documented API example: `gpt-4.1-mini` via an OpenAI-compatible chat endpoint

Only the validated baseline should be treated as a known-good reference configuration. The fallback candidates and cloud example are supported configuration shapes, not a promise that they have been equally exercised on this repository.

### Local Ollama

```bash
export WAGENT_MODEL_API_KIND=ollama
export WAGENT_MODEL=qwen2.5:7b
export WAGENT_MODEL_API_URL=http://localhost:11434/api/generate
```

### OpenAI-Compatible Cloud Endpoint

```bash
export WAGENT_MODEL_API_KIND=openai-chat
export WAGENT_MODEL=gpt-4.1-mini
export WAGENT_MODEL_API_URL=https://api.openai.com/v1/chat/completions
export WAGENT_MODEL_API_KEY=replace-me
```

Backward compatibility is preserved for older local setups that still export `OLLAMA_MODEL` and `OLLAMA_API`.

### Sanity Test Before Running Bots

Before you start the full workflow with a new model, run:

```bash
source venv/bin/activate
python model_sanity_check.py --print-response
```

Expected result:

- `MODEL SANITY CHECK PASSED`
- a short action such as `look`
- valid JSON output without markdown fences

If it fails, fix the model endpoint or switch to a more instruction-stable model before running `bots.py`, `scanner.py`, or `runner.py`.

## 5. Run The Workflow

Typical entrypoints from the repository root:

```bash
python bots.py
python scanner.py --target-room "corner of castle ruins"
python runner.py --target-room "corner of castle ruins"
```

Role-specific wrappers such as `bridge_runner.py` and `target_scanner.py` are also valid public entrypoints.

## 6. Repository Rules

- Root `scanner.py`, `runner.py`, and `bots.py` are canonical.
- `mygame/` is the Evennia game directory, not the live workflow copy.
- Runtime logs, temporary observation memories, and local account pools stay out of git.
- Shared truth files such as `wagent_map_memory.json` and `wagent_route_memory.json` may be tracked when they represent the intended baseline state.