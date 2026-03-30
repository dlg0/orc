# PRD: orc MVP

## Summary

Build a single-project orchestrator that runs inside an existing repository,
reads its `bd` dependency graph, and drives Amp through ready issues until the
backlog is exhausted or the operator pauses execution.

The orchestrator owns queueing, worktree lifecycle, retries, merge sequencing,
and durable state. Amp remains a per-issue worker that receives one bounded
issue at a time, performs a decomposition preflight, implements or decomposes
the work, runs verification, and returns a structured result.

The MVP is intentionally conservative:

- Single project only
- Manual start only
- Default concurrency of one worker
- One issue per worktree and branch
- Merge to `main` only after verification and rebase
- Close `bd` issues only after merge succeeds

## Problem

Amp users can invest substantial effort in producing a high-quality `bd`
backlog with dependencies, priorities, acceptance criteria, and scoped tasks,
but still need to manually launch and supervise execution issue by issue.

The missing capability is a reliable backlog runner that:

- trusts `bd` as the source of truth for readiness
- avoids giant, context-busting issue executions
- isolates changes to avoid branch contamination and overlap
- can stop, resume, and survive interruptions
- creates follow-up work when execution discovers new prerequisites or adjacent
  scope

## Goals

1. Execute a single repository's ready `bd` issues with minimal human
   supervision.
2. Keep orchestration outside Amp so queueing, retries, locks, and recovery are
   deterministic and restartable.
3. Run each issue in a fresh git worktree and branch rooted at the latest
   `origin/main`.
4. Perform a lightweight decomposition preflight before implementation.
5. Allow the operator to pause, resume, inspect, and stop safely.
6. Produce an auditable event log and machine-readable run state.
7. Keep the MVP simple enough that another agent can finish it quickly.

## Non-Goals

1. Multi-project orchestration.
2. Automatic execution on program launch.
3. Aggressive parallelism in the MVP.
4. Full PR review automation or merge queue integration.
5. Rich cloud scheduling or remote distributed workers.
6. Replacing `bd`, Amp, or git hosting workflows.

## User Experience

## Primary User

A developer with a repository that already uses `bd`, has a ready backlog, and
wants the system to work through it without repeatedly launching each issue by
hand.

## Entry Model

The user runs the orchestrator from within the target project directory, for
example:

```bash
orc status
orc start
```

The orchestrator infers project context from the current working directory:

- local git repo
- local `bd` project
- repository root
- configured branch policy

The orchestrator does not start work automatically on launch. A user must
explicitly request `start` or trigger execution through the TUI.

## Required Controls

The MVP must support these operator actions:

1. `status`: show current state, active issue, queue snapshot, and last result.
2. `start`: begin processing ready issues.
3. `pause`: finish the current safe boundary, then stop scheduling new issues.
4. `resume`: continue from persisted state.
5. `stop`: stop after the current issue attempt finishes or reaches a safe
   checkpoint.
6. `inspect <issue>`: view the last run summary, logs, branch, and worktree for
   an issue.

The MVP may implement these as a CLI first and leave the TUI as a thin layer on
top of the same core engine.

## Product Principles

1. `bd` is the source of truth for ordering, dependencies, and issue lifecycle.
2. Amp is a worker for one issue, not the long-lived scheduler.
3. Sequential, conservative execution is better than unsafe throughput.
4. Every issue run must be restartable and auditable.
5. Human intervention should be needed for ambiguity, not for routine dispatch.

## Recommended Architecture

## High-Level Components

1. Project detector
2. Queue manager
3. Lock manager
4. Worktree manager
5. Amp worker adapter
6. Verification and merge manager
7. State store and event log
8. CLI and optional TUI shell

### Project Detector

Resolves:

- repo root
- current branch
- presence of `.git`
- presence of `.beads`
- operator configuration file, if any

Fails fast if the current directory is not a valid single project workspace.

### Queue Manager

Polls `bd ready --json`, chooses the next issue, and records decisions. The
selection policy for MVP should be:

