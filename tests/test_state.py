"""Tests for orc.state."""

from __future__ import annotations

import pytest

from orc.state import (
    can_retry_merge,
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    queue_retry,
    RunCheckpoint,
    RunStage,
    StateStore,
)


def test_default_state_is_idle() -> None:
    state = OrchestratorState()
    assert state.mode is OrchestratorMode.idle


def test_save_load_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    checkpoint = RunCheckpoint(
        issue_id="ISSUE-42",
        issue_title="Fix widgets",
        branch="fix/issue-42",
        worktree_path="/tmp/wt",
        stage=RunStage.amp_running,
    )
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_run=checkpoint.to_dict(),
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
    """Old state.json files with active_* fields migrate to active_run."""
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
    assert state.active_branch == "amp/X-1"
    assert state.active_worktree_path == "/tmp/wt"
    assert state.active_run is not None
    assert state.active_run["issue_id"] == "X-1"


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
        "ISSUE-10": {
            "category": "issue_needs_rework",
            "action": "hold_until_backlog_changes",
            "stage": "legacy",
            "summary": "Missing tests",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "attempts": 1,
            "branch": None,
            "worktree_path": None,
            "preserve_worktree": False,
            "extra": None,
        },
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
        "ISSUE-5": {
            "category": "issue_needs_rework",
            "action": "hold_until_backlog_changes",
            "stage": "legacy",
            "summary": "Bad output",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "attempts": 1,
            "branch": None,
            "worktree_path": None,
            "preserve_worktree": False,
            "extra": None,
        },
    }


def test_load_normalizes_legacy_issue_failures(tmp_path) -> None:
    import json

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "mode": "idle",
        "last_completed_issue": None,
        "last_error": None,
        "run_history": [],
        "issue_failures": {
            "ISSUE-5": {"summary": "Bad output", "timestamp": "2026-01-01T00:00:00+00:00"},
            "ISSUE-6": "Needs another pass",
        },
    }))

    store = StateStore(tmp_path)
    state = store.load()

    assert state.issue_failures["ISSUE-5"]["category"] == "issue_needs_rework"
    assert state.issue_failures["ISSUE-5"]["action"] == "hold_until_backlog_changes"
    assert state.issue_failures["ISSUE-5"]["stage"] == "legacy"
    assert state.issue_failures["ISSUE-5"]["summary"] == "Bad output"

    assert state.issue_failures["ISSUE-6"]["category"] == "issue_needs_rework"
    assert state.issue_failures["ISSUE-6"]["action"] == "hold_until_backlog_changes"
    assert state.issue_failures["ISSUE-6"]["stage"] == "legacy"
    assert state.issue_failures["ISSUE-6"]["summary"] == "Needs another pass"
    assert state.issue_failures["ISSUE-6"]["timestamp"] == ""

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


def test_can_retry_merge_requires_preserved_branch_and_worktree() -> None:
    assert can_retry_merge({
        "category": "stale_or_conflicted",
        "summary": "conflict",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "branch": "amp/X-1",
        "worktree_path": "/tmp/wt",
        "preserve_worktree": True,
    }) is True
    assert can_retry_merge({
        "category": "stale_or_conflicted",
        "summary": "conflict",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "branch": "amp/X-1",
        "worktree_path": None,
        "preserve_worktree": True,
    }) is False


def test_queue_retry_uses_resume_candidate_for_merge_retryable_failures() -> None:
    state = OrchestratorState(
        issue_failures={
            "X-1": {
                "category": "stale_or_conflicted",
                "action": "hold_for_retry",
                "stage": "merge/rebase",
                "summary": "conflict",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "branch": "amp/X-1",
                "worktree_path": "/tmp/wt",
                "preserve_worktree": True,
            }
        }
    )

    message = queue_retry(state, "X-1")

    assert message == "Scheduled merge retry for X-1 — will retry verify-and-merge on next run"
    assert "X-1" not in state.issue_failures
    assert state.resume_candidate is not None
    assert state.resume_candidate["issue_id"] == "X-1"
    assert state.resume_candidate["stage"] == "ready_to_merge"


