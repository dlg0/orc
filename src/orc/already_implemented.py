"""Preflight check: detect already-implemented issues before dispatching.

Spawns a lightweight rush-mode Amp task that searches the codebase for
evidence that the functionality described in an issue already exists.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from orc.worktree import build_worktree_env

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120  # 2 minutes — keep it fast


class Confidence(str, Enum):
    already_done = "already_done"
    likely_done = "likely_done"
    not_done = "not_done"


@dataclass(frozen=True)
class AlreadyImplementedResult:
    confidence: Confidence
    summary: str
    evidence: list[str]

    @property
    def should_skip(self) -> bool:
        return self.confidence in (Confidence.already_done, Confidence.likely_done)


class AlreadyImplementedChecker(Protocol):
    def check(
        self,
        issue_id: str,
        title: str,
        description: str,
        acceptance_criteria: str,
        cwd: Path,
        *,
        log_path: Path | None = None,
    ) -> AlreadyImplementedResult: ...


class StubAlreadyImplementedChecker:
    """Test double that returns a pre-configured result."""

    def __init__(self, result: AlreadyImplementedResult | None = None) -> None:
        self._result = result or AlreadyImplementedResult(
            confidence=Confidence.not_done,
            summary="Stub: not implemented",
            evidence=[],
        )

    def check(
        self,
        issue_id: str,
        title: str,
        description: str,
        acceptance_criteria: str,
        cwd: Path,
        *,
        log_path: Path | None = None,
    ) -> AlreadyImplementedResult:
        return self._result

    @classmethod
    def not_done(cls) -> StubAlreadyImplementedChecker:
        return cls()

    @classmethod
    def already_done(cls, summary: str = "Already implemented") -> StubAlreadyImplementedChecker:
        return cls(AlreadyImplementedResult(
            confidence=Confidence.already_done,
            summary=summary,
            evidence=["stub evidence"],
        ))

    @classmethod
    def likely_done(cls, summary: str = "Likely implemented") -> StubAlreadyImplementedChecker:
        return cls(AlreadyImplementedResult(
            confidence=Confidence.likely_done,
            summary=summary,
            evidence=["stub evidence"],
        ))


class AmpAlreadyImplementedChecker:
    """Invokes the ``amp`` CLI in rush mode to check if work is already done."""

    def __init__(self, mode: str = "rush", timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._mode = mode
        self._timeout = timeout

    def check(
        self,
        issue_id: str,
        title: str,
        description: str,
        acceptance_criteria: str,
        cwd: Path,
        *,
        log_path: Path | None = None,
    ) -> AlreadyImplementedResult:
        amp_path = shutil.which("amp")
        if amp_path is None:
            logger.warning("Cannot run already-implemented check: amp CLI not found")
            return AlreadyImplementedResult(
                confidence=Confidence.not_done,
                summary="amp CLI not found — skipping check",
                evidence=[],
            )

        prompt = self._build_prompt(issue_id, title, description, acceptance_criteria)
        cmd = [
            amp_path,
            "-x",
            prompt,
            "--mode",
            self._mode,
            "--dangerously-allow-all",
            "--no-notifications",
            "--no-color",
            "--stream-json",
            "--archive",
        ]

        logger.info("Running already-implemented check for %s in %s", issue_id, cwd)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=build_worktree_env(cwd),
            )
        except subprocess.TimeoutExpired:
            logger.warning("Already-implemented check timed out for %s", issue_id)
            return AlreadyImplementedResult(
                confidence=Confidence.not_done,
                summary=f"check timed out after {self._timeout}s — assuming not done",
                evidence=[],
            )

        # Persist the stream-json output so the TUI can inspect it
        if log_path is not None and proc.stdout:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(proc.stdout, encoding="utf-8")
            except OSError:
                logger.warning("Failed to write preflight log to %s", log_path)

        if proc.returncode != 0:
            logger.warning(
                "Already-implemented check failed (exit %d) for %s",
                proc.returncode,
                issue_id,
            )
            return AlreadyImplementedResult(
                confidence=Confidence.not_done,
                summary=f"check failed (exit {proc.returncode}) — assuming not done",
                evidence=[],
            )

        return self._parse_output(proc)

    @staticmethod
    def _build_prompt(
        issue_id: str,
        title: str,
        description: str,
        acceptance_criteria: str,
    ) -> str:
        parts = [
            "You are a preflight checker. Your ONLY job is to determine whether the",
            "functionality described in the following issue has ALREADY been implemented",
            "in this codebase.",
            "",
            "Context: It is possible this issue was completed by another agent and the",
            "completion got lost in the system. You are NOT checking issue state (open/closed)",
            "— you are checking the actual codebase for the described functionality.",
            "",
            f"Issue: {issue_id}",
            f"Title: {title}",
            "",
            "Description:",
            description or "(no description)",
            "",
            "Acceptance criteria:",
            acceptance_criteria or "(none specified)",
            "",
            "Instructions:",
            "1. Search the codebase for evidence that the described functionality exists.",
            "2. Look for matching code, tests, configurations, or other artifacts.",
            "3. Be thorough but fast — check the most likely locations first.",
            "4. Do NOT make any changes to the codebase.",
            "",
            "When you are finished, output EXACTLY one JSON block (fenced with ```json)",
            "containing your assessment:",
            "```json",
            '{"confidence": "<already_done|likely_done|not_done>",',
            ' "summary": "<brief explanation of your assessment>",',
            ' "evidence": ["<specific files or code that support your assessment>"]}',
            "```",
            "",
            "Confidence levels:",
            "- already_done: The described functionality is clearly present and working.",
            "- likely_done: Strong evidence suggests the work is done, but some uncertainty remains.",
            "- not_done: No evidence that the described functionality exists, or it is clearly incomplete.",
        ]
        return "\n".join(parts)

    def _parse_output(self, proc: subprocess.CompletedProcess[str]) -> AlreadyImplementedResult:
        stdout = proc.stdout or ""

        # Parse stream JSON messages
        assistant_texts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "assistant":
                content = msg.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        assistant_texts.append(block.get("text", ""))

        combined_text = "\n".join(assistant_texts)

        # Try fenced JSON block first
        json_block = self._extract_json_block(combined_text)
        if json_block is not None:
            return self._json_to_result(json_block)

        # Try bare JSON line
        for text_line in reversed(combined_text.splitlines()):
            text_line = text_line.strip()
            if text_line.startswith("{") and text_line.endswith("}"):
                try:
                    data = json.loads(text_line)
                    if "confidence" in data:
                        return self._json_to_result(data)
                except json.JSONDecodeError:
                    continue

        # Fallback: assume not done
        return AlreadyImplementedResult(
            confidence=Confidence.not_done,
            summary="no structured result — assuming not done",
            evidence=[],
        )

    @staticmethod
    def _extract_json_block(text: str) -> dict | None:
        pattern = r"```json\s*\n(.*?)```"
        matches = re.findall(pattern, text, re.DOTALL)
        for match in reversed(matches):
            try:
                data = json.loads(match.strip())
                if isinstance(data, dict) and "confidence" in data:
                    return data
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _json_to_result(data: dict) -> AlreadyImplementedResult:
        raw_confidence = data.get("confidence", "not_done")
        try:
            confidence = Confidence(raw_confidence)
        except ValueError:
            confidence = Confidence.not_done

        return AlreadyImplementedResult(
            confidence=confidence,
            summary=data.get("summary", ""),
            evidence=data.get("evidence", []),
        )
