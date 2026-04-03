"""Shared models for the Beads dispatch exploration harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

IssueClassification = Literal["worker", "container", "unsupported"]
ScenarioStatus = Literal["ok", "mismatch", "setup_error"]


@dataclass(frozen=True)
class IssueSpec:
    """Declarative Beads issue definition for an exploration scenario."""

    key: str
    title: str
    issue_type: str = "task"
    priority: int | None = None
    status: str = "open"
    parent: str | None = None
    blockers: tuple[str, ...] = ()
    defer_until: str | None = None
    description: str = ""
    create_delay_seconds: float = 0.0


@dataclass(frozen=True)
class ScenarioExpectation:
    """Expectation checks applied to an observed scenario run."""

    ready_contains: tuple[str, ...] = ()
    ready_excludes: tuple[str, ...] = ()
    dispatch_contains: tuple[str, ...] = ()
    dispatch_excludes: tuple[str, ...] = ()
    invalid_due_to_types: tuple[str, ...] = ()
    dispatch_preserves_ready_order: bool = False


@dataclass(frozen=True)
class ScenarioDefinition:
    """Full scenario definition used by the harness."""

    name: str
    description: str
    issues: tuple[IssueSpec, ...]
    hypotheses: tuple[str, ...] = ()
    expectations: ScenarioExpectation = field(default_factory=ScenarioExpectation)


@dataclass
class CommandRecord:
    """One recorded Beads CLI invocation."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class ObservedIssue:
    """Observed Beads issue snapshot."""

    id: str
    key: str | None
    title: str
    issue_type: str
    status: str
    priority: int | None
    parent_id: str | None
    child_ids: list[str] = field(default_factory=list)
    blocker_ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObservedState:
    """Observed Beads state for a scenario sandbox."""

    ids_by_key: dict[str, str] = field(default_factory=dict)
    keys_by_id: dict[str, str] = field(default_factory=dict)
    ready_ids_in_order: list[str] = field(default_factory=list)
    issues_by_id: dict[str, ObservedIssue] = field(default_factory=dict)
    descendants_by_id: dict[str, list[str]] = field(default_factory=dict)
    list_tree: str = ""
    ready_raw: list[dict[str, Any]] = field(default_factory=list)
    list_raw: list[dict[str, Any]] = field(default_factory=list)
    command_transcript: list[CommandRecord] = field(default_factory=list)


@dataclass
class UnsupportedTypeFinding:
    """A fail-closed unsupported issue type encountered during planning."""

    issue_id: str
    key: str | None
    issue_type: str
    reason: str


@dataclass
class PlanEntry:
    """One decision made by the trial planner."""

    issue_id: str
    key: str | None
    title: str
    issue_type: str
    status: str
    classification: IssueClassification
    dispatchable: bool
    reason: str
    ready_index: int | None
    nested_entries: list["PlanEntry"] = field(default_factory=list)
    ready_descendant_ids: list[str] = field(default_factory=list)
    already_accounted_for_ids: list[str] = field(default_factory=list)
    suppressed_by: str | None = None


@dataclass
class TrialPlan:
    """Trial Orc plan derived from observed Beads state."""

    entries: list[PlanEntry] = field(default_factory=list)
    dispatchable_ids: list[str] = field(default_factory=list)
    invalid: bool = False
    unsupported_types: list[UnsupportedTypeFinding] = field(default_factory=list)
    type_policy: dict[str, IssueClassification] = field(default_factory=dict)


@dataclass
class ScenarioRunResult:
    """End-to-end result for one scenario run."""

    scenario: ScenarioDefinition
    sandbox_path: Path
    created_ids_by_key: dict[str, str]
    observations: ObservedState
    plan: TrialPlan
    mismatches: list[str]
    markdown_path: Path
    json_path: Path
    setup_error: str | None = None

    @property
    def status(self) -> ScenarioStatus:
        if self.setup_error is not None:
            return "setup_error"
        if self.mismatches:
            return "mismatch"
        return "ok"


@dataclass
class ExplorationSummary:
    """Aggregate result for a CLI exploration run."""

    output_dir: Path
    results: list[ScenarioRunResult]

    @property
    def exit_code(self) -> int:
        if any(result.setup_error for result in self.results):
            return 2
        if any(result.mismatches for result in self.results):
            return 1
        return 0
