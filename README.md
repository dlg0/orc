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

# Explore raw Beads dispatch behavior before changing scheduler policy
orc explore dispatch --all
```

## Dispatch Policy

`orc` has two distinct concepts:

- `bd ready` is the Beads-owned answer to "what is ready right now?"
- Orc dispatchability is the Orc-owned answer to "what is safe to hand to a worker right now?"

The exploration harness behind `orc explore dispatch` established the policy the
main scheduler now follows. That policy is:

1. Preserve the exact order returned by `bd ready`.
2. Dispatch only worker leaf types: `task`, `bug`, `feature`, and `chore`.
3. Never dispatch control/container nodes such as `epic`, `integration`, or any issue that currently has children.
4. Exclude `in_progress` work from a new dispatch frontier, even if Beads ever includes it in `bd ready`.
5. Fail closed on unsupported types, and suppress any ready descendants that sit inside an unsupported container subtree.

This keeps Orc predictable: Beads decides readiness and ordering, while Orc adds
only a small set of explicit safety filters.

## Dispatch Examples

The exploration harness records both raw Beads observations and the Orc dispatch
frontier. These examples come from live harness runs against the current `bd`
behavior:

| Scenario | `bd ready` order | Orc dispatch frontier | Why |
|---------|------------------|-----------------------|-----|
| Open epic with children | `E.2`, `E.1`, `E` | `E.2`, `E.1` | Beads can return child tasks before the parent epic; Orc preserves that order and filters out the container. |
| Nested integration container | `P.2`, `P.1`, `I.3`, `I.2`, `I.1`, `I`, `P` | `P.2`, `P.1`, `I.3`, `I.2`, `I.1` | `integration` behaves like a real parent/container in Beads, so Orc treats it as control-only once classified. |
| Unknown custom container | `X.1`, `X`, `P` | `(none)` from the `X` subtree | Unsupported container semantics are treated as unsafe; Orc suppresses the subtree until the type is classified. |
| In-progress sibling | `Open` | `Open` | Current Beads builds exclude `in_progress` work from `bd ready`, and Orc keeps the same behavior by default. |

## Exploration CLI

Use `orc explore dispatch` to validate scheduler assumptions before changing the
production queue logic.

```bash
# Run the whole scenario suite and write markdown + JSON reports
orc explore dispatch --all

# Run one scenario by name
orc explore dispatch --scenario open-epic-with-children

# Keep temporary Beads sandboxes for manual inspection
orc explore dispatch --scenario nested-integration-container --keep-sandbox
```

By default, reports are written under `.orc/explore/dispatch-<timestamp>/`.
Each scenario gets:

- `report.md` - human-readable scenario definition, raw `bd` observations, Orc plan, and mismatches
- `report.json` - machine-readable artifact for regression checks or future tooling

Exit codes:

- `0` - all selected scenarios matched expectations
- `1` - one or more scenarios ran but mismatched expectations
- `2` - one or more scenarios failed during sandbox setup or observation

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

## Evaluation Follow-Ups

When post-merge evaluation is enabled, Orc runs an independent evaluation step
after Amp finishes work and the merged result passes local verification.

If that evaluation fails, Orc automatically creates a follow-up `bd` issue in
the current project's Beads backlog. The follow-up is created as a sibling of
the original issue, includes the evaluation summary and reported gaps, and the
original issue is then closed. The run is recorded as `completed_with_followup`.

This behavior is controlled by `.orc/config.yaml`, primarily via
`enable_evaluation`, `evaluation_mode`, and `evaluation_timeout`. There is no
implemented `ORC_SELF_REPORT_*` environment-variable flow for filing bug reports
into the Orc repo itself; those env vars were discussed in planning but are not
read by the shipped code today. To disable automatic follow-up issue creation,
disable evaluation.

## TUI Dashboard

The TUI (`orc tui`) provides a live dashboard for monitoring the orchestrator.

### Queue Semantics

The **Dispatch Frontier** table shows the filtered set of issues that Orc
considers safe to hand to a worker, *not* the raw output of `bd ready`.  The
distinction matters because Orc applies dispatch-safety filters on top of Beads
readiness:

| Concept | Source | Description |
|---------|--------|-------------|
| **Beads ready** | `bd ready` | All issues Beads considers ready to work on. |
| **Policy-skipped** | Orc dispatch policy | Ready items filtered out because they are containers (epics, integration issues), have children, or are unsupported types. |
| **Held (ready)** | Orc state | Dispatchable items currently held due to prior failures (transient errors, needs-rework, conflicts). |
| **Runnable** | Orc dispatch policy | Dispatchable items not held — these are what the scheduler will actually pick up. |

These counts appear in the **Status** panel.  When policy-skipped items exist,
grouped skip reasons are shown beneath the counts and in the queue diagnostics
area.

The default queue view preserves the exact order returned by Beads.  Local
view toggles (press `o`) let the operator re-order by age without affecting
dispatch order.

When the queue refresh fails (e.g. `bd ready` times out), the TUI preserves
the last successful queue view and shows a queue-specific error without
advancing the queue-refresh timestamp.

## Architecture

- **Queue Manager** — reads raw `bd ready`, preserves Beads ordering, and applies Orc dispatch-safety filtering for containers, unsupported types, and local holds
- **Worktree Manager** — creates isolated git worktrees from `origin/main`
- **Amp Runner** — invokes Amp per-issue (stub adapter for MVP)
- **Merge Manager** — rebase, verify, merge, push, and `bd close`
- **State Store** — durable JSON state in `.orc/`
- **Event Log** — append-only JSONL event log

## Documents

- [Product requirements document](docs/prds/0001-orc-mvp.md)
- [Dispatch exploration PRD](docs/prds/orc-beads-dispatch-exploration-prd.md)
- [Dispatch policy derived from exploration](docs/prds/orc-epic-orchestration-prd.md)

## Design Principles

- Single-project only for the MVP
- Manual start only; never begins execution automatically on launch
- One worker, one worktree per issue
- `bd` is the source of truth for issue readiness and dependency ordering
- Amp is a per-issue worker, not the long-lived orchestrator
- Issues are only closed after merge succeeds
