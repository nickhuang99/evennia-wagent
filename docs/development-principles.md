# Development Principles

## Non-Negotiable Rules

1. Root-level `runner.py`, `scanner.py`, and `bots.py` are the live workflow until an explicit migration says otherwise.
2. Runner owns durable promotion into shared map and route memory by default.
3. Scanner findings are useful, but they are candidate evidence until confirmed.
4. Blind transit is for confirmed travel only.
5. Architecture knowledge must be written into Git-tracked docs or repo memory, not left in chat history alone.
6. Real credentials, account pools, and machine-local runtime paths must not be committed.

## Preferred Change Style

Bias toward data and policy, not room-by-room branching.

Good:

- priority room-action tables
- overlay or candidate maps for quarantine
- observation handoff rules
- route-memory and target-room discipline
- orchestrator policies for scanner/runner exchange

Use room-specific if/else only when the parser or world mechanics force it and no generalizable policy exists.

## What To Preserve

Preserve these boundaries when changing code:

- scanner discovers
- runner confirms
- bots orchestrates
- shared map/route remain confirmation products
- observation memory remains allowed to be noisy

## What Needs Extra Care

These are the points that should be reconsidered before large refactors.

1. Do not merge safety heuristics and architecture rules into one undifferentiated layer.
2. Do not describe observation memory as if it were authoritative.
3. Do not assume every confirmation must be a full replay from game start.
4. Do not remove deterministic fallback behavior from parser-heavy rooms unless live proof shows parity.
5. Do not let root-vs-`mygame/` ambiguity reappear in docs, scripts, or task handoff.

## Operational Do / Don't

Do:

- inspect current shared files before proving a new frontier edge
- keep live-proof lessons in `/memories/repo/` and in docs when they become policy
- use candidate maps when validating uncertain edges before main promotion
- keep artifact sprawl out of the root once a run is historical
- keep only example config in git and keep local pool files ignored

Do not:

- edit duplicate `mygame/` copies by accident
- promote scanner output directly into shared truth unless explicitly enabling a controlled override
- use blind routing as a replacement for in-room exploration
- treat raw logs as the handoff mechanism instead of summarizing the lesson in docs