def test_queue_retry_merge_only_rejects_non_merge_failure() -> None:
    state = OrchestratorState(
        issue_failures={
            "X-2": {
                "category": "issue_needs_rework",
                "action": "hold_for_retry",
                "stage": "evaluation",
                "summary": "tests",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        }
    )

    with pytest.raises(ValueError, match="not eligible for merge-only retry"):
        queue_retry(state, "X-2", merge_only=True)


def test_active_issue_title_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    checkpoint = RunCheckpoint(
        issue_id="X-1",
        issue_title="Fix the widget",
        stage=RunStage.amp_running,
    )
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_run=checkpoint.to_dict(),
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


def test_run_checkpoint_to_dict_from_dict_round_trip() -> None:
    checkpoint = RunCheckpoint(
        issue_id="TEST-1",
        issue_title="Fix the bug",
        branch="amp/TEST-1-fix-the-bug",
        worktree_path="/tmp/wt/TEST-1",
        stage=RunStage.amp_running,
        bd_claimed=True,
        amp_result={"result": "completed", "summary": "done"},
        eval_result=None,
        preserve_worktree=False,
        resume_attempts=1,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    d = checkpoint.to_dict()
    assert d["issue_id"] == "TEST-1"
    assert d["stage"] == "amp_running"
    assert d["bd_claimed"] is True
    assert d["resume_attempts"] == 1

    restored = RunCheckpoint.from_dict(d)
    assert restored.issue_id == "TEST-1"
    assert restored.issue_title == "Fix the bug"
    assert restored.branch == "amp/TEST-1-fix-the-bug"
    assert restored.stage is RunStage.amp_running
    assert restored.bd_claimed is True
    assert restored.amp_result == {"result": "completed", "summary": "done"}
    assert restored.eval_result is None
    assert restored.resume_attempts == 1
    assert restored.updated_at == "2026-01-01T00:00:00+00:00"


def test_run_checkpoint_from_dict_defaults() -> None:
    d = {"issue_id": "X-1", "issue_title": "Test", "stage": "claimed"}
    checkpoint = RunCheckpoint.from_dict(d)
    assert checkpoint.branch is None
    assert checkpoint.worktree_path is None
    assert checkpoint.bd_claimed is False
    assert checkpoint.amp_result is None
    assert checkpoint.resume_attempts == 0
    assert checkpoint.updated_at == ""


def test_run_stage_values() -> None:
    expected = {
        "worktree_created", "claimed", "amp_running", "amp_finished",
        "evaluation_running", "ready_to_merge", "merge_running",
        "claim_release_pending",
    }
    assert {s.value for s in RunStage} == expected


def test_active_run_round_trip(tmp_path) -> None:
    """active_run dict saves and loads correctly."""
    store = StateStore(tmp_path)
    checkpoint = RunCheckpoint(
        issue_id="X-1",
        issue_title="Test",
        branch="amp/X-1",
        stage=RunStage.claimed,
        bd_claimed=True,
    )
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_run=checkpoint.to_dict(),
    )
    store.save(state)
    loaded = store.load()
    assert loaded.active_run is not None
    assert loaded.active_run["issue_id"] == "X-1"
    assert loaded.active_run["bd_claimed"] is True
    assert loaded.active_issue_id == "X-1"
    assert loaded.active_branch == "amp/X-1"


def test_active_run_none_round_trip(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = OrchestratorState(active_run=None)
    store.save(state)
    loaded = store.load()
    assert loaded.active_run is None
    assert loaded.active_issue_id is None
    assert loaded.active_branch is None
    assert loaded.active_stage is None


def test_backward_compat_active_fields_to_active_run(tmp_path) -> None:
    """Old state.json with active_issue_id but no active_run migrates properly."""
    import json
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "mode": "running",
        "active_issue_id": "LEGACY-1",
        "active_issue_title": "Legacy title",
        "active_branch": "amp/LEGACY-1",
        "active_worktree_path": "/tmp/wt/LEGACY-1",
        "active_stage": "running agent",
        "active_started_at": "2026-01-01T00:00:00+00:00",
        "last_completed_issue": None,
        "last_error": None,
        "run_history": [],
    }))
    store = StateStore(tmp_path)
    state = store.load()
    assert state.active_run is not None
    assert state.active_issue_id == "LEGACY-1"
    assert state.active_issue_title == "Legacy title"
    assert state.active_branch == "amp/LEGACY-1"
    assert state.active_worktree_path == "/tmp/wt/LEGACY-1"


def test_backward_compat_null_active_id_to_none_active_run(tmp_path) -> None:
    """Old state.json with active_issue_id=null → active_run=None."""
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
    assert state.active_run is None
    assert state.active_issue_id is None


def test_failure_category_values() -> None:
    expected = {
        "transient_external",
        "stale_or_conflicted",
        "issue_needs_rework",
        "blocked_by_dependency",
        "fatal_run_error",
    }
    assert {c.value for c in FailureCategory} == expected
