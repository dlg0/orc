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
