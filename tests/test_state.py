"""Tests for amp_orchestrator.state."""

from __future__ import annotations

import pytest

from amp_orchestrator.state import (
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    StateStore,
)


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


def test_issue_failures_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState(
        issue_failures={
            "ISSUE-10": {"summary": "Missing tests", "timestamp": "2026-01-01T00:00:00+00:00"},
        },
    )
    store.save(state)
    loaded = store.load()
    assert loaded.issue_failures == {
        "ISSUE-10": {"summary": "Missing tests", "timestamp": "2026-01-01T00:00:00+00:00"},
    }


def test_load_backward_compat_needs_rework_migrated(tmp_path) -> None:
    """Old state.json with needs_rework loads as issue_failures."""
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
        "needs_rework": {
            "ISSUE-5": {"summary": "Bad output", "timestamp": "2026-01-01T00:00:00+00:00"},
        },
    }))
    store = StateStore(tmp_path)
    state = store.load()
    assert state.issue_failures == {
        "ISSUE-5": {"summary": "Bad output", "timestamp": "2026-01-01T00:00:00+00:00"},
    }


def test_load_backward_compat_missing_issue_failures(tmp_path) -> None:
    """Old state.json without needs_rework or issue_failures loads as empty dict."""
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
    assert state.issue_failures == {}


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


def test_issue_failure_to_dict_from_dict_round_trip() -> None:
    failure = IssueFailure(
        category=FailureCategory.issue_needs_rework,
        action=FailureAction.hold_until_backlog_changes,
        stage="eval",
        summary="Missing tests",
        timestamp="2026-01-01T00:00:00+00:00",
        attempts=2,
        branch="amp/test-1",
        worktree_path="/tmp/wt",
        preserve_worktree=True,
        extra={"key": "value"},
    )
    d = failure.to_dict()
    assert d["category"] == "issue_needs_rework"
    assert d["action"] == "hold_until_backlog_changes"
    assert d["stage"] == "eval"
    assert d["summary"] == "Missing tests"
    assert d["attempts"] == 2
    assert d["branch"] == "amp/test-1"
    assert d["preserve_worktree"] is True
    assert d["extra"] == {"key": "value"}

    restored = IssueFailure.from_dict(d)
    assert restored.category is FailureCategory.issue_needs_rework
    assert restored.action is FailureAction.hold_until_backlog_changes
    assert restored.stage == "eval"
    assert restored.summary == "Missing tests"
    assert restored.timestamp == "2026-01-01T00:00:00+00:00"
    assert restored.attempts == 2
    assert restored.branch == "amp/test-1"
    assert restored.worktree_path == "/tmp/wt"
    assert restored.preserve_worktree is True
    assert restored.extra == {"key": "value"}


def test_issue_failure_from_dict_defaults() -> None:
    d = {
        "category": "transient_external",
        "action": "auto_retry",
        "stage": "amp",
        "summary": "Network timeout",
        "timestamp": "2026-03-01T00:00:00+00:00",
    }
    failure = IssueFailure.from_dict(d)
    assert failure.attempts == 1
    assert failure.branch is None
    assert failure.worktree_path is None
    assert failure.preserve_worktree is False
    assert failure.extra is None


def test_failure_category_values() -> None:
    expected = {
        "transient_external",
        "stale_or_conflicted",
        "issue_needs_rework",
        "blocked_by_dependency",
        "fatal_run_error",
    }
    assert {c.value for c in FailureCategory} == expected
