"""Tests for TUI dashboard widgets."""

from __future__ import annotations

from pathlib import Path

import pytest

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState
from amp_orchestrator.tui.snapshot import DashboardSnapshot
from amp_orchestrator.tui.widgets import (
    MODE_STYLES,
    _ACTION_ENABLED,
    ActiveIssuePanel,
    ConfigPanel,
    ControlsPanel,
    EventsLog,
    HistoryTable,
    QueueTable,
    StatusPanel,
)


def _snap(
    mode: OrchestratorMode = OrchestratorMode.idle,
    active_issue_id: str | None = None,
    active_issue_title: str | None = None,
    active_branch: str | None = None,
    active_worktree_path: str | None = None,
    last_completed_issue: str | None = None,
    last_error: str | None = None,
    run_history: list[dict] | None = None,
    ready_issues: list[BdIssue] | None = None,
    recent_events: list[dict] | None = None,
    config: OrchestratorConfig | None = None,
) -> DashboardSnapshot:
    return DashboardSnapshot(
        state=OrchestratorState(
            mode=mode,
            active_issue_id=active_issue_id,
            active_issue_title=active_issue_title,
            active_branch=active_branch,
            active_worktree_path=active_worktree_path,
            last_completed_issue=last_completed_issue,
            last_error=last_error,
            run_history=run_history or [],
        ),
        ready_issues=ready_issues or [],
        recent_events=recent_events or [],
        config=config or OrchestratorConfig(),
    )


def test_mode_styles_covers_all_modes() -> None:
    for mode in OrchestratorMode:
        assert mode in MODE_STYLES


def test_status_panel_composes() -> None:
    panel = StatusPanel()
    children = list(panel.compose())
    assert len(children) == 5


def test_active_issue_panel_composes() -> None:
    panel = ActiveIssuePanel()
    children = list(panel.compose())
    assert len(children) == 2


def test_config_panel_composes() -> None:
    panel = ConfigPanel()
    children = list(panel.compose())
    assert len(children) == 2


def test_action_enabled_covers_all_actions() -> None:
    assert set(_ACTION_ENABLED.keys()) == {"start", "pause", "resume", "stop"}


def test_controls_panel_has_all_actions() -> None:
    """ControlsPanel covers start/pause/resume/stop."""
    assert "start" in _ACTION_ENABLED
    assert "pause" in _ACTION_ENABLED
    assert "resume" in _ACTION_ENABLED
    assert "stop" in _ACTION_ENABLED


def test_queue_table_composes() -> None:
    panel = QueueTable()
    children = list(panel.compose())
    assert len(children) == 2  # Label + DataTable


def test_events_log_composes() -> None:
    panel = EventsLog()
    children = list(panel.compose())
    assert len(children) == 2  # Label + RichLog


def test_history_table_composes() -> None:
    panel = HistoryTable()
    children = list(panel.compose())
    assert len(children) == 2  # Label + DataTable


def test_inspect_modal_init() -> None:
    from amp_orchestrator.tui.modals import InspectModal

    modal = InspectModal(title="Test Title", body="Body text")
    assert modal._title == "Test Title"
    assert modal._body == "Body text"
