"""Integration tests for the TUI dashboard layout."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore
from amp_orchestrator.tui.app import OrchestratorApp
from amp_orchestrator.tui.snapshot import DashboardSnapshot
from amp_orchestrator.tui.widgets import (
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
    async with app.run_test() as pilot:
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
async def test_quit_binding() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("q")
