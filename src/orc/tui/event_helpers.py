"""Event formatting utilities shared between TUI widgets."""

from __future__ import annotations

EVENT_COLORS: dict[str, str] = {
    "error": "bold red",
    "issue_selected": "cyan",
    "amp_started": "dodger_blue2",
    "amp_finished": "green",
    "merge_attempt": "orchid1",
    "issue_closed": "green bold",
    "pause_requested": "bright_yellow",
    "stop_requested": "bright_yellow",
    "state_changed": "white",
    "verification_run": "dodger_blue2",
    "evaluation_started": "dodger_blue2",
    "evaluation_finished": "green",
    "issue_needs_rework": "bold bright_yellow",
    "conflict_detected": "bold red",
    "conflict_resolution_started": "bright_yellow",
    "conflict_resolution_finished": "green",
    "resume_attempted": "dodger_blue2",
    "resume_succeeded": "green",
    "resume_failed": "bold red",
    "parent_promoted": "green",
    "issue_failure_pruned": "white",
}

# Severity classification for event types
EVENT_SEVERITY: dict[str, str] = {
    "error": "ERR",
    "conflict_detected": "ERR",
    "issue_needs_rework": "WARN",
    "pause_requested": "WARN",
    "stop_requested": "WARN",
    "conflict_resolution_started": "WARN",
}
# Default severity is "INFO" for any event type not listed above.


def _event_severity(event_type: str, data: dict | None = None) -> str:
    """Return the severity tag for an event type."""
    if event_type == "evaluation_finished":
        payload = data or {}
        if payload.get("classification") == "infrastructure_error":
            return "ERR"
        if payload.get("verdict") == "fail":
            return "WARN"
    return EVENT_SEVERITY.get(event_type, "INFO")


_SEVERITY_STYLE: dict[str, str] = {
    "ERR": "bold red",
    "WARN": "bold bright_yellow",
    "INFO": "bright_white",
}


def _human_message(event_type: str, data: dict | None) -> str:
    """Convert raw event_type + data into a human-readable message."""
    d = data or {}
    iid = d.get("issue_id", "")

    def _trim(text: str, limit: int = 60) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    match event_type:
        case "issue_selected":
            title = d.get("title", "")
            return f"Selected issue {iid}" + (f": {title}" if title else "")
        case "amp_started":
            mode = d.get("mode", "")
            msg = f"Agent started on {iid}" if iid else "Agent started"
            if mode:
                msg += f" (mode={mode})"
            return msg
        case "amp_finished":
            result = d.get("result", "")
            summary = d.get("summary", "")
            msg = f"Agent finished {iid}" if iid else "Agent finished"
            if result:
                msg += f" ({result})"
            if summary:
                msg += f" — {summary}"
            return msg
        case "verification_run":
            cmd = d.get("command", "")
            result = d.get("result", "")
            msg = f"Verification on {iid}" if iid else "Verification"
            if cmd:
                msg += f": {cmd}"
            if result:
                msg += f" [{result}]"
            return msg
        case "merge_attempt":
            return f"Merge attempt for {iid}" if iid else "Merge attempt"
        case "issue_closed":
            reason = d.get("reason", "")
            msg = f"Issue {iid} closed" if iid else "Issue closed"
            if reason:
                msg += f" ({reason.replace('_', ' ')})"
            return msg
        case "pause_requested":
            return "Pause requested"
        case "stop_requested":
            return "Stop requested"
        case "state_changed":
            to = d.get("to", "")
            frm = d.get("from", "")
            reason = d.get("reason", "")
            msg = f"State → {to}" if to else "State changed"
            if frm:
                msg = f"State {frm} → {to}"
            if reason:
                msg += f" ({reason})"
            return msg
        case "error":
            stage = d.get("stage", "")
            err = d.get("error", "")
            msg = f"Error on {iid}" if iid else "Error"
            if stage:
                msg += f" [{stage}]"
            if d.get("outcome_kind"):
                msg += f" ({d['outcome_kind']})"
            if d.get("exception_type"):
                msg += f" {d['exception_type']}"
            if d.get("returncode") is not None:
                msg += f" rc={d['returncode']}"
            if err:
                msg += f": {err}"
            elif d.get("stderr_tail"):
                msg += f": {_trim(d['stderr_tail'])}"
            return msg
        case "evaluation_started":
            requested = d.get("mode_requested") or "default"
            effective = d.get("mode_effective") or d.get("mode", "")
            msg = f"Evaluation started for {iid}" if iid else "Evaluation started"
            if effective:
                msg += f" (effective={effective}"
                if requested:
                    msg += f", requested={requested}"
                msg += ")"
            return msg
        case "evaluation_finished":
            verdict = d.get("verdict", "")
            outcome_kind = d.get("outcome_kind", "")
            summary = d.get("summary", "")
            msg = f"Evaluation finished for {iid}" if iid else "Evaluation finished"
            if verdict:
                msg += f" ({verdict})"
            if outcome_kind and outcome_kind != "completed":
                msg += f" [{outcome_kind}]"
            if summary:
                msg += f" — {_trim(summary)}"
            return msg
        case "issue_needs_rework":
            return f"Issue {iid} needs rework" if iid else "Issue needs rework"
        case "conflict_detected":
            branch = d.get("branch", "")
            msg = f"Conflict detected on {iid}" if iid else "Conflict detected"
            if branch:
                msg += f" (branch: {branch})"
            return msg
        case "conflict_resolution_started":
            return f"Conflict resolution started for {iid}" if iid else "Conflict resolution started"
        case "conflict_resolution_finished":
            result = d.get("result", d.get("status", ""))
            msg = f"Conflict resolution finished for {iid}" if iid else "Conflict resolution finished"
            if result:
                msg += f" ({result})"
            return msg
        case _:
            return event_type if not d else f"{event_type}: {d}"


# Failure-category icons for richer labelling in the history table.
_CATEGORY_ICONS: dict[str, str] = {
    "transient_external": "↻",
    "stale_or_conflicted": "⚡",
    "awaiting_subtasks": "⏳",
    "blocked_by_dependency": "⛔",
    "agent_failed": "✖",
    "agent_crashed": "💥",
    "merge_exhausted": "🔀",
    "resume_failed": "⟳",
    "sync_failed": "⇄",
    "fatal_run_error": "☠",
}

# Human-readable labels for failure categories (shown in status panel & history).
_CATEGORY_LABELS: dict[str, str] = {
    "transient_external": "Transient error",
    "stale_or_conflicted": "Conflict/stale branch",
    "awaiting_subtasks": "Awaiting subtasks",
    "blocked_by_dependency": "Dependency blocked",
    "agent_failed": "Agent failed",
    "agent_crashed": "Agent crashed",
    "merge_exhausted": "Merge exhausted",
    "resume_failed": "Resume failed",
    "sync_failed": "Sync failed",
    "fatal_run_error": "Fatal run error",
}
