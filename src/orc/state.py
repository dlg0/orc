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


from orc.workflow import WorkflowPhase, RESUMABLE_PHASES  # noqa: E402

# Backward-compatible alias — new code should use WorkflowPhase directly.
RunStage = WorkflowPhase


@dataclass
class RunCheckpoint:
    issue_id: str
    issue_title: str
    issue_description: str = ""
    issue_acceptance_criteria: str = ""
    branch: str | None = None
    worktree_path: str | None = None
    stage: RunStage = RunStage.worktree_created
    bd_claimed: bool = False
    amp_result: dict | None = None
    eval_result: dict | None = None
    preserve_worktree: bool = False
    amp_log_path: str | None = None
    preflight_log_path: str | None = None
    eval_log_path: str | None = None
    resume_attempts: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict:
        d: dict = {
            "issue_id": self.issue_id,
            "issue_title": self.issue_title,
            "issue_description": self.issue_description,
            "issue_acceptance_criteria": self.issue_acceptance_criteria,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "stage": self.stage.value,
            "bd_claimed": self.bd_claimed,
            "amp_result": self.amp_result,
            "eval_result": self.eval_result,
            "preserve_worktree": self.preserve_worktree,
            "amp_log_path": self.amp_log_path,
            "preflight_log_path": self.preflight_log_path,
            "eval_log_path": self.eval_log_path,
            "resume_attempts": self.resume_attempts,
            "updated_at": self.updated_at,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict) -> RunCheckpoint:
        return cls(
            issue_id=data["issue_id"],
            issue_title=data["issue_title"],
            issue_description=data.get("issue_description", ""),
            issue_acceptance_criteria=data.get("issue_acceptance_criteria", ""),
            branch=data.get("branch"),
            worktree_path=data.get("worktree_path"),
            stage=RunStage(data["stage"]),
            bd_claimed=data.get("bd_claimed", False),
            amp_result=data.get("amp_result"),
            eval_result=data.get("eval_result") or data.get("evaluation"),
            preserve_worktree=data.get("preserve_worktree", False),
            amp_log_path=data.get("amp_log_path"),
            preflight_log_path=data.get("preflight_log_path"),
            eval_log_path=data.get("eval_log_path")
            or ((data.get("eval_result") or {}).get("log_path")),
            resume_attempts=data.get("resume_attempts", 0),
            updated_at=data.get("updated_at", ""),
        )


class FailureCategory(Enum):
    transient_external = "transient_external"
    stale_or_conflicted = "stale_or_conflicted"
    awaiting_subtasks = "awaiting_subtasks"
    blocked_by_dependency = "blocked_by_dependency"
    agent_failed = "agent_failed"
    agent_crashed = "agent_crashed"
    merge_exhausted = "merge_exhausted"
    resume_failed = "resume_failed"
    sync_failed = "sync_failed"
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
        FailureCategory.awaiting_subtasks: FailureAction.hold_until_backlog_changes,
        FailureCategory.blocked_by_dependency: FailureAction.hold_until_backlog_changes,
        FailureCategory.agent_failed: FailureAction.pause_orchestrator,
        FailureCategory.agent_crashed: FailureAction.pause_orchestrator,
        FailureCategory.merge_exhausted: FailureAction.pause_orchestrator,
        FailureCategory.resume_failed: FailureAction.pause_orchestrator,
        FailureCategory.sync_failed: FailureAction.pause_orchestrator,
        FailureCategory.fatal_run_error: FailureAction.pause_orchestrator,
    }[category]


def _normalize_issue_failure(info: object) -> dict:
    if isinstance(info, str):
        normalized = {"summary": info}
    elif isinstance(info, dict):
        normalized = dict(info)
    else:
        normalized = {"summary": str(info)}

    category = normalized.get("category") or FailureCategory.agent_failed.value
    # Migrate legacy category names from old state files
    _LEGACY_CATEGORY_MAP = {
        "issue_needs_rework": FailureCategory.agent_failed.value,
    }
    category = _LEGACY_CATEGORY_MAP.get(category, category)
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


_RESUMABLE_STAGES = RESUMABLE_PHASES

_MAX_RESUME_ATTEMPTS = 2



def clear_issue_hold(state: OrchestratorState, issue_id: str) -> str:
    """Remove a held issue from issue_failures so the scheduler re-picks it.

    Returns a user-facing status message.
    """
    if issue_id not in state.issue_failures:
        raise KeyError(issue_id)
    del state.issue_failures[issue_id]
    return f"Removed hold for {issue_id} — eligible for normal scheduling on next run"


def clear_last_error(state: OrchestratorState) -> bool:
    """Clear the persisted last error if one is present."""
    if state.last_error is None:
        return False
    state.last_error = None
    return True



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
    def active_amp_log_path(self) -> str | None:
        if self.active_run:
            return self.active_run.get("amp_log_path")
        return None

    @property
    def active_eval_log_path(self) -> str | None:
        if self.active_run:
            return self.active_run.get("eval_log_path")
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


class RequestQueue:
    """Out-of-band request channel for external actors (TUI, CLI).

    Instead of directly mutating state.json while the scheduler is running,
    external actors enqueue requests as tiny JSON files in ``state_dir/requests/``.
    The scheduler drains and applies these before every save, preventing
    lost-update races where the scheduler's stale in-memory state would
    overwrite external changes.
    """

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "requests"

    def enqueue(self, request_type: str, **data: object) -> Path:
        """Write a request file and return its path.

        Uses tempfile + rename for atomicity.
        """
        import time

        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {"type": request_type, **data}
        # Monotonic-ish filename for ordering; not critical.
        ts = f"{time.time_ns()}"
        fd, tmp_path = tempfile.mkstemp(
            dir=self._dir, suffix=".tmp", prefix=f"req_{ts}_"
        )
        try:
            with open(fd, "w") as f:
                json.dump(payload, f)
            dest = self._dir / f"req_{ts}.json"
            Path(tmp_path).replace(dest)
            return dest
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def drain(self) -> list[dict]:
        """Read and delete all pending requests, oldest first.

        Returns a list of request dicts.  Each file is deleted after
        successful read; replay is safe because handlers are idempotent.
        """
        if not self._dir.is_dir():
            return []
        files = sorted(self._dir.glob("req_*.json"))
        requests: list[dict] = []
        for f in files:
            try:
                requests.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                pass  # skip corrupt files
            f.unlink(missing_ok=True)
        return requests

    def is_empty(self) -> bool:
        if not self._dir.is_dir():
            return True
        return not any(self._dir.glob("req_*.json"))


def apply_requests(state: OrchestratorState, state_dir: Path) -> bool:
    """Drain the request queue and apply each request to *state*.

    Returns True if any requests were applied (caller should save).
    All handlers are idempotent.
    """
    rq = RequestQueue(state_dir)
    requests = rq.drain()
    if not requests:
        return False

    for req in requests:
        rt = req.get("type", "")
        if rt == "unhold":
            issue_id = req.get("issue_id", "")
            state.issue_failures.pop(issue_id, None)  # idempotent
        elif rt == "retry":
            issue_id = req.get("issue_id", "")
            state.issue_failures.pop(issue_id, None)  # idempotent
        elif rt == "clear_last_error":
            state.last_error = None
        elif rt == "pause":
            if state.mode == OrchestratorMode.running:
                state.mode = OrchestratorMode.pause_requested
        elif rt == "stop":
            if state.mode in (
                OrchestratorMode.running,
                OrchestratorMode.pause_requested,
            ):
                state.mode = OrchestratorMode.stopping
        # Unknown types are silently ignored (forward compat)

    return True
