# Autonomous Mapping Proof

This project is only interesting if it can discover and promote new map truth that was not already hardcoded.

If the shared map is already complete, then the system is mostly demonstrating route replay and blind traversal. That may still be useful operationally, but it is not the core claim of Wagent.

## Core Claim

The core capability is:

- start from incomplete shared truth
- reach a frontier room
- discover a new exit, room, or transition chain through local probing
- promote that discovery into shared map truth only after runner confirmation

The important point is not that the bot can walk a known route. The important point is that it can extend the known route graph.

## What Counts As A Real Success

Treat a run as a real autonomous-mapping success only if all of the following are true:

1. The relevant edge or room was not already present in the starting shared map.
2. The system reached the frontier using existing confirmed knowledge only as staging transit.
3. A scanner or equivalent local-probing phase discovered a candidate transition through live interaction with the game.
4. Runner confirmation promoted the result into shared truth.
5. The resulting before/after diff of shared map truth shows a genuinely new confirmed edge or room.

Good examples:

- a previously unknown tomb action is confirmed to land in `dark cell`
- a puzzle affordance such as `root-covered wall` is turned into a confirmed route toward `underground passages`
- a previously unconfirmed local exit is promoted from candidate evidence into the shared map

Non-examples:

- replaying an already known path from `dark cell` to `antechamber`
- walking a route that was already written into `wagent_map_memory.json`
- collecting scanner observation text without any runner-confirmed promotion
- using a hand-authored complete map and then claiming the traversal itself proves autonomy

## Best Demo Modes

There are three useful ways to demonstrate the system.

### 1. Frontier Expansion Demo

This is the most practical and honest demo mode.

- Keep a real shared map with only confirmed staging routes.
- Choose a target room where at least one local branch is still unknown.
- Let the system use blind transit only to reach that staging room.
- Judge success only by whether a new edge or room is added after local probing and confirmation.

This is the recommended public demo because it isolates the actual value proposition: not generic travel, but frontier expansion.

### 2. Cold-Start Demo

This is the strongest conceptual demo, but usually the hardest operationally.

- Start from an empty or near-empty shared map.
- Use fresh accounts.
- Run the system long enough to bootstrap the earliest durable edges.

This proves the strongest autonomy claim, but it is also more fragile because tutorial handling, dark rooms, and account/session noise can dominate the run.

### 3. Candidate-Overlay Demo

This is useful when you want a controlled proof without mutating the main shared map immediately.

- Keep `wagent_map_memory.json` as the stable baseline.
- Let the scanner or runner write into a candidate overlay map first.
- Promote only after an explicit confirmation pass.

This is the cleanest option when showing cautious discovery instead of optimistic map mutation.

## Recommended Public Proof Protocol

Use this when you want to show value to another user or reviewer.

1. Save a copy of the starting shared map files.
2. Identify one frontier where the target exit or room is not yet present in shared truth.
3. Use fresh accounts so existing character position does not invalidate the run.
4. Run the workflow with a clear target room and, if appropriate, a scan target.
5. Save the ending shared map files.
6. Show a before/after diff of the confirmed map.
7. Tie the promoted edge back to the relevant log or orchestrator summary.

The proof artifact should answer four questions:

- what was unknown before the run?
- what did the scanner locally discover?
- what did the runner confirm?
- what changed in shared truth afterward?

## Cold-Start Proof Script

This repository now includes a dedicated proof runner:

```bash
./run_autonomous_mapping_proof.sh \
	--account-pool-file wagent_account_pool.local.json \
	--runner-target-room "corner of castle ruins" \
	--scanner-target-room "corner of castle ruins" \
	--proof-name obelisk_cold_start
```

What it does:

- creates an isolated proof directory under `artifacts/current/`
- starts from an empty shared map file
- starts from an empty shared route-memory file
- starts from an empty shared experience-memory file
- runs `bots.py` against those empty proof files instead of the repository baseline memory

This is the right shape of proof because it prevents the system from silently reusing the normal checked-in shared map and route table.

## What To Inspect After The Run

After the proof run, inspect:

- the proof map JSON
- the proof route-memory JSON
- the proof summary JSON
- the orchestrator log

Success means those isolated proof files gained genuine confirmed edges or rooms that were absent at the start.

## Important Caveat

Do not promise that a single cold-start run will fully reconstruct the entire world map.

That is a much stronger claim than the current system is designed to guarantee. The honest proof target is this instead:

- starting from empty or incomplete shared truth
- the system can grow the confirmed map by discovering and confirming previously unknown transitions

That is already enough to prove the core capability.

## Recommended Success Metrics

Prefer these metrics when describing system performance:

- new confirmed edges added to shared truth
- new room names added to shared truth
- time-to-confirmation for a previously unknown frontier edge
- ratio of candidate discoveries to confirmed promotions

Do not lead with these as the primary proof of value:

- total commands executed
- total rooms revisited
- total observations collected
- ability to traverse a complete prewritten map

## Existing Evidence In This Repository

The repository already contains evidence of frontier expansion behavior, including cases where live runs promoted previously unknown or unconfirmed edges into map truth or candidate overlays.

Examples already noted in repository memory include:

- `tomb of woman on horse -> dark cell`
- `tomb of the shield -> dark cell`
- `root-covered wall -> underground passages`
- tutorial exit confirmation such as `intro -> exit tutorial`

Those are the right shape of result because they are discovery-and-confirmation events, not just replay of a fully authored route table.

## README-Level Message

When describing the system briefly, use wording like this:

> Wagent is valuable when shared map truth is incomplete. Its core job is not to replay a prewritten route table, but to discover and confirm new frontier edges through live interaction with the world.

That sentence sets the correct bar for readers immediately.