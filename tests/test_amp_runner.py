"""Tests for the Amp worker adapter module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from orc.amp_runner import (
    AmpResult,
    AmpRunner,
    IssueContext,
    RealAmpRunner,
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


# --- AmpResult context_window_usage_pct default ---


def test_amp_result_context_window_usage_pct_default_none() -> None:
    r = AmpResult(result=ResultType.completed, summary="done")
    assert r.context_window_usage_pct is None


# --- RealAmpRunner._parse_context_usage ---


def test_parse_context_usage_percentage_pattern() -> None:
    assert RealAmpRunner._parse_context_usage("Context window usage: 73%") == 73.0


def test_parse_context_usage_percentage_with_decimal() -> None:
    assert RealAmpRunner._parse_context_usage("Context usage: 85.5%") == 85.5


def test_parse_context_usage_token_fraction() -> None:
    assert RealAmpRunner._parse_context_usage("tokens used: 150000/200000") == 75.0


def test_parse_context_usage_no_match() -> None:
    assert RealAmpRunner._parse_context_usage("no usage info here") is None


def test_parse_context_usage_lowercase_context() -> None:
    assert RealAmpRunner._parse_context_usage("context usage: 42%") == 42.0


def test_json_to_result_includes_context_window_usage_pct() -> None:
    data = {
        "result": "completed",
        "summary": "done",
        "context_window_usage_pct": 91.2,
    }
    result = RealAmpRunner._json_to_result(data)
    assert result.context_window_usage_pct == 91.2


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


# --- RealAmpRunner passes worktree env ---


@patch("orc.amp_runner.shutil.which", return_value="/usr/bin/amp")
@patch("orc.amp_runner.subprocess.run")
@patch("orc.amp_runner.build_worktree_env")
def test_real_runner_passes_worktree_env(mock_env, mock_run, mock_which) -> None:
    """RealAmpRunner.run() passes env=build_worktree_env() to subprocess."""
    import subprocess as _sp

    fake_env = {"PYTHONPATH": "/tmp/worktree/src", "PATH": "/usr/bin"}
    mock_env.return_value = fake_env
    mock_run.return_value = _sp.CompletedProcess(
        args=["amp"], returncode=0, stdout="", stderr="",
    )

    runner = RealAmpRunner()
    ctx = _make_context()
    runner.run(ctx)

    mock_env.assert_called_once_with(ctx.worktree_path)
    # The first subprocess.run call is the amp invocation
    amp_call = mock_run.call_args_list[0]
    assert amp_call[1]["env"] is fake_env


# --- AmpResult thread_id ---


def test_amp_result_thread_id_default_none() -> None:
    r = AmpResult(result=ResultType.completed, summary="done")
    assert r.thread_id is None


def test_amp_result_thread_id_set() -> None:
    r = AmpResult(result=ResultType.completed, summary="done", thread_id="abc-123")
    assert r.thread_id == "abc-123"


# --- Stream JSON thread_id extraction ---


def test_parse_stream_json_captures_thread_id() -> None:
    import subprocess as _sp

    stream_output = (
        '{"type":"session_start","thread_id":"T-aaa-bbb-ccc"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"```json\\n{\\"result\\": \\"completed\\", \\"summary\\": \\"done\\", \\"merge_ready\\": false}\\n```"}]}}\n'
        '{"type":"result","is_error":false,"usage":{"input_tokens":100,"max_tokens":1000}}\n'
    )
    proc = _sp.CompletedProcess(args=["amp"], returncode=0, stdout=stream_output, stderr="")
    runner = RealAmpRunner()
    result = runner._parse_stream_json(proc, Path("/tmp/worktree"))
    assert result.thread_id == "T-aaa-bbb-ccc"


def test_parse_stream_json_no_thread_id() -> None:
    import subprocess as _sp

    stream_output = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"```json\\n{\\"result\\": \\"completed\\", \\"summary\\": \\"done\\", \\"merge_ready\\": false}\\n```"}]}}\n'
        '{"type":"result","is_error":false}\n'
    )
    proc = _sp.CompletedProcess(args=["amp"], returncode=0, stdout=stream_output, stderr="")
    runner = RealAmpRunner()
    result = runner._parse_stream_json(proc, Path("/tmp/worktree"))
    assert result.thread_id is None


def test_parse_stream_json_thread_id_on_error() -> None:
    import subprocess as _sp

    stream_output = '{"type":"session_start","thread_id":"T-err-111"}\n{"type":"result","is_error":true,"error":"oops"}\n'
    proc = _sp.CompletedProcess(args=["amp"], returncode=0, stdout=stream_output, stderr="")
    runner = RealAmpRunner()
    result = runner._parse_stream_json(proc, Path("/tmp/worktree"))
    assert result.thread_id == "T-err-111"
    assert result.result == ResultType.failed


# --- Rush summary extraction ---


@patch("orc.amp_runner.shutil.which", return_value="/usr/bin/amp")
@patch("orc.amp_runner.subprocess.run")
def test_extract_rush_summary_success(mock_run, mock_which) -> None:
    import subprocess as _sp

    mock_run.return_value = _sp.CompletedProcess(
        args=["amp"], returncode=0,
        stdout="Implemented authentication module with JWT tokens.\n",
        stderr="",
    )
    result = RealAmpRunner.extract_rush_summary("aaa-bbb", Path("/tmp"))
    assert result == "Implemented authentication module with JWT tokens."
    # Verify --archive flag is passed
    cmd = mock_run.call_args[0][0]
    assert "--archive" in cmd
    assert "--mode" in cmd


@patch("orc.amp_runner.shutil.which", return_value="/usr/bin/amp")
@patch("orc.amp_runner.subprocess.run")
def test_extract_rush_summary_failure(mock_run, mock_which) -> None:
    import subprocess as _sp

    mock_run.return_value = _sp.CompletedProcess(
        args=["amp"], returncode=1, stdout="", stderr="error",
    )
    result = RealAmpRunner.extract_rush_summary("aaa-bbb", Path("/tmp"))
    assert result is None


@patch("orc.amp_runner.shutil.which", return_value=None)
def test_extract_rush_summary_no_amp(mock_which) -> None:
    result = RealAmpRunner.extract_rush_summary("aaa-bbb", Path("/tmp"))
    assert result is None


@patch("orc.amp_runner.shutil.which", return_value="/usr/bin/amp")
@patch("orc.amp_runner.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="amp", timeout=120))
def test_extract_rush_summary_timeout(mock_run, mock_which) -> None:
    result = RealAmpRunner.extract_rush_summary("aaa-bbb", Path("/tmp"))
    assert result is None
