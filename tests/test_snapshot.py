"""Tests for the DashboardSnapshot loader."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore
from amp_orchestrator.tui.snapshot import DashboardSnapshot, load_snapshot


def test_load_snapshot_defaults(tmp_path: Path) -> None:
    """With no state file, returns default state."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=[]):
        snap = load_snapshot(tmp_path, state_dir)

    assert snap.state.mode == OrchestratorMode.idle
    assert snap.ready_issues == []
    assert snap.recent_events == []
    assert isinstance(snap.config, OrchestratorConfig)


def test_load_snapshot_with_state(tmp_path: Path) -> None:
    """Snapshot reflects persisted state."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    store = StateStore(state_dir)
    store.save(OrchestratorState(
        mode=OrchestratorMode.running,
        active_issue_id="X-1",
        active_issue_title="Fix bug",
    ))

    issue = BdIssue(id="X-2", title="New feature", priority=2, created="2026-01-01")
    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=[issue]):
        snap = load_snapshot(tmp_path, state_dir)

    assert snap.state.mode == OrchestratorMode.running
    assert snap.state.active_issue_id == "X-1"
    assert snap.state.active_issue_title == "Fix bug"
    assert len(snap.ready_issues) == 1
    assert snap.ready_issues[0].id == "X-2"


def test_load_snapshot_with_events(tmp_path: Path) -> None:
    """Snapshot includes recent events."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    from amp_orchestrator.events import EventLog, EventType
    events = EventLog(state_dir)
    events.record(EventType.state_changed, {"to": "running"})
    events.record(EventType.issue_selected, {"issue_id": "X-1"})

    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=[]):
        snap = load_snapshot(tmp_path, state_dir)

    assert len(snap.recent_events) == 2


def test_load_snapshot_config_error_uses_default(tmp_path: Path) -> None:
    """If config loading fails, use defaults."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    with (
        patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=[]),
        patch("amp_orchestrator.tui.snapshot.load_config", side_effect=Exception("bad config")),
    ):
        snap = load_snapshot(tmp_path, state_dir)

    assert snap.config.base_branch == "main"
