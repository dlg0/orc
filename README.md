# orc

`orc` is a single-project backlog runner for Amp and `bd`.

It launches inside one project repository, reads that project's Beads issues
and dependencies, and executes ready work conservatively with one isolated git
worktree per issue.

## Quick Start

```bash
# Install
uv sync

# Initialize config (optional)
orc init-config

# Check status
orc status

# Start processing issues
orc start

# Pause after the current issue finishes
orc pause

# Resume from paused state
orc resume

# Stop gracefully
orc stop

# Inspect a specific issue run
orc inspect <issue-id>

# View recent event log
orc logs
```

## Architecture

- **Queue Manager** — reads `bd ready --json`, selects next issue by priority/age
- **Worktree Manager** — creates isolated git worktrees from `origin/main`
- **Amp Runner** — invokes Amp per-issue (stub adapter for MVP)
- **Merge Manager** — rebase, verify, merge, push, and `bd close`
- **State Store** — durable JSON state in `.orc/`
- **Event Log** — append-only JSONL event log

## Documents

- [Product requirements document](docs/prds/0001-orc-mvp.md)

## Design Principles

- Single-project only for the MVP
- Manual start only; never begins execution automatically on launch
- One worker, one worktree per issue
- `bd` is the source of truth for issue readiness and dependency ordering
- Amp is a per-issue worker, not the long-lived orchestrator
- Issues are only closed after merge succeeds
