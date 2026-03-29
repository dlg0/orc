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
