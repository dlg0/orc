# amp-orchestrator

`amp-orchestrator` is a single-project backlog runner for Amp and `bd`.

It is intended to be launched inside one project repository, read that project's
Beads issues and dependencies, and then execute ready work conservatively with
one isolated git worktree per issue.

Current status: PRD only.

## Documents

- [Product requirements document](docs/prds/0001-amp-orchestrator-mvp.md)

## Core Product Shape

- Single-project only for the MVP
- Manual start only; never begins execution automatically on launch
- Supports pause and resume
- Uses `bd` as the system of record for issue readiness and dependency ordering
- Uses Amp as a per-issue worker, not as the long-lived orchestrator
