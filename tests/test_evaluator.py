"""Tests for the independent completion evaluator module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from amp_orchestrator.amp_runner import IssueContext
from unittest.mock import patch

from amp_orchestrator.evaluator import (
    AmpEvaluatorRunner,
    EvaluationResult,
    EvaluationVerdict,
    StubEvaluator,
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


# --- EvaluationResult basics ---


def test_fail_creates_failed_result() -> None:
    result = EvaluationResult.fail("something broke")
    assert result.verdict is EvaluationVerdict.failed
    assert result.summary == "something broke"


def test_passed_property_true_for_passed_verdict() -> None:
    result = EvaluationResult(verdict=EvaluationVerdict.passed, summary="ok")
    assert result.passed is True


def test_passed_property_false_for_failed_verdict() -> None:
    result = EvaluationResult.fail("nope")
    assert result.passed is False


def test_to_dict_round_trip() -> None:
    result = EvaluationResult(
        verdict=EvaluationVerdict.passed,
        summary="looks good",
        evidence=["file changed"],
        tests_run=["pytest"],
        gaps=["edge case"],
        task_too_large_signal=True,
    )
    d = result.to_dict()
    assert d == {
        "verdict": "pass",
        "summary": "looks good",
        "evidence": ["file changed"],
        "tests_run": ["pytest"],
        "gaps": ["edge case"],
        "task_too_large_signal": True,
        "context_window_usage_pct": None,
    }


def test_to_dict_includes_context_window_usage_pct() -> None:
    result = EvaluationResult(
        verdict=EvaluationVerdict.passed,
        summary="ok",
        context_window_usage_pct=88.3,
    )
    d = result.to_dict()
    assert d["context_window_usage_pct"] == 88.3


def test_context_window_usage_pct_default_none() -> None:
    result = EvaluationResult(verdict=EvaluationVerdict.passed, summary="ok")
    assert result.context_window_usage_pct is None


# --- StubEvaluator ---


def test_stub_default_returns_passed() -> None:
    stub = StubEvaluator()
    result = stub.evaluate(_make_context(), "main", [])
    assert result.passed is True


def test_stub_passed_classmethod() -> None:
    stub = StubEvaluator.passed(summary="all clear")
    result = stub.evaluate(_make_context(), "main", [])
    assert result.passed is True
    assert result.summary == "all clear"


def test_stub_failed_classmethod() -> None:
    stub = StubEvaluator.failed(summary="no good")
    result = stub.evaluate(_make_context(), "main", [])
    assert result.passed is False
    assert result.summary == "no good"


def test_stub_evaluate_returns_configured_result() -> None:
    custom = EvaluationResult(
        verdict=EvaluationVerdict.failed,
        summary="custom fail",
        gaps=["missing feature"],
    )
    stub = StubEvaluator(custom)
    result = stub.evaluate(_make_context(), "main", ["make test"])
    assert result is custom


# --- AmpEvaluatorRunner._build_prompt ---


def test_build_prompt_contains_issue_fields() -> None:
    ctx = _make_context()
    prompt = AmpEvaluatorRunner._build_prompt(ctx, "main", [])
    assert ctx.issue_id in prompt
    assert ctx.title in prompt
    assert ctx.description in prompt
    assert ctx.acceptance_criteria in prompt


def test_build_prompt_contains_base_branch() -> None:
    prompt = AmpEvaluatorRunner._build_prompt(_make_context(), "develop", [])
    assert "origin/develop" in prompt


def test_build_prompt_formats_verification_commands() -> None:
    cmds = ["pytest", "mypy src/"]
    prompt = AmpEvaluatorRunner._build_prompt(_make_context(), "main", cmds)
    assert "- pytest" in prompt
    assert "- mypy src/" in prompt


def test_build_prompt_no_verification_commands() -> None:
    prompt = AmpEvaluatorRunner._build_prompt(_make_context(), "main", [])
    assert "(none configured)" in prompt


# --- AmpEvaluatorRunner._extract_json_block ---


def test_extract_json_block_valid() -> None:
    text = 'Some text\n```json\n{"verdict": "pass", "summary": "ok"}\n```\n'
    result = AmpEvaluatorRunner._extract_json_block(text)
    assert result is not None
    assert result["verdict"] == "pass"


def test_extract_json_block_prefers_last() -> None:
    text = (
        '```json\n{"verdict": "fail", "summary": "first"}\n```\n'
        'middle\n'
        '```json\n{"verdict": "pass", "summary": "second"}\n```\n'
    )
    result = AmpEvaluatorRunner._extract_json_block(text)
    assert result is not None
    assert result["summary"] == "second"


def test_extract_json_block_no_blocks() -> None:
    assert AmpEvaluatorRunner._extract_json_block("no json here") is None


def test_extract_json_block_missing_verdict_key() -> None:
    text = '```json\n{"summary": "no verdict"}\n```\n'
    assert AmpEvaluatorRunner._extract_json_block(text) is None


# --- AmpEvaluatorRunner._json_to_result ---


def test_json_to_result_pass_verdict() -> None:
    result = AmpEvaluatorRunner._json_to_result({"verdict": "pass", "summary": "ok"})
    assert result.verdict is EvaluationVerdict.passed
    assert result.summary == "ok"


def test_json_to_result_fail_verdict() -> None:
    result = AmpEvaluatorRunner._json_to_result({"verdict": "fail", "summary": "bad"})
    assert result.verdict is EvaluationVerdict.failed


def test_json_to_result_invalid_verdict_defaults_to_failed() -> None:
    result = AmpEvaluatorRunner._json_to_result({"verdict": "maybe", "summary": "idk"})
    assert result.verdict is EvaluationVerdict.failed


def test_json_to_result_parses_context_window_usage_pct() -> None:
    data = {
        "verdict": "pass",
        "summary": "done",
        "context_window_usage_pct": 72.5,
    }
    result = AmpEvaluatorRunner._json_to_result(data)
    assert result.context_window_usage_pct == 72.5


def test_evaluator_parse_context_usage_percentage() -> None:
    assert AmpEvaluatorRunner._parse_context_usage("Context window usage: 65%") == 65.0


def test_evaluator_parse_context_usage_tokens() -> None:
    assert AmpEvaluatorRunner._parse_context_usage("tokens used: 80000/100000") == 80.0


def test_evaluator_parse_context_usage_no_match() -> None:
    assert AmpEvaluatorRunner._parse_context_usage("nothing here") is None


def test_json_to_result_extracts_all_fields() -> None:
    data = {
        "verdict": "pass",
        "summary": "done",
        "evidence": ["changed foo.py"],
        "tests_run": ["pytest tests/"],
        "gaps": ["no edge case test"],
        "task_too_large_signal": True,
    }
    result = AmpEvaluatorRunner._json_to_result(data)
    assert result.evidence == ["changed foo.py"]
    assert result.tests_run == ["pytest tests/"]
    assert result.gaps == ["no edge case test"]
    assert result.task_too_large_signal is True


# --- AmpEvaluatorRunner._parse_output ---


def _make_proc(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["amp"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _stream_assistant(text: str) -> str:
    """Build a stream-JSON assistant line containing the given text."""
    msg = {
        "type": "assistant",
        "message": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }
    return json.dumps(msg)


def _stream_result(
    *,
    is_error: bool = False,
    error: str | None = None,
    input_tokens: int = 5000,
    max_tokens: int = 200000,
) -> str:
    """Build a stream-JSON result line."""
    msg: dict = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "duration_ms": 1000,
        "is_error": is_error,
        "num_turns": 1,
        "session_id": "T-test",
        "usage": {
            "input_tokens": input_tokens,
            "max_tokens": max_tokens,
            "output_tokens": 200,
        },
    }
    if is_error and error:
        msg["error"] = error
    if not is_error:
        msg["result"] = "ok"
    return json.dumps(msg)


def test_parse_output_nonzero_exit_code() -> None:
    runner = AmpEvaluatorRunner()
    proc = _make_proc(returncode=1)
    result = runner._parse_output(proc)
    assert result.passed is False
    assert "code 1" in result.summary


def test_parse_output_valid_json_block() -> None:
    runner = AmpEvaluatorRunner()
    assistant_text = '```json\n{"verdict": "pass", "summary": "all good"}\n```'
    stdout = (
        _stream_assistant(assistant_text)
        + "\n"
        + _stream_result()
        + "\n"
    )
    result = runner._parse_output(_make_proc(stdout=stdout))
    assert result.passed is True
    assert result.summary == "all good"


def test_parse_output_bare_json_line() -> None:
    runner = AmpEvaluatorRunner()
    assistant_text = '{"verdict": "pass", "summary": "bare json"}'
    stdout = (
        _stream_assistant(assistant_text)
        + "\n"
        + _stream_result()
        + "\n"
    )
    result = runner._parse_output(_make_proc(stdout=stdout))
    assert result.passed is True
    assert result.summary == "bare json"


def test_parse_output_no_structured_output() -> None:
    runner = AmpEvaluatorRunner()
    stdout = (
        _stream_assistant("just some text")
        + "\n"
        + _stream_result()
        + "\n"
    )
    result = runner._parse_output(_make_proc(stdout=stdout))
    assert result.passed is False
    assert "no structured result" in result.summary.lower()


def test_parse_output_stream_error() -> None:
    runner = AmpEvaluatorRunner()
    stdout = _stream_result(is_error=True, error="context window exhausted") + "\n"
    result = runner._parse_output(_make_proc(stdout=stdout))
    assert result.passed is False
    assert "context window exhausted" in result.summary


def test_parse_output_context_usage_from_stream() -> None:
    runner = AmpEvaluatorRunner()
    assistant_text = '```json\n{"verdict": "pass", "summary": "ok"}\n```'
    stdout = (
        _stream_assistant(assistant_text)
        + "\n"
        + _stream_result(input_tokens=80000, max_tokens=200000)
        + "\n"
    )
    result = runner._parse_output(_make_proc(stdout=stdout))
    assert result.passed is True
    assert result.context_window_usage_pct == 40.0


# --- AmpEvaluatorRunner passes worktree env ---


@patch("amp_orchestrator.evaluator.build_worktree_env")
@patch("amp_orchestrator.evaluator.subprocess.run")
@patch("amp_orchestrator.evaluator.shutil.which", return_value="/usr/bin/amp")
def test_evaluator_passes_worktree_env(mock_which, mock_run, mock_env) -> None:
    """AmpEvaluatorRunner.evaluate() passes env=build_worktree_env() to subprocess."""
    fake_env = {"PYTHONPATH": "/tmp/worktree/src", "PATH": "/usr/bin"}
    mock_env.return_value = fake_env
    mock_run.return_value = subprocess.CompletedProcess(
        args=["amp"], returncode=0, stdout="", stderr="",
    )

    runner = AmpEvaluatorRunner()
    ctx = _make_context()
    runner.evaluate(ctx, "main", [])

    mock_env.assert_called_once_with(ctx.worktree_path)
    _, kwargs = mock_run.call_args
    assert kwargs["env"] is fake_env
