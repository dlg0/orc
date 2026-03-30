"""Orchestrator state management."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class OrchestratorMode(Enum):
    idle = "idle"
    running = "running"
    pause_requested = "pause_requested"
    paused = "paused"
    stopping = "stopping"
    error = "error"


class RunStage(Enum):
    worktree_created = "worktree_created"
    claimed = "claimed"
    amp_running = "amp_running"
    amp_finished = "amp_finished"
    evaluation_running = "evaluation_running"
    ready_to_merge = "ready_to_merge"
    merge_running = "merge_running"
    claim_release_pending = "claim_release_pending"


@dataclass
class RunCheckpoint:
    issue_id: str
    issue_title: str
    branch: str | None = None
    worktree_path: str | None = None
    stage: RunStage = RunStage.worktree_created
    bd_claimed: bool = False
    amp_result: dict | None = None
    eval_result: dict | None = None
    preserve_worktree: bool = False
    resume_attempts: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict:
        d: dict = {
            "issue_id": self.issue_id,
            "issue_title": self.issue_title,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "stage": self.stage.value,
            "bd_claimed": self.bd_claimed,
            "amp_result": self.amp_result,
            "eval_result": self.eval_result,
            "preserve_worktree": self.preserve_worktree,
            "resume_attempts": self.resume_attempts,
            "updated_at": self.updated_at,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict) -> RunCheckpoint:
        return cls(
            issue_id=data["issue_id"],
            issue_title=data["issue_title"],
            branch=data.get("branch"),
            worktree_path=data.get("worktree_path"),
            stage=RunStage(data["stage"]),
            bd_claimed=data.get("bd_claimed", False),
            amp_result=data.get("amp_result"),
            eval_result=data.get("eval_result"),
            preserve_worktree=data.get("preserve_worktree", False),
            resume_attempts=data.get("resume_attempts", 0),
            updated_at=data.get("updated_at", ""),
        )


class FailureCategory(Enum):
    transient_external = "transient_external"
    stale_or_conflicted = "stale_or_conflicted"
    issue_needs_rework = "issue_needs_rework"
    blocked_by_dependency = "blocked_by_dependency"
    fatal_run_error = "fatal_run_error"


class FailureAction(Enum):
    auto_retry = "auto_retry"
    hold_for_retry = "hold_for_retry"
    hold_until_backlog_changes = "hold_until_backlog_changes"
    pause_orchestrator = "pause_orchestrator"


def _default_failure_action(category: FailureCategory) -> FailureAction:
    return {
        FailureCategory.transient_external: FailureAction.auto_retry,
        FailureCategory.stale_or_conflicted: FailureAction.hold_for_retry,
        FailureCategory.issue_needs_rework: FailureAction.hold_until_backlog_changes,
        FailureCategory.blocked_by_dependency: FailureAction.hold_until_backlog_changes,
        FailureCategory.fatal_run_error: FailureAction.pause_orchestrator,
    }[category]


def _normalize_issue_failure(info: object) -> dict:
    if isinstance(info, str):
        normalized = {"summary": info}
    elif isinstance(info, dict):
        normalized = dict(info)
    else:
        normalized = {"summary": str(info)}

    category = normalized.get("category") or FailureCategory.issue_needs_rework.value
    normalized["category"] = category

    try:
        action = normalized.get("action") or _default_failure_action(FailureCategory(category)).value
    except ValueError:
        action = normalized.get("action") or FailureAction.hold_until_backlog_changes.value
    normalized["action"] = action

    normalized.setdefault("stage", "legacy")
    normalized.setdefault("summary", "")
    normalized.setdefault("timestamp", "")
    normalized.setdefault("attempts", 1)
    normalized.setdefault("branch", None)
    normalized.setdefault("worktree_path", None)
    normalized.setdefault("preserve_worktree", False)
    normalized.setdefault("extra", None)
    return normalized


def _normalize_issue_failures(entries: object) -> dict[str, dict]:
    if not isinstance(entries, dict):
        return {}
    return {issue_id: _normalize_issue_failure(info) for issue_id, info in entries.items()}


@dataclass
class IssueFailure:
    category: FailureCategory
    action: FailureAction
    stage: str
    summary: str
    timestamp: str
    attempts: int = 1
    branch: str | None = None
    worktree_path: str | None = None
    preserve_worktree: bool = False
    extra: dict | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "category": self.category.value,
            "action": self.action.value,
            "stage": self.stage,
            "summary": self.summary,
            "timestamp": self.timestamp,
            "attempts": self.attempts,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "preserve_worktree": self.preserve_worktree,
            "extra": self.extra,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict) -> IssueFailure:
        return cls(
            category=FailureCategory(data["category"]),
            action=FailureAction(data["action"]),
            stage=data["stage"],
            summary=data["summary"],
            timestamp=data["timestamp"],
            attempts=data.get("attempts", 1),
            branch=data.get("branch"),
            worktree_path=data.get("worktree_path"),
            preserve_worktree=data.get("preserve_worktree", False),
            extra=data.get("extra"),
        )


VALID_TRANSITIONS: dict[OrchestratorMode, set[OrchestratorMode]] = {
    OrchestratorMode.idle: {OrchestratorMode.running},
    OrchestratorMode.running: {
        OrchestratorMode.pause_requested,
        OrchestratorMode.stopping,
        OrchestratorMode.error,
        OrchestratorMode.idle,
    },
    OrchestratorMode.pause_requested: {
        OrchestratorMode.paused,
        OrchestratorMode.stopping,
        OrchestratorMode.error,
    },
    OrchestratorMode.paused: {
        OrchestratorMode.running,
        OrchestratorMode.idle,
    },
    OrchestratorMode.stopping: {
        OrchestratorMode.idle,
        OrchestratorMode.error,
    },
    OrchestratorMode.error: {OrchestratorMode.idle},
}


_RESUMABLE_STAGES = {
    RunStage.claimed.value,
    RunStage.amp_running.value,
    RunStage.amp_finished.value,
    RunStage.ready_to_merge.value,
}

_MAX_RESUME_ATTEMPTS = 2


def can_retry_merge(info: object) -> bool:
    """Return True when a held failure can resume at verify-and-merge."""
    failure = _normalize_issue_failure(info)
    return (
        failure["category"] == FailureCategory.stale_or_conflicted.value
        and failure["preserve_worktree"] is True
        and isinstance(failure["branch"], str)
        and bool(failure["branch"])
        and isinstance(failure["worktree_path"], str)
        and bool(failure["worktree_path"])
    )


def queue_retry(
    state: OrchestratorState,
    issue_id: str,
    *,
    issue_title: str = "",
    merge_only: bool = False,
) -> str:
    """Queue a held issue for retry and return a user-facing status message."""
    failure = state.issue_failures.get(issue_id)
    if failure is None:
        raise KeyError(issue_id)

    if state.active_run and state.active_run.get("issue_id") != issue_id:
        raise ValueError(
            f"Cannot queue {issue_id} while {state.active_run['issue_id']} is active"
        )
    if state.resume_candidate and state.resume_candidate.get("issue_id") != issue_id:
        raise ValueError(
            "Another retry is already queued — run it or clear it before queueing a new one"
        )

    if merge_only and not can_retry_merge(failure):
        raise ValueError(f"{issue_id} is not eligible for merge-only retry")

    if merge_only or can_retry_merge(failure):
        checkpoint = RunCheckpoint(
            issue_id=issue_id,
            issue_title=issue_title,
            branch=failure["branch"],
            worktree_path=failure["worktree_path"],
            stage=RunStage.ready_to_merge,
            preserve_worktree=True,
            updated_at=failure.get("timestamp", ""),
        )
        state.resume_candidate = checkpoint.to_dict()
        del state.issue_failures[issue_id]
        return (
            f"Scheduled merge retry for {issue_id} — will retry verify-and-merge on next run"
        )

    del state.issue_failures[issue_id]
    return f"Cleared failure status for {issue_id} — will be re-queued on next run"


@dataclass
class OrchestratorState:
    mode: OrchestratorMode = OrchestratorMode.idle
    active_run: dict | None = None
    resume_candidate: dict | None = None
    last_completed_issue: str | None = None
    last_error: str | None = None
    run_history: list[dict] = field(default_factory=list)
    issue_failures: dict[str, dict] = field(default_factory=dict)
    promoted_parent: str | None = None

    # --- Convenience accessors for backward compatibility ---

    @property
    def active_issue_id(self) -> str | None:
        return self.active_run["issue_id"] if self.active_run else None

    @property
    def active_issue_title(self) -> str | None:
        return self.active_run.get("issue_title") if self.active_run else None

    @property
    def active_branch(self) -> str | None:
        return self.active_run.get("branch") if self.active_run else None

    @property
    def active_worktree_path(self) -> str | None:
        return self.active_run.get("worktree_path") if self.active_run else None

    @property
    def active_stage(self) -> str | None:
        if self.active_run:
            return self.active_run.get("stage")
        return None

    @property
    def active_started_at(self) -> str | None:
        if self.active_run:
            return self.active_run.get("updated_at")
        return None


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / "state.json"

    def load(self) -> OrchestratorState:
        if not self._state_file.exists():
            return OrchestratorState()
        raw = json.loads(self._state_file.read_text())
        raw["mode"] = OrchestratorMode(raw["mode"])
        # Migrate legacy needs_rework → issue_failures
        if "needs_rework" in raw and "issue_failures" not in raw:
            raw["issue_failures"] = raw.pop("needs_rework")
        raw.pop("needs_rework", None)
        raw["issue_failures"] = _normalize_issue_failures(raw.get("issue_failures", {}))
        # Migrate legacy active_* fields → active_run
        if "active_issue_id" in raw and "active_run" not in raw:
            aid = raw.pop("active_issue_id", None)
            if aid:
                raw["active_run"] = RunCheckpoint(
                    issue_id=aid,
                    issue_title=raw.pop("active_issue_title", None) or "",
                    branch=raw.pop("active_branch", None),
                    worktree_path=raw.pop("active_worktree_path", None),
                    stage=RunStage.amp_running,
                    updated_at=raw.pop("active_started_at", None) or "",
                ).to_dict()
            else:
                raw["active_run"] = None
            for legacy_key in ("active_issue_id", "active_issue_title", "active_branch",
                               "active_worktree_path", "active_stage", "active_started_at"):
                raw.pop(legacy_key, None)
        # Drop unknown keys and let missing fields use defaults (backward compat)
        known = {f.name for f in OrchestratorState.__dataclass_fields__.values()}
        raw = {k: v for k, v in raw.items() if k in known}
        return OrchestratorState(**raw)

    def save(self, state: OrchestratorState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(state)
        data["mode"] = state.mode.value
        fd, tmp_path = tempfile.mkstemp(
            dir=self._state_dir, suffix=".tmp", prefix="state_"
        )
        try:
            with open(fd, "w") as f:
                json.dump(data, f, indent=2)
            Path(tmp_path).replace(self._state_file)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def transition(
        self, state: OrchestratorState, new_mode: OrchestratorMode
    ) -> OrchestratorState:
        allowed = VALID_TRANSITIONS.get(state.mode, set())
        if new_mode not in allowed:
            raise ValueError(
                f"Invalid transition: {state.mode.value} → {new_mode.value}"
            )
        state.mode = new_mode
        self.save(state)
        return state
