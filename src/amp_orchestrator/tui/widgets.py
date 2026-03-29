"""Dashboard widgets for the amp-orchestrator TUI."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode
from amp_orchestrator.tui.snapshot import DashboardSnapshot

MODE_STYLES: dict[OrchestratorMode, tuple[str, str]] = {
    OrchestratorMode.running: ("bold green", "● RUN"),
    OrchestratorMode.paused: ("bold bright_yellow", "⏸ PAUSE"),
    OrchestratorMode.pause_requested: ("bold bright_yellow", "⏸ PAUSE REQUESTED"),
    OrchestratorMode.stopping: ("bold bright_red", "■ STOPPING"),
    OrchestratorMode.error: ("bold red", "✖ ERR"),
    OrchestratorMode.idle: ("bold white", "○ IDLE"),
}

NO_PROJECT_MSG = "[bold red]⚠ Not connected to repo/state directory[/]"
NO_PROJECT_PLACEHOLDER = "[italic]Not available — no project detected[/]"

# Max display width for values in the narrow left column.
_LEFT_COL_MAX = 40


def _truncate(text: str, max_len: int = _LEFT_COL_MAX) -> str:
    """Truncate *text* with an ellipsis if it exceeds *max_len*."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _format_run_timestamp(ts: str) -> str:
    """Format an ISO timestamp for the run history table.

    Returns relative time (e.g. '5m ago') for recent runs (< 24h),
    or 'YYYY-MM-DD HH:MM' for older ones.
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return dt.strftime("%Y-%m-%d %H:%M")
        if total_seconds < 60:
            return "just now"
        if total_seconds < 3600:
            return f"{total_seconds // 60}m ago"
        if total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h ago"
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts


class SummaryStrip(Static):
    """One-line summary strip for at-a-glance orchestrator status."""

    DEFAULT_CSS = """
    SummaryStrip {
        height: 1;
        background: $surface;
        color: white;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("○ IDLE", id="summary-strip-text")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        color, text = MODE_STYLES.get(
            snap.state.mode, ("bold white", snap.state.mode.value)
        )
        parts: list[str] = [f"[{color}]{text}[/]"]

        # Active agents
        active = 1 if snap.state.active_issue_id else 0
        if active:
            parts.append(f"[bold]{active} active[/]")

        # Queued (only available on full snapshots)
        if not snap.is_fast and snap.ready_issues is not None:
            parts.append(f"{len(snap.ready_issues)} queued")

        # Held issues
        held = len(snap.state.issue_failures)
        if held:
            parts.append(f"[bold bright_yellow]{held} held[/]")

        # Error count from events
        err_count = sum(
            1 for e in snap.recent_events if _event_severity(e.get("event_type", "")) == "ERR"
        )
        if err_count:
            parts.append(f"[bold red]{err_count} error{'s' if err_count != 1 else ''}[/]")

        # Last refresh time
        try:
            ts = snap.recent_events[-1].get("timestamp", "") if snap.recent_events else ""
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            if ts:
                parts.append(f"[italic]updated {ts}[/]")
        except (IndexError, AttributeError):
            pass

        self.query_one("#summary-strip-text", Label).update(" | ".join(parts))

    def show_no_project(self) -> None:
        self.query_one("#summary-strip-text", Label).update(
            "[bold red]⚠ NOT CONNECTED[/]"
        )

    def show_transitional(self, text: str) -> None:
        self.query_one("#summary-strip-text", Label).update(
            f"[bright_yellow]{text}[/]"
        )

    def show_frozen(self) -> None:
        """Prepend a FROZEN badge to the current summary text."""
        label = self.query_one("#summary-strip-text", Label)
        current = str(label.renderable)
        # Avoid stacking multiple FROZEN badges
        if "FROZEN" not in current:
            label.update(f"[bold bright_cyan on #003344]❄ FROZEN[/] {current}")

    def hide_frozen(self) -> None:
        """Remove the FROZEN badge (next update_snapshot will rebuild cleanly)."""
        label = self.query_one("#summary-strip-text", Label)
        current = str(label.renderable)
        # Strip the frozen prefix if present
        import re
        label.update(re.sub(r"\[bold bright_cyan on #003344\]❄ FROZEN\[/\] ?", "", current))


class StaleBanner(Static):
    """Banner shown when dashboard data is stale or refresh has failed."""

    DEFAULT_CSS = """
    StaleBanner {
        height: auto;
        background: #4a3500;
        color: white;
        text-align: center;
        text-style: bold;
        padding: 0 1;
        display: none;
    }
    StaleBanner.visible {
        display: block;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="stale-banner-text")

    def show_stale(self, seconds_ago: int) -> None:
        """Show a staleness warning with the elapsed time."""
        self.query_one("#stale-banner-text", Label).update(
            f"[bold yellow on #4a3500]⚠ STALE: Dashboard data not refreshed for {seconds_ago}s[/]"
        )
        self.add_class("visible")

    def show_error(self, message: str) -> None:
        """Show a refresh-failed banner."""
        self.query_one("#stale-banner-text", Label).update(
            f"[bold red on #4a3500]⚠ Refresh failed: {message}[/]"
        )
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")


class NotConnectedBanner(Static):
    """Persistent banner shown when no repo/state directory is detected."""

    DEFAULT_CSS = """
    NotConnectedBanner {
        height: auto;
        background: $error-darken-2;
        color: white;
        text-align: center;
        text-style: bold;
        padding: 0 1;
        display: none;
    }
    NotConnectedBanner.visible {
        display: block;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(NO_PROJECT_MSG, id="no-project-banner-text")


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


def _event_severity(event_type: str) -> str:
    """Return the severity tag for an event type."""
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
    match event_type:
        case "issue_selected":
            title = d.get("title", "")
            return f"Selected issue {iid}" + (f": {title}" if title else "")
        case "amp_started":
            return f"Agent started on {iid}" if iid else "Agent started"
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
            return f"Issue {iid} closed" if iid else "Issue closed"
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
            if err:
                msg += f": {err}"
            return msg
        case "evaluation_started":
            return f"Evaluation started for {iid}" if iid else "Evaluation started"
        case "evaluation_finished":
            return f"Evaluation finished for {iid}" if iid else "Evaluation finished"
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


class ErrorAlert(Static):
    """Persistent, high-salience error alert shown when last_error exists."""

    DEFAULT_CSS = """
    ErrorAlert {
        height: auto;
        display: none;
        background: #4a0000;
        color: white;
        padding: 0 1;
        margin: 1 0 0 0;
        text-style: bold;
    }
    ErrorAlert.visible {
        display: block;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "inspect_error", "Inspect Error", show=True),
        Binding("i", "inspect_error", "Inspect Error", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._full_error: str = ""

    def compose(self) -> ComposeResult:
        yield Label("", id="error-alert-text")

    def set_error(self, error: str) -> None:
        self._full_error = error
        if error:
            truncated = error if len(error) <= 120 else error[:117] + "…"
            self.query_one("#error-alert-text", Label).update(
                f"[bold red on #4a0000]⚠ ERROR: {truncated}[/]  [italic](Enter/i to inspect)[/]"
            )
            self.add_class("visible")
        else:
            self.query_one("#error-alert-text", Label).update("")
            self.remove_class("visible")

    def action_inspect_error(self) -> None:
        if not self._full_error:
            return
        from amp_orchestrator.tui.modals import InspectModal

        self.app.push_screen(
            InspectModal(title="Error Details", body=self._full_error)
        )


class StatusPanel(Static):
    """Status panel: mode badge, queue count, last completed/error, error alert."""

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        min-height: 10;
        border: solid grey;
        padding: 0 1;
    }
    StatusPanel.error-state {
        border: solid red;
    }
    StatusPanel .panel-title {
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Status", classes="panel-title")
        yield Label("[bold white]○ IDLE[/]", id="mode-badge")
        yield Label("[italic]Last refresh: —[/]", id="last-updated")
        yield Label("[italic]Queue last refreshed: —[/]", id="queue-last-refreshed")
        yield Label("Ready Queue: 0 issue(s)", id="queue-count")
        yield Label("[italic]Events: —[/]", id="event-severity-counts")
        yield Label("[italic]Held: —[/]", id="failed-count")
        yield Label("[italic]Last completed: —[/]", id="last-completed")
        yield Label("[italic]Last error: —[/]", id="last-error")
        yield ErrorAlert()

    def show_no_project(self) -> None:
        self.query_one("#mode-badge", Label).update(
            "[bold red]⚠ NOT CONNECTED[/]"
        )
        self.query_one("#last-updated", Label).update("[italic]Last refresh: —[/]")
        self.query_one("#queue-last-refreshed", Label).update("[italic]Queue last refreshed: —[/]")
        self.query_one("#queue-count", Label).update(NO_PROJECT_PLACEHOLDER)
        self.query_one("#event-severity-counts", Label).update("[italic]Events: —[/]")
        self.query_one("#failed-count", Label).update("[italic]Held: —[/]")
        self.query_one("#last-completed", Label).update("[italic]Last completed: —[/]")
        self.query_one("#last-error", Label).update("[italic]Last error: —[/]")
        self.query_one(ErrorAlert).set_error("")

    def update_last_refreshed(self, ts: datetime) -> None:
        """Update the 'Last refresh' display with the given timestamp."""
        time_str = ts.strftime("%H:%M:%S")
        self.query_one("#last-updated", Label).update(
            f"[italic]Last refresh: {time_str}[/]"
        )

    def update_queue_last_refreshed(self, ts: datetime) -> None:
        """Update the 'Queue last refreshed' display with the given timestamp."""
        time_str = ts.strftime("%H:%M:%S")
        self.query_one("#queue-last-refreshed", Label).update(
            f"[italic]Queue last refreshed: {time_str}[/]"
        )

    def show_stale(self) -> None:
        """Show STALE badge on the last-refresh label."""
        label = self.query_one("#last-updated", Label)
        current = str(label.renderable)
        if "STALE" not in current:
            # Extract time from current text
            label.update(
                f"[bold yellow]⚠ STALE[/] {current}"
            )

    def show_transitional(self, text: str) -> None:
        """Show a transitional status like 'Starting…' or 'Pausing…'."""
        self.query_one("#mode-badge", Label).update(f"[bright_yellow]{text}[/]")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        color, text = MODE_STYLES.get(
            snap.state.mode, ("bold white", snap.state.mode.value)
        )
        badge = self.query_one("#mode-badge", Label)
        badge.update(f"[{color}]{text}[/]")

        if snap.state.mode == OrchestratorMode.error:
            self.add_class("error-state")
        else:
            self.remove_class("error-state")

        if not snap.is_fast:
            self.query_one("#queue-count", Label).update(
                f"Ready Queue: {len(snap.ready_issues)} issue(s)"
            )

        # Event severity counts
        sev_label = self.query_one("#event-severity-counts", Label)
        err_count = sum(
            1 for e in snap.recent_events if _event_severity(e.get("event_type", "")) == "ERR"
        )
        warn_count = sum(
            1 for e in snap.recent_events if _event_severity(e.get("event_type", "")) == "WARN"
        )
        if err_count or warn_count:
            parts: list[str] = []
            if err_count:
                parts.append(f"[bold red]✖ {err_count} error(s)[/]")
            if warn_count:
                parts.append(f"[bold bright_yellow]⚠ {warn_count} warning(s)[/]")
            sev_label.update("Events: " + ", ".join(parts))
        else:
            sev_label.update("[italic]Events: —[/]")

        # Held issues by category
        fc = self.query_one("#failed-count", Label)
        if snap.state.issue_failures:
            by_cat: dict[str, int] = {}
            for info in snap.state.issue_failures.values():
                cat = info.get("category", "unknown") if isinstance(info, dict) else "unknown"
                by_cat[cat] = by_cat.get(cat, 0) + 1
            parts = ", ".join(f"{_CATEGORY_LABELS.get(cat, cat)}: {n}" for cat, n in sorted(by_cat.items()))
            fc.update(f"[bold red]⚠ Held: {len(snap.state.issue_failures)} ({parts})[/]")
        else:
            fc.update("[italic]Held: —[/]")

        lc = self.query_one("#last-completed", Label)
        if snap.state.last_completed_issue:
            lc.update(f"[green]✔ Last completed: {snap.state.last_completed_issue}[/]")
        else:
            lc.update("[italic]Last completed: —[/]")

        le = self.query_one("#last-error", Label)
        if snap.state.last_error:
            le.update(f"[bold red]✖ Last error: {snap.state.last_error}[/]")
        else:
            le.update("[italic]Last error: —[/]")

        # Persistent error alert
        self.query_one(ErrorAlert).set_error(snap.state.last_error or "")


def _format_elapsed(started_at: str) -> str:
    """Return a human-readable elapsed string like '2m 35s'."""
    try:
        start = datetime.fromisoformat(started_at)
        delta = datetime.now(timezone.utc) - start
        total_secs = int(delta.total_seconds())
        if total_secs < 0:
            return "0s"
        hours, remainder = divmod(total_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
    except (ValueError, TypeError):
        return "?"


_STAGE_STYLES: dict[str, tuple[str, str]] = {
    "claiming": ("cyan", "⏳ Claiming"),
    "running agent": ("dodger_blue2", "🤖 Running Agent"),
    "evaluating": ("orchid1", "🔍 Evaluating"),
    "merging": ("green", "🔀 Merging"),
}

# Result icons for the run history table — provide non-color cues.
_RESULT_ICONS: dict[str, str] = {
    "completed": "✔",
    "failed": "✖",
    "error": "✖",
    "skipped": "⊘",
    "timeout": "⏱",
}

# Failure-category icons for richer labelling in the history table.
_CATEGORY_ICONS: dict[str, str] = {
    "transient_external": "↻",
    "stale_or_conflicted": "⚡",
    "issue_needs_rework": "✎",
    "blocked_by_dependency": "⛔",
    "fatal_run_error": "☠",
}

# Human-readable labels for failure categories (shown in status panel & history).
_CATEGORY_LABELS: dict[str, str] = {
    "transient_external": "Transient error",
    "stale_or_conflicted": "Conflict/stale branch",
    "issue_needs_rework": "Needs rework",
    "blocked_by_dependency": "Dependency blocked",
    "fatal_run_error": "Fatal run error",
}


class ActiveIssuePanel(Static):
    """Active issue panel: id, title, stage, elapsed, branch, worktree."""

    DEFAULT_CSS = """
    ActiveIssuePanel {
        height: auto;
        min-height: 7;
        border: solid grey;
        padding: 0 1;
    }
    ActiveIssuePanel .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "inspect", "Inspect", show=True),
        Binding("i", "inspect", "Inspect", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._last_snap_state: OrchestratorState | None = None

    def compose(self) -> ComposeResult:
        yield Label("Active Issue (Enter/i to inspect)", classes="panel-title")
        yield Label("[italic]No active issue[/]", id="active-detail")

    def show_no_project(self) -> None:
        self.query_one("#active-detail", Label).update(NO_PROJECT_PLACEHOLDER)

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        self._last_snap_state = snap.state
        detail = self.query_one("#active-detail", Label)
        if snap.state.active_issue_id:
            lines = [f"[bold]{snap.state.active_issue_id}[/]"]
            if snap.state.active_issue_title:
                lines.append(f"  {_truncate(snap.state.active_issue_title)}")
            if snap.state.active_stage:
                color, label = _STAGE_STYLES.get(
                    snap.state.active_stage,
                    ("bright_yellow", snap.state.active_stage),
                )
                elapsed = ""
                if snap.state.active_started_at:
                    elapsed = f" ({_format_elapsed(snap.state.active_started_at)})"
                lines.append(f"  Stage: [{color}]{label}[/]{elapsed}")
            if snap.state.active_branch:
                lines.append(f"  Branch: {_truncate(snap.state.active_branch)}")
            if snap.state.active_worktree_path:
                lines.append(f"  Worktree: {_truncate(snap.state.active_worktree_path)}")
            detail.update("\n".join(lines))
        else:
            detail.update("[italic]No active issue[/]")

    def action_inspect(self) -> None:
        self._show_inspect()

    def _show_inspect(self) -> None:
        state = self._last_snap_state
        if not state or not state.active_issue_id:
            return
        title = f"Active Issue: {state.active_issue_id}"
        lines = [f"[bold]Issue ID:[/] {state.active_issue_id}"]
        if state.active_issue_title:
            lines.append(f"[bold]Title:[/] {state.active_issue_title}")
        if state.active_stage:
            color, label = _STAGE_STYLES.get(
                state.active_stage,
                ("yellow", state.active_stage),
            )
            elapsed = ""
            if state.active_started_at:
                elapsed = f" ({_format_elapsed(state.active_started_at)})"
            lines.append(f"[bold]Stage:[/] [{color}]{label}[/]{elapsed}")
        if state.active_started_at:
            lines.append(f"[bold]Started At:[/] {state.active_started_at}")
        if state.active_branch:
            lines.append(f"[bold]Branch:[/] {state.active_branch}")
        if state.active_worktree_path:
            lines.append(f"[bold]Worktree:[/] {state.active_worktree_path}")
        from amp_orchestrator.tui.modals import CopyableField, InspectModal

        copyable: list[CopyableField] = []
        if state.active_branch:
            copyable.append(CopyableField(label="Branch", value=state.active_branch, key="b"))
        if state.active_worktree_path:
            copyable.append(CopyableField(label="Worktree", value=state.active_worktree_path, key="w"))

        self.app.push_screen(
            InspectModal(title=title, body="\n".join(lines), copyable_fields=copyable)
        )


class ConfigPanel(Static):
    """Config summary panel."""

    DEFAULT_CSS = """
    ConfigPanel {
        height: auto;
        border: solid grey;
        padding: 0 1;
        display: none;
    }
    ConfigPanel.visible {
        display: block;
    }
    ConfigPanel .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "inspect", "Inspect", show=True),
        Binding("i", "inspect", "Inspect", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._last_config: OrchestratorConfig | None = None

    def compose(self) -> ComposeResult:
        yield Label("Config (Enter/i to inspect)", classes="panel-title")
        yield Label("", id="config-detail")

    def show_no_project(self) -> None:
        self.query_one("#config-detail", Label).update(NO_PROJECT_PLACEHOLDER)

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        self._last_config = snap.config
        cfg = snap.config
        lines = [
            f"Base branch: {cfg.base_branch}",
            f"Auto push: {cfg.auto_push}",
            f"Amp mode: {cfg.amp_mode}",
            f"Summary: {cfg.summary_mode}",
        ]
        if cfg.verification_commands:
            lines.append(f"Verify: {', '.join(cfg.verification_commands)}")
        self.query_one("#config-detail", Label).update("\n".join(lines))

    def action_inspect(self) -> None:
        self._show_inspect()

    def _show_inspect(self) -> None:
        cfg = self._last_config
        if not cfg:
            return
        title = "Configuration"
        lines = [
            f"[bold]Base branch:[/] {cfg.base_branch}",
            f"[bold]Max workers:[/] {cfg.max_workers}",
            f"[bold]Auto push:[/] {cfg.auto_push}",
            f"[bold]Require clean worktree:[/] {cfg.require_clean_worktree}",
            f"[bold]Amp mode:[/] {cfg.amp_mode}",
            f"[bold]Summary mode:[/] {cfg.summary_mode}",
            f"[bold]Summary amp mode:[/] {cfg.summary_amp_mode}",
            f"[bold]Use decomposition preflight:[/] {cfg.use_decomposition_preflight}",
            f"[bold]Enable evaluation:[/] {cfg.enable_evaluation}",
            f"[bold]Evaluation mode:[/] {cfg.evaluation_mode or 'default'}",
            f"[bold]Evaluation timeout:[/] {cfg.evaluation_timeout}s",
            f"[bold]Context window warn threshold:[/] {cfg.context_window_warn_threshold}",
        ]
        if cfg.verification_commands:
            lines.append(f"\n[bold]Verification commands:[/]")
            for cmd in cfg.verification_commands:
                lines.append(f"  • {cmd}")
        else:
            lines.append(f"\n[bold]Verification commands:[/] (none)")
        from amp_orchestrator.tui.modals import InspectModal

        self.app.push_screen(InspectModal(title=title, body="\n".join(lines)))


_ACTION_ENABLED: dict[str, set[OrchestratorMode]] = {
    "start": {OrchestratorMode.idle, OrchestratorMode.paused},
    "pause": {OrchestratorMode.running},
    "resume": {OrchestratorMode.paused},
    "stop": {OrchestratorMode.running, OrchestratorMode.pause_requested},
}


class ControlsPanel(Static):
    """Controls panel with start/pause/resume/stop buttons."""

    DEFAULT_CSS = """
    ControlsPanel {
        height: auto;
        border: solid grey;
        padding: 0 1;
    }
    ControlsPanel .panel-title {
        text-style: bold;
    }
    #controls-buttons {
        height: auto;
    }
    #controls-buttons Button {
        margin: 0 1 0 0;
        min-width: 10;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Controls", classes="panel-title")
        with Horizontal(id="controls-buttons"):
            yield Button("Start", id="btn-start", variant="success")
            yield Button("Pause", id="btn-pause", variant="warning")
            yield Button("Resume", id="btn-resume", variant="primary")
            yield Button("Stop", id="btn-stop", variant="error")

    def show_no_project(self) -> None:
        for btn_id in ("#btn-start", "#btn-pause", "#btn-resume", "#btn-stop"):
            btn = self.query_one(btn_id, Button)
            btn.disabled = True
            btn.tooltip = "No project detected"

    def disable_all(self) -> None:
        """Disable all control buttons (used during in-flight actions)."""
        for btn_id in ("#btn-start", "#btn-pause", "#btn-resume", "#btn-stop"):
            self.query_one(btn_id, Button).disabled = True

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        mode = snap.state.mode
        for action, btn_id in [
            ("start", "#btn-start"),
            ("pause", "#btn-pause"),
            ("resume", "#btn-resume"),
            ("stop", "#btn-stop"),
        ]:
            btn = self.query_one(btn_id, Button)
            btn.disabled = mode not in _ACTION_ENABLED[action]


class QueueTable(Static):
    """Ready queue table with sort and search/filter support."""

    DEFAULT_CSS = """
    QueueTable {
        height: 2fr;
        border: solid grey;
        padding: 0 1;
    }
    QueueTable .panel-title {
        text-style: bold;
    }
    QueueTable .filter-bar {
        height: auto;
        display: none;
    }
    QueueTable .filter-bar.visible {
        display: block;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "inspect", "Inspect", show=True),
        Binding("i", "inspect", "Inspect", show=False),
        Binding("o", "cycle_sort", "Sort", show=True),
        Binding("slash", "toggle_filter", "Filter", show=True),
    ]

    # Sort modes cycle: priority (default) → age-newest → age-oldest
    _SORT_MODES = ("priority", "age_newest", "age_oldest")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._issues: list[BdIssue] = []
        self._filtered_issues: list[BdIssue] = []
        self._row_key: list[str] = []
        self._sort_mode: str = "priority"
        self._filter_text: str = ""

    def compose(self) -> ComposeResult:
        yield Label("Ready Queue (Enter/i inspect, o sort, / filter)", classes="panel-title")
        yield Input(placeholder="Filter by issue ID…", id="queue-filter", classes="filter-bar")
        yield DataTable(id="queue-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.add_columns("Pri", "ID", "Title", "Created")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "queue-filter":
            self._filter_text = event.value.strip()
            self._row_key = []  # force re-render
            self._rebuild_table()

    def show_no_project(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.clear()
        table.add_row("-", "-", NO_PROJECT_PLACEHOLDER, "-")

    def _sort_issues(self, issues: list[BdIssue]) -> list[BdIssue]:
        """Sort issues based on current sort mode."""
        if self._sort_mode == "age_newest":
            return sorted(issues, key=lambda i: i.created, reverse=True)
        if self._sort_mode == "age_oldest":
            return sorted(issues, key=lambda i: i.created)
        # Default: priority (lower number = higher priority, 0 treated as lowest)
        from amp_orchestrator.queue import _sort_key
        return sorted(issues, key=_sort_key)

    def _apply_filter(self, issues: list[BdIssue]) -> list[BdIssue]:
        """Filter issues by ID or title substring."""
        if not self._filter_text:
            return issues
        needle = self._filter_text.lower()
        return [i for i in issues if needle in i.id.lower() or needle in i.title.lower()]

    def _rebuild_table(self) -> None:
        """Re-render the table with current sort and filter settings."""
        table = self.query_one("#queue-datatable", DataTable)
        saved_cursor = table.cursor_row
        filtered = self._apply_filter(self._issues)
        self._filtered_issues = self._sort_issues(filtered)
        table.clear()
        if not self._filtered_issues:
            if self._filter_text:
                table.add_row("-", "-", f"[italic]No matches for '{self._filter_text}'[/]", "-")
            else:
                table.add_row("-", "-", "[italic]No issues in queue[/]", "-")
            return
        for issue in self._filtered_issues:
            pri = str(issue.priority) if issue.priority else "-"
            table.add_row(pri, issue.id, issue.title, issue.created)
        if table.row_count > 0:
            table.move_cursor(row=min(saved_cursor, table.row_count - 1))

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        new_keys = [issue.id for issue in snap.ready_issues]
        if new_keys == self._row_key:
            return
        self._issues = list(snap.ready_issues)
        self._row_key = new_keys
        self._rebuild_table()

    def action_cycle_sort(self) -> None:
        """Cycle through sort modes: priority → newest → oldest."""
        idx = self._SORT_MODES.index(self._sort_mode)
        self._sort_mode = self._SORT_MODES[(idx + 1) % len(self._SORT_MODES)]
        sort_labels = {"priority": "Priority", "age_newest": "Newest first", "age_oldest": "Oldest first"}
        self.query_one(".panel-title", Label).update(
            f"Ready Queue [Sort: {sort_labels[self._sort_mode]}] (o sort, / filter)"
        )
        self._row_key = []  # force re-render
        self._rebuild_table()

    def action_toggle_filter(self) -> None:
        """Toggle the filter input bar."""
        filter_input = self.query_one("#queue-filter", Input)
        filter_input.toggle_class("visible")
        if filter_input.has_class("visible"):
            filter_input.focus()
        else:
            self._filter_text = ""
            filter_input.value = ""
            self._row_key = []
            self._rebuild_table()

    def action_inspect(self) -> None:
        self._show_inspect()

    def _show_inspect(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        if not self._filtered_issues or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._filtered_issues):
            return
        issue = self._filtered_issues[row_idx]
        title = f"Issue: {issue.id}"
        lines = [
            f"[bold]Title:[/] {issue.title}",
            f"[bold]Priority:[/] {issue.priority}",
            f"[bold]Created:[/] {issue.created}",
        ]
        if issue.description:
            lines.append(f"\n[bold]Description:[/]\n{issue.description}")
        if issue.acceptance_criteria:
            lines.append(
                f"\n[bold]Acceptance Criteria:[/]\n{issue.acceptance_criteria}"
            )
        from amp_orchestrator.tui.modals import InspectModal

        self.app.push_screen(InspectModal(title=title, body="\n".join(lines)))


class EventsLog(Static):
    """Live events log with delta-based appending and error-only toggle."""

    DEFAULT_CSS = """
    EventsLog {
        height: 1fr;
        border: solid grey;
        padding: 0 1;
    }
    EventsLog .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("e", "toggle_errors_only", "Errors only", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._seen_count: int = 0
        self._last_event_keys: list[str] = []
        self._errors_only: bool = False
        self._all_events: list[dict] = []

    @staticmethod
    def _event_key(entry: dict) -> str:
        return f"{entry.get('timestamp', '')}:{entry.get('event_type', '')}"

    @staticmethod
    def _format_entry(entry: dict) -> str:
        ts = entry.get("timestamp", "?")
        if "T" in ts:
            ts = _format_run_timestamp(ts)
        etype = entry.get("event_type", "?")
        data = entry.get("data")
        color = EVENT_COLORS.get(etype, "white")
        severity = _event_severity(etype)
        sev_style = _SEVERITY_STYLE.get(severity, "white")
        message = _human_message(etype, data)
        return f"[italic]{ts}[/] [{sev_style}][{severity}][/] [{color}]{message}[/]"

    def _filter_events(self, events: list[dict]) -> list[dict]:
        """Filter events based on current error-only setting."""
        if not self._errors_only:
            return events
        return [e for e in events if _event_severity(e.get("event_type", "")) == "ERR"]

    def compose(self) -> ComposeResult:
        yield Label("Events (e errors only)", classes="panel-title")
        yield RichLog(id="events-richlog", wrap=True, max_lines=200)

    def show_no_project(self) -> None:
        log = self.query_one("#events-richlog", RichLog)
        log.clear()
        log.write(NO_PROJECT_PLACEHOLDER)

    def _rebuild_log(self) -> None:
        """Re-render the log with the current filter."""
        log = self.query_one("#events-richlog", RichLog)
        log.clear()
        filtered = self._filter_events(self._all_events)
        if not filtered:
            if self._errors_only:
                log.write("[italic]No error events[/]")
            else:
                log.write("[italic]No events yet[/]")
            return
        for entry in filtered:
            log.write(self._format_entry(entry))

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        log = self.query_one("#events-richlog", RichLog)
        new_keys = [self._event_key(e) for e in snap.recent_events]
        if new_keys == self._last_event_keys:
            return
        self._all_events = list(snap.recent_events)
        if not snap.recent_events:
            if self._last_event_keys:
                log.clear()
                log.write("[italic]No events yet[/]")
                self._last_event_keys = []
                self._seen_count = 0
            return
        # When errors_only is active, always do a full rebuild
        if self._errors_only:
            self._last_event_keys = new_keys
            self._seen_count = len(snap.recent_events)
            self._rebuild_log()
            return
        # If the log was empty or events were reset, do a full write
        if not self._last_event_keys or new_keys[: len(self._last_event_keys)] != self._last_event_keys:
            log.clear()
            for entry in snap.recent_events:
                log.write(self._format_entry(entry))
        else:
            # Append only new entries (delta)
            for entry in snap.recent_events[len(self._last_event_keys) :]:
                log.write(self._format_entry(entry))
        self._last_event_keys = new_keys
        self._seen_count = len(snap.recent_events)

    def action_toggle_errors_only(self) -> None:
        """Toggle error-only event filter."""
        self._errors_only = not self._errors_only
        label = "Events"
        if self._errors_only:
            label += " [bold red][ERRORS ONLY][/]"
        label += " (e errors only)"
        self.query_one(".panel-title", Label).update(label)
        self._rebuild_log()


class HistoryTable(Static):
    """Run history table with result-type filter cycling and search."""

    _FAILED_RESULTS = frozenset({"failed", "error"})
    _NEEDS_REWORK_RESULTS = frozenset({"needs_rework", "needs_human"})
    # Cycle: all → failed → needs_rework → completed
    _RESULT_FILTER_MODES = ("all", "failed", "needs_rework", "completed")

    DEFAULT_CSS = """
    HistoryTable {
        height: 1fr;
        border: solid grey;
        padding: 0 1;
    }
    HistoryTable .panel-title {
        text-style: bold;
    }
    HistoryTable .filter-bar {
        height: auto;
        display: none;
    }
    HistoryTable .filter-bar.visible {
        display: block;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "inspect", "Inspect", show=True),
        Binding("i", "inspect", "Inspect", show=False),
        Binding("f", "cycle_result_filter", "Result filter", show=True),
        Binding("slash", "toggle_filter", "Filter", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._runs: list[dict] = []
        self._filtered_runs: list[dict] = []
        self._row_key: list[str] = []
        self._last_snap: DashboardSnapshot | None = None
        self._result_filter: str = "all"
        self._filter_text: str = ""

    def compose(self) -> ComposeResult:
        yield Label("Run History (Enter/i inspect, f result filter, / filter)", classes="panel-title")
        yield Input(placeholder="Filter by issue ID…", id="history-filter", classes="filter-bar")
        yield DataTable(id="history-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        table.add_columns("Timestamp", "Issue", "Result", "Branch", "Summary")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "history-filter":
            self._filter_text = event.value.strip()
            self._row_key = []  # force re-render
            self._rebuild_table()

    def show_no_project(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        table.clear()
        table.add_row("-", "-", NO_PROJECT_PLACEHOLDER, "-", "-")

    @staticmethod
    def _run_key(run: dict) -> str:
        return f"{run.get('timestamp', '')}:{run.get('issue_id', '')}:{run.get('result', '')}"

    def _apply_filters(self, runs: list[dict]) -> list[dict]:
        """Apply result-type and text filters."""
        result = runs
        if self._result_filter == "failed":
            result = [r for r in result if r.get("result", "") in self._FAILED_RESULTS]
        elif self._result_filter == "needs_rework":
            result = [r for r in result if r.get("result", "") in self._NEEDS_REWORK_RESULTS
                      or self._has_rework_category(r)]
        elif self._result_filter == "completed":
            result = [r for r in result if r.get("result", "") == "completed"]
        if self._filter_text:
            needle = self._filter_text.lower()
            result = [r for r in result if needle in r.get("issue_id", "").lower()]
        return result

    def _has_rework_category(self, run: dict) -> bool:
        """Check if a run's issue has a needs_rework failure category."""
        snap = self._last_snap
        if not snap:
            return False
        issue_id = run.get("issue_id", "")
        failure_info = snap.state.issue_failures.get(issue_id)
        if failure_info and isinstance(failure_info, dict):
            return failure_info.get("category", "") == "issue_needs_rework"
        return False

    def _rebuild_table(self) -> None:
        """Re-render the table with current filters."""
        snap = self._last_snap
        table = self.query_one("#history-datatable", DataTable)
        saved_cursor = table.cursor_row
        self._filtered_runs = self._apply_filters(self._runs)
        table.clear()
        if not self._filtered_runs:
            if self._result_filter != "all" or self._filter_text:
                table.add_row("-", "-", "[italic]No matching runs[/]", "-", "-")
            else:
                table.add_row("-", "-", "[italic]No run history[/]", "-", "-")
            return
        for run in self._filtered_runs:
            ts = run.get("timestamp", "")
            if "T" in ts:
                ts = _format_run_timestamp(ts)
            issue_id = run.get("issue_id", "")
            raw_result = run.get("result", "")
            result_colors = {"completed": "green", "failed": "bold red", "error": "bold red"}
            rc = result_colors.get(raw_result, "white")
            icon = _RESULT_ICONS.get(raw_result, "·")
            # Check if this issue has a failure category for richer coloring
            if snap:
                failure_info = snap.state.issue_failures.get(issue_id)
            else:
                failure_info = None
            if failure_info and isinstance(failure_info, dict):
                cat = failure_info.get("category", "")
                category_colors = {
                    "transient_external": "bright_yellow",
                    "stale_or_conflicted": "bold bright_yellow",
                    "issue_needs_rework": "bold red",
                    "blocked_by_dependency": "orchid1",
                    "fatal_run_error": "bold red",
                }
                rc = category_colors.get(cat, rc)
                icon = _CATEGORY_ICONS.get(cat, icon)
            result_label = _CATEGORY_LABELS.get(
                failure_info.get("category", "") if failure_info and isinstance(failure_info, dict) else "",
                raw_result,
            )
            result = f"[{rc}]{icon} {result_label}[/]"
            branch = run.get("branch", "")
            summary = run.get("summary", "")
            table.add_row(ts, issue_id, result, branch, summary)
        if table.row_count > 0:
            table.move_cursor(row=min(saved_cursor, table.row_count - 1))

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        runs = list(reversed(snap.state.run_history))
        new_keys = [self._run_key(r) for r in runs]
        if new_keys == self._row_key:
            return
        self._runs = runs
        self._row_key = new_keys
        self._last_snap = snap
        self._rebuild_table()

    def action_cycle_result_filter(self) -> None:
        """Cycle through result filter modes: all → failed → needs_rework → completed."""
        idx = self._RESULT_FILTER_MODES.index(self._result_filter)
        self._result_filter = self._RESULT_FILTER_MODES[(idx + 1) % len(self._RESULT_FILTER_MODES)]
        filter_labels = {
            "all": "",
            "failed": " [bold red][FAILED ONLY][/]",
            "needs_rework": " [bold bright_yellow][NEEDS REWORK][/]",
            "completed": " [bold green][COMPLETED][/]",
        }
        label = "Run History" + filter_labels[self._result_filter]
        label += " (f result filter, / filter)"
        self.query_one(".panel-title", Label).update(label)
        self._row_key = []  # force re-render
        self._rebuild_table()

    def action_toggle_filter(self) -> None:
        """Toggle the filter input bar."""
        filter_input = self.query_one("#history-filter", Input)
        filter_input.toggle_class("visible")
        if filter_input.has_class("visible"):
            filter_input.focus()
        else:
            self._filter_text = ""
            filter_input.value = ""
            self._row_key = []
            self._rebuild_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_inspect()

    def action_inspect(self) -> None:
        self._show_inspect()

    def _show_inspect(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        if not self._filtered_runs or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._filtered_runs):
            return
        run = self._filtered_runs[row_idx]
        title = f"Run: {run.get('issue_id', '?')}"
        lines = [
            f"[bold]Issue:[/] {run.get('issue_id', '')}",
            f"[bold]Result:[/] {run.get('result', '')}",
            f"[bold]Timestamp:[/] {run.get('timestamp', '')}",
        ]
        if run.get("branch"):
            lines.append(f"[bold]Branch:[/] {run['branch']}")
        if run.get("worktree_path"):
            lines.append(f"[bold]Worktree:[/] {run['worktree_path']}")
        if run.get("thread_id"):
            from amp_orchestrator.tui.modals import _THREAD_URL_PREFIX

            thread_url = f"{_THREAD_URL_PREFIX}{run['thread_id']}"
            lines.append(f"[bold]Thread:[/] {run['thread_id']}")
            lines.append(f"[bold]Thread URL:[/] {thread_url}")
        if run.get("summary"):
            lines.append(f"\n[bold]Summary:[/]\n{run['summary']}")
        from amp_orchestrator.tui.modals import CopyableField, InspectModal, _THREAD_URL_PREFIX

        copyable: list[CopyableField] = []
        if run.get("thread_id"):
            copyable.append(CopyableField(label="Thread ID", value=run["thread_id"], key="t"))
            copyable.append(
                CopyableField(
                    label="Thread URL",
                    value=f"{_THREAD_URL_PREFIX}{run['thread_id']}",
                    key="u",
                )
            )
        if run.get("branch"):
            copyable.append(CopyableField(label="Branch", value=run["branch"], key="b"))
        if run.get("worktree_path"):
            copyable.append(CopyableField(label="Worktree", value=run["worktree_path"], key="w"))

        self.app.push_screen(
            InspectModal(title=title, body="\n".join(lines), copyable_fields=copyable)
        )
