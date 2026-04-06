"""Full-screen active issue inspection with workflow step navigation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Label, Static

from orc.events import EventLog
from orc.state import OrchestratorState
from orc.tui.event_helpers import (
    EVENT_COLORS,
    _SEVERITY_STYLE,
    _event_severity,
    _human_message,
)
from orc.tui.modals import AmpStreamModal, _THREAD_URL_PREFIX, build_thread_continue_cmd
from orc.workflow import PHASE_INFO, PHASE_ORDER, phase_label


@dataclass
class ActiveWorkflowStep:
    phase: str
    label: str
    status: Literal["done", "active", "pending", "skipped"]
    has_amp_log: bool = False


@dataclass
class ActiveIssueModel:
    issue_id: str
    issue_title: str
    started_at: str
    branch: str | None
    worktree_path: str | None
    current_phase: str | None
    amp_log_path: str | None
    thread_id: str | None
    events: list[dict] = field(default_factory=list)
    timeline: list[ActiveWorkflowStep] = field(default_factory=list)


def build_active_model(
    state: OrchestratorState,
    state_dir: Path,
) -> ActiveIssueModel | None:
    """Build an ActiveIssueModel from the current active run."""
    if not state.active_run:
        return None

    run = state.active_run
    issue_id = run["issue_id"]
    current_phase = run.get("stage")

    # Load issue-specific events
    events = [
        e
        for e in EventLog(state_dir).all()
        if isinstance(e.get("data"), dict) and e["data"].get("issue_id") == issue_id
    ]

    # Determine thread_id from amp_result or events
    thread_id = None
    amp_result = run.get("amp_result")
    if amp_result and isinstance(amp_result, dict):
        thread_id = amp_result.get("thread_id")
    if not thread_id:
        for e in reversed(events):
            if e.get("event_type") == "amp_finished":
                data = e.get("data", {})
                if data.get("thread_id"):
                    thread_id = data["thread_id"]
                    break

    amp_log_path = run.get("amp_log_path")
    timeline = _build_active_timeline(current_phase, amp_log_path)

    return ActiveIssueModel(
        issue_id=issue_id,
        issue_title=run.get("issue_title", ""),
        started_at=run.get("updated_at", ""),
        branch=run.get("branch"),
        worktree_path=run.get("worktree_path"),
        current_phase=current_phase,
        amp_log_path=amp_log_path,
        thread_id=thread_id,
        events=events,
        timeline=timeline,
    )


def _build_active_timeline(
    current_phase: str | None,
    amp_log_path: str | None,
) -> list[ActiveWorkflowStep]:
    """Build the workflow step timeline for an active issue."""
    phase_keys = [p.value for p in PHASE_ORDER]
    try:
        current_idx = phase_keys.index(current_phase) if current_phase else -1
    except ValueError:
        current_idx = -1

    # Phases that are only shown if they're the active or done phase
    optional_phases = {
        "summary_extraction", "dirty_worktree_check", "conflict_resolution",
        "parent_promotion", "claim_release_pending", "merge_recovery",
    }

    steps: list[ActiveWorkflowStep] = []
    for i, phase in enumerate(PHASE_ORDER):
        info = PHASE_INFO[phase]
        key = phase.value

        if current_idx < 0:
            status: Literal["done", "active", "pending", "skipped"] = "pending"
        elif i < current_idx:
            status = "done"
        elif i == current_idx:
            status = "active"
        else:
            if key in optional_phases:
                status = "skipped"
            else:
                status = "pending"

        # Skip optional phases that haven't been reached
        if status == "skipped":
            continue

        has_log = key == "amp_running" and amp_log_path is not None

        steps.append(ActiveWorkflowStep(
            phase=key,
            label=info.label,
            status=status,
            has_amp_log=has_log,
        ))

    return steps


# -- Screen --------------------------------------------------------------------

_STATUS_GLYPHS = {
    "done": "[green]✔[/]",
    "active": "[bold dodger_blue2]●[/]",
    "pending": "[dim]○[/]",
    "skipped": "[dim]↷[/]",
}

# Phase-to-color for the stage display
_PHASE_COLORS: dict[str, str] = {
    "preflight": "bright_yellow",
    "already_implemented_check": "bright_yellow",
    "worktree_created": "cyan",
    "claimed": "cyan",
    "amp_running": "dodger_blue2",
    "amp_finished": "dodger_blue2",
    "summary_extraction": "orchid1",
    "post_merge_eval": "orchid1",
    "dirty_worktree_check": "bright_yellow",
    "evaluation_running": "orchid1",
    "ready_to_merge": "green",
    "merge_running": "green",
    "merge_recovery": "bright_yellow",
    "conflict_resolution": "bright_yellow",
    "parent_promotion": "green",
    "claim_release_pending": "dim",
}


class ActiveIssueInspectScreen(Screen[None]):
    """Full-screen workflow view for the currently active issue."""

    DEFAULT_CSS = """
    ActiveIssueInspectScreen {
        background: #0b0f14;
        color: #d7dde8;
    }
    #ai-root {
        height: 1fr;
    }
    #ai-header {
        height: auto;
        padding: 0 1;
        background: #111827;
    }
    #ai-header-title {
        text-style: bold;
    }
    #ai-header-meta {
        color: #93a4b8;
    }
    #ai-main {
        height: 1fr;
    }
    #ai-sidebar {
        width: 40;
        border-right: solid #263041;
        padding: 0 1;
    }
    #ai-timeline-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
        color: #94a3b8;
    }
    #ai-timeline {
        height: auto;
    }
    #ai-timeline-table {
        height: auto;
        max-height: 20;
    }
    #ai-links {
        height: auto;
        margin-top: 1;
        color: #93a4b8;
    }
    #ai-content {
        width: 1fr;
        padding: 0 1;
    }
    #ai-events-scroll {
        height: 1fr;
        min-height: 8;
    }
    #ai-events-table {
        height: 1fr;
        min-height: 6;
    }
    #ai-hints {
        height: auto;
        padding: 0 1;
        background: #0f172a;
        color: #93a4b8;
    }
    .ai-section-title {
        text-style: bold;
        margin-top: 1;
        color: #8fb2ff;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("a", "open_amp_log", "Amp log"),
        Binding("b", "copy_branch", "Copy branch", show=False),
        Binding("w", "copy_worktree", "Copy worktree", show=False),
        Binding("t", "copy_thread_id", "Copy thread ID", show=False),
        Binding("u", "copy_thread_url", "Copy thread URL", show=False),
        Binding("d", "copy_debug_cmd", "Copy debug cmd", show=False),
    ]

    def __init__(self, model: ActiveIssueModel, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model = model

    def compose(self) -> ComposeResult:
        m = self._model
        with Vertical(id="ai-root"):
            # Header
            with Vertical(id="ai-header"):
                yield Label(f"Active: {m.issue_id}", id="ai-header-title")
                meta_parts = []
                if m.issue_title:
                    meta_parts.append(m.issue_title)
                if m.current_phase:
                    color = _PHASE_COLORS.get(m.current_phase, "white")
                    phase_lbl = phase_label(m.current_phase)
                    meta_parts.append(f"[{color}]{phase_lbl}[/]")
                yield Label(" — ".join(meta_parts) if meta_parts else "", id="ai-header-meta")

            # Main area: sidebar + content
            with Horizontal(id="ai-main"):
                with Vertical(id="ai-sidebar"):
                    yield Label("Workflow Steps", id="ai-timeline-title")
                    yield DataTable(id="ai-timeline-table", cursor_type="row")
                    yield Static(self._render_links(), id="ai-links")

                with Vertical(id="ai-content"):
                    yield Label("Events", classes="ai-section-title")
                    with VerticalScroll(id="ai-events-scroll"):
                        yield DataTable(id="ai-events-table", cursor_type="row")

            yield Static(self._render_hints(), id="ai-hints")

    def on_mount(self) -> None:
        # Populate timeline table
        timeline_table = self.query_one("#ai-timeline-table", DataTable)
        timeline_table.add_columns("", "Step", "Phase")
        for step in self._model.timeline:
            glyph = _STATUS_GLYPHS.get(step.status, "?")
            phase_color = _PHASE_COLORS.get(step.phase, "white")
            label = step.label
            if step.has_amp_log and step.status == "active":
                label += " [dim](a=log)[/]"
            timeline_table.add_row(glyph, f"[{phase_color}]{label}[/]", step.phase)

        # Populate events table
        events_table = self.query_one("#ai-events-table", DataTable)
        events_table.add_columns("Time", "Sev", "Phase", "Type", "Message")
        for event in self._model.events:
            ts = event.get("timestamp", "")
            if "T" in ts:
                ts = ts.split("T")[1].split(".")[0] if "." in ts.split("T")[1] else ts.split("T")[1].split("+")[0]
            event_type = event.get("event_type", "")
            sev = _event_severity(event_type)
            sev_style = _SEVERITY_STYLE.get(sev, "bright_white")
            color = EVENT_COLORS.get(event_type, "white")
            phase_str = phase_label(event.get("phase"))
            message = _human_message(event_type, event.get("data"))
            if len(message) > 100:
                message = message[:97] + "…"
            events_table.add_row(
                ts,
                f"[{sev_style}]{sev}[/]",
                phase_str,
                f"[{color}]{event_type}[/]",
                message,
            )
        if events_table.row_count == 0:
            events_table.add_row("-", "-", "-", "-", "[italic]No events yet[/]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open amp log when the amp_running step is selected in the timeline."""
        table = event.data_table
        if table.id != "ai-timeline-table":
            return
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._model.timeline):
            step = self._model.timeline[row_idx]
            if step.has_amp_log and self._model.amp_log_path:
                self._open_amp_log()

    def _render_links(self) -> str:
        m = self._model
        lines: list[str] = []
        if m.branch:
            lines.append(f"[bold]Branch:[/] {m.branch}")
        if m.worktree_path:
            lines.append(f"[bold]Worktree:[/] {m.worktree_path}")
        if m.thread_id:
            lines.append(f"[bold]Thread:[/] {m.thread_id}")
            lines.append(f"[bold]URL:[/] {_THREAD_URL_PREFIX}{m.thread_id}")
        if m.amp_log_path:
            lines.append(f"[bold]Amp log:[/] {m.amp_log_path}")
        return "\n".join(lines) if lines else "[dim]No links yet[/]"

    def _render_hints(self) -> str:
        parts: list[str] = ["[bold]q[/]/[bold]Esc[/] close"]
        if self._model.amp_log_path:
            parts.append("[bold]a[/] amp log")
        if self._model.branch:
            parts.append("[bold]b[/] copy branch")
        if self._model.worktree_path:
            parts.append("[bold]w[/] copy worktree")
        if self._model.thread_id:
            parts.append("[bold]t[/] copy thread ID")
            parts.append("[bold]u[/] copy thread URL")
            parts.append("[bold]d[/] copy debug cmd")
        return "  ".join(parts)

    # -- Actions ---------------------------------------------------------------

    def _open_amp_log(self) -> None:
        if not self._model.amp_log_path:
            self.app.notify("No amp log available", severity="warning")
            return
        if not Path(self._model.amp_log_path).exists():
            self.app.notify("Amp log file not found on disk", severity="warning")
            return
        header_lines = [f"Issue: {self._model.issue_id}"]
        if self._model.branch:
            header_lines.append(f"Branch: {self._model.branch}")
        if self._model.current_phase:
            header_lines.append(f"Phase: {phase_label(self._model.current_phase)}")
        self.app.push_screen(
            AmpStreamModal(
                title=f"Live: {self._model.issue_id}",
                log_path=self._model.amp_log_path,
                header_lines=header_lines,
            )
        )

    def action_open_amp_log(self) -> None:
        self._open_amp_log()

    def action_copy_branch(self) -> None:
        if self._model.branch:
            self.app.copy_to_clipboard(self._model.branch)
            self.app.notify(f"Copied branch: {self._model.branch}", timeout=2)

    def action_copy_worktree(self) -> None:
        if self._model.worktree_path:
            self.app.copy_to_clipboard(self._model.worktree_path)
            self.app.notify(f"Copied worktree: {self._model.worktree_path}", timeout=2)

    def action_copy_thread_id(self) -> None:
        if self._model.thread_id:
            self.app.copy_to_clipboard(self._model.thread_id)
            self.app.notify(f"Copied thread ID: {self._model.thread_id}", timeout=2)

    def action_copy_thread_url(self) -> None:
        if self._model.thread_id:
            url = f"{_THREAD_URL_PREFIX}{self._model.thread_id}"
            self.app.copy_to_clipboard(url)
            self.app.notify("Copied thread URL", timeout=2)

    def action_copy_debug_cmd(self) -> None:
        if self._model.thread_id:
            cmd = build_thread_continue_cmd(self._model.thread_id, self._model.worktree_path)
            self.app.copy_to_clipboard(cmd)
            self.app.notify("Copied debug cmd", timeout=2)
