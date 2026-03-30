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

# Diagnose common issues
orc doctor            # report only
orc doctor --fix      # apply safe auto-remediations
```

## Configuration

Configuration lives in `.orc/config.yaml`. Run `orc init-config` to generate a
file with all defaults.

| Setting | Default | Description |
|---------|---------|-------------|
| `base_branch` | `"main"` | Target branch for worktree creation, rebasing, and merging. |
| `max_workers` | `1` | Number of parallel workers. **Must be 1 for the MVP.** |
| `require_clean_worktree` | `true` | Require a clean working tree (no uncommitted changes) before starting. |
| `auto_push` | `true` | Automatically push to origin after a successful merge and verification. |
| `verification_commands` | `[]` | Shell commands (e.g. `["pytest", "ruff check"]`) run against the merged branch before pushing. |
| `amp_mode` | `"smart"` | Amp agent mode used for issue execution (`"smart"`, `"deep"`, `"rush"`, etc.). |
| `use_decomposition_preflight` | `true` | Run a preflight pass that decomposes complex issues into subtasks before execution. |
| `enable_evaluation` | `true` | Run an LLM-based evaluation step after Amp finishes work on an issue. |
| `evaluation_mode` | `null` | Amp mode override for the evaluation step. Falls back to `amp_mode` when `null`. |
| `evaluation_timeout` | `900` | Maximum seconds allowed for the evaluation step. |
| `context_window_warn_threshold` | `0.85` | Fraction (0–1) of context window usage at which a warning is logged. |
| `summary_mode` | `"self-report"` | How thread summaries are generated: `"self-report"`, `"rush-extract"`, or `"stream-json"`. |
| `summary_amp_mode` | `"rush"` | Amp mode used when extracting summaries (applies to `"rush-extract"` mode). |
| `fail_fast` | `false` | Stop the orchestrator loop on the first issue failure instead of continuing to the next issue. |

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
