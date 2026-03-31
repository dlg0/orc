"""Append-only event log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from orc.workflow import WorkflowPhase, infer_event_phase


class EventType(Enum):
    issue_selected = "issue_selected"
    amp_started = "amp_started"
    amp_finished = "amp_finished"
    verification_run = "verification_run"
    merge_attempt = "merge_attempt"
    issue_closed = "issue_closed"
    pause_requested = "pause_requested"
    stop_requested = "stop_requested"
    error = "error"
    state_changed = "state_changed"
    evaluation_started = "evaluation_started"
    evaluation_finished = "evaluation_finished"
    issue_needs_rework = "issue_needs_rework"
    conflict_detected = "conflict_detected"
    conflict_resolution_started = "conflict_resolution_started"
    conflict_resolution_finished = "conflict_resolution_finished"
    resume_attempted = "resume_attempted"
    resume_succeeded = "resume_succeeded"
    resume_failed = "resume_failed"
    parent_promoted = "parent_promoted"
    issue_failure_pruned = "issue_failure_pruned"
    followup_created = "followup_created"


class EventLog:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._log_file = state_dir / "events.jsonl"
        self._current_phase: str | None = None

    def set_phase(self, phase: WorkflowPhase | str | None) -> None:
        """Set the current workflow phase for subsequent events."""
        if isinstance(phase, WorkflowPhase):
            self._current_phase = phase.value
        else:
            self._current_phase = phase

    def record(
        self,
        event_type: EventType,
        data: dict | None = None,
        *,
        phase: WorkflowPhase | str | None = None,
    ) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(phase, WorkflowPhase):
            phase_value = phase.value
        elif phase:
            phase_value = phase
        else:
            phase_value = self._current_phase or ""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type.value,
            "phase": phase_value,
            "data": data,
        }
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def recent(self, n: int = 20) -> list[dict]:
        return self.all()[-n:]

    def all(self) -> list[dict]:
        if not self._log_file.exists():
            return []
        entries: list[dict] = []
        for line in self._log_file.read_text().splitlines():
            if line.strip():
                entry = json.loads(line)
                # Backfill phase for legacy events
                if not entry.get("phase"):
                    entry["phase"] = infer_event_phase(
                        entry.get("event_type", ""),
                        entry.get("data"),
                    )
                entries.append(entry)
        return entries
