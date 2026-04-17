# Architecture

## Core Model

The system is intentionally split into discovery and confirmation.

- `scanner.py` explores locally, probes unknown exits or objects, and writes observation-style output.
- `runner.py` is the confirmation role. It replays or validates candidate discoveries and is the default owner of shared-truth promotion.
- `bots.py` coordinates the handoff between the two roles.

This is not an abstract target architecture. It already matches the current root-level implementation.

## Memory Layers

There are two different classes of memory.

### Confirmed Shared Truth

These files are the durable layer used for real routing and confirmed state.

- `wagent_map_memory.json`
- `wagent_route_memory.json`

Only runner-confirmed results should be treated as shared truth by default.

### Unverified Or Partial Memory

These files may contain useful but unconfirmed information.

- scanner observation memories
- run-local experience memories
- run summaries and orchestrator snapshots
- candidate or overlay maps used for quarantine or proof work

This layer is intentionally allowed to be incomplete, noisy, or partially wrong.

## Travel Versus Frontier Work

Blind transit has one narrow purpose: get to a confirmed staging room quickly.

- It may use confirmed route hops.
- It should stop being the dominant policy once the agent reaches the frontier staging room.
- From that point onward, local exploration and confirmation logic take over.

The route table is therefore a confirmed transit layer, not a frontier-exploration policy.

## Scanner / Runner Handoff

The intended loop is:

1. Runner or scanner reaches a confirmed staging room.
2. Scanner explores locally and produces observation or partial experience.
3. `bots.py` detects useful scanner deltas and hands the candidate back to runner.
4. Runner confirms the candidate in place or by replay.
5. Shared map and route memory are updated only after confirmation.

This model is important for dark-cell style puzzles where no single run may discover the entire chain at once.

## Conflict Handling

Parallel scanners may produce conflicting or incomplete results. That is acceptable.

- scanner outputs are candidate evidence
- runner confirmation is arbitration
- candidate/overlay maps are valid quarantine tools when proving a new edge before main-map promotion

## Important Nuances

The following points are part of the architecture, not edge-case commentary.

1. Safety rails and protocol rails are different. Some rules protect the agent from parser traps; others enforce the intended workflow boundary.
2. Scanner output should be modeled as observation, not truth.
3. Runner confirmation does not always need a full end-to-end expedition; sometimes the unit of validation is a local replay at the correct frontier room.
4. Observation memory is effectively a shared unverified handoff bus, not purely private bot memory.
5. Deterministic fallback logic still matters in dark rooms, parser traps, and tutorial/intro transitions.