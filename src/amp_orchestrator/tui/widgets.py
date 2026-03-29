"""Dashboard widgets for the amp-orchestrator TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Label, RichLog, Static

from amp_orchestrator.state import OrchestratorMode
from amp_orchestrator.tui.snapshot import DashboardSnapshot

MODE_STYLES: dict[OrchestratorMode, tuple[str, str]] = {
    OrchestratorMode.running: ("green", "● RUNNING"),
    OrchestratorMode.paused: ("yellow", "⏸ PAUSED"),
    OrchestratorMode.pause_requested: ("yellow", "⏸ PAUSE REQUESTED"),
    OrchestratorMode.stopping: ("yellow", "■ STOPPING"),
    OrchestratorMode.error: ("red", "✖ ERROR"),
    OrchestratorMode.idle: ("grey", "○ IDLE"),
}

EVENT_COLORS: dict[str, str] = {
    "error": "red",
    "issue_selected": "cyan",
    "amp_started": "blue",
    "amp_finished": "green",
    "merge_attempt": "magenta",
    "issue_closed": "green bold",
    "pause_requested": "yellow",
    "stop_requested": "yellow",
    "state_changed": "grey",
    "verification_run": "blue",
}


class StatusPanel(Static):
    """Status panel: mode badge, queue count, last completed/error."""

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        border: solid grey;
        padding: 0 1;
    }
    StatusPanel .panel-title {
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Status", classes="panel-title")
        yield Label("○ IDLE", id="mode-badge")
        yield Label("Queue: 0 issue(s)", id="queue-count")
        yield Label("", id="last-completed")
        yield Label("", id="last-error")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        color, text = MODE_STYLES.get(
            snap.state.mode, ("grey", snap.state.mode.value)
        )
        badge = self.query_one("#mode-badge", Label)
        badge.update(f"[{color}]{text}[/]")

        self.query_one("#queue-count", Label).update(
            f"Queue: {len(snap.ready_issues)} issue(s)"
        )

        lc = self.query_one("#last-completed", Label)
        if snap.state.last_completed_issue:
            lc.update(f"Last completed: {snap.state.last_completed_issue}")
        else:
            lc.update("")

        le = self.query_one("#last-error", Label)
        if snap.state.last_error:
            le.update(f"[red]Last error: {snap.state.last_error}[/]")
        else:
            le.update("")


class ActiveIssuePanel(Static):
    """Active issue panel: id, title, branch, worktree."""

    DEFAULT_CSS = """
    ActiveIssuePanel {
        height: auto;
        border: solid grey;
        padding: 0 1;
    }
    ActiveIssuePanel .panel-title {
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Active Issue", classes="panel-title")
        yield Label("No active issue", id="active-detail")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        detail = self.query_one("#active-detail", Label)
        if snap.state.active_issue_id:
            lines = [f"[bold]{snap.state.active_issue_id}[/]"]
            if snap.state.active_issue_title:
                lines.append(f"  {snap.state.active_issue_title}")
            if snap.state.active_branch:
                lines.append(f"  Branch: {snap.state.active_branch}")
            if snap.state.active_worktree_path:
                lines.append(f"  Worktree: {snap.state.active_worktree_path}")
            detail.update("\n".join(lines))
        else:
            detail.update("No active issue")


class ConfigPanel(Static):
    """Config summary panel."""

    DEFAULT_CSS = """
    ConfigPanel {
        height: auto;
        border: solid grey;
        padding: 0 1;
    }
    ConfigPanel .panel-title {
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Config", classes="panel-title")
        yield Label("", id="config-detail")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        cfg = snap.config
        lines = [
            f"Base branch: {cfg.base_branch}",
            f"Auto push: {cfg.auto_push}",
            f"Amp mode: {cfg.amp_mode}",
        ]
        if cfg.verification_commands:
            lines.append(f"Verify: {', '.join(cfg.verification_commands)}")
        self.query_one("#config-detail", Label).update("\n".join(lines))


class QueueTable(Static):
    """Ready queue table."""

    DEFAULT_CSS = """
    QueueTable {
        height: 1fr;
        border: solid grey;
        padding: 0 1;
    }
    QueueTable .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Label("Ready Queue", classes="panel-title")
        yield DataTable(id="queue-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.add_columns("Pri", "ID", "Title", "Created")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.clear()
        for issue in snap.ready_issues:
            pri = str(issue.priority) if issue.priority else "-"
            table.add_row(pri, issue.id, issue.title, issue.created)


class EventsLog(Static):
    """Live events log with auto-scrolling."""

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

    def compose(self) -> ComposeResult:
        yield Label("Events", classes="panel-title")
        yield RichLog(id="events-richlog", wrap=True, max_lines=200)

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        log = self.query_one("#events-richlog", RichLog)
        log.clear()
        for entry in snap.recent_events:
            ts = entry.get("timestamp", "?")
            if "T" in ts:
                ts = ts.split("T")[1][:8]
            etype = entry.get("event_type", "?")
            data = entry.get("data")
            color = EVENT_COLORS.get(etype, "white")
            line = f"[dim]{ts}[/] [{color}]{etype}[/]"
            if data:
                line += f"  {data}"
            log.write(line)


class HistoryTable(Static):
    """Run history table."""

    DEFAULT_CSS = """
    HistoryTable {
        height: 1fr;
        border: solid grey;
        padding: 0 1;
    }
    HistoryTable .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Label("Run History", classes="panel-title")
        yield DataTable(id="history-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        table.add_columns("Timestamp", "Issue", "Result", "Branch", "Summary")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        table = self.query_one("#history-datatable", DataTable)
        table.clear()
        for run in reversed(snap.state.run_history):
            ts = run.get("timestamp", "")
            if "T" in ts:
                ts = ts.split("T")[0]
            issue_id = run.get("issue_id", "")
            result = run.get("result", "")
            branch = run.get("branch", "")
            summary = run.get("summary", "")
            table.add_row(ts, issue_id, result, branch, summary)
