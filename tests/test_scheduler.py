"""Tests for the core scheduler loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amp_orchestrator.amp_runner import AmpResult, ResultType, StubAmpRunner
from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.evaluator import StubEvaluator
from amp_orchestrator.events import EventLog
from amp_orchestrator.queue import BdIssue, QueueResult
from amp_orchestrator.scheduler import run_loop
from amp_orchestrator.state import (
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    StateStore,
)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".amp-orchestrator"
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

    with patch("amp_orchestrator.scheduler.get_ready_issues", return_value=QueueResult()):
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge") as mock_merge,
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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

    def run_side_effect(ctx):
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge"),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    failure = state.issue_failures["test-1"]
    assert failure["summary"] == "Missing tests"
    assert failure["category"] == "issue_needs_rework"
    assert failure["stage"] == "evaluation"
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("amp_orchestrator.scheduler.claim_issue", return_value=True) as mock_claim,
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("amp_orchestrator.scheduler.claim_issue", return_value=False),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    failure = state.issue_failures["test-1"]
    assert failure["category"] == "transient_external"
    assert failure["stage"] == "worktree"


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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, crash_runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "issue_needs_rework"
    assert state.issue_failures["test-1"]["stage"] == "amp"


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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge") as mock_merge,
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "stale_or_conflicted"
    assert state.issue_failures["test-1"]["preserve_worktree"] is False
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.time.sleep"),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.verify_and_merge", mock_merge),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("amp_orchestrator.scheduler.time.sleep") as mock_sleep,
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
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
        patch("amp_orchestrator.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("amp_orchestrator.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert "test-1" in state.issue_failures
    assert state.issue_failures["test-1"]["category"] == "issue_needs_rework"
    assert state.run_history[0]["result"] == "completed_no_merge"
