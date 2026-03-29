"""Tests for the Amp worker adapter module."""

from __future__ import annotations

from pathlib import Path

from amp_orchestrator.amp_runner import (
    AmpResult,
    AmpRunner,
    IssueContext,
    ResultType,
    StubAmpRunner,
)


def _make_context() -> IssueContext:
    return IssueContext(
        issue_id="TEST-1",
        title="Test issue",
        description="A test description",
        acceptance_criteria="It works",
        worktree_path=Path("/tmp/worktree"),
        repo_root=Path("/tmp/repo"),
    )


# --- ResultType enum ---


def test_result_type_values() -> None:
    assert set(ResultType) == {
        ResultType.completed,
        ResultType.decomposed,
        ResultType.blocked,
        ResultType.failed,
        ResultType.needs_human,
    }


def test_result_type_is_str() -> None:
    assert isinstance(ResultType.completed, str)
    assert ResultType.completed == "completed"


# --- AmpResult dataclass ---


def test_amp_result_defaults() -> None:
    r = AmpResult(result=ResultType.completed, summary="done")
    assert r.changed_paths == []
    assert r.tests_run == []
    assert r.followup_bd_issues == []
    assert r.blockers == []
    assert r.merge_ready is False


def test_amp_result_full_fields() -> None:
    r = AmpResult(
        result=ResultType.blocked,
        summary="waiting",
        changed_paths=["a.py"],
        tests_run=["test_a"],
        followup_bd_issues=["ISSUE-2"],
        blockers=["ISSUE-3"],
        merge_ready=False,
    )
    assert r.result is ResultType.blocked
    assert r.changed_paths == ["a.py"]
    assert r.blockers == ["ISSUE-3"]


# --- IssueContext dataclass ---


def test_issue_context_fields() -> None:
    ctx = _make_context()
    assert ctx.issue_id == "TEST-1"
    assert ctx.worktree_path == Path("/tmp/worktree")


# --- Default StubAmpRunner ---


def test_default_stub_returns_completed() -> None:
    stub = StubAmpRunner()
    result = stub.run(_make_context())
    assert result.result is ResultType.completed
    assert result.merge_ready is True
    assert result.summary


# --- StubAmpRunner satisfies AmpRunner protocol ---


def test_stub_satisfies_protocol() -> None:
    runner: AmpRunner = StubAmpRunner()
    result = runner.run(_make_context())
    assert result.result is ResultType.completed


# --- Factory class methods ---


def test_factory_completed() -> None:
    stub = StubAmpRunner.completed(summary="all good")
    result = stub.run(_make_context())
    assert result.result is ResultType.completed
    assert result.summary == "all good"
    assert result.merge_ready is True


def test_factory_decomposed() -> None:
    stub = StubAmpRunner.decomposed(summary="split up")
    result = stub.run(_make_context())
    assert result.result is ResultType.decomposed
    assert result.summary == "split up"
    assert result.merge_ready is False


def test_factory_failed() -> None:
    stub = StubAmpRunner.failed(summary="boom")
    result = stub.run(_make_context())
    assert result.result is ResultType.failed
    assert result.summary == "boom"


def test_factory_blocked() -> None:
    stub = StubAmpRunner.blocked(summary="stuck", blockers=["DEP-1", "DEP-2"])
    result = stub.run(_make_context())
    assert result.result is ResultType.blocked
    assert result.blockers == ["DEP-1", "DEP-2"]


def test_factory_blocked_default_blockers() -> None:
    stub = StubAmpRunner.blocked()
    result = stub.run(_make_context())
    assert result.blockers == []


def test_factory_needs_human() -> None:
    stub = StubAmpRunner.needs_human(summary="help me")
    result = stub.run(_make_context())
    assert result.result is ResultType.needs_human
    assert result.summary == "help me"


# --- Context passthrough ---


def test_context_passed_to_run() -> None:
    """Stub returns its result regardless of context content."""
    ctx1 = _make_context()
    ctx2 = IssueContext(
        issue_id="OTHER-99",
        title="Other",
        description="",
        acceptance_criteria="",
        worktree_path=Path("/other"),
        repo_root=Path("/other"),
    )
    stub = StubAmpRunner.completed(summary="ok")
    assert stub.run(ctx1).summary == "ok"
    assert stub.run(ctx2).summary == "ok"
