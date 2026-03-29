"""Dashboard widgets for the amp-orchestrator TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.widgets import Button, DataTable, Label, RichLog, Static

from amp_orchestrator.queue import BdIssue
from amp_orchestrator.state import OrchestratorMode
from amp_orchestrator.tui.snapshot import DashboardSnapshot

MODE_STYLES: dict[OrchestratorMode, tuple[str, str]] = {
    OrchestratorMode.running: ("green", "● RUNNING"),
    OrchestratorMode.paused: ("dark_orange", "⏸ PAUSED"),
    OrchestratorMode.pause_requested: ("yellow", "⏸ PAUSE REQUESTED"),
    OrchestratorMode.stopping: ("orange1", "■ STOPPING"),
    OrchestratorMode.error: ("red", "✖ ERROR"),
    OrchestratorMode.idle: ("grey", "○ IDLE"),
}

NO_PROJECT_MSG = "[bold red]⚠ Not connected to repo/state directory[/]"
NO_PROJECT_PLACEHOLDER = "[dim]Not available — no project detected[/]"

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
    StatusPanel.error-state {
        border: solid red;
    }
    StatusPanel .panel-title {
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Status", classes="panel-title")
        yield Label("[grey]○ IDLE[/]", id="mode-badge")
        yield Label("Queue: 0 issue(s)", id="queue-count")
        yield Label("", id="last-completed")
        yield Label("", id="last-error")

    def show_no_project(self) -> None:
        self.query_one("#mode-badge", Label).update(
            "[bold red]⚠ NOT CONNECTED[/]"
        )
        self.query_one("#queue-count", Label).update(NO_PROJECT_PLACEHOLDER)
        self.query_one("#last-completed", Label).update("")
        self.query_one("#last-error", Label).update("")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        color, text = MODE_STYLES.get(
            snap.state.mode, ("grey", snap.state.mode.value)
        )
        badge = self.query_one("#mode-badge", Label)
        badge.update(f"[{color}]{text}[/]")

        if snap.state.mode == OrchestratorMode.error:
            self.add_class("error-state")
        else:
            self.remove_class("error-state")

        self.query_one("#queue-count", Label).update(
            f"Queue: {len(snap.ready_issues)} issue(s)"
        )

        lc = self.query_one("#last-completed", Label)
        if snap.state.last_completed_issue:
            lc.update(f"[green]Last completed: {snap.state.last_completed_issue}[/]")
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
        yield Label("[dim]No active issue[/]", id="active-detail")

    def show_no_project(self) -> None:
        self.query_one("#active-detail", Label).update(NO_PROJECT_PLACEHOLDER)

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
            detail.update("[dim]No active issue[/]")


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

    def show_no_project(self) -> None:
        self.query_one("#config-detail", Label).update(NO_PROJECT_PLACEHOLDER)

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

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._issues: list[BdIssue] = []

    def compose(self) -> ComposeResult:
        yield Label("Ready Queue", classes="panel-title")
        yield DataTable(id="queue-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.add_columns("Pri", "ID", "Title", "Created")

    def show_no_project(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.clear()
        table.add_row("-", "-", NO_PROJECT_PLACEHOLDER, "-")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        self._issues = list(snap.ready_issues)
        table = self.query_one("#queue-datatable", DataTable)
        table.clear()
        if not snap.ready_issues:
            table.add_row("-", "-", "[dim]No issues in queue[/]", "-")
            return
        for issue in snap.ready_issues:
            pri = str(issue.priority) if issue.priority else "-"
            table.add_row(pri, issue.id, issue.title, issue.created)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_inspect()

    def on_key(self, event: Key) -> None:
        if event.key == "i":
            self._show_inspect()

    def _show_inspect(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        if not self._issues or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._issues):
            return
        issue = self._issues[row_idx]
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

    def show_no_project(self) -> None:
        log = self.query_one("#events-richlog", RichLog)
        log.clear()
        log.write(NO_PROJECT_PLACEHOLDER)

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        log = self.query_one("#events-richlog", RichLog)
        log.clear()
        if not snap.recent_events:
            log.write("[dim]No events yet[/]")
            return
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

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._runs: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Label("Run History", classes="panel-title")
        yield DataTable(id="history-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        table.add_columns("Timestamp", "Issue", "Result", "Branch", "Summary")

    def show_no_project(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        table.clear()
        table.add_row("-", "-", NO_PROJECT_PLACEHOLDER, "-", "-")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        self._runs = list(reversed(snap.state.run_history))
        table = self.query_one("#history-datatable", DataTable)
        table.clear()
        if not self._runs:
            table.add_row("-", "-", "[dim]No run history[/]", "-", "-")
            return
        for run in self._runs:
            ts = run.get("timestamp", "")
            if "T" in ts:
                ts = ts.split("T")[0]
            issue_id = run.get("issue_id", "")
            raw_result = run.get("result", "")
            result_colors = {"completed": "green", "failed": "red", "error": "red"}
            rc = result_colors.get(raw_result, "white")
            result = f"[{rc}]{raw_result}[/]"
            branch = run.get("branch", "")
            summary = run.get("summary", "")
            table.add_row(ts, issue_id, result, branch, summary)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_inspect()

    def on_key(self, event: Key) -> None:
        if event.key == "i":
            self._show_inspect()

    def _show_inspect(self) -> None:
        table = self.query_one("#history-datatable", DataTable)
        if not self._runs or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._runs):
            return
        run = self._runs[row_idx]
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
        if run.get("summary"):
            lines.append(f"\n[bold]Summary:[/]\n{run['summary']}")
        from amp_orchestrator.tui.modals import InspectModal

        self.app.push_screen(InspectModal(title=title, body="\n".join(lines)))
