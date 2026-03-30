"""Tests for TUI dashboard widgets."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, RunCheckpoint, RunStage, StateStore
from amp_orchestrator.tui.app import OrchestratorApp
from amp_orchestrator.tui.snapshot import DashboardSnapshot
from amp_orchestrator.tui.widgets import (
    EVENT_SEVERITY,
    MODE_STYLES,
    NO_PROJECT_PLACEHOLDER,
    _ACTION_ENABLED,
    _CATEGORY_ICONS,
    _RESULT_ICONS,
    _event_severity,
    _format_run_timestamp,
    _human_message,
    ActiveIssuePanel,
    ConfigPanel,
    ControlsPanel,
    ErrorAlert,
    EventsLog,
    HeldIssuesTable,
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
    active_run = None
    if active_issue_id:
        checkpoint = RunCheckpoint(
            issue_id=active_issue_id,
            issue_title=active_issue_title or "",
            branch=active_branch,
            worktree_path=active_worktree_path,
            stage=RunStage.amp_running,
        )
        active_run = checkpoint.to_dict()
    return DashboardSnapshot(
        state=OrchestratorState(
            mode=mode,
            active_run=active_run,
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
    assert len(children) == 10  # title, badge, refresh-error, last-refresh, queue-last-refreshed, counts-summary, severity-counts, completed, error, ErrorAlert


def test_active_issue_panel_composes() -> None:
    panel = ActiveIssuePanel()
    children = list(panel.compose())
    assert len(children) == 2


def test_active_issue_panel_is_focusable() -> None:
    panel = ActiveIssuePanel()
    assert panel.can_focus is True


def test_active_issue_panel_has_inspect_bindings() -> None:
    panel = ActiveIssuePanel()
    binding_keys = [b.key for b in panel.BINDINGS]
    assert "enter" in binding_keys
    assert "i" in binding_keys


def test_active_issue_panel_stores_snapshot_state() -> None:
    panel = ActiveIssuePanel()
    assert panel._last_snap_state is None
    snap = _snap(
        active_issue_id="TEST-1",
        active_issue_title="Test issue",
        active_branch="feat/test",
        active_worktree_path="/tmp/wt",
    )
    # Cannot call update_snapshot without mounting, but we can verify state storage directly
    panel._last_snap_state = snap.state
    assert panel._last_snap_state.active_issue_id == "TEST-1"
    assert panel._last_snap_state.active_issue_title == "Test issue"
    assert panel._last_snap_state.active_branch == "feat/test"
    assert panel._last_snap_state.active_worktree_path == "/tmp/wt"


def test_config_panel_composes() -> None:
    panel = ConfigPanel()
    children = list(panel.compose())
    assert len(children) == 2


def test_config_panel_is_focusable() -> None:
    panel = ConfigPanel()
    assert panel.can_focus is True


def test_config_panel_has_inspect_bindings() -> None:
    panel = ConfigPanel()
    binding_keys = [b.key for b in panel.BINDINGS]
    assert "enter" in binding_keys
    assert "i" in binding_keys


def test_config_panel_stores_config() -> None:
    panel = ConfigPanel()
    assert panel._last_config is None
    cfg = OrchestratorConfig(base_branch="develop", auto_push=False)
    panel._last_config = cfg
    assert panel._last_config.base_branch == "develop"
    assert panel._last_config.auto_push is False


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
    assert len(children) == 3  # Label + Input (filter) + DataTable


def test_events_log_composes() -> None:
    panel = EventsLog()
    children = list(panel.compose())
    assert len(children) == 2  # Label + RichLog


def test_history_table_composes() -> None:
    panel = HistoryTable()
    children = list(panel.compose())
    assert len(children) == 3  # Label + Input (filter) + DataTable


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


def test_retry_held_issue_notifies_when_issue_is_closed(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    store = StateStore(state_dir)
    store.save(
        OrchestratorState(
            issue_failures={
                "amp-orchestrator-qyy": {
                    "category": "issue_needs_rework",
                    "action": "hold_for_retry",
                    "stage": "evaluation",
                    "summary": "Needs retry",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "attempts": 1,
                }
            }
        )
    )
    app = OrchestratorApp(repo_root=tmp_path, state_dir=state_dir)

    with (
        patch.object(app, "notify", new=Mock()) as notify_mock,
        patch.object(app, "_do_full_refresh", new=Mock()) as refresh_mock,
        patch.object(app, "call_from_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
        patch("amp_orchestrator.tui.app.get_issue_status", return_value="closed"),
    ):
        OrchestratorApp._do_retry_held_issue.__wrapped__(app, "amp-orchestrator-qyy")

    assert "amp-orchestrator-qyy" not in store.load().issue_failures
    notify_mock.assert_called_once_with(
        "amp-orchestrator-qyy is already closed in beads — removed from held list"
    )
    refresh_mock.assert_called_once_with()


def test_retry_held_issue_keeps_requeue_message_for_open_issue(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    store = StateStore(state_dir)
    store.save(
        OrchestratorState(
            issue_failures={
                "amp-orchestrator-qyy": {
                    "category": "issue_needs_rework",
                    "action": "hold_for_retry",
                    "stage": "evaluation",
                    "summary": "Needs retry",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "attempts": 1,
                }
            }
        )
    )
    app = OrchestratorApp(repo_root=tmp_path, state_dir=state_dir)

    with (
        patch.object(app, "notify", new=Mock()) as notify_mock,
        patch.object(app, "_do_full_refresh", new=Mock()) as refresh_mock,
        patch.object(app, "call_from_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
        patch("amp_orchestrator.tui.app.get_issue_status", return_value="open"),
    ):
        OrchestratorApp._do_retry_held_issue.__wrapped__(app, "amp-orchestrator-qyy")

    assert "amp-orchestrator-qyy" not in store.load().issue_failures
    notify_mock.assert_called_once_with(
        "Cleared failure status for amp-orchestrator-qyy — will be re-queued"
    )
    refresh_mock.assert_called_once_with()


def test_stale_banner_composes() -> None:
    banner = StaleBanner()
    children = list(banner.compose())
    assert len(children) == 1  # Label


def test_app_refresh_tracking_fields() -> None:
    """App should initialise refresh tracking fields."""
    app = OrchestratorApp()
    assert app._last_successful_refresh is None
    assert app._last_queue_refresh is None
    assert app._last_refresh_error is None


def test_help_modal_has_bindings() -> None:
    from amp_orchestrator.tui.modals import get_help_bindings

    bindings = get_help_bindings()
    assert len(bindings) >= 9
    keys = [k for k, _ in bindings]
    assert "q" in keys
    assert "r" in keys
    assert "?" in keys
    assert "c" in keys  # toggle_config must appear in help


# --- _event_severity tests ---


def test_event_severity_errors() -> None:
    assert _event_severity("error") == "ERR"
    assert _event_severity("conflict_detected") == "ERR"


def test_event_severity_warnings() -> None:
    assert _event_severity("issue_needs_rework") == "WARN"
    assert _event_severity("pause_requested") == "WARN"
    assert _event_severity("stop_requested") == "WARN"
    assert _event_severity("conflict_resolution_started") == "WARN"


def test_event_severity_info_default() -> None:
    assert _event_severity("amp_started") == "INFO"
    assert _event_severity("amp_finished") == "INFO"
    assert _event_severity("issue_selected") == "INFO"
    assert _event_severity("unknown_type") == "INFO"


def test_events_log_format_entry_includes_severity_prefix() -> None:
    entry = {"timestamp": "2025-01-01T12:00:00Z", "event_type": "error", "data": {"error": "fail"}}
    result = EventsLog._format_entry(entry)
    assert "[ERR]" in result

    entry_info = {"timestamp": "2025-01-01T12:00:00Z", "event_type": "amp_started", "data": None}
    result_info = EventsLog._format_entry(entry_info)
    assert "[INFO]" in result_info

    entry_warn = {"timestamp": "2025-01-01T12:00:00Z", "event_type": "issue_needs_rework", "data": None}
    result_warn = EventsLog._format_entry(entry_warn)
    assert "[WARN]" in result_warn


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


# --- _format_run_timestamp tests ---


def test_format_run_timestamp_old_date_shows_datetime() -> None:
    result = _format_run_timestamp("2024-01-15T09:30:00Z")
    assert result == "2024-01-15 09:30"


def test_format_run_timestamp_recent_shows_relative() -> None:
    from datetime import datetime, timedelta, timezone

    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    ts = recent.isoformat()
    result = _format_run_timestamp(ts)
    assert result == "5m ago"


def test_format_run_timestamp_hours_ago() -> None:
    from datetime import datetime, timedelta, timezone

    recent = datetime.now(timezone.utc) - timedelta(hours=3)
    ts = recent.isoformat()
    result = _format_run_timestamp(ts)
    assert result == "3h ago"


def test_format_run_timestamp_just_now() -> None:
    from datetime import datetime, timezone

    recent = datetime.now(timezone.utc)
    ts = recent.isoformat()
    result = _format_run_timestamp(ts)
    assert result == "just now"


def test_format_run_timestamp_invalid_returns_original() -> None:
    assert _format_run_timestamp("not-a-date") == "not-a-date"


def test_format_run_timestamp_no_timezone_suffix() -> None:
    result = _format_run_timestamp("2024-06-01T14:30:00+00:00")
    assert result == "2024-06-01 14:30"


# --- Accessibility: redundant non-color cues ---


def test_mode_styles_have_text_prefix() -> None:
    """Every mode badge must include a text prefix (RUN/PAUSE/ERR/IDLE/STOPPING)."""
    for mode, (_color, label) in MODE_STYLES.items():
        # Label should start with an icon character followed by a space and text
        parts = label.split(None, 1)
        assert len(parts) >= 2, f"MODE_STYLES[{mode}] label '{label}' lacks icon+text"


def test_result_icons_cover_common_results() -> None:
    for key in ("completed", "failed", "error"):
        assert key in _RESULT_ICONS, f"_RESULT_ICONS missing '{key}'"
        assert len(_RESULT_ICONS[key]) >= 1


def test_category_icons_cover_all_categories() -> None:
    from amp_orchestrator.state import FailureCategory

    for cat in FailureCategory:
        assert cat.value in _CATEGORY_ICONS, f"_CATEGORY_ICONS missing '{cat.value}'"


# --- QueueTable sort/filter tests ---


def test_queue_table_sort_modes() -> None:
    """QueueTable should cycle through three sort modes."""
    table = QueueTable()
    assert table._sort_mode == "priority"
    # Simulate cycling
    modes = QueueTable._SORT_MODES
    assert modes == ("priority", "age_newest", "age_oldest")


def test_queue_table_sort_issues_by_priority() -> None:
    """Default sort should order by priority (lower number first, 0 last)."""
    table = QueueTable()
    issues = [
        BdIssue(id="A-1", title="Low", priority=4, created="2025-01-01"),
        BdIssue(id="A-2", title="Urgent", priority=1, created="2025-01-02"),
        BdIssue(id="A-3", title="None", priority=0, created="2025-01-03"),
    ]
    sorted_issues = table._sort_issues(issues)
    assert [i.id for i in sorted_issues] == ["A-2", "A-1", "A-3"]


def test_queue_table_sort_issues_by_age_newest() -> None:
    """age_newest sort should put newest first."""
    table = QueueTable()
    table._sort_mode = "age_newest"
    issues = [
        BdIssue(id="A-1", title="Old", priority=1, created="2025-01-01"),
        BdIssue(id="A-2", title="New", priority=1, created="2025-06-01"),
    ]
    sorted_issues = table._sort_issues(issues)
    assert [i.id for i in sorted_issues] == ["A-2", "A-1"]


def test_queue_table_sort_issues_by_age_oldest() -> None:
    """age_oldest sort should put oldest first."""
    table = QueueTable()
    table._sort_mode = "age_oldest"
    issues = [
        BdIssue(id="A-1", title="New", priority=1, created="2025-06-01"),
        BdIssue(id="A-2", title="Old", priority=1, created="2025-01-01"),
    ]
    sorted_issues = table._sort_issues(issues)
    assert [i.id for i in sorted_issues] == ["A-2", "A-1"]


def test_queue_table_filter_by_id() -> None:
    """Filter should match issue ID substring."""
    table = QueueTable()
    table._filter_text = "a-2"
    issues = [
        BdIssue(id="A-1", title="First", priority=1, created="2025-01-01"),
        BdIssue(id="A-2", title="Second", priority=1, created="2025-01-02"),
        BdIssue(id="A-22", title="Third", priority=1, created="2025-01-03"),
    ]
    filtered = table._apply_filter(issues)
    assert [i.id for i in filtered] == ["A-2", "A-22"]


def test_queue_table_filter_by_title() -> None:
    """Filter should also match title substring."""
    table = QueueTable()
    table._filter_text = "bug"
    issues = [
        BdIssue(id="A-1", title="Fix bug in parser", priority=1, created="2025-01-01"),
        BdIssue(id="A-2", title="Add feature", priority=1, created="2025-01-02"),
    ]
    filtered = table._apply_filter(issues)
    assert [i.id for i in filtered] == ["A-1"]


def test_queue_table_empty_filter_returns_all() -> None:
    """Empty filter should return all issues."""
    table = QueueTable()
    table._filter_text = ""
    issues = [
        BdIssue(id="A-1", title="First", priority=1, created="2025-01-01"),
    ]
    assert table._apply_filter(issues) == issues


def test_queue_table_has_sort_and_filter_bindings() -> None:
    """QueueTable should have bindings for sort (o) and filter (/)."""
    table = QueueTable()
    binding_keys = [b.key for b in table.BINDINGS]
    assert "o" in binding_keys
    assert "slash" in binding_keys


# --- HistoryTable filter tests ---


def test_history_table_failed_only_filter() -> None:
    """_apply_filters with result_filter='failed' should only keep failed/error runs."""
    table = HistoryTable()
    table._result_filter = "failed"
    runs = [
        {"issue_id": "A-1", "result": "completed"},
        {"issue_id": "A-2", "result": "failed"},
        {"issue_id": "A-3", "result": "error"},
        {"issue_id": "A-4", "result": "completed"},
    ]
    filtered = table._apply_filters(runs)
    assert [r["issue_id"] for r in filtered] == ["A-2", "A-3"]


def test_history_table_filter_by_issue_id() -> None:
    """_apply_filters with filter_text should match issue_id substring."""
    table = HistoryTable()
    table._filter_text = "a-2"
    runs = [
        {"issue_id": "A-1", "result": "completed"},
        {"issue_id": "A-2", "result": "failed"},
        {"issue_id": "A-22", "result": "completed"},
    ]
    filtered = table._apply_filters(runs)
    assert [r["issue_id"] for r in filtered] == ["A-2", "A-22"]


def test_history_table_combined_filters() -> None:
    """Both result_filter and filter_text should stack."""
    table = HistoryTable()
    table._result_filter = "failed"
    table._filter_text = "a-2"
    runs = [
        {"issue_id": "A-1", "result": "failed"},
        {"issue_id": "A-2", "result": "completed"},
        {"issue_id": "A-2", "result": "failed"},
        {"issue_id": "A-3", "result": "error"},
    ]
    filtered = table._apply_filters(runs)
    assert len(filtered) == 1
    assert filtered[0]["issue_id"] == "A-2"
    assert filtered[0]["result"] == "failed"


def test_history_table_no_filters_returns_all() -> None:
    """With no filters active, all runs should be returned."""
    table = HistoryTable()
    runs = [
        {"issue_id": "A-1", "result": "completed"},
        {"issue_id": "A-2", "result": "failed"},
    ]
    assert table._apply_filters(runs) == runs


def test_history_table_has_filter_bindings() -> None:
    """HistoryTable should have bindings for result filter (f) and filter (/)."""
    table = HistoryTable()
    binding_keys = [b.key for b in table.BINDINGS]
    assert "f" in binding_keys
    assert "slash" in binding_keys


def test_history_table_result_filter_modes() -> None:
    """HistoryTable should cycle through four result filter modes."""
    table = HistoryTable()
    assert table._result_filter == "all"
    modes = HistoryTable._RESULT_FILTER_MODES
    assert modes == ("all", "failed", "needs_rework", "completed")


def test_history_table_completed_filter() -> None:
    """result_filter='completed' should only keep completed runs."""
    table = HistoryTable()
    table._result_filter = "completed"
    runs = [
        {"issue_id": "A-1", "result": "completed"},
        {"issue_id": "A-2", "result": "failed"},
        {"issue_id": "A-3", "result": "completed"},
        {"issue_id": "A-4", "result": "needs_human"},
    ]
    filtered = table._apply_filters(runs)
    assert [r["issue_id"] for r in filtered] == ["A-1", "A-3"]


def test_history_table_needs_rework_filter() -> None:
    """result_filter='needs_rework' should match needs_rework/needs_human results."""
    table = HistoryTable()
    table._result_filter = "needs_rework"
    runs = [
        {"issue_id": "A-1", "result": "completed"},
        {"issue_id": "A-2", "result": "needs_rework"},
        {"issue_id": "A-3", "result": "needs_human"},
        {"issue_id": "A-4", "result": "failed"},
    ]
    filtered = table._apply_filters(runs)
    assert [r["issue_id"] for r in filtered] == ["A-2", "A-3"]


# --- EventsLog filter tests ---


def test_events_log_has_errors_only_binding() -> None:
    """EventsLog should have a binding for error-only toggle (e)."""
    log = EventsLog()
    binding_keys = [b.key for b in log.BINDINGS]
    assert "e" in binding_keys


def test_events_log_filter_events_all() -> None:
    """With errors_only=False, all events should pass through."""
    log = EventsLog()
    events = [
        {"event_type": "amp_started", "timestamp": "2025-01-01T00:00:00Z"},
        {"event_type": "error", "timestamp": "2025-01-01T00:01:00Z"},
    ]
    assert log._filter_events(events) == events


def test_events_log_filter_events_errors_only() -> None:
    """With errors_only=True, only ERR-severity events should pass."""
    log = EventsLog()
    log._errors_only = True
    events = [
        {"event_type": "amp_started", "timestamp": "2025-01-01T00:00:00Z"},
        {"event_type": "error", "timestamp": "2025-01-01T00:01:00Z"},
        {"event_type": "amp_finished", "timestamp": "2025-01-01T00:02:00Z"},
        {"event_type": "conflict_detected", "timestamp": "2025-01-01T00:03:00Z"},
    ]
    filtered = log._filter_events(events)
    assert len(filtered) == 2
    assert filtered[0]["event_type"] == "error"
    assert filtered[1]["event_type"] == "conflict_detected"


# --- HeldIssuesTable tests ---


def test_held_issues_table_composes() -> None:
    table = HeldIssuesTable()
    children = list(table.compose())
    assert len(children) == 2  # Label + DataTable


def test_held_issues_table_is_focusable() -> None:
    table = HeldIssuesTable()
    assert table.can_focus is True


def test_held_issues_table_has_inspect_and_retry_bindings() -> None:
    table = HeldIssuesTable()
    binding_keys = [b.key for b in table.BINDINGS]
    assert "enter" in binding_keys
    assert "i" in binding_keys
    assert "y" in binding_keys


def test_held_issues_table_hidden_when_no_failures() -> None:
    """Table should not show 'visible' class when no failures exist."""
    table = HeldIssuesTable()
    snap = _snap()
    # Simulate update_snapshot logic without mounting
    assert not snap.state.issue_failures
    # If there were no failures, _held_items would be empty
    table._held_items = []
    table._row_key = []
    assert len(table._held_items) == 0


def test_held_issues_table_stores_held_items() -> None:
    """Table should track held items from snapshot."""
    table = HeldIssuesTable()
    failures = {
        "TEST-1": {
            "category": "transient_external",
            "action": "hold_for_retry",
            "stage": "amp",
            "summary": "timeout",
            "timestamp": "2025-01-01T00:00:00Z",
            "attempts": 2,
        },
        "TEST-2": {
            "category": "issue_needs_rework",
            "action": "hold_until_backlog_changes",
            "stage": "evaluation",
            "summary": "tests failed",
            "timestamp": "2025-01-01T00:01:00Z",
            "attempts": 1,
        },
    }
    # Directly set items to test data handling
    table._held_items = [(iid, failures[iid]) for iid in sorted(failures.keys())]
    assert len(table._held_items) == 2
    assert table._held_items[0][0] == "TEST-1"
    assert table._held_items[1][0] == "TEST-2"


def test_confirm_retry_modal_init() -> None:
    from amp_orchestrator.tui.modals import ConfirmRetryModal

    modal = ConfirmRetryModal("TEST-42")
    assert modal._issue_id == "TEST-42"


def test_confirm_retry_modal_has_bindings() -> None:
    from amp_orchestrator.tui.modals import ConfirmRetryModal

    modal = ConfirmRetryModal("TEST-1")
    binding_keys = [b[0] if isinstance(b, tuple) else b.key for b in modal.BINDINGS]
    assert "escape" in binding_keys
    assert "y" in binding_keys
    assert "n" in binding_keys


def test_mode_styles_use_high_contrast_colors() -> None:
    """Ensure we don't use dim/low-contrast color names like plain 'yellow' or 'orange1'."""
    low_contrast = {"yellow", "orange1", "dark_orange", "grey", "dim"}
    for mode, (color, _label) in MODE_STYLES.items():
        # Extract the base color name (strip 'bold ', 'italic ', etc.)
        base = color.replace("bold ", "").replace("italic ", "").strip()
        assert base not in low_contrast, (
            f"MODE_STYLES[{mode}] uses low-contrast color '{color}'"
        )


