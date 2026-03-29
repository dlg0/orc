"""Append-only event log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


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


class EventLog:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._log_file = state_dir / "events.jsonl"

    def record(self, event_type: EventType, data: dict | None = None) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type.value,
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
                entries.append(json.loads(line))
        return entries
