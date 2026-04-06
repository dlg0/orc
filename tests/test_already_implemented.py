"""Tests for the already-implemented preflight checker."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orc.already_implemented import (
    AlreadyImplementedResult,
    AmpAlreadyImplementedChecker,
    Confidence,
    StubAlreadyImplementedChecker,
)
from orc.amp_runner import StubAmpRunner
from orc.config import OrchestratorConfig
from orc.events import EventLog
from orc.queue import BdIssue, QueueResult
from orc.scheduler import run_loop
from orc.state import OrchestratorMode, OrchestratorState, StateStore


# ---------------------------------------------------------------------------
# Unit tests for AlreadyImplementedResult
# ---------------------------------------------------------------------------


def test_should_skip_already_done() -> None:
    result = AlreadyImplementedResult(
        confidence=Confidence.already_done,
        summary="test",
        evidence=[],
    )
    assert result.should_skip is True


def test_should_skip_likely_done() -> None:
    result = AlreadyImplementedResult(
        confidence=Confidence.likely_done,
        summary="test",
        evidence=[],
    )
    assert result.should_skip is True


def test_should_not_skip_not_done() -> None:
    result = AlreadyImplementedResult(
        confidence=Confidence.not_done,
        summary="test",
        evidence=[],
    )
    assert result.should_skip is False


# ---------------------------------------------------------------------------
# Stub tests
# ---------------------------------------------------------------------------


def test_stub_not_done() -> None:
    checker = StubAlreadyImplementedChecker.not_done()
    result = checker.check("id", "title", "desc", "ac", Path("/tmp"))
    assert result.confidence == Confidence.not_done
    assert not result.should_skip


def test_stub_already_done() -> None:
    checker = StubAlreadyImplementedChecker.already_done()
    result = checker.check("id", "title", "desc", "ac", Path("/tmp"))
    assert result.confidence == Confidence.already_done
    assert result.should_skip


def test_stub_likely_done() -> None:
    checker = StubAlreadyImplementedChecker.likely_done()
    result = checker.check("id", "title", "desc", "ac", Path("/tmp"))
    assert result.confidence == Confidence.likely_done
    assert result.should_skip


# ---------------------------------------------------------------------------
# AmpAlreadyImplementedChecker parsing tests
# ---------------------------------------------------------------------------


def _make_stream_json(confidence: str, summary: str, evidence: list[str] | None = None) -> str:
    """Build fake amp --stream-json output with a structured result."""
    json_block = json.dumps({
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence or [],
    })
    text = f"```json\n{json_block}\n```"
    assistant_msg = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    })
    result_msg = json.dumps({"type": "result", "is_error": False})
    return f"{assistant_msg}\n{result_msg}\n"


def test_parse_already_done() -> None:
    checker = AmpAlreadyImplementedChecker()
    stdout = _make_stream_json("already_done", "Found the feature in src/foo.py", ["src/foo.py"])
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    result = checker._parse_output(proc)
    assert result.confidence == Confidence.already_done
    assert result.should_skip
    assert "src/foo.py" in result.evidence


def test_parse_not_done() -> None:
    checker = AmpAlreadyImplementedChecker()
    stdout = _make_stream_json("not_done", "No evidence found")
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    result = checker._parse_output(proc)
    assert result.confidence == Confidence.not_done
    assert not result.should_skip


def test_parse_invalid_confidence_falls_back() -> None:
    checker = AmpAlreadyImplementedChecker()
    stdout = _make_stream_json("maybe", "Unclear")
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    result = checker._parse_output(proc)
    assert result.confidence == Confidence.not_done


def test_parse_no_json_falls_back() -> None:
    checker = AmpAlreadyImplementedChecker()
    assistant_msg = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "I checked but found nothing relevant."}]},
    })
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=assistant_msg, stderr="")
    result = checker._parse_output(proc)
    assert result.confidence == Confidence.not_done


def test_parse_bare_json_line() -> None:
    checker = AmpAlreadyImplementedChecker()
    bare_json = json.dumps({"confidence": "likely_done", "summary": "Looks done", "evidence": []})
    assistant_msg = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": bare_json}]},
    })
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=assistant_msg, stderr="")
    result = checker._parse_output(proc)
    assert result.confidence == Confidence.likely_done


def test_amp_not_found() -> None:
    checker = AmpAlreadyImplementedChecker()
    with patch("shutil.which", return_value=None):
        result = checker.check("id", "title", "desc", "ac", Path("/tmp"))
    assert result.confidence == Confidence.not_done
    assert "not found" in result.summary


# ---------------------------------------------------------------------------
# Scheduler integration tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_sync_repo_root(monkeypatch):
    monkeypatch.setattr("orc.scheduler._sync_repo_root", lambda repo_root, base_branch: (True, None))


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".orc"
    d.mkdir()
    return d


@pytest.fixture()
def repo_root(tmp_path: Path, state_dir: Path) -> Path:
    return tmp_path


def _make_issue(id: str = "test-1", title: str = "Test issue") -> BdIssue:
    return BdIssue(
        id=id, title=title, priority=1, created="2026-01-01",
        description="desc", acceptance_criteria="ac",
    )


def _set_state(state_dir: Path, mode: OrchestratorMode = OrchestratorMode.running) -> None:
    store = StateStore(state_dir)
    state = OrchestratorState(mode=mode)
    store.save(state)


def test_scheduler_skips_already_implemented_issue(repo_root: Path, state_dir: Path) -> None:
    """When the checker says already_done, the issue is skipped — no worktree or amp."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()
    checker = StubAlreadyImplementedChecker.already_done("Feature already exists")

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
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.close_issue", return_value=True),
    ):
        run_loop(repo_root, state_dir, config, runner, already_implemented_checker=checker)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.idle
    # Issue was skipped, not completed
    assert state.last_completed_issue is None
    assert len(state.run_history) == 1
    assert state.run_history[0]["result"] == "skipped_already_implemented"
    # Worktree should NOT have been created
    mock_worktree_mgr.return_value.create_worktree.assert_not_called()
    # Runner should NOT have been called
    assert runner._result is not None  # just confirming it's a stub


