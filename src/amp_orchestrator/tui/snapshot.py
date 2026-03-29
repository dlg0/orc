"""Dashboard snapshot: unified read model for TUI widgets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from amp_orchestrator.config import OrchestratorConfig, load_config
from amp_orchestrator.events import EventLog
from amp_orchestrator.queue import BdIssue, get_ready_issues
from amp_orchestrator.state import OrchestratorState, StateStore


@dataclass
class DashboardSnapshot:
    state: OrchestratorState
    ready_issues: list[BdIssue]
    recent_events: list[dict]
    config: OrchestratorConfig
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
    state = StateStore(state_dir).load()

    config_error: str | None = None
    try:
        config = load_config(repo_root)
    except Exception as exc:
        config = OrchestratorConfig()
        config_error = str(exc)

    queue_result = get_ready_issues(repo_root)
    recent_events = EventLog(state_dir).recent(100)

    return DashboardSnapshot(
        state=state,
        ready_issues=queue_result.issues,
        recent_events=recent_events,
        config=config,
        config_error=config_error,
    )
