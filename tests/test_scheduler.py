"""Tests for the core scheduler loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amp_orchestrator.amp_runner import AmpResult, ResultType, StubAmpRunner
from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.evaluator import StubEvaluator
from amp_orchestrator.events import EventLog
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.scheduler import run_loop
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore


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

    with patch("amp_orchestrator.scheduler.get_ready_issues", return_value=[]):
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
            return [issue]
        return []

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
            return [issue]
        return []

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
            return [issue1, issue2]
        if ready_call == 2:
            return [issue2]  # fail-1 will be in skip_ids
        return []

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
            return [issue]
        return []

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
            return [issue]
        return []

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
            return [issue]
        return []

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
            return [issue]
        return []

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
            return [issue]
        return []

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
            return [issue]
        return []

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
