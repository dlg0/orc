"""Integration tests for the TUI dashboard layout."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from textual.widgets import DataTable

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore
from amp_orchestrator.tui.app import OrchestratorApp
from amp_orchestrator.tui.snapshot import DashboardSnapshot
from amp_orchestrator.tui.widgets import (
    ActiveIssuePanel,
    ConfigPanel,
    ControlsPanel,
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
    async with app.run_test() as pilot:
        assert app.query_one(StatusPanel)
        assert app.query_one(ActiveIssuePanel)
        assert app.query_one(ConfigPanel)
        assert app.query_one(ControlsPanel)
        assert app.query_one(QueueTable)
        assert app.query_one(EventsLog)
        assert app.query_one(HistoryTable)


@pytest.mark.asyncio
async def test_apply_snapshot_running() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(
            state=OrchestratorState(
                mode=OrchestratorMode.running,
                active_issue_id="bz1",
                active_issue_title="Fix widget",
                active_branch="amp/bz1-fix",
                active_worktree_path="/tmp/wt",
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
        assert history_table.row_count == 0


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
    async with app.run_test() as pilot:
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

        from amp_orchestrator.tui.modals import InspectModal

        assert isinstance(app.screen, InspectModal)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InspectModal)


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

        from amp_orchestrator.tui.modals import InspectModal

        assert isinstance(app.screen, InspectModal)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InspectModal)


@pytest.mark.asyncio
async def test_quit_binding() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("q")


@pytest.mark.asyncio
async def test_controls_buttons_disabled_in_idle() -> None:
    """In idle mode, only Start should be enabled."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(state=OrchestratorState(mode=OrchestratorMode.idle))
        app._apply_snapshot(snap)
        await pilot.pause()

        from textual.widgets import Button

        assert not app.query_one("#btn-start", Button).disabled
        assert app.query_one("#btn-pause", Button).disabled
        assert app.query_one("#btn-resume", Button).disabled
        assert app.query_one("#btn-stop", Button).disabled


@pytest.mark.asyncio
async def test_controls_buttons_in_running() -> None:
    """In running mode, Pause and Stop should be enabled."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(state=OrchestratorState(mode=OrchestratorMode.running))
        app._apply_snapshot(snap)
        await pilot.pause()

        from textual.widgets import Button

        assert app.query_one("#btn-start", Button).disabled
        assert not app.query_one("#btn-pause", Button).disabled
        assert app.query_one("#btn-resume", Button).disabled
        assert not app.query_one("#btn-stop", Button).disabled


@pytest.mark.asyncio
async def test_controls_buttons_in_paused() -> None:
    """In paused mode, Start and Resume should be enabled."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        snap = _make_snap(state=OrchestratorState(mode=OrchestratorMode.paused))
        app._apply_snapshot(snap)
        await pilot.pause()

        from textual.widgets import Button

        assert not app.query_one("#btn-start", Button).disabled
        assert app.query_one("#btn-pause", Button).disabled
        assert not app.query_one("#btn-resume", Button).disabled
        assert app.query_one("#btn-stop", Button).disabled


@pytest.mark.asyncio
async def test_stop_shows_confirmation_modal() -> None:
    """Pressing 'x' should show the ConfirmStopModal."""
    app = OrchestratorApp(state_dir=Path("/tmp/fake"))
    async with app.run_test() as pilot:
        await pilot.press("x")
        await pilot.pause()

        from amp_orchestrator.tui.modals import ConfirmStopModal

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

        from amp_orchestrator.tui.modals import HelpModal

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

        from amp_orchestrator.tui.modals import HelpModal

        assert isinstance(app.screen, HelpModal)

        await pilot.press("question_mark")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_pause_no_project_shows_notification() -> None:
    """Pressing 'p' with no state_dir should show an error notification."""
    app = OrchestratorApp()
    async with app.run_test(notifications=True) as pilot:
        await pilot.press("p")
        await pilot.pause()