1. only issues that are ready in `bd`
2. highest priority first
3. oldest ready issue as tie-breaker

The orchestrator must not invent dependency ordering outside `bd`.

### Lock Manager

The MVP can implement a simple single-worker global execution lock plus a per-
issue lock. This still matters for pause/resume safety and for future-proofing
the design.

Future versions may add area or path locks for safe parallel execution.

### Worktree Manager

For each issue:

1. fetch latest remote state
2. create a fresh worktree from `origin/main`
3. create a branch named `amp/<issue-id>-<slug>`
4. expose worktree path to the Amp worker

Suggested local layout:

- runtime state: `.orc/`
- worktree parent: `.worktrees/<issue-id>/`

### Amp Worker Adapter

Invokes Amp execution mode with a strict per-issue contract. It should supply:

- issue title
- issue description
- acceptance criteria
- dependency context
- repository instructions
- decomposition rule
- verification expectations
- required structured output schema

Amp should be instructed to do the following in order:

1. run a decomposition preflight
2. decompose if the issue is too large or ambiguous
3. otherwise implement the issue in the worktree
4. run verification commands
5. create follow-up `bd` issues for newly discovered substantial work
6. emit a structured result

### Verification And Merge Manager

If Amp reports success and the local checks pass, the orchestrator must:

1. fetch latest `origin/main`
2. rebase the issue branch onto latest `origin/main`
3. rerun required verification
4. merge into `main`
5. push
6. close the `bd` issue
7. clean up worktree and branch

If rebase or verification fails, the issue is not closed and the run is marked
failed or blocked.

### State Store And Event Log

The orchestrator needs durable local state so it can resume after interruption.

Store at minimum:

- current mode: idle, running, pause_requested, paused, stopping
- active issue id
- active branch name
- active worktree path
- last completed issue
- last error
- event history

The simplest acceptable MVP format is JSON files in `.orc/`.

## MVP State Machine

### Orchestrator States

- `idle`
- `running`
- `pause_requested`
- `paused`
- `stopping`
- `error`

### Issue Run Outcomes

- `completed`
- `decomposed`
- `blocked`
- `failed`
- `needs_human`

The scheduler loop should stop on `pause_requested` after the current issue
reaches a safe boundary.

## CLI Requirements

The MVP must ship with a CLI. TUI support is optional and may come later.

Required commands:

```text
orc init-config
orc status
orc start
orc pause
orc resume
orc stop
orc inspect ISSUE_ID
orc logs [--tail N]
```

### `init-config`

Creates a local config file with safe defaults. This is optional if zero-config
discovery is sufficient, but it is useful for explicit quality gates and branch
policy.

### `status`

Shows:

- orchestrator state
- active issue, if any
- queue summary
- paused or stop intent
- last result

### `start`

Starts the event loop. It must refuse to start if another active orchestrator
lock exists for the same repo.

### `pause`

Sets `pause_requested`. It must not kill the current work abruptly.

### `resume`

Moves from `paused` back to `running`.

### `stop`

Requests a clean stop after the current issue reaches a safe checkpoint.

### `inspect`

Provides issue-specific run metadata, including the last result payload and any
preserved worktree path.

## Amp Execution Contract

Each issue run should end with a machine-readable payload similar to:

```json
{
  "result": "completed",
  "summary": "Implemented backlog runner state persistence and pause handling.",
  "changed_paths": [
    "orchestrator/state.py",
    "tests/test_pause.py"
  ],
  "tests_run": [
    "uv run pytest tests/test_pause.py"
  ],
  "followup_bd_issues": [],
  "blockers": [],
  "merge_ready": true
}
```

Accepted `result` values:

- `completed`
- `decomposed`
- `blocked`
- `failed`
- `needs_human`

## Decomposition Policy

The orchestrator should require a cheap decomposition preflight on every issue.

Decompose when any of these are true:

1. the work will likely exceed one coherent branch or PR
2. the issue spans multiple unrelated subsystems
3. acceptance criteria are too broad or incomplete
4. the work introduces major prerequisites not yet represented in `bd`
5. the expected verification surface is too large for one autonomous run

