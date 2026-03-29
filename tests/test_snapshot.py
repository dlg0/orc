"""Tests for the DashboardSnapshot loader."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue, QueueResult
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, RunCheckpoint, RunStage, StateStore
from amp_orchestrator.tui.snapshot import DashboardSnapshot, load_snapshot, load_snapshot_fast


def test_load_snapshot_defaults(tmp_path: Path) -> None:
    """With no state file, returns default state."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=QueueResult()):
        snap = load_snapshot(tmp_path, state_dir)

    assert snap.state.mode == OrchestratorMode.idle
    assert snap.ready_issues == []
    assert snap.recent_events == []
    assert isinstance(snap.config, OrchestratorConfig)
    assert snap.is_fast is False


def test_load_snapshot_with_state(tmp_path: Path) -> None:
    """Snapshot reflects persisted state."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    store = StateStore(state_dir)
    checkpoint = RunCheckpoint(
        issue_id="X-1", issue_title="Fix bug", stage=RunStage.amp_running,
    )
    store.save(OrchestratorState(
        mode=OrchestratorMode.running,
        active_run=checkpoint.to_dict(),
    ))

    issue = BdIssue(id="X-2", title="New feature", priority=2, created="2026-01-01")
    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=QueueResult(issues=[issue])):
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

    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=QueueResult()):
        snap = load_snapshot(tmp_path, state_dir)

    assert len(snap.recent_events) == 2


def test_load_snapshot_config_error_uses_default(tmp_path: Path) -> None:
    """If config loading fails, use defaults."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    with (
        patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=QueueResult()),
        patch("amp_orchestrator.tui.snapshot.load_config", side_effect=Exception("bad config")),
    ):
        snap = load_snapshot(tmp_path, state_dir)

    assert snap.config.base_branch == "main"


def test_load_snapshot_fast_defaults(tmp_path: Path) -> None:
    """Fast snapshot returns state and events, no queue."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    snap = load_snapshot_fast(state_dir)

    assert snap.state.mode == OrchestratorMode.idle
    assert snap.ready_issues == []
    assert snap.recent_events == []
    assert isinstance(snap.config, OrchestratorConfig)
    assert snap.is_fast is True


def test_load_snapshot_fast_with_config(tmp_path: Path) -> None:
    """Fast snapshot uses provided config."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    config = OrchestratorConfig(base_branch="develop")
    snap = load_snapshot_fast(state_dir, config)

    assert snap.config.base_branch == "develop"


def test_load_snapshot_corrupt_state(tmp_path: Path) -> None:
    """Corrupt state file causes StateStore.load() to raise; snapshot should handle."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    state_file = state_dir / "state.json"
    state_file.write_text("{not valid json!!!")

    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=QueueResult()):
        import pytest
        with pytest.raises(Exception):
            load_snapshot(tmp_path, state_dir)


def test_load_snapshot_missing_state_dir(tmp_path: Path) -> None:
    """Missing state dir (no state.json) returns default state."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    with patch("amp_orchestrator.tui.snapshot.get_ready_issues", return_value=QueueResult()):
        snap = load_snapshot(tmp_path, state_dir)

    assert snap.state.mode == OrchestratorMode.idle


def test_load_snapshot_fast_corrupt_state(tmp_path: Path) -> None:
    """Corrupt state file raises on fast snapshot too."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    state_file = state_dir / "state.json"
    state_file.write_text("{{bad}}")

    import pytest
    with pytest.raises(Exception):
        load_snapshot_fast(state_dir)


def test_load_snapshot_fast_with_events(tmp_path: Path) -> None:
    """Fast snapshot includes recent events."""
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()

    from amp_orchestrator.events import EventLog, EventType

    events = EventLog(state_dir)
    events.record(EventType.state_changed, {"to": "running"})

    snap = load_snapshot_fast(state_dir)

    assert len(snap.recent_events) == 1
    assert snap.ready_issues == []
