"""Dashboard snapshot: unified read model for TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orc.config import OrchestratorConfig, load_config
from orc.events import EventLog
from orc.queue import BdIssue, QueueBreakdown, compute_queue_breakdown, get_ready_issues, reconcile_issue_failures
from orc.state import OrchestratorState, StateStore


@dataclass
class DashboardSnapshot:
    state: OrchestratorState
    ready_issues: list[BdIssue]
    recent_events: list[dict]
    config: OrchestratorConfig
    queue_breakdown: QueueBreakdown | None = None
    is_fast: bool = False
    config_error: str | None = None


def load_snapshot_fast(
    state_dir: Path, config: OrchestratorConfig | None = None
) -> DashboardSnapshot:
    """Load state and events only (no queue subprocess call)."""
    state = StateStore(state_dir).load()
    recent_events = EventLog(state_dir).recent(100)
    return DashboardSnapshot(
        state=state,
        ready_issues=[],
        recent_events=recent_events,
        config=config or OrchestratorConfig(),
        is_fast=True,
    )


def load_snapshot(repo_root: Path, state_dir: Path) -> DashboardSnapshot:
    """Load all dashboard data in a single call."""
    store = StateStore(state_dir)
    state = store.load()

    # Reconcile held issues against beads so closed/missing issues
    # disappear from the TUI even when the scheduler isn't running.
    # Reconcile in-memory only for display — the scheduler owns state.json writes.
    if state.issue_failures:
        reconcile_issue_failures(state.issue_failures, cwd=repo_root)

    config_error: str | None = None
    try:
        config = load_config(repo_root)
    except Exception as exc:
        config = OrchestratorConfig()
        config_error = str(exc)

    queue_result = get_ready_issues(repo_root)
    recent_events = EventLog(state_dir).recent(100)
    breakdown = compute_queue_breakdown(queue_result.issues, state.issue_failures)

    return DashboardSnapshot(
        state=state,
        ready_issues=queue_result.issues,
        recent_events=recent_events,
        config=config,
        queue_breakdown=breakdown,
        config_error=config_error,
    )
