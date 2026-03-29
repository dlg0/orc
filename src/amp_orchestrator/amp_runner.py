"""Amp worker adapter: protocol, result types, and stub implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol


class ResultType(str, Enum):
    completed = "completed"
    decomposed = "decomposed"
    blocked = "blocked"
    failed = "failed"
    needs_human = "needs_human"


@dataclass
class AmpResult:
    result: ResultType
    summary: str
    changed_paths: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    followup_bd_issues: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    merge_ready: bool = False


@dataclass
class IssueContext:
    issue_id: str
    title: str
    description: str
    acceptance_criteria: str
    worktree_path: Path
    repo_root: Path


class AmpRunner(Protocol):
    def run(self, context: IssueContext) -> AmpResult: ...


class StubAmpRunner:
    """Test double that returns a pre-configured AmpResult."""

    def __init__(self, result: AmpResult | None = None) -> None:
        self._result = result or AmpResult(
            result=ResultType.completed,
            summary="Stub completed successfully",
            merge_ready=True,
        )

    def run(self, context: IssueContext) -> AmpResult:
        return self._result

    @classmethod
    def completed(cls, summary: str = "Completed successfully") -> StubAmpRunner:
        return cls(AmpResult(result=ResultType.completed, summary=summary, merge_ready=True))

    @classmethod
    def decomposed(cls, summary: str = "Decomposed into sub-issues") -> StubAmpRunner:
        return cls(AmpResult(result=ResultType.decomposed, summary=summary))

    @classmethod
    def failed(cls, summary: str = "Task failed") -> StubAmpRunner:
        return cls(AmpResult(result=ResultType.failed, summary=summary))

    @classmethod
    def blocked(
        cls, summary: str = "Blocked on dependencies", blockers: list[str] | None = None
    ) -> StubAmpRunner:
        return cls(AmpResult(
            result=ResultType.blocked,
            summary=summary,
            blockers=blockers or [],
        ))

    @classmethod
    def needs_human(cls, summary: str = "Needs human review") -> StubAmpRunner:
        return cls(AmpResult(result=ResultType.needs_human, summary=summary))
