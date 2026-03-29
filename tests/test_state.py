"""Tests for amp_orchestrator.state."""

from __future__ import annotations

import pytest

from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore


def test_default_state_is_idle() -> None:
    state = OrchestratorState()
    assert state.mode is OrchestratorMode.idle


def test_save_load_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_issue_id="ISSUE-42",
        active_branch="fix/issue-42",
        active_worktree_path="/tmp/wt",
        last_completed_issue="ISSUE-41",
        last_error=None,
        run_history=[{"issue": "ISSUE-41", "result": "ok"}],
    )
    store.save(state)
    loaded = store.load()
    assert loaded.mode is OrchestratorMode.running
    assert loaded.active_issue_id == "ISSUE-42"
    assert loaded.active_branch == "fix/issue-42"
    assert loaded.active_worktree_path == "/tmp/wt"
    assert loaded.last_completed_issue == "ISSUE-41"
    assert loaded.last_error is None
    assert loaded.run_history == [{"issue": "ISSUE-41", "result": "ok"}]


def test_load_missing_file_returns_default(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = store.load()
    assert state.mode is OrchestratorMode.idle
    assert state.active_issue_id is None


def test_valid_transition(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState()
    state = store.transition(state, OrchestratorMode.running)
    assert state.mode is OrchestratorMode.running


def test_invalid_transition_raises(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState()
    with pytest.raises(ValueError, match="Invalid transition"):
        store.transition(state, OrchestratorMode.paused)


def test_transition_saves_state(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState()
    store.transition(state, OrchestratorMode.running)
    loaded = store.load()
    assert loaded.mode is OrchestratorMode.running


def test_load_backward_compat_missing_fields(tmp_path) -> None:
    """Old state.json files without active_issue_title still load fine."""
    import json
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "mode": "running",
        "active_issue_id": "X-1",
        "active_branch": "amp/X-1",
        "active_worktree_path": "/tmp/wt",
        "last_completed_issue": None,
        "last_error": None,
        "run_history": [],
    }))
    store = StateStore(tmp_path)
    state = store.load()
    assert state.mode is OrchestratorMode.running
    assert state.active_issue_id == "X-1"
    assert state.active_issue_title is None


def test_needs_rework_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState(
        needs_rework={
            "ISSUE-10": {"summary": "Missing tests", "timestamp": "2026-01-01T00:00:00+00:00"},
        },
    )
    store.save(state)
    loaded = store.load()
    assert loaded.needs_rework == {
        "ISSUE-10": {"summary": "Missing tests", "timestamp": "2026-01-01T00:00:00+00:00"},
    }


def test_load_backward_compat_missing_needs_rework(tmp_path) -> None:
    """Old state.json without needs_rework loads as empty dict."""
    import json
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "mode": "idle",
        "active_issue_id": None,
        "active_branch": None,
        "active_worktree_path": None,
        "last_completed_issue": None,
        "last_error": None,
        "run_history": [],
    }))
    store = StateStore(tmp_path)
    state = store.load()
    assert state.needs_rework == {}


def test_active_issue_title_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_issue_id="X-1",
        active_issue_title="Fix the widget",
    )
    store.save(state)
    loaded = store.load()
    assert loaded.active_issue_title == "Fix the widget"
