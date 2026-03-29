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


@dataclass
class OrchestratorState:
    mode: OrchestratorMode = OrchestratorMode.idle
    active_issue_id: str | None = None
    active_issue_title: str | None = None
    active_branch: str | None = None
    active_worktree_path: str | None = None
    last_completed_issue: str | None = None
    last_error: str | None = None
    run_history: list[dict] = field(default_factory=list)
    needs_rework: dict[str, dict] = field(default_factory=dict)


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / "state.json"

    def load(self) -> OrchestratorState:
        if not self._state_file.exists():
            return OrchestratorState()
        raw = json.loads(self._state_file.read_text())
        raw["mode"] = OrchestratorMode(raw["mode"])
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
