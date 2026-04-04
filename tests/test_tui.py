"""Tests for the TUI module."""
from __future__ import annotations

from click.testing import CliRunner
from unittest.mock import Mock

from orc.cli import main
from orc.config import OrchestratorConfig
from orc.queue import BdIssue, QueueBreakdown, QueueResult
from orc.state import OrchestratorState
from orc.tui.app import OrchestratorApp
from orc.tui.snapshot import DashboardSnapshot
from orc.tui.widgets import StaleBanner, StatusPanel


def _snapshot(
    *,
    ready_issues: list[BdIssue] | None = None,
    queue_breakdown: QueueBreakdown | None = None,
    queue_result: QueueResult | None = None,
    config_error: str | None = None,
) -> DashboardSnapshot:
    return DashboardSnapshot(
        state=OrchestratorState(),
        ready_issues=ready_issues or [],
        recent_events=[],
        config=OrchestratorConfig(),
        queue_breakdown=queue_breakdown,
        queue_result=queue_result,
        config_error=config_error,
    )


def test_orchestrator_app_instantiates() -> None:
    app = OrchestratorApp()
    assert app.TITLE == "orc"


def test_tui_command_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "tui" in result.output


def test_apply_loaded_snapshot_preserves_last_good_queue_on_failure() -> None:
    app = OrchestratorApp()
    status_panel = Mock()
    banner = Mock()

    def fake_query_one(widget_type):
        if widget_type is StatusPanel:
            return status_panel
        if widget_type is StaleBanner:
            return banner
        raise AssertionError(f"Unexpected query_one({widget_type!r})")

    app.query_one = Mock(side_effect=fake_query_one)  # type: ignore[method-assign]
    app._apply_snapshot = Mock()

    issue = BdIssue(id="X-2", title="New feature", priority=2, created="2026-01-01")
    breakdown = QueueBreakdown(
        beads_ready=3,
        policy_skipped=1,
        held_and_ready=1,
        runnable=1,
    )
    good_snap = _snapshot(
        ready_issues=[issue],
        queue_breakdown=breakdown,
        queue_result=QueueResult(issues=[issue]),
    )

    app._apply_loaded_snapshot(good_snap)

    first_queue_refresh = app._last_queue_refresh
    assert first_queue_refresh is not None
    assert app._apply_snapshot.call_args.args[0] is good_snap
    status_panel.update_queue_last_refreshed.assert_called_once()

    app._apply_snapshot.reset_mock()
    status_panel.show_refresh_error.reset_mock()

    failed_snap = _snapshot(
        queue_result=QueueResult(success=False, error="bd ready failed"),
    )
    app._apply_loaded_snapshot(failed_snap)

    displayed_snap = app._apply_snapshot.call_args.args[0]
    assert displayed_snap.ready_issues == [issue]
    assert displayed_snap.queue_breakdown == breakdown
    assert app._last_queue_refresh == first_queue_refresh
    status_panel.show_refresh_error.assert_called_once_with("Queue: bd ready failed")
    status_panel.update_queue_last_refreshed.assert_called_once()


def test_mark_refresh_success_can_preserve_existing_error() -> None:
    app = OrchestratorApp()
    status_panel = Mock()
    banner = Mock()

    def fake_query_one(widget_type):
        if widget_type is StatusPanel:
            return status_panel
        if widget_type is StaleBanner:
            return banner
        raise AssertionError(f"Unexpected query_one({widget_type!r})")

    app.query_one = Mock(side_effect=fake_query_one)  # type: ignore[method-assign]
    app._last_refresh_error = "Queue: bd ready failed"

    app._mark_refresh_success(clear_error=False)

    assert app._last_refresh_error == "Queue: bd ready failed"
    status_panel.hide_refresh_error.assert_not_called()
    banner.hide.assert_not_called()