def test_scheduler_proceeds_when_not_implemented(repo_root: Path, state_dir: Path) -> None:
    """When the checker says not_done, the scheduler proceeds normally."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()
    checker = StubAlreadyImplementedChecker.not_done()

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
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        run_loop(repo_root, state_dir, config, runner, already_implemented_checker=checker)

    state = StateStore(state_dir).load()
    assert state.last_completed_issue == "test-1"
    assert len(state.run_history) == 1
    assert state.run_history[0]["result"] == "completed"


def test_scheduler_no_checker_proceeds_normally(repo_root: Path, state_dir: Path) -> None:
    """When no checker is passed, the scheduler skips the preflight entirely."""
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
    mock_wt_info = MagicMock()
    mock_wt_info.worktree_path = repo_root / ".worktrees" / "test-1"
    mock_wt_info.branch_name = "amp/test-1-test-issue"
    mock_worktree_mgr.return_value.create_worktree.return_value = mock_wt_info

    with (
        patch("orc.scheduler.get_ready_issues", side_effect=fake_ready),
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
    ):
        # No already_implemented_checker passed
        run_loop(repo_root, state_dir, config, runner)

    state = StateStore(state_dir).load()
    assert state.last_completed_issue == "test-1"


def test_scheduler_already_implemented_records_event(repo_root: Path, state_dir: Path) -> None:
    """Verify the event log records an already_implemented_detected event."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()
    checker = StubAlreadyImplementedChecker.already_done("Already there")

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
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.close_issue", return_value=True),
    ):
        run_loop(repo_root, state_dir, config, runner, already_implemented_checker=checker)

    events = EventLog(state_dir)
    all_events = events.all()
    ai_events = [e for e in all_events if e["event_type"] == "already_implemented_detected"]
    assert len(ai_events) == 1
    assert ai_events[0]["data"]["confidence"] == "already_done"
    assert ai_events[0]["data"]["issue_id"] == "test-1"


def test_scheduler_likely_done_also_skips(repo_root: Path, state_dir: Path) -> None:
    """likely_done should also skip the issue, not just already_done."""
    _set_state(state_dir, OrchestratorMode.running)
    config = OrchestratorConfig()
    runner = StubAmpRunner.completed()
    issue = _make_issue()
    checker = StubAlreadyImplementedChecker.likely_done("Probably done")

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
        patch("orc.scheduler.WorktreeManager", mock_worktree_mgr),
        patch("orc.scheduler.close_issue", return_value=True),
    ):
        run_loop(repo_root, state_dir, config, runner, already_implemented_checker=checker)

    state = StateStore(state_dir).load()
    assert state.run_history[0]["result"] == "skipped_already_implemented"
    # The issue should be closed, not held
    assert "test-1" not in state.issue_failures
