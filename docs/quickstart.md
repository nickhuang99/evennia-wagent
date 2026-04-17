# Quickstart

## What This Repo Contains

This repository tracks the Wagent workflow and the Evennia game directory at `mygame/`.

It does not vendor the Evennia engine by default. The default bootstrap path installs `evennia` from `requirements.txt` so a fresh checkout can work without restoring an old local environment.

If you prefer a vendored Evennia source checkout later, place the upstream repository at `vendor/evennia` as a git submodule or editable checkout. `start_evennia.sh` will install it automatically when that directory exists.

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

## 2. Bootstrap Evennia

From `mygame/`:

```bash
evennia migrate
evennia collectstatic --noinput
evennia start
```

The bundled `mygame/` directory is the game directory generated for Evennia. The live Wagent workflow remains in the repository root.

## 3. Configure Credentials

Do not commit real credentials.

Use one of these options locally:

1. Export `EVENNIA_USER` and `EVENNIA_PASS`.
2. Create `wagent_account_pool.local.json` from `wagent_account_pool.example.json`.
3. Generate a pool file with:

```bash
python provision_account_pool.py --pool-file wagent_account_pool.local.json --count 2
```

`wagent_account_pool.local.json` is gitignored on purpose.

## 4. Configure A Model Provider

The workflow is not restricted to Ollama.

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