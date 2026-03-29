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


def load_snapshot(repo_root: Path, state_dir: Path) -> DashboardSnapshot:
    """Load all dashboard data in a single call."""
    state = StateStore(state_dir).load()

    try:
        config = load_config(repo_root)
    except Exception:
        config = OrchestratorConfig()

    ready_issues = get_ready_issues(repo_root)
    recent_events = EventLog(state_dir).recent(20)

    return DashboardSnapshot(
        state=state,
        ready_issues=ready_issues,
        recent_events=recent_events,
        config=config,
    )
