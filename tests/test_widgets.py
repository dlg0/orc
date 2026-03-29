"""Tests for TUI dashboard widgets."""

from __future__ import annotations

from pathlib import Path

import pytest

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState
from amp_orchestrator.tui.app import OrchestratorApp
from amp_orchestrator.tui.snapshot import DashboardSnapshot
from amp_orchestrator.tui.widgets import (
    MODE_STYLES,
    NO_PROJECT_PLACEHOLDER,
    _ACTION_ENABLED,
    _human_message,
    ActiveIssuePanel,
    ConfigPanel,
    ControlsPanel,
    ErrorAlert,
    EventsLog,
    HistoryTable,
    NotConnectedBanner,
    QueueTable,
    StaleBanner,
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
    assert len(children) == 8  # title, badge, last-updated, queue, failed, completed, error, ErrorAlert


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


def test_not_connected_banner_composes() -> None:
    banner = NotConnectedBanner()
    children = list(banner.compose())
    assert len(children) == 1  # Label


def test_no_project_placeholder_is_defined() -> None:
    assert "no project detected" in NO_PROJECT_PLACEHOLDER.lower()


def test_app_no_project_state() -> None:
    """App without repo/state should call _show_no_project."""
    app = OrchestratorApp()
    assert app._repo_root is None
    assert app._state_dir is None


def test_error_alert_composes() -> None:
    alert = ErrorAlert()
    children = list(alert.compose())
    assert len(children) == 1  # Label


def test_error_alert_has_inspect_bindings() -> None:
    alert = ErrorAlert()
    binding_keys = [b.key for b in alert.BINDINGS]
    assert "enter" in binding_keys
    assert "i" in binding_keys


def test_snap_with_failed_runs() -> None:
    """Snapshot with failed runs should produce a non-zero failed count."""
    snap = _snap(
        run_history=[
            {"issue_id": "A-1", "result": "completed"},
            {"issue_id": "A-2", "result": "failed"},
            {"issue_id": "A-3", "result": "error"},
        ],
    )
    failed = sum(
        1 for r in snap.state.run_history if r.get("result") in ("failed", "error")
    )
    assert failed == 2


def test_pending_action_suppresses_refresh() -> None:
    """When _pending_action is set, refreshes should not update status/controls."""
    app = OrchestratorApp()
    app._pending_action = "start"
    snap_idle = _snap(mode=OrchestratorMode.idle)
    # While pending and mode hasn't reached expected, suppress should be True
    assert app._check_pending_action(snap_idle) is True
    assert app._pending_action == "start"
    # Once mode reaches expected (running), suppress should clear
    snap_running = _snap(mode=OrchestratorMode.running)
    assert app._check_pending_action(snap_running) is False
    assert app._pending_action is None


def test_pending_action_clear_on_failure() -> None:
    """_clear_pending_action should reset the guard."""
    app = OrchestratorApp()
    app._pending_action = "pause"
    app._clear_pending_action()
    assert app._pending_action is None
    # After clearing, check_pending_action should not suppress
    snap = _snap(mode=OrchestratorMode.idle)
    assert app._check_pending_action(snap) is False


def test_pending_action_none_does_not_suppress() -> None:
    """When no pending action, check_pending_action returns False."""
    app = OrchestratorApp()
    snap = _snap(mode=OrchestratorMode.idle)
    assert app._check_pending_action(snap) is False


def test_stale_banner_composes() -> None:
    banner = StaleBanner()
    children = list(banner.compose())
    assert len(children) == 1  # Label


def test_app_refresh_tracking_fields() -> None:
    """App should initialise refresh tracking fields."""
    app = OrchestratorApp()
    assert app._last_successful_refresh is None
    assert app._last_refresh_error is None


def test_help_modal_has_bindings() -> None:
    from amp_orchestrator.tui.modals import _HELP_BINDINGS

    assert len(_HELP_BINDINGS) >= 9
    keys = [k for k, _ in _HELP_BINDINGS]
    assert "q" in keys
    assert "r" in keys
    assert "?" in keys


# --- _human_message tests ---


@pytest.mark.parametrize(
    "event_type, data, expected_substring",
    [
        ("issue_selected", {"issue_id": "X-1", "title": "Fix bug"}, "Selected issue X-1: Fix bug"),
        ("issue_selected", {"issue_id": "X-1"}, "Selected issue X-1"),
        ("amp_started", {"issue_id": "X-2"}, "Agent started on X-2"),
        ("amp_started", None, "Agent started"),
        (
            "amp_finished",
            {"issue_id": "X-3", "result": "completed", "summary": "done"},
            "Agent finished X-3 (completed)",
        ),
        ("amp_finished", {}, "Agent finished"),
        ("verification_run", {"issue_id": "X-4", "command": "pytest", "result": "pass"}, "Verification on X-4: pytest [pass]"),
        ("merge_attempt", {"issue_id": "X-5"}, "Merge attempt for X-5"),
        ("issue_closed", {"issue_id": "X-6"}, "Issue X-6 closed"),
        ("pause_requested", None, "Pause requested"),
        ("stop_requested", None, "Stop requested"),
        ("state_changed", {"from": "paused", "to": "running"}, "State paused → running"),
        ("state_changed", {"to": "idle", "reason": "queue_empty"}, "State → idle (queue_empty)"),
        ("error", {"issue_id": "X-7", "stage": "amp", "error": "timeout"}, "Error on X-7 [amp]: timeout"),
        ("evaluation_started", {"issue_id": "X-8"}, "Evaluation started for X-8"),
        ("evaluation_finished", {"issue_id": "X-9"}, "Evaluation finished for X-9"),
        ("issue_needs_rework", {"issue_id": "X-10"}, "Issue X-10 needs rework"),
        ("conflict_detected", {"issue_id": "X-11", "branch": "feat/x"}, "Conflict detected on X-11 (branch: feat/x)"),
        ("conflict_resolution_started", {"issue_id": "X-12"}, "Conflict resolution started for X-12"),
        ("conflict_resolution_finished", {"issue_id": "X-13", "result": "success"}, "Conflict resolution finished for X-13 (success)"),
        ("unknown_event", {"foo": "bar"}, "unknown_event"),
        ("unknown_event", None, "unknown_event"),
    ],
)
def test_human_message(event_type: str, data: dict | None, expected_substring: str) -> None:
    result = _human_message(event_type, data)
    assert expected_substring in result
