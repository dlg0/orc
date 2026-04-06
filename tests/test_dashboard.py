"""Integration tests for the TUI dashboard layout."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from textual.widgets import DataTable, Label

from orc.config import OrchestratorConfig
from orc.dispatch_policy import DispatchSkip
from orc.queue import BdIssue, QueueBreakdown, QueueResult, compute_queue_breakdown
from orc.state import OrchestratorMode, OrchestratorState, RunCheckpoint, RunStage
from orc.tui.app import OrchestratorApp
from orc.tui.snapshot import DashboardSnapshot
from orc.tui.widgets import (
    ActiveIssuePanel,
    ConfigPanel,
    EventsLog,
    HistoryTable,
    QueueTable,
    StatusPanel,
)


def _make_snap(**kwargs) -> DashboardSnapshot:
    defaults = dict(
        state=OrchestratorState(),
        ready_issues=[],
        recent_events=[],
        config=OrchestratorConfig(),
    )
    defaults.update(kwargs)
    return DashboardSnapshot(**defaults)


@pytest.mark.asyncio
async def test_app_has_all_panels() -> None:
    app = OrchestratorApp()
    async with app.run_test():
        assert app.query_one(StatusPanel)
        assert app.query_one(ActiveIssuePanel)
        assert app.query_one(ConfigPanel)
        assert app.query_one(QueueTable)
        assert app.query_one(EventsLog)
        assert app.query_one(HistoryTable)


@pytest.mark.asyncio
async def test_apply_snapshot_running() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        checkpoint = RunCheckpoint(
            issue_id="bz1", issue_title="Fix widget",
            branch="amp/bz1-fix", worktree_path="/tmp/wt",
            stage=RunStage.amp_running,
        )
        snap = _make_snap(
            state=OrchestratorState(
                mode=OrchestratorMode.running,
                active_run=checkpoint.to_dict(),
                last_completed_issue="bz0",
            ),
            ready_issues=[
                BdIssue(id="bz2", title="Add feature", priority=2, created="2026-01-01"),
                BdIssue(id="bz3", title="Refactor", priority=3, created="2026-01-02"),
            ],
            recent_events=[
                {"timestamp": "2026-01-01T12:00:00", "event_type": "issue_selected", "data": {"issue_id": "bz1"}},
            ],
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        queue_table = app.query_one("#queue-datatable")
        assert queue_table.row_count == 2

        history_table = app.query_one("#history-datatable")
        assert history_table.row_count == 1  # empty-state placeholder row


@pytest.mark.asyncio
async def test_apply_snapshot_preserves_frontier_order_and_marks_held_rows() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(
                issue_failures={
                    "bz3": {
                        "category": "agent_failed",
                        "action": "pause_orchestrator",
                        "summary": "Needs follow-up",
                    }
                }
            ),
            ready_issues=[
                BdIssue(id="bz3", title="Held but first in Beads order", priority=4, created="2026-01-02"),
                BdIssue(id="bz2", title="Runnable second", priority=1, created="2026-01-01"),
            ],
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        queue_table = app.query_one("#queue-datatable", DataTable)
        assert queue_table.get_row_at(0) == [
            "bz3",
            "[bold bright_yellow]Held (ready)[/]",
            "4",
            "Held but first in Beads order",
            "2026-01-02",
        ]
        assert queue_table.get_row_at(1) == [
            "bz2",
            "[bold green]Runnable[/]",
            "1",
            "Runnable second",
            "2026-01-01",
        ]


@pytest.mark.asyncio
async def test_apply_snapshot_surfaces_dispatch_diagnostics() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        held_issue = BdIssue(
            id="bz3",
            title="Held but first in Beads order",
            priority=4,
            created="2026-01-02",
        )
        runnable_issue = BdIssue(
            id="bz2",
            title="Runnable second",
            priority=1,
            created="2026-01-01",
        )
        queue_result = QueueResult(
            issues=[held_issue, runnable_issue],
            raw_issues=[
                BdIssue(id="epic-1", title="Epic container", priority=0, created="2026-01-03"),
                held_issue,
                runnable_issue,
            ],
            skipped=[
                DispatchSkip(
                    issue_id="epic-1",
                    issue_type="epic",
                    status="open",
                    category="container/control",
                    reason="container/control issue; Orc does not dispatch containers directly",
                )
            ],
        )
        state = OrchestratorState(
            issue_failures={
                "bz3": {
                    "category": "agent_failed",
                    "action": "pause_orchestrator",
                    "summary": "Needs follow-up",
                }
            }
        )
        snap = _make_snap(
            state=state,
            ready_issues=queue_result.issues,
            queue_result=queue_result,
            queue_skip_summary={"container/control": 1},
            queue_breakdown=compute_queue_breakdown(queue_result, state.issue_failures),
        )

        app._apply_snapshot(snap)
        await pilot.pause()

        counts_summary = app.query_one("#counts-summary", Label).render().plain
        assert "Skipped: 1" in counts_summary
        assert "Held (ready): 1" in counts_summary

        diagnostics = app.query_one("#queue-diagnostics", Label).render().plain
        assert "Policy skips: container/control: 1" in diagnostics
        assert "Held-ready: bz3" in diagnostics


@pytest.mark.asyncio
async def test_apply_snapshot_distinguishes_no_runnable_from_empty_queue() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        queue_result = QueueResult(
            issues=[],
            raw_issues=[
                BdIssue(id="epic-1", title="Epic container", priority=0, created="2026-01-03")
            ],
            skipped=[
                DispatchSkip(
                    issue_id="epic-1",
                    issue_type="epic",
                    status="open",
                    category="container/control",
                    reason="container/control issue; Orc does not dispatch containers directly",
                )
            ],
        )
        snap = _make_snap(
            queue_result=queue_result,
            queue_skip_summary={"container/control": 1},
            queue_breakdown=compute_queue_breakdown(queue_result, {}),
        )

        app._apply_snapshot(snap)
        await pilot.pause()

        diagnostics = app.query_one("#queue-diagnostics", Label).render().plain
        assert "No runnable issues — 1 skipped by dispatch policy." in diagnostics
        assert "Policy skips: container/control: 1" in diagnostics

        queue_table = app.query_one("#queue-datatable", DataTable)
        assert queue_table.get_row_at(0) == [
            "-",
            "-",
            "-",
            "[italic]No runnable issues — see dispatch diagnostics above[/]",
            "-",
        ]


@pytest.mark.asyncio
async def test_apply_loaded_snapshot_keeps_last_good_queue_on_queue_failure() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        issue = BdIssue(id="bz2", title="Cached queue issue", priority=2, created="2026-01-01")
        good_result = QueueResult(issues=[issue])
        good_snap = _make_snap(
            ready_issues=[issue],
            queue_result=good_result,
            queue_breakdown=compute_queue_breakdown(good_result, {}),
        )

        app._apply_loaded_snapshot(good_snap)
        await pilot.pause()
        initial_queue_refresh = app._last_queue_refresh

        failed_snap = _make_snap(
            queue_result=QueueResult(success=False, error="bd list failed"),
            queue_error="bd list failed",
        )
        app._apply_loaded_snapshot(failed_snap)
        await pilot.pause()

        assert app._last_queue_refresh == initial_queue_refresh

        refresh_error = app.query_one("#refresh-error", Label).render().plain
        assert "Queue: bd list failed" in refresh_error

        diagnostics = app.query_one("#queue-diagnostics", Label).render().plain
        assert "showing last good queue view" in diagnostics

        queue_table = app.query_one("#queue-datatable", DataTable)
        assert queue_table.get_row_at(0) == [
            "bz2",
            "[bold green]Runnable[/]",
            "2",
            "Cached queue issue",
            "2026-01-01",
        ]


@pytest.mark.asyncio
async def test_apply_snapshot_with_history() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(
                mode=OrchestratorMode.idle,
                run_history=[
                    {"issue_id": "bz1", "result": "completed", "summary": "done", "timestamp": "2026-01-01T10:00:00", "branch": "amp/bz1"},
                    {"issue_id": "bz2", "result": "failed", "summary": "boom", "timestamp": "2026-01-01T11:00:00"},
                ],
            ),
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        history_table = app.query_one("#history-datatable")
        assert history_table.row_count == 2


@pytest.mark.asyncio
async def test_apply_snapshot_error_state() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(
                mode=OrchestratorMode.error,
                last_error="merge failed at rebase",
            ),
        )
        app._apply_snapshot(snap)
        await pilot.pause()


@pytest.mark.asyncio
async def test_refresh_binding_no_crash() -> None:
    """Pressing 'r' triggers action_refresh without error."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()


