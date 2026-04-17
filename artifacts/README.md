# Artifacts

This directory holds non-source run products that are still worth keeping after they stop being active root-level files.

## Retention Model

- Root keeps live entrypoints and currently active shared state.
- `artifacts/current/` keeps live runtime logs, live observation memories, current orchestration summaries, and the active `recovery_status.json` runtime snapshot.
- `artifacts/archive/` keeps historical experiment bundles, proof logs, and retired configs.
- Durable reasoning belongs in docs and repo memory, not only in raw logs.

Archive content is organized so that future work can recover evidence without turning the repository root into an unmanaged dump.