If decomposed:

1. create child issues with `bd create "<title>" --parent <current-issue-id>` (do **not** use `--deps "parent:<id>"` — that creates a flat dependency, not a true parent-child relationship)
2. let `bd` parent-child blocking govern readiness
3. rewrite the parent into a verification/integration issue
4. mark the current issue outcome as `decomposed`
5. stop implementation on the parent

## Git And Merge Policy

For MVP, always target `main`.

Rules:

1. all work starts from the latest `origin/main`
2. one worktree per issue
3. one branch per issue
4. no fast, implicit reuse of dirty worktrees
5. no issue is closed before the merge succeeds
6. merge only after rebase and rerun of required checks

The MVP may merge directly if the repository policy allows it. Future versions
may open pull requests instead.

## Pause And Resume Requirements

Pause is a first-class product requirement.

Behavior:

1. `pause` does not interrupt an in-flight shell command or Amp execution.
2. `pause` prevents selection of the next issue.
3. persisted state allows process restart without losing run history.
4. `resume` continues from `paused` without re-running already closed issues.

If the process crashes mid-issue, the orchestrator should detect the preserved
active state on restart and present recovery options, such as:

1. inspect and continue
2. retry the issue from scratch in a new worktree
3. mark the issue as failed and move on

## Observability

The MVP must keep a local event log with timestamps for:

- issue selection
- Amp invocation start and end
- verification commands
- merge attempts
- issue closure
- pause and stop requests
- failures and retries

Logs should be human-readable and also parseable for future automation.

## Configuration

MVP configuration should be minimal and repo-local. Suggested fields:

```yaml
base_branch: main
max_workers: 1
require_clean_worktree: true
auto_push: true
verification_commands: []
amp_mode: smart
use_decomposition_preflight: true
```

The product must reject `max_workers > 1` in MVP unless parallel support is
explicitly implemented.

## Technology Recommendation

Use Python with `uv` for the MVP because:

- it matches the local tooling ecosystem
- subprocess orchestration is straightforward
- JSON and file-based state handling are simple
- the future CLI and TUI can share core modules cleanly

Suggested structure:

```text
src/orc/
  cli.py
  config.py
  state.py
  queue.py
  worktree.py
  amp_runner.py
  merge.py
  events.py
tests/
docs/prds/
```

## Success Metrics

1. A user can run the orchestrator from a repo with `bd` and process at least
   one issue end to end without manual issue-by-issue launch.
2. A paused run can be resumed safely.
3. A failed issue does not corrupt the queue or close the issue incorrectly.
4. The orchestrator never auto-starts work on launch.
5. A decomposed issue produces child issues and stops cleanly.

## Open Questions

1. What is the exact invocation contract for Amp execution mode from a shellable
   CLI?
2. Should MVP preserve failed worktrees by default for debugging, or clean them
   immediately after snapshotting logs?
3. Should direct merges be allowed in all repos, or should PR-based merge be a
   configurable strategy from day one?
4. What is the safest default behavior if `bd ready` returns a task that lacks
   clear acceptance criteria?

## Milestones

### Milestone 1: Core Loop

- detect project
- read config
- persist orchestrator state
- select next ready issue
- create worktree and branch
- stub Amp adapter

### Milestone 2: Issue Execution

- implement Amp invocation contract
- parse result payload
- support decomposition outcome
- capture logs and event history

### Milestone 3: Verification And Merge

- run verification commands
- rebase on latest `main`
- merge and push
- close issue in `bd`

### Milestone 4: Control Surface

- implement pause, resume, stop
- implement inspect and logs commands
- harden crash recovery

## Recommendation Summary

The product should not try to embed the entire backlog orchestration loop inside
an Amp conversation. Instead, it should be a thin, deterministic external runner
that repeatedly calls Amp on one issue at a time. That division of labor keeps
the scheduling and recovery logic simple while preserving Amp's strength as a
bounded autonomous worker.