@pytest.mark.asyncio
async def test_app_no_timers_without_paths() -> None:
    """App without repo_root/state_dir should not start timers."""
    app = OrchestratorApp()
    async with app.run_test():
        assert app.query_one(StatusPanel)
        assert app._config is not None


@pytest.mark.asyncio
async def test_inspect_queue_item() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            ready_issues=[
                BdIssue(
                    id="bz1",
                    title="Fix bug",
                    priority=1,
                    created="2026-01-01",
                    description="Some long desc",
                    acceptance_criteria="It works",
                ),
            ],
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        table = app.query_one("#queue-datatable", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()

        from orc.tui.issue_inspect import IssueInspectScreen

        assert isinstance(app.screen, IssueInspectScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, IssueInspectScreen)


@pytest.mark.asyncio
async def test_inspect_history_item() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(
                run_history=[
                    {
                        "issue_id": "bz1",
                        "result": "completed",
                        "summary": "done",
                        "timestamp": "2026-01-01T10:00:00",
                        "branch": "amp/bz1",
                    },
                ],
            ),
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        table = app.query_one("#history-datatable", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        from orc.tui.issue_inspect import IssueInspectScreen

        assert isinstance(app.screen, IssueInspectScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, IssueInspectScreen)


@pytest.mark.asyncio
async def test_inspect_history_item_open_log_prefers_preflight(tmp_path: Path) -> None:
    log_path = tmp_path / "preflight.jsonl"
    log_path.write_text('{"type":"session_start"}\n', encoding="utf-8")

    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(
                run_history=[
                    {
                        "issue_id": "bz2",
                        "result": "skipped_already_implemented",
                        "summary": "already done",
                        "timestamp": "2026-01-01T10:00:00",
                        "preflight_log_path": str(log_path),
                    },
                ],
            ),
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        table = app.query_one("#history-datatable", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        from orc.tui.issue_inspect import IssueInspectScreen
        from orc.tui.modals import AmpStreamModal

        assert isinstance(app.screen, IssueInspectScreen)

        await pilot.press("a")
        await pilot.pause()

        assert isinstance(app.screen, AmpStreamModal)


@pytest.mark.asyncio
async def test_quit_binding() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("q")


@pytest.mark.asyncio
async def test_stop_shows_confirmation_modal() -> None:
    """Pressing 'x' should show the ConfirmStopModal."""
    app = OrchestratorApp(state_dir=Path("/tmp/fake"))
    async with app.run_test() as pilot:
        app._orch_mode = OrchestratorMode.running
        await pilot.press("x")
        await pilot.pause()

        from orc.tui.modals import ConfirmStopModal

        assert isinstance(app.screen, ConfirmStopModal)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmStopModal)


@pytest.mark.asyncio
async def test_help_overlay_opens_and_closes() -> None:
    """Pressing '?' shows the HelpModal, pressing Escape closes it."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()

        from orc.tui.modals import HelpModal

        assert isinstance(app.screen, HelpModal)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_help_overlay_closes_with_question_mark() -> None:
    """Pressing '?' again dismisses the HelpModal."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()

        from orc.tui.modals import HelpModal

        assert isinstance(app.screen, HelpModal)

        await pilot.press("question_mark")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_start_launches_subprocess() -> None:
    """Pressing 's' launches orchestrator via subprocess, not blocking call."""
    app = OrchestratorApp(repo_root=Path("/tmp/repo"), state_dir=Path("/tmp/state"))
    async with app.run_test(notifications=True) as pilot:
        with patch(
            "orc.subprocess_launcher.launch_orchestrator"
        ) as mock_launch:
            from unittest.mock import MagicMock

            mock_launch.return_value = MagicMock(pid=42)
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()  # extra pause for thread to complete
            mock_launch.assert_called_once_with(
                "start", Path("/tmp/repo"), Path("/tmp/state")
            )


@pytest.mark.asyncio
async def test_apply_snapshot_policy_skipped_counts() -> None:
    """StatusPanel should include policy-skipped count after applying snapshot."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(mode=OrchestratorMode.idle),
            queue_breakdown=QueueBreakdown(
                beads_ready=5,
                policy_skipped=3,
                held_and_ready=0,
                runnable=2,
            ),
            queue_skip_summary={"container_parent": 2, "unsupported_type": 1},
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        status_panel = app.query_one(StatusPanel)
        assert status_panel._cached_policy_skipped == 3


@pytest.mark.asyncio
async def test_apply_snapshot_empty_frontier_policy_skipped() -> None:
    """QueueTable should show policy-skipped empty-state message."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(mode=OrchestratorMode.idle),
            queue_breakdown=QueueBreakdown(
                beads_ready=3,
                policy_skipped=3,
                held_and_ready=0,
                runnable=0,
            ),
        )
        app._apply_snapshot(snap)
        await pilot.pause()

        table = app.query_one("#queue-datatable", DataTable)
        assert table.row_count == 1  # placeholder row


@pytest.mark.asyncio
async def test_pause_no_project_shows_notification() -> None:
    """Pressing 'p' with no state_dir should show an error notification."""
    app = OrchestratorApp()
    async with app.run_test(notifications=True) as pilot:
        await pilot.press("p")
        await pilot.pause()
