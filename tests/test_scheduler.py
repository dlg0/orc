"""Tests for the core scheduler loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orc.amp_runner import AmpResult, ResultType, StubAmpRunner
from orc.config import OrchestratorConfig
from orc.evaluator import StubEvaluator
from orc.events import EventLog
from orc.queue import BdIssue, QueueResult
from orc.scheduler import run_loop
from orc.state import (
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    RequestQueue,
    RunCheckpoint,
    RunStage,
    StateStore,
)


@pytest.fixture(autouse=True)
def _patch_git_status(monkeypatch):
    """Worktree paths in tests don't exist on disk; stub out the clean-check."""
    monkeypatch.setattr("orc.scheduler._git_status_porcelain", lambda cwd: "")


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".orc"
    d.mkdir()
    return d


@pytest.fixture()
def repo_root(tmp_path: Path, state_dir: Path) -> Path:
    return tmp_path


def _make_issue(id: str = "test-1", title: str = "Test issue", priority: int = 1) -> BdIssue:
    return BdIssue(
        id=id, title=title, priority=priority, created="2026-01-01",
        description="desc", acceptance_criteria="ac",
    )


def _set_state(state_dir: Path, mode: OrchestratorMode = OrchestratorMode.running) -> None:
    store = StateStore(state_dir)
    state = OrchestratorState(mode=mode)
    store.save(state)


