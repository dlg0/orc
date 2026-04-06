"""Post-merge completion evaluator: verifies landed changes on the base branch."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from orc.amp_runner import IssueContext
from orc.worktree import build_worktree_env

logger = logging.getLogger(__name__)

_STDERR_TAIL_MAX_CHARS = 4000
_STDERR_TAIL_MAX_LINES = 40


class EvaluationVerdict(str, Enum):
    passed = "pass"
    failed = "fail"


class EvaluationClassification(str, Enum):
    verdict = "verdict"
    infrastructure_error = "infrastructure_error"


@dataclass
class EvaluationResult:
    verdict: EvaluationVerdict
    summary: str
    evidence: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    task_too_large_signal: bool = False
    context_window_usage_pct: float | None = None
    classification: EvaluationClassification = EvaluationClassification.verdict
    mode_requested: str | None = None
    mode_effective: str | None = None
    timeout_seconds: int | None = None
    log_path: str | None = None
    outcome_kind: str = "completed"
    returncode: int | None = None
    stderr_tail: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    duration_ms: int | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == EvaluationVerdict.passed

    @property
    def infrastructure_failure(self) -> bool:
        return self.classification == EvaluationClassification.infrastructure_error

    @property
    def requires_rework(self) -> bool:
        return not self.passed and not self.infrastructure_failure

    @classmethod
    def fail(cls, summary: str, **kwargs: object) -> EvaluationResult:
        return cls(verdict=EvaluationVerdict.failed, summary=summary, **kwargs)

    @classmethod
    def infrastructure_error(cls, summary: str, **kwargs: object) -> EvaluationResult:
        return cls(
            verdict=EvaluationVerdict.failed,
            summary=summary,
            classification=EvaluationClassification.infrastructure_error,
            **kwargs,
        )

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "tests_run": self.tests_run,
            "gaps": self.gaps,
            "task_too_large_signal": self.task_too_large_signal,
            "context_window_usage_pct": self.context_window_usage_pct,
            "classification": self.classification.value,
            "mode_requested": self.mode_requested,
            "mode_effective": self.mode_effective,
            "timeout_seconds": self.timeout_seconds,
            "log_path": self.log_path,
            "outcome_kind": self.outcome_kind,
            "returncode": self.returncode,
            "stderr_tail": self.stderr_tail,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "duration_ms": self.duration_ms,
        }


class IssueEvaluator(Protocol):
    def evaluate(
        self,
        context: IssueContext,
        base_branch: str,
        verification_commands: list[str],
        *,
        log_path: Path | None = None,
    ) -> EvaluationResult: ...


class StubEvaluator:
    """Test double that returns a pre-configured EvaluationResult."""

    def __init__(self, result: EvaluationResult | None = None) -> None:
        self._result = result or EvaluationResult(
            verdict=EvaluationVerdict.passed,
            summary="Stub evaluation passed",
        )

    def evaluate(
        self,
        context: IssueContext,
        base_branch: str,
        verification_commands: list[str],
        *,
        log_path: Path | None = None,
    ) -> EvaluationResult:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("# stub evaluation log\n", encoding="utf-8")
        if self._result.log_path is None and log_path is not None:
            self._result.log_path = str(log_path)
        return self._result

    @classmethod
    def passed(cls, summary: str = "Evaluation passed") -> StubEvaluator:
        return cls(EvaluationResult(verdict=EvaluationVerdict.passed, summary=summary))

    @classmethod
    def failed(cls, summary: str = "Evaluation failed") -> StubEvaluator:
        return cls(EvaluationResult(verdict=EvaluationVerdict.failed, summary=summary))

    @classmethod
    def infrastructure_error(cls, summary: str = "Evaluation infrastructure failed") -> StubEvaluator:
        return cls(EvaluationResult.infrastructure_error(summary))


_DEFAULT_TIMEOUT = 900  # 15 minutes


class AmpEvaluatorRunner:
    """Invokes the ``amp`` CLI to independently evaluate worker output."""

    def __init__(
        self,
        mode: str | None = "smart",
        timeout: int = _DEFAULT_TIMEOUT,
        *,
        requested_mode: str | None = None,
    ) -> None:
        self._mode = self._normalize_mode(mode)
        self._requested_mode = requested_mode
        self._timeout = self._normalize_timeout(timeout)

    # ------------------------------------------------------------------
    # Public interface (satisfies IssueEvaluator protocol)
    # ------------------------------------------------------------------

    def evaluate(
        self,
        context: IssueContext,
        base_branch: str,
        verification_commands: list[str],
        *,
        log_path: Path | None = None,
    ) -> EvaluationResult:
        log_path_str = str(log_path) if log_path is not None else None
        base_metadata = {
            "mode_requested": self._requested_mode,
            "mode_effective": self._mode,
            "timeout_seconds": self._timeout,
            "log_path": log_path_str,
        }
        amp_path = shutil.which("amp")
        if amp_path is None:
            self._append_log_record(log_path, "orc evaluation error", {
                **base_metadata,
                "timestamp": _now_iso(),
                "outcome_kind": "missing_amp_cli",
                "exception_type": "RuntimeError",
                "exception_message": "amp CLI not found in PATH",
            })
            return EvaluationResult.infrastructure_error(
                "amp CLI not found in PATH. Install it or ensure it is on the PATH.",
                outcome_kind="missing_amp_cli",
                exception_type="RuntimeError",
                exception_message="amp CLI not found in PATH",
                **base_metadata,
            )

        prompt = self._build_prompt(context, base_branch, verification_commands)
        cmd = [
            amp_path,
            "-x",
            prompt,
            "--dangerously-allow-all",
            "--no-notifications",
            "--no-color",
            "--stream-json",
            "--mode",
            self._mode,
        ]

        logger.info("Running evaluator in %s", context.repo_root)
        logger.debug("Command: %s", cmd)

        self._append_log_record(log_path, "orc evaluation invocation", {
            **base_metadata,
            "timestamp": _now_iso(),
            "issue_id": context.issue_id,
            "repo_root": str(context.repo_root),
            "base_branch": base_branch,
            "command": cmd,
        })
        self._append_log_marker(log_path, "amp stdout (raw --stream-json)")

        started_at = time.monotonic()
        try:
            proc = self._run_subprocess(context, cmd, log_path)
        except subprocess.TimeoutExpired as exc:
            logger.error("Evaluator timed out after %d seconds", self._timeout)
            duration_ms = int((time.monotonic() - started_at) * 1000)
            stderr_tail = self._tail_text(exc.stderr)
            self._append_log_record(log_path, "orc evaluation timeout", {
                **base_metadata,
                "timestamp": _now_iso(),
                "duration_ms": duration_ms,
                "stderr_tail": stderr_tail,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            })
            return EvaluationResult.infrastructure_error(
                f"Evaluator timed out after {self._timeout}s",
                outcome_kind="timeout",
                stderr_tail=stderr_tail,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                duration_ms=duration_ms,
                **base_metadata,
            )
        except Exception as exc:
            logger.exception("Evaluator invocation failed")
            duration_ms = int((time.monotonic() - started_at) * 1000)
            stderr_tail = self._tail_text(getattr(exc, "stderr", None))
            self._append_log_record(log_path, "orc evaluation exception", {
                **base_metadata,
                "timestamp": _now_iso(),
                "duration_ms": duration_ms,
                "stderr_tail": stderr_tail,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            })
            return EvaluationResult.infrastructure_error(
                f"Evaluator invocation failed: {exc}",
                outcome_kind="exception",
                stderr_tail=stderr_tail,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                duration_ms=duration_ms,
                **base_metadata,
            )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        stderr_tail = self._tail_text(proc.stderr)

        logger.info("Evaluator exited with code %d", proc.returncode)
        if proc.stdout:
            logger.debug("stdout:\n%s", proc.stdout)
        if proc.stderr:
            logger.debug("stderr:\n%s", proc.stderr)

        result = self._parse_output(proc)
        result.mode_requested = self._requested_mode
        result.mode_effective = self._mode
        result.timeout_seconds = self._timeout
        result.log_path = log_path_str
        result.returncode = proc.returncode
        result.stderr_tail = stderr_tail
        result.duration_ms = duration_ms

        self._append_log_record(log_path, "orc evaluation completion", {
            **result.to_dict(),
            "timestamp": _now_iso(),
        })
        return result

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_mode(mode: object) -> str:
        if mode is None:
            return "smart"
        if not isinstance(mode, str):
            raise ValueError("Evaluator mode must be a non-empty string")

        normalized = mode.strip()
        if not normalized:
            raise ValueError("Evaluator mode must be a non-empty string")
        return normalized

    @staticmethod
    def _normalize_timeout(timeout: object) -> int:
        if timeout is None:
            return _DEFAULT_TIMEOUT
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("Evaluator timeout must be a positive integer")
        if timeout <= 0:
            raise ValueError("Evaluator timeout must be a positive integer")
        return timeout

    def _run_subprocess(
        self,
        context: IssueContext,
        cmd: list[str],
        log_path: Path | None,
    ) -> subprocess.CompletedProcess[str]:
        env = build_worktree_env(context.repo_root)
        if log_path is None:
            return subprocess.run(
                cmd,
                cwd=str(context.repo_root),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )

        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_fh:
            proc = subprocess.run(
                cmd,
                cwd=str(context.repo_root),
                stdout=log_fh,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self._timeout,
                env=env,
            )

        stdout = log_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=proc.stderr,
        )

    @staticmethod
    def _append_log_marker(log_path: Path | None, label: str) -> None:
        if log_path is None:
            return
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_fh:
            log_fh.write(f"# {label}\n")

    @staticmethod
    def _append_log_record(log_path: Path | None, label: str, payload: dict) -> None:
        if log_path is None:
            return
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_fh:
            log_fh.write(f"# {label}\n")
            log_fh.write(json.dumps(payload, sort_keys=True) + "\n")

    @staticmethod
    def _tail_text(text: str | bytes | None) -> str | None:
        if text is None:
            return None
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > _STDERR_TAIL_MAX_LINES:
            lines = lines[-_STDERR_TAIL_MAX_LINES:]
        tailed = "\n".join(lines).strip()
        if len(tailed) > _STDERR_TAIL_MAX_CHARS:
            tailed = tailed[-_STDERR_TAIL_MAX_CHARS:]
        return tailed or None

    @staticmethod
    def _build_prompt(
        context: IssueContext,
        base_branch: str,
        verification_commands: list[str],
    ) -> str:
        if verification_commands:
            formatted_cmds = "\n".join(f"- {cmd}" for cmd in verification_commands)
        else:
            formatted_cmds = "- (none configured)"

        parts = [
            f"You are an independent completion evaluator for bd issue {context.issue_id}.",
            "",
            "Your job is ONLY to decide:",
            "1. Was this issue actually completed and landed on the base branch?",
            "2. What concrete evidence supports that decision?",
            "",
            "This is NOT a full code review.",
            "Ignore style nits, refactor ideas, and speculative improvements.",
            "Fail only if the required work is missing, incorrect, unsupported by evidence, or verification fails.",
            "",
            "Source-of-truth requirements:",
            "",
            f"Title: {context.title}",
            "",
            "Description:",
            context.description or "(no description)",
            "",
            "Acceptance criteria:",
            context.acceptance_criteria or "(none specified)",
            "",
            f"Evaluate the current repository state on `{base_branch}` in:",
            str(context.repo_root),
            "",
            "The issue has already been landed (merged and pushed). Judge whether the",
            "current repository state satisfies the original issue requirements.",
            "",
            "Rules:",
            "- Do NOT read or rely on any previous Amp thread, transcript, or worker self-report.",
            "- Do NOT inspect orchestrator logs/state files as evidence.",
            "- Do NOT make code changes, commits, rebases, merges, pushes, or bd updates/closes.",
            "- You MAY inspect git status/diff, read files, and run verification commands.",
            "- If verification commands are provided, run them yourself.",
            "- If you cannot find evidence that a requirement was met, return fail.",
            "- If the issue seems too large/broad for confident single-pass evaluation, set task_too_large_signal to true.",
            "",
            "Suggested procedure:",
            f"1. Check you are on `{base_branch}` and inspect recent commits.",
            "2. Read the most relevant files for the issue requirements.",
            "3. Run these verification commands:",
            formatted_cmds,
            "4. Decide pass/fail based only on issue requirements and evidence in the repository.",
            "",
            "When you are finished, output EXACTLY one JSON block (fenced with ```json) containing your result.",
            "Include ALL of these fields:",
            "```json",
            '{"verdict": "<pass|fail>", '
            '"summary": "<brief justification>", '
            '"evidence": ["<specific code or test evidence>"], '
            '"tests_run": ["<commands actually run>"], '
            '"gaps": ["<missing requirement or failed verification>"], '
            '"task_too_large_signal": <true|false>}',
            "```",
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_context_usage(text: str) -> float | None:
        """Best-effort extraction of context window usage percentage from evaluator output."""
        m = re.search(r'[Cc]ontext\s+(?:window\s+)?usage[:\s]+(\d+(?:\.\d+)?)\s*%', text)
        if m:
            return float(m.group(1))
        m = re.search(r'tokens?\s+used[:\s]+(\d+)\s*/\s*(\d+)', text)
        if m:
            used, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return round(used / total * 100, 1)
        return None

    def _parse_output(self, proc: subprocess.CompletedProcess[str]) -> EvaluationResult:
        stdout = proc.stdout or ""

        # Parse stream JSON messages
        result_msg: dict | None = None
        assistant_texts: list[str] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "result":
                result_msg = msg
            elif msg_type == "assistant":
                content = msg.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        assistant_texts.append(block.get("text", ""))

        # Extract context window usage from result message
        context_usage: float | None = None
        if result_msg and "usage" in result_msg:
            usage = result_msg["usage"]
            input_tokens = usage.get("input_tokens", 0)
            max_tokens = usage.get("max_tokens", 0)
            if max_tokens > 0:
                context_usage = round(input_tokens / max_tokens * 100, 1)

        # Check for error
        if result_msg and result_msg.get("is_error"):
            error_text = result_msg.get("error", "Unknown error")
            return EvaluationResult.infrastructure_error(
                error_text,
                outcome_kind="stream_error",
            )

        if proc.returncode != 0:
            return EvaluationResult.infrastructure_error(
                f"Evaluator exited with code {proc.returncode}",
                outcome_kind="nonzero_exit",
            )

        # Try to extract verdict JSON from assistant text
        combined_text = "\n".join(assistant_texts)

        json_block = self._extract_json_block(combined_text)
        if json_block is not None:
            result = self._json_to_result(json_block)
        else:
            result = None
            for text_line in reversed(combined_text.splitlines()):
                text_line = text_line.strip()
                if text_line.startswith("{") and text_line.endswith("}"):
                    try:
                        data = json.loads(text_line)
                        if "verdict" in data:
                            result = self._json_to_result(data)
                            break
                    except json.JSONDecodeError:
                        continue

        if result is None:
            return EvaluationResult.infrastructure_error(
                "Evaluator produced no structured result",
                outcome_kind="unstructured_output",
            )

        # Override context_window_usage_pct from stream JSON (authoritative)
        if context_usage is not None:
            result.context_window_usage_pct = context_usage
        elif result.context_window_usage_pct is None:
            result.context_window_usage_pct = self._parse_context_usage(combined_text)

        return result

    @staticmethod
    def _extract_json_block(text: str) -> dict | None:
        pattern = r"```json\s*\n(.*?)```"
        matches = re.findall(pattern, text, re.DOTALL)
        for match in reversed(matches):  # prefer last block
            try:
                data = json.loads(match.strip())
                if isinstance(data, dict) and "verdict" in data:
                    return data
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _json_to_result(data: dict) -> EvaluationResult:
        verdict_raw = data.get("verdict", "fail")
        try:
            verdict = EvaluationVerdict(verdict_raw)
        except ValueError:
            verdict = EvaluationVerdict.failed

        classification_raw = data.get("classification", EvaluationClassification.verdict.value)
        try:
            classification = EvaluationClassification(classification_raw)
        except ValueError:
            classification = EvaluationClassification.verdict

        return EvaluationResult(
            verdict=verdict,
            summary=data.get("summary", ""),
            evidence=data.get("evidence", []),
            tests_run=data.get("tests_run", []),
            gaps=data.get("gaps", []),
            task_too_large_signal=raw
            if isinstance((raw := data.get("task_too_large_signal", False)), bool)
            else False,
            context_window_usage_pct=data.get("context_window_usage_pct"),
            classification=classification,
            mode_requested=data.get("mode_requested"),
            mode_effective=data.get("mode_effective"),
            timeout_seconds=data.get("timeout_seconds"),
            log_path=data.get("log_path"),
            outcome_kind=data.get("outcome_kind", "completed"),
            returncode=data.get("returncode"),
            stderr_tail=data.get("stderr_tail"),
            exception_type=data.get("exception_type"),
            exception_message=data.get("exception_message"),
            duration_ms=data.get("duration_ms"),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
