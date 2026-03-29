"""Independent completion evaluator: verifies worker Amp output before merge."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from amp_orchestrator.amp_runner import IssueContext

logger = logging.getLogger(__name__)


class EvaluationVerdict(str, Enum):
    passed = "pass"
    failed = "fail"


@dataclass
class EvaluationResult:
    verdict: EvaluationVerdict
    summary: str
    evidence: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    task_too_large_signal: bool = False
    context_window_usage_pct: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == EvaluationVerdict.passed

    @classmethod
    def fail(cls, summary: str) -> EvaluationResult:
        return cls(verdict=EvaluationVerdict.failed, summary=summary)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "tests_run": self.tests_run,
            "gaps": self.gaps,
            "task_too_large_signal": self.task_too_large_signal,
            "context_window_usage_pct": self.context_window_usage_pct,
        }


class IssueEvaluator(Protocol):
    def evaluate(
        self,
        context: IssueContext,
        base_branch: str,
        verification_commands: list[str],
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
    ) -> EvaluationResult:
        return self._result

    @classmethod
    def passed(cls, summary: str = "Evaluation passed") -> StubEvaluator:
        return cls(EvaluationResult(verdict=EvaluationVerdict.passed, summary=summary))

    @classmethod
    def failed(cls, summary: str = "Evaluation failed") -> StubEvaluator:
        return cls(EvaluationResult(verdict=EvaluationVerdict.failed, summary=summary))


_DEFAULT_TIMEOUT = 900  # 15 minutes


class AmpEvaluatorRunner:
    """Invokes the ``amp`` CLI to independently evaluate worker output."""

    def __init__(self, mode: str = "smart", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._mode = mode
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public interface (satisfies IssueEvaluator protocol)
    # ------------------------------------------------------------------

    def evaluate(
        self,
        context: IssueContext,
        base_branch: str,
        verification_commands: list[str],
    ) -> EvaluationResult:
        amp_path = shutil.which("amp")
        if amp_path is None:
            raise RuntimeError(
                "amp CLI not found in PATH. Install it or ensure it is on the PATH."
            )

        prompt = self._build_prompt(context, base_branch, verification_commands)
        cmd = [
            amp_path,
            "-x",
            prompt,
            "--dangerously-allow-all",
            "--no-notifications",
            "--no-color",
            "--mode",
            self._mode,
        ]

        logger.info("Running evaluator in %s", context.worktree_path)
        logger.debug("Command: %s", cmd)

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(context.worktree_path),
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("Evaluator timed out after %d seconds", self._timeout)
            return EvaluationResult.fail(
                f"Evaluator timed out after {self._timeout}s",
            )

        logger.info("Evaluator exited with code %d", proc.returncode)
        if proc.stdout:
            logger.debug("stdout:\n%s", proc.stdout)
        if proc.stderr:
            logger.debug("stderr:\n%s", proc.stderr)

        return self._parse_output(proc)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

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
            "1. Was this issue actually completed in the current worktree?",
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
            "Evaluate only the repository state in this worktree:",
            str(context.worktree_path),
            "",
            "Rules:",
            "- Do NOT read or rely on any previous Amp thread, transcript, or worker self-report.",
            "- Do NOT inspect orchestrator logs/state files as evidence.",
            "- Do NOT make code changes, commits, rebases, merges, pushes, or bd updates/closes.",
            "- You MAY inspect git status/diff, read files, and run verification commands.",
            "- If verification commands are provided, run them yourself.",
            "- If you cannot find evidence that a requirement was met, return fail.",
            "- If the issue seems too large/broad for confident single-pass evaluation, set task_too_large_signal to true.",
            "- Leave the worktree clean when finished.",
            "",
            "Suggested procedure:",
            f"1. Inspect git status and git diff against origin/{base_branch}.",
            "2. Read the most relevant changed files.",
            "3. Run these verification commands:",
            formatted_cmds,
            "4. Decide pass/fail based only on issue requirements and evidence in the worktree.",
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
        if proc.returncode != 0:
            return EvaluationResult.fail(
                f"Evaluator exited with code {proc.returncode}",
            )

        stdout = proc.stdout or ""

        json_block = self._extract_json_block(stdout)
        if json_block is not None:
            result = self._json_to_result(json_block)
        else:
            result = None
            # Try bare JSON object on a single line
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if "verdict" in data:
                            result = self._json_to_result(data)
                            break
                    except json.JSONDecodeError:
                        continue

        if result is None:
            return EvaluationResult.fail(
                "Evaluator produced no structured result",
            )

        # Best-effort context window usage extraction
        combined = stdout + "\n" + (proc.stderr or "")
        if result.context_window_usage_pct is None:
            result.context_window_usage_pct = self._parse_context_usage(combined)

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
        )