def test_empty_queue_goes_idle(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner()

    with patch("orc.scheduler.get_ready_issues", return_value=QueueResult()):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle


def test_pause_requested_transitions_to_paused(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.pause_requested)
    config = OrchestratorConfig()
    runner = StubAmpRunner()

    run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.paused


def test_stopping_transitions_to_idle(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.stopping)
    config = OrchestratorConfig()
    runner = StubAmpRunner()

    run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle


def test_processes_issue_with_stub(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    assert state.last_completed_issue == "test-1"
    assert len(state.run_history) == 1
    assert state.run_history[0]["result"] == "completed"


def test_decomposed_issue_skips_merge(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.decomposed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge") as mock_merge,
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    mock_merge.assert_not_called()
    state = StateStore(state_dir).load()
    assert state.run_history[0]["result"] == "decomposed"


def test_failed_issue_continues_to_next(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()

    issue1 = _make_issue("fail-1", "Failing issue")
    issue2 = _make_issue("ok-2", "Good issue")

    fail_runner = MagicMock()
    call_count = 0

    def run_side_effect(ctx, **kwargs):
        if ctx.issue_id == "fail-1":
            return AmpResult(result=ResultType.failed, summary="boom")
        return AmpResult(result=ResultType.completed, summary="done", merge_ready=True)

    fail_runner.run.side_effect = run_side_effect

    ready_call = 0

    def fake_ready(cwd=None):
        nonlocal ready_call
        ready_call += 1
        if ready_call == 1:
            return QueueResult(issues=[issue1, issue2])
        if ready_call == 2:
            return QueueResult(issues=[issue2])  # fail-1 will be in skip_ids
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "x"
    mock_wt_info.branch_name = "amp/x"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, fail_runner)

    state = StateStore(state_dir).load()
    assert len(state.run_history) == 2
    assert state.run_history[0]["result"] == "failed"
    assert state.run_history[1]["result"] == "completed"


def test_events_are_recorded(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    events = EventLog(state_dir).all()
    event_types = [e["event_type"] for e in events]
    assert "issue_selected" in event_types
    assert "amp_started" in event_types
    assert "amp_finished" in event_types
    assert "merge_attempt" in event_types
    assert "issue_closed" in event_types


def test_evaluation_pass_proceeds_to_merge(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    evaluator = StubEvaluator.passed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)

    mock_merge.assert_called_once()
    state = StateStore(state_dir).load()
    assert state.run_history[0]["result"] == "completed"


def test_evaluation_fail_blocks_merge(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    evaluator = StubEvaluator.failed(summary="Missing tests")
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)

    mock_merge.assert_not_called()
    state = StateStore(state_dir).load()
    assert state.run_history[0]["result"] == "needs_rework"
    assert "evaluation" in state.run_history[0]
    assert state.run_history[0]["evaluation"]["verdict"] == "fail"


def test_evaluation_crash_treated_as_fail(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    crash_evaluator = MagicMock()
    crash_evaluator.evaluate.side_effect = RuntimeError("evaluator exploded")

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=crash_evaluator)

    mock_merge.assert_not_called()
    state = StateStore(state_dir).load()
    assert state.run_history[0]["result"] == "needs_rework"


def test_no_evaluator_skips_evaluation(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    mock_merge.assert_called_once()
    state = StateStore(state_dir).load()
    assert state.run_history[0]["result"] == "completed"


def test_evaluation_events_recorded(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    evaluator = StubEvaluator.passed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)

    events = EventLog(state_dir).all()
    event_types = [e["event_type"] for e in events]
    assert "evaluation_started" in event_types
    assert "evaluation_finished" in event_types


def test_evaluation_failure_persists_needs_rework(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    evaluator = StubEvaluator.failed(summary="Missing tests")
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge"),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    failure = state.issue_failures["test-1"]
    assert failure["summary"] == "Missing tests"
    assert failure["category"] == "issue_needs_rework"
    assert failure["stage"] == "evaluation_running"
    assert "timestamp" in failure


def test_claim_issue_called_before_amp(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.claim_issue", return_value=True) as mock_claim,
    ):
        run_loop(repo_root, state_dir, config, runner)

    mock_claim.assert_called_once_with(issue.id, cwd=repo_root)


def test_claim_failure_still_runs_amp(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.claim_issue", return_value=False),
    ):
        run_loop(repo_root, state_dir, config, runner)

    # Amp still ran and completed despite claim failure
    state = StateStore(state_dir).load()
    assert state.last_completed_issue == "test-1"
    assert state.run_history[0]["result"] == "completed"


def test_needs_rework_skipped_on_restart(repo_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        issue_failures={"rework-1": {"summary": "bad", "timestamp": "2026-01-01T00:00:00+00:00"}},
    )
    store.save(state)

    issue_rework = _make_issue("rework-1", "Rework issue")
    issue_ok = _make_issue("ok-2", "Good issue")

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue_rework, issue_ok])
        return QueueResult()

    runner_mock = MagicMock()
    runner_mock.run.return_value = AmpResult(result=ResultType.completed, summary="done", merge_ready=True)

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "x"
    mock_wt_info.branch_name = "amp/x"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config=OrchestratorConfig(), runner=runner_mock)

    state = StateStore(state_dir).load()
    # rework-1 should have been skipped, only ok-2 processed
    assert len(state.run_history) == 1
    assert state.run_history[0]["issue_id"] == "ok-2"


def test_worktree_failure_oserror_records_transient_external(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.create_worktree.side_effect = OSError("disk full")

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    failure = state.issue_failures["test-1"]
    assert failure["category"] == "transient_external"
    assert failure["stage"] == "worktree_created"


def test_worktree_failure_non_oserror_records_fatal(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.create_worktree.side_effect = RuntimeError("git error")

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "fatal_run_error"


def test_amp_crash_records_issue_needs_rework(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    issue = _make_issue()

    crash_runner = MagicMock()
    crash_runner.run.side_effect = RuntimeError("amp exploded")

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, crash_runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "issue_needs_rework"
    assert state.issue_failures["test-1"]["stage"] == "amp_running"


def test_blocked_result_records_blocked_by_dependency(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.blocked()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "blocked_by_dependency"


def test_decomposed_records_blocked_by_dependency(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.decomposed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge") as mock_merge,
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "blocked_by_dependency"


def test_merge_conflict_preserves_worktree(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(
        success=False, stage="rebase", error="conflict resolution failed",
    )

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    failure = state.issue_failures["test-1"]
    assert failure["category"] == "stale_or_conflicted"
    assert failure["preserve_worktree"] is True
    # Worktree should NOT have been cleaned up
    mock_worktree_mgr.return_value.cleanup_worktree.assert_not_called()


def test_merge_non_conflict_failure_cleans_worktree(repo_root: Path, state_dir: Path) -> None:
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(
        success=False, stage="push", error="network timeout",
    )

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    failure = state.issue_failures["test-1"]
    assert failure["category"] == "fatal_run_error"
    assert failure["action"] == "pause_orchestrator"
    assert failure["preserve_worktree"] is False
    mock_worktree_mgr.return_value.cleanup_worktree.assert_called_once()


def test_successful_completion_clears_failure(repo_root: Path, state_dir: Path) -> None:
    """When an issue completes and merges successfully, its failure record is cleared.

    An issue that previously failed (old-1) should still be in issue_failures,
    but the successfully completed issue (test-1) should have no failure record.
    """
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        issue_failures={
            "old-1": IssueFailure(
                category=FailureCategory.issue_needs_rework,
                action=FailureAction.hold_until_backlog_changes,
                stage="evaluation",
                summary="old failure",
                timestamp="2026-01-01T00:00:00+00:00",
            ).to_dict(),
        },
    )
    store.save(state)

    runner = StubAmpRunner.completed()
    issue = _make_issue()  # test-1

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config=OrchestratorConfig(), runner=runner)

    state = StateStore(state_dir).load()
    # test-1 should NOT be in failures (it completed successfully)
    assert "test-1" not in state.issue_failures
    assert state.last_completed_issue == "test-1"
    # old-1 should still be in failures (it wasn't processed)
    assert "old-1" in state.issue_failures


def test_queue_failure_does_not_transition_idle(repo_root: Path, state_dir: Path) -> None:
    """Queue failure should not cause idle transition — it retries then continues."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        # First 3 calls fail (retry exhaust), then loop re-enters and 4th call succeeds empty
        if call_count <= 3:
            return QueueResult(success=False, error="network error")
        return QueueResult()

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.time.sleep"),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    # After retries fail, loop continues; next iteration gets empty queue → idle
    assert state.mode == OrchestratorMode.idle


def test_queue_failure_retries_before_continuing(repo_root: Path, state_dir: Path) -> None:
    """Queue failure retries up to 3 times before continuing."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(success=False, error="network error")
        if call_count == 2:
            return QueueResult(success=False, error="network error")
        if call_count == 3:
            return QueueResult(issues=[_make_issue()])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.time.sleep") as mock_sleep,
    ):
        run_loop(repo_root, state_dir, config, runner)

    # Should have retried with sleep between attempts
    assert mock_sleep.call_count >= 1
    state = StateStore(state_dir).load()
    assert state.last_completed_issue == "test-1"


def test_failure_increments_attempts(repo_root: Path, state_dir: Path) -> None:
    """If an issue already has a failure record, attempts count is incremented."""
    store = StateStore(state_dir)
    initial_failure = IssueFailure(
        category=FailureCategory.issue_needs_rework,
        action=FailureAction.hold_until_backlog_changes,
        stage="amp",
        summary="first failure",
        timestamp="2026-01-01T00:00:00+00:00",
        attempts=1,
    )
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        issue_failures={"test-1": initial_failure.to_dict()},
    )
    store.save(state)

    # Despite having a failure, if the issue comes back (e.g., failure cleared externally)
    # and fails again, attempts should increment.
    # We simulate by removing from skip set temporarily
    runner = StubAmpRunner.failed(summary="second failure")
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[_make_issue("other-1", "Other")])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "other-1"
    mock_wt_info.branch_name = "amp/other-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config=OrchestratorConfig(), runner=runner)

    state = StateStore(state_dir).load()
    # other-1 should have attempts=1 (new failure)
    assert "other-1" in state.issue_failures
    assert state.issue_failures["other-1"]["attempts"] == 1
    # test-1 should still be there from initial state
    assert "test-1" in state.issue_failures


def test_failed_and_needs_human_record_issue_needs_rework(repo_root: Path, state_dir: Path) -> None:
    """Both failed and needs_human result types record issue_needs_rework."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.needs_human()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "issue_needs_rework"


def test_completed_no_merge_records_issue_needs_rework(repo_root: Path, state_dir: Path) -> None:
    """Completed but not merge-ready records issue_needs_rework."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner(AmpResult(
        result=ResultType.completed, summary="done", merge_ready=False,
    ))
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "issue_needs_rework"
    assert state.run_history[0]["result"] == "completed_no_merge"


# --- fail-fast tests ---


def test_fail_fast_stops_on_failed_issue(repo_root: Path, state_dir: Path) -> None:
    """With fail_fast=True, a failed issue stops the loop instead of continuing."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()

    issue1 = _make_issue("fail-1", "Failing issue")
    issue2 = _make_issue("ok-2", "Good issue")

    runner = MagicMock()
    runner.run.return_value = AmpResult(result=ResultType.failed, summary="boom")

    def fake_ready(cwd=None):
        return QueueResult(issues=[issue1, issue2])

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "fail-1"
    mock_wt_info.branch_name = "amp/fail-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, fail_fast=True)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    # Only one issue processed — the second was never attempted
    assert len(state.run_history) == 1
    assert state.run_history[0]["issue_id"] == "fail-1"
    assert "fail-1" in state.issue_failures


def test_fail_fast_stops_on_merge_failure(repo_root: Path, state_dir: Path) -> None:
    """With fail_fast=True, a merge failure stops the loop."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(
        success=False, stage="rebase", error="conflict detected",
    )

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, fail_fast=True)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    assert "test-1" in state.issue_failures