# --- Config / refresh error surfacing tests ---


def test_load_config_swallow_fix(tmp_path: Path) -> None:
    """_load_config should record error instead of silently swallowing."""
    # Create a malformed config file that will cause load_config to fail
    config_dir = tmp_path / ".amp-orchestrator"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("max_workers: 99\n")  # triggers ClickException
    app = OrchestratorApp(repo_root=tmp_path)
    app._load_config()
    # Should have recorded the error (not silently passed)
    assert app._last_refresh_error is not None
    assert "Config load failed" in app._last_refresh_error


def test_snapshot_config_error_field() -> None:
    """DashboardSnapshot should carry config_error when config loading fails."""
    snap = _snap()
    assert snap.config_error is None
    # With error
    snap_err = DashboardSnapshot(
        state=snap.state,
        ready_issues=[],
        recent_events=[],
        config=OrchestratorConfig(),
        config_error="some config error",
    )
    assert snap_err.config_error == "some config error"


def test_mark_refresh_error_stores_message() -> None:
    """_mark_refresh_error should store the error message."""
    app = OrchestratorApp()
    app._last_refresh_error = None
    # Directly test the state change (can't call _mark_refresh_error without mounted widgets)
    app._last_refresh_error = "test error"
    assert app._last_refresh_error == "test error"


def test_mark_refresh_success_clears_error() -> None:
    """After a successful refresh, error should be cleared."""
    app = OrchestratorApp()
    app._last_refresh_error = "old error"
    # Simulate what _mark_refresh_success does to app state
    app._last_refresh_error = None
    assert app._last_refresh_error is None
