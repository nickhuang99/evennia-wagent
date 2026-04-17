# Wagent Design Notes

This file is still useful as a design note, but `README.md` is now the primary handoff entrypoint for crash recovery and model takeover.

## Purpose

This file records the intended design for the root-level bot workflow so the system can be recovered after crashes without relying on chat history.

Active entrypoints:

- `runner.py`
- `scanner.py`
- `bots.py`

## Core Principles

### Collective Wisdom

The system should benefit from many bots contributing partial discoveries.

- A final discovery may require multiple partial successes from multiple bots.
- Dark-cell style puzzles may only become solvable after shared experience accumulates enough partial evidence.
- Shared experience should help later bots avoid repeating dead loops and should preserve useful partial progress even before confirmation.

### Role Separation

Scanner and runner have different jobs.

- `scanner.py` explores frontier candidates and contributes observations, including partial success, failure, and ambiguous evidence.
- `runner.py` is the confirmation role. It should verify candidate discoveries and promote only confirmed results into shared durable memory.
- `bots.py` should orchestrate handoff between exploration and confirmation instead of collapsing both responsibilities into one role.

### Experience Versus Memory

Experience and memory are intentionally different.

- Experience is broad. It can include partial findings, failures, generic heuristics, or observations that later turn out to be wrong because of bugs or random events.
- Memory is promoted truth. It should contain only useful, confirmed information such as shared map memory and shared route memory.
- Promotion from experience into memory should happen through runner confirmation.

## Blind Traversal

Blind traversal is intentionally narrow in scope.

- Blind traversal is for quickly reaching a confirmed frontier staging room using runner-confirmed route hops.
- Blind traversal should not continue beyond the confirmed frontier into unconfirmed space.
- Once the bot reaches the frontier room, local exploration and confirmation logic must take over.
- Route-table hops should therefore represent confirmed transit, not speculative frontier actions.

Implication:

- The route table is a confirmed travel layer, not a frontier-exploration policy.

## Frontier Discovery Workflow

The intended workflow is:

1. A scanner reaches a confirmed frontier staging room using blind traversal if available.
2. The scanner explores candidate frontier exits and records results into shared experience.
3. Those results may include failures, partial successes, puzzle hints, or ambiguous observations.
4. A runner is then sent to confirm the candidate discovery.
5. Only after runner confirmation should shared map memory and shared route memory be updated.

This means scanners can contribute useful knowledge without directly promoting unconfirmed frontier observations into durable shared truth.

## Dark Cell Principle

Dark-cell style progression should follow the same cooperative model.

- One bot may discover a useful combination fragment.
- Another bot may discover a follow-up affordance.
- A later runner may confirm the successful chain.
- The final durable memory may therefore be assembled from multiple runs and multiple roles rather than a single perfect session.

The system should exploit this instead of expecting one scanner to solve the entire loop alone.

## Design Constraints For Future Changes

- Keep root-level scripts as the active workflow unless explicitly changed.
- Preserve the distinction between shared experience and confirmed memory.
- Preserve runner ownership of promotion into shared map memory and shared route memory.
- Treat blind traversal as confirmed transit only.
- Use shared experience to help bots escape loops and accumulate puzzle knowledge across runs.

## Current Gap To Close

Blind traversal to confirmed frontier rooms exists, but the broader cooperative promotion model is not yet fully enforced everywhere.

The main gap is not route-based staging travel. The main gap is how scanner-side frontier findings are accumulated as shared experience and then promoted by runner confirmation into durable map and route memory.

## SOP

Use this procedure for repeatable frontier expansion.

1. Keep `runner.py`, `scanner.py`, and `bots.py` as the only active workflow.
2. Start from a trusted confirmed base in shared map memory and shared route memory.
3. Treat `wagent_experience_memory.json` as disposable development memory unless the run has been validated.
4. Launch orchestration with scanner observation memory enabled and with the runner target set to the confirmed staging room nearest the frontier.
5. Let the runner use only confirmed route hops to reach the staging room.
6. Let the scanner explore locally from that staging room and stop as soon as it creates either new observation or new confirmed memory.
7. Hand control back to the runner with `stop_on_target=0` and target equal to the scanner's last confirmed room so the runner performs the confirmation pass in place.
8. Accept durable promotion only from runner slices: confirmed exits must land in shared map memory and confirmed transit hops must land in shared route memory.
9. If a run falls into a dark or blind room, prioritize recovery affordances before any route fast path.
10. If debugging churn pollutes shared failed-action memory, truncate it and rerun from the same confirmed map and route baseline.
11. After each orchestration cycle, verify both outputs: new exits in map memory and matching confirmed hops in route memory.
12. Only then reuse the newly confirmed room as the next staging target for another expansion cycle.