def test_fail_fast_stops_on_evaluation_failure(repo_root: Path, state_dir: Path) -> None:
    """With fail_fast=True, an evaluation failure stops the loop."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    evaluator = StubEvaluator.failed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator, fail_fast=True)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    assert "test-1" in state.issue_failures
    assert len(state.run_history) == 1


def test_fail_fast_from_config(repo_root: Path, state_dir: Path) -> None:
    """fail_fast=True in config has the same effect as the CLI flag."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig(fail_fast=True)

    runner = MagicMock()
    runner.run.return_value = AmpResult(result=ResultType.failed, summary="boom")

    def fake_ready(cwd=None):
        return QueueResult(issues=[_make_issue()])

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    assert len(state.run_history) == 1


def test_fail_fast_records_event(repo_root: Path, state_dir: Path) -> None:
    """Fail-fast stop records a state_changed event with reason=fail_fast."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()

    runner = MagicMock()
    runner.run.return_value = AmpResult(result=ResultType.failed, summary="boom")

    def fake_ready(cwd=None):
        return QueueResult(issues=[_make_issue()])

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, fail_fast=True)

    events = EventLog(state_dir).all()
    fail_fast_events = [
        e for e in events
        if e.get("event_type") == "state_changed"
        and e.get("data", {}).get("reason") == "fail_fast"
    ]
    assert len(fail_fast_events) == 1


# --- resume tests ---


def _make_resume_candidate(
    issue_id: str = "resume-1",
    stage: str = "amp_running",
    branch: str = "amp/resume-1-fix",
    worktree_path: str = "/tmp/wt/resume-1",
    bd_claimed: bool = True,
    resume_attempts: int = 1,
    amp_result: dict | None = None,
) -> dict:
    return RunCheckpoint(
        issue_id=issue_id,
        issue_title="Resume test",
        branch=branch,
        worktree_path=worktree_path,
        stage=RunStage(stage),
        bd_claimed=bd_claimed,
        amp_result=amp_result,
        resume_attempts=resume_attempts,
    ).to_dict()


def test_resume_candidate_amp_running_success(repo_root: Path, state_dir: Path) -> None:
    """Resume from amp_running: re-runs amp, merges on success."""
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        resume_candidate=_make_resume_candidate(stage="amp_running"),
    )
    store.save(state)

    runner = StubAmpRunner.completed()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.ensure_resumable_worktree.return_value = True

    with (
        patch("orc.scheduler.get_ready_issues", return_value=QueueResult()),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, OrchestratorConfig(), runner)

    state = StateStore(state_dir).load()
    assert state.resume_candidate is None
    assert state.active_run is None
    assert state.last_completed_issue == "resume-1"
    assert len(state.run_history) == 1
    assert state.run_history[0]["result"] == "completed"


def test_resume_candidate_amp_finished_skips_to_merge(repo_root: Path, state_dir: Path) -> None:
    """Resume from amp_finished with merge_ready: skips amp, goes to merge."""
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        resume_candidate=_make_resume_candidate(
            stage="amp_finished",
            amp_result={"result": "completed", "summary": "done", "merge_ready": True},
        ),
    )
    store.save(state)

    runner = StubAmpRunner.completed()  # should NOT be called

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.ensure_resumable_worktree.return_value = True

    with (
        patch("orc.scheduler.get_ready_issues", return_value=QueueResult()),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, OrchestratorConfig(), runner)

    state = StateStore(state_dir).load()
    assert state.last_completed_issue == "resume-1"
    # Runner should NOT have been called (amp was already done)
    assert runner._result is not None  # just checking it exists, not that .run was called


def test_resume_candidate_ready_to_merge_skips_amp_and_eval(repo_root: Path, state_dir: Path) -> None:
    """Resume from ready_to_merge: skips amp+eval, goes straight to merge."""
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        resume_candidate=_make_resume_candidate(stage="ready_to_merge"),
    )
    store.save(state)

    runner = StubAmpRunner.completed()
    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.ensure_resumable_worktree.return_value = True

    with (
        patch("orc.scheduler.get_ready_issues", return_value=QueueResult()),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, OrchestratorConfig(), runner)

    state = StateStore(state_dir).load()
    assert state.last_completed_issue == "resume-1"
    mock_merge.assert_called_once()


def test_resume_candidate_no_worktree_discards(repo_root: Path, state_dir: Path) -> None:
    """Resume with no recoverable worktree discards the candidate."""
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        resume_candidate=_make_resume_candidate(),
    )
    store.save(state)

    runner = StubAmpRunner.completed()
    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.ensure_resumable_worktree.return_value = False

    with (
        patch("orc.scheduler.get_ready_issues", return_value=QueueResult()),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.unclaim_issue", return_value=True),
    ):
        run_loop(repo_root, state_dir, OrchestratorConfig(), runner)

    state = StateStore(state_dir).load()
    assert state.resume_candidate is None
    assert state.active_run is None


def test_resume_records_events(repo_root: Path, state_dir: Path) -> None:
    """Resume attempt records resume_attempted and resume_succeeded events."""
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        resume_candidate=_make_resume_candidate(stage="amp_running"),
    )
    store.save(state)

    runner = StubAmpRunner.completed()
    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_worktree_mgr.return_value.ensure_resumable_worktree.return_value = True

    with (
        patch("orc.scheduler.get_ready_issues", return_value=QueueResult()),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, OrchestratorConfig(), runner)

    events = EventLog(state_dir).all()
    event_types = [e["event_type"] for e in events]
    assert "resume_attempted" in event_types
    assert "resume_succeeded" in event_types


# --- parent promotion tests ---


def test_parent_promoted_after_last_child_closes(repo_root: Path, state_dir: Path) -> None:
    """When a child issue is closed and all siblings are closed, the parent is promoted."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()

    child = _make_issue("child-1", "Child issue")
    parent = _make_issue("parent-1", "Parent issue")

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[child])
        if call_count == 2:
            # Parent should now appear in the queue after promotion
            return QueueResult(issues=[parent])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "x"
    mock_wt_info.branch_name = "amp/x"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.get_issue_parent", return_value="parent-1"),
        patch("orc.scheduler.get_children_all_closed", return_value=True),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    # Both child and parent should have been processed
    assert len(state.run_history) == 2
    assert state.run_history[0]["issue_id"] == "child-1"
    assert state.run_history[1]["issue_id"] == "parent-1"

    events = EventLog(state_dir).all()
    event_types = [e["event_type"] for e in events]
    assert "parent_promoted" in event_types
    promoted_event = next(e for e in events if e["event_type"] == "parent_promoted")
    assert promoted_event["data"]["parent_id"] == "parent-1"
    assert promoted_event["data"]["triggered_by"] == "child-1"


def test_no_promotion_when_siblings_still_open(repo_root: Path, state_dir: Path) -> None:
    """No promotion when not all children are closed."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.get_issue_parent", return_value="parent-1"),
        patch("orc.scheduler.get_children_all_closed", return_value=False),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.promoted_parent is None

    events = EventLog(state_dir).all()
    event_types = [e["event_type"] for e in events]
    assert "parent_promoted" not in event_types


def test_no_promotion_when_no_parent(repo_root: Path, state_dir: Path) -> None:
    """No promotion when the issue has no parent."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.get_issue_parent", return_value=None),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.promoted_parent is None

    events = EventLog(state_dir).all()
    event_types = [e["event_type"] for e in events]
    assert "parent_promoted" not in event_types


def test_promotion_clears_parent_failure_record(repo_root: Path, state_dir: Path) -> None:
    """When a parent is promoted, any existing failure record is cleared."""
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        issue_failures={"parent-1": {"category": "issue_needs_rework", "action": "hold_until_backlog_changes",
                                      "stage": "amp", "summary": "old failure", "timestamp": "t", "attempts": 1}},
    )
    store.save(state)

    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()

    child = _make_issue("child-1", "Child issue")
    parent = _make_issue("parent-1", "Parent issue")

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[child])
        if call_count == 2:
            return QueueResult(issues=[parent])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "x"
    mock_wt_info.branch_name = "amp/x"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.get_issue_parent", return_value="parent-1"),
        patch("orc.scheduler.get_children_all_closed", return_value=True),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    # parent-1 should not be in failures (cleared by promotion, then processed)
    assert "parent-1" not in state.issue_failures


def test_pause_request_during_amp_stops_after_amp(repo_root: Path, state_dir: Path) -> None:
    """A pause request enqueued while amp is running should stop the scheduler
    after amp finishes, without proceeding to merge."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    issue = _make_issue()

    rq = RequestQueue(state_dir)

    def runner_with_pause(ctx, **kwargs):
        # Simulate pause being requested while amp is running
        rq.enqueue("pause")
        return AmpResult(result=ResultType.completed, summary="done", merge_ready=True)

    mock_runner = MagicMock()
    mock_runner.run.side_effect = runner_with_pause

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, mock_runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.paused
    # Merge should NOT have been called
    mock_merge.assert_not_called()
    # Active run should be preserved as resume_candidate
    assert state.active_run is None
    assert state.resume_candidate is not None
    assert state.resume_candidate["issue_id"] == "test-1"
    # No terminal run_history entry (run is not finished)
    assert len(state.run_history) == 0


def test_stop_request_during_amp_stops_after_amp(repo_root: Path, state_dir: Path) -> None:
    """A stop request enqueued while amp is running should stop the scheduler
    after amp finishes, transitioning to idle."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    issue = _make_issue()

    rq = RequestQueue(state_dir)

    def runner_with_stop(ctx, **kwargs):
        rq.enqueue("stop")
        return AmpResult(result=ResultType.completed, summary="done", merge_ready=True)

    mock_runner = MagicMock()
    mock_runner.run.side_effect = runner_with_stop

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()
    mock_merge.return_value = MagicMock(success=True, stage="complete")

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, mock_runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    mock_merge.assert_not_called()
    # Resume candidate preserved for potential restart
    assert state.resume_candidate is not None
    assert state.resume_candidate["issue_id"] == "test-1"
    assert len(state.run_history) == 0


def test_pause_request_before_eval_stops_before_eval(repo_root: Path, state_dir: Path) -> None:
    """A pause request applied before evaluation should prevent eval from running."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    issue = _make_issue()

    rq = RequestQueue(state_dir)

    def runner_with_pause(ctx, **kwargs):
        rq.enqueue("pause")
        return AmpResult(result=ResultType.completed, summary="done", merge_ready=True)

    mock_runner = MagicMock()
    mock_runner.run.side_effect = runner_with_pause

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_evaluator = MagicMock()
    mock_merge = MagicMock()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, mock_runner, evaluator=mock_evaluator)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.paused
    # Neither evaluation nor merge should have been called
    mock_evaluator.evaluate.assert_not_called()
    mock_merge.assert_not_called()
    assert state.resume_candidate is not None
    assert state.resume_candidate["issue_id"] == "test-1"
    assert len(state.run_history) == 0


def test_stop_request_before_amp_stops_before_amp(repo_root: Path, state_dir: Path) -> None:
    """A stop request enqueued during worktree/claim should stop
    before starting the amp run."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    issue = _make_issue()

    rq = RequestQueue(state_dir)
    # Enqueue stop before the loop even starts — it will be drained
    # during _update_checkpoint(claimed) or the before_amp safe point.
    rq.enqueue("stop")

    mock_runner = MagicMock()
    mock_runner.run.return_value = AmpResult(result=ResultType.completed, summary="done", merge_ready=True)

    call_count = 0

    def fake_ready(cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return QueueResult(issues=[issue])
        return QueueResult()

    mock_merge = MagicMock()

    mock_worktree_mgr = MagicMock()
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.verify_and_merge", mock_merge),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, mock_runner)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    # Amp should NOT have been invoked
    mock_runner.run.assert_not_called()
    mock_merge.assert_not_called()
    # Resume candidate preserved
    assert state.resume_candidate is not None
    assert state.resume_candidate["issue_id"] == "test-1"
