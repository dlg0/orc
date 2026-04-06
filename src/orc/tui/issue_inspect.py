"""Unified full-screen issue inspection for any issue state."""

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
from orc.queue import BdIssue
from orc.state import OrchestratorState
from orc.tui.event_helpers import (
    EVENT_COLORS,
    _CATEGORY_ICONS,
    _CATEGORY_LABELS,
    _SEVERITY_STYLE,
    _event_severity,
    _human_message,
)
from orc.tui.modals import AmpStreamModal, _THREAD_URL_PREFIX, build_thread_continue_cmd
from orc.workflow import PHASE_INFO, PHASE_ORDER, normalize_failure_phase, phase_label


# -- Data Model ----------------------------------------------------------------

StepStatus = Literal["done", "active", "failed", "pending", "skipped"]


@dataclass
class IssueInspectStep:
    phase: str
    label: str
    status: StepStatus
    detail: str | None = None
    has_log: bool = False


@dataclass
class IssueInspectModel:
    # Identity
    issue_id: str
    issue_title: str
    source: Literal["active", "held", "history", "queue"]
    state_label: str  # e.g. "Active", "Held", "Completed", "Runnable"
    status_tone: str  # Textual color for the state badge

    # Issue text
    description: str = ""
    acceptance_criteria: str = ""

    # Timing
    created_at: str = ""
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    timestamp: str = ""

    # Queue
    priority: int = 0
    dispatch_state: str = ""

    # Workflow
    current_phase: str | None = None
    result: str = ""
    summary: str = ""
    failure_category: str = ""
    failure_action: str = ""
    failure_summary: str = ""
    attempts: int = 0

    # Artifacts
    branch: str | None = None
    worktree_path: str | None = None
    thread_id: str | None = None
    amp_log_path: str | None = None
    preflight_log_path: str | None = None
    preserve_worktree: bool = False

    # Structured outputs (raw dicts)
    agent_result: dict | None = None
    evaluation_result: dict | None = None
    merge_details: dict | None = None

    # Derived
    workflow_steps: list[IssueInspectStep] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


# -- Builder Functions ---------------------------------------------------------

def build_from_active(
    state: OrchestratorState,
    state_dir: Path,
) -> IssueInspectModel | None:
    """Build an IssueInspectModel from the current active run."""
    if not state.active_run:
        return None

    run = state.active_run
    issue_id = run["issue_id"]
    current_phase = run.get("stage")

    events = [
        e
        for e in EventLog(state_dir).all()
        if isinstance(e.get("data"), dict) and e["data"].get("issue_id") == issue_id
    ]

    # Determine thread_id
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
    preflight_log_path = run.get("preflight_log_path")
    timeline = _build_active_timeline(current_phase, amp_log_path, preflight_log_path)

    return IssueInspectModel(
        issue_id=issue_id,
        issue_title=run.get("issue_title", ""),
        source="active",
        state_label="Active",
        status_tone="dodger_blue2",
        description=run.get("issue_description", ""),
        acceptance_criteria=run.get("issue_acceptance_criteria", ""),
        started_at=run.get("updated_at", ""),
        current_phase=current_phase,
        branch=run.get("branch"),
        worktree_path=run.get("worktree_path"),
        thread_id=thread_id,
        amp_log_path=amp_log_path,
        preflight_log_path=preflight_log_path,
        agent_result=amp_result if isinstance(amp_result, dict) else None,
        workflow_steps=timeline,
        events=events,
    )


def build_from_held(
    issue_id: str,
    failure: dict,
    state: OrchestratorState,
    state_dir: Path,
) -> IssueInspectModel:
    """Build an IssueInspectModel from a held/failed issue."""
    related_runs = [
        r for r in reversed(state.run_history) if r.get("issue_id") == issue_id
    ]

    events = [
        e
        for e in EventLog(state_dir).all()
        if isinstance(e.get("data"), dict) and e["data"].get("issue_id") == issue_id
    ]

    extra = failure.get("extra") or {}
    latest_run = related_runs[0] if related_runs else {}

    amp_result = extra.get("amp_result") or latest_run.get("amp_result")
    eval_result = (
        extra.get("eval_result")
        or extra.get("evaluation")
        or latest_run.get("eval_result")
        or latest_run.get("evaluation")
    )
    merge_details: dict | None = None
    if extra.get("merge_stage") or extra.get("merge_error"):
        merge_details = {
            "stage": extra.get("merge_stage"),
            "error": extra.get("merge_error"),
            "conflict_resolved": extra.get("conflict_resolved", False),
        }
    if extra.get("merge_diagnostics"):
        if merge_details is None:
            merge_details = {}
        merge_details["diagnostics"] = extra["merge_diagnostics"]

    amp_log_path = extra.get("amp_log_path") or latest_run.get("amp_log_path")
    thread_id = extra.get("thread_id") or latest_run.get("thread_id")
    branch = failure.get("branch") or latest_run.get("branch")
    worktree_path = failure.get("worktree_path") or latest_run.get("worktree_path")

    cat = failure.get("category", "unknown")
    cat_label = _CATEGORY_LABELS.get(cat, cat)
    cat_icon = _CATEGORY_ICONS.get(cat, "·")

    timeline = _build_held_timeline(failure, eval_result is not None)

    return IssueInspectModel(
        issue_id=issue_id,
        issue_title=failure.get("issue_title", latest_run.get("issue_title", "")),
        source="held",
        state_label="Held",
        status_tone="bright_yellow",
        failure_category=cat,
        failure_action=failure.get("action", ""),
        failure_summary=failure.get("summary", ""),
        attempts=failure.get("attempts", 1),
        timestamp=failure.get("timestamp", ""),
        current_phase=failure.get("stage"),
        branch=branch,
        worktree_path=worktree_path,
        thread_id=thread_id,
        amp_log_path=amp_log_path,
        preserve_worktree=failure.get("preserve_worktree", False),
        agent_result=amp_result if isinstance(amp_result, dict) else None,
        evaluation_result=eval_result if isinstance(eval_result, dict) else None,
        merge_details=merge_details,
        workflow_steps=timeline,
        events=events,
    )


def build_from_history(run: dict) -> IssueInspectModel:
    """Build an IssueInspectModel from a run history entry."""
    raw_result = run.get("result", "")
    tone = "green" if raw_result == "completed" else "red" if raw_result in ("failed", "error") else "white"

    return IssueInspectModel(
        issue_id=run.get("issue_id", ""),
        issue_title=run.get("issue_title", ""),
        source="history",
        state_label=raw_result.capitalize() if raw_result else "Unknown",
        status_tone=tone,
        result=raw_result,
        summary=run.get("summary", ""),
        timestamp=run.get("timestamp", ""),
        branch=run.get("branch"),
        worktree_path=run.get("worktree_path"),
        thread_id=run.get("thread_id"),
        amp_log_path=run.get("amp_log_path"),
        agent_result=run.get("amp_result") if isinstance(run.get("amp_result"), dict) else None,
        evaluation_result=run.get("eval_result") if isinstance(run.get("eval_result"), dict) else None,
    )


def build_from_queue(issue: BdIssue, dispatch_state: str) -> IssueInspectModel:
    """Build an IssueInspectModel from a queue/backlog issue."""
    tone = "bright_yellow" if dispatch_state == "Held (ready)" else "green"
    return IssueInspectModel(
        issue_id=issue.id,
        issue_title=issue.title,
        source="queue",
        state_label=dispatch_state,
        status_tone=tone,
        priority=issue.priority,
        dispatch_state=dispatch_state,
        created_at=issue.created,
        description=issue.description,
        acceptance_criteria=issue.acceptance_criteria,
    )


# -- Timeline Builders --------------------------------------------------------

def _build_active_timeline(
    current_phase: str | None,
    amp_log_path: str | None,
    preflight_log_path: str | None = None,
) -> list[IssueInspectStep]:
    """Build the workflow step timeline for an active issue."""
    phase_keys = [p.value for p in PHASE_ORDER]
    try:
        current_idx = phase_keys.index(current_phase) if current_phase else -1
    except ValueError:
        current_idx = -1

    optional_phases = {
        "summary_extraction", "dirty_worktree_check", "conflict_resolution",
        "parent_promotion", "claim_release_pending", "merge_recovery",
    }

    steps: list[IssueInspectStep] = []
    for i, phase in enumerate(PHASE_ORDER):
        info = PHASE_INFO[phase]
        key = phase.value

        if current_idx < 0:
            status: StepStatus = "pending"
        elif i < current_idx:
            status = "done"
        elif i == current_idx:
            status = "active"
        else:
            if key in optional_phases:
                status = "skipped"
            else:
                status = "pending"

        if status == "skipped":
            continue

        has_log = (
            (key == "amp_running" and amp_log_path is not None)
            or (key == "already_implemented_check" and preflight_log_path is not None)
        )

        steps.append(IssueInspectStep(
            phase=key,
            label=info.label,
            status=status,
            has_log=has_log,
        ))

    return steps


def _build_held_timeline(failure: dict, had_evaluation: bool) -> list[IssueInspectStep]:
    """Build workflow timeline from a failure record."""
    stage = failure.get("stage", "legacy")
    fail_at = normalize_failure_phase(stage)

    phase_keys = [p.value for p in PHASE_ORDER]
    try:
        fail_idx = phase_keys.index(fail_at)
    except ValueError:
        fail_idx = -1

    steps: list[IssueInspectStep] = []
    for i, phase in enumerate(PHASE_ORDER):
        info = PHASE_INFO[phase]
        key = phase.value
        label = info.label

        if fail_idx < 0:
            status: StepStatus = "pending"
        elif i < fail_idx:
            status = "done"
        elif i == fail_idx:
            status = "failed"
        else:
            if key == "evaluation_running" and not had_evaluation and fail_at != key:
                status = "skipped"
            elif key in ("summary_extraction", "dirty_worktree_check", "conflict_resolution", "parent_promotion"):
                status = "skipped"
            else:
                status = "pending"

        detail = failure.get("summary") if status == "failed" else None
        steps.append(IssueInspectStep(
            phase=key, label=label, status=status, detail=detail,
        ))

    return steps


# -- Screen --------------------------------------------------------------------

_STATUS_GLYPHS: dict[str, str] = {
    "done": "[green]✔[/]",
    "active": "[bold dodger_blue2]●[/]",
    "failed": "[bold red]✖[/]",
    "pending": "[dim]○[/]",
    "skipped": "[dim]↷[/]",
}

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


class IssueInspectScreen(Screen[None]):
    """Unified full-screen inspection for any issue regardless of state."""

    DEFAULT_CSS = """
    IssueInspectScreen {
        background: #0b0f14;
        color: #d7dde8;
    }
    #ii-root {
        height: 1fr;
    }
    #ii-header {
        height: auto;
        padding: 0 1;
        background: #111827;
    }
    #ii-header-title {
        text-style: bold;
    }
    #ii-header-meta {
        color: #93a4b8;
    }
    #ii-main {
        height: 1fr;
    }
    #ii-sidebar {
        width: 40;
        border-right: solid #263041;
        padding: 0 1;
    }
    #ii-timeline-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
        color: #94a3b8;
    }
    #ii-timeline-table {
        height: auto;
        max-height: 20;
    }
    #ii-timeline-static {
        height: auto;
    }
    #ii-links {
        height: auto;
        margin-top: 1;
        color: #93a4b8;
    }
    #ii-content {
        width: 1fr;
        padding: 0 1;
    }
    #ii-content-scroll {
        height: 1fr;
        min-height: 8;
    }
    .ii-section-title {
        text-style: bold;
        margin-top: 1;
        color: #8fb2ff;
    }
    .ii-section-body {
        height: auto;
        margin-bottom: 1;
    }
    #ii-events-table {
        height: auto;
        min-height: 6;
        max-height: 16;
        border-top: solid #263041;
    }
    #ii-hints {
        height: auto;
        padding: 0 1;
        background: #0f172a;
        color: #93a4b8;
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

    def __init__(self, model: IssueInspectModel, **kwargs) -> None:
        super().__init__(**kwargs)
        self._model = model

    def compose(self) -> ComposeResult:
        m = self._model
        with Vertical(id="ii-root"):
            # Header
            with Vertical(id="ii-header"):
                yield Label(
                    f"[{m.status_tone}]{m.state_label}[/]: {m.issue_id}",
                    id="ii-header-title",
                )
                yield Label(self._build_header_meta(), id="ii-header-meta")

            # Main area: sidebar + content
            with Horizontal(id="ii-main"):
                with Vertical(id="ii-sidebar"):
                    yield Label("Workflow Steps", id="ii-timeline-title")
                    if m.source in ("active",):
                        yield DataTable(id="ii-timeline-table", cursor_type="row")
                    elif m.source in ("held",):
                        yield Static(self._render_timeline_static(), id="ii-timeline-static")
                    else:
                        yield Static("[dim]No workflow data[/]", id="ii-timeline-static")
                    yield Static(self._render_links(), id="ii-links")

                with Vertical(id="ii-content"):
                    with VerticalScroll(id="ii-content-scroll"):
                        # Overview (always)
                        yield Label("Overview", classes="ii-section-title")
                        yield Static(self._render_overview(), classes="ii-section-body")

                        # Issue Details (if description or AC available)
                        if m.description or m.acceptance_criteria:
                            yield Label("Issue Details", classes="ii-section-title")
                            yield Static(self._render_issue_details(), classes="ii-section-body")

                        # Failure Details (held only)
                        if m.source == "held" and m.failure_category:
                            yield Label("Failure Details", classes="ii-section-title")
                            yield Static(self._render_failure_details(), classes="ii-section-body")

                        # Agent Result
                        if m.agent_result:
                            yield Label("Agent Result", classes="ii-section-title")
                            yield Static(self._render_amp_result(), classes="ii-section-body")

                        # Evaluation
                        if m.evaluation_result:
                            yield Label("Evaluation", classes="ii-section-title")
                            yield Static(self._render_eval_result(), classes="ii-section-body")

                        # Merge Details
                        if m.merge_details:
                            yield Label("Merge", classes="ii-section-title")
                            yield Static(self._render_merge_details(), classes="ii-section-body")

                    # Events table (if events exist)
                    if m.events:
                        yield DataTable(id="ii-events-table", cursor_type="row")

            yield Static(self._render_hints(), id="ii-hints")

    def on_mount(self) -> None:
        m = self._model

        # Populate active timeline table
        if m.source == "active":
            try:
                timeline_table = self.query_one("#ii-timeline-table", DataTable)
            except Exception:
                pass
            else:
                timeline_table.add_columns("", "Step", "Phase")
                for step in m.workflow_steps:
                    glyph = _STATUS_GLYPHS.get(step.status, "?")
                    phase_color = _PHASE_COLORS.get(step.phase, "white")
                    label = step.label
                    if step.has_log and step.status == "active":
                        label += " [dim](a=log)[/]"
                    timeline_table.add_row(glyph, f"[{phase_color}]{label}[/]", step.phase)

        # Populate events table
        if m.events:
            try:
                events_table = self.query_one("#ii-events-table", DataTable)
            except Exception:
                pass
            else:
                events_table.add_columns("Time", "Sev", "Phase", "Type", "Message")
                for event in m.events:
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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open log when a timeline step with a log is selected."""
        table = event.data_table
        if table.id != "ii-timeline-table":
            return
        row_idx = table.cursor_row
        m = self._model
        if 0 <= row_idx < len(m.workflow_steps):
            step = m.workflow_steps[row_idx]
            if step.has_log:
                if step.phase == "already_implemented_check" and m.preflight_log_path:
                    self._open_log(m.preflight_log_path, f"Preflight: {m.issue_id}")
                elif m.amp_log_path:
                    self._open_amp_log()

    # -- Header ----------------------------------------------------------------

    def _build_header_meta(self) -> str:
        m = self._model
        parts: list[str] = []
        if m.issue_title:
            parts.append(m.issue_title)
        if m.current_phase:
            color = _PHASE_COLORS.get(m.current_phase, "white")
            parts.append(f"[{color}]{phase_label(m.current_phase)}[/]")
        if m.source == "held" and m.failure_category:
            cat_label = _CATEGORY_LABELS.get(m.failure_category, m.failure_category)
            cat_icon = _CATEGORY_ICONS.get(m.failure_category, "·")
            parts.append(f"{cat_icon} {cat_label}")
            parts.append(f"Attempts: {m.attempts}")
        if m.source == "queue":
            parts.append(f"Dispatch: {m.dispatch_state}")
            if m.priority:
                parts.append(f"Priority: {m.priority}")
        if m.source == "history" and m.result:
            parts.append(f"Result: {m.result}")
        return " — ".join(parts) if parts else ""

    # -- Sidebar Renderers -----------------------------------------------------

    def _render_timeline_static(self) -> str:
        lines: list[str] = []
        for step in self._model.workflow_steps:
            glyph = _STATUS_GLYPHS.get(step.status, "?")
            line = f"  {glyph} {step.label}"
            if step.detail and step.status == "failed":
                detail = step.detail if len(step.detail) <= 40 else step.detail[:37] + "…"
                line += f"\n     [dim]{detail}[/]"
            lines.append(line)
        return "\n".join(lines) if lines else "[dim]No workflow data[/]"

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
        if m.preflight_log_path:
            lines.append(f"[bold]Preflight log:[/] {m.preflight_log_path}")
        if m.amp_log_path:
            lines.append(f"[bold]Amp log:[/] {m.amp_log_path}")
        if m.preserve_worktree:
            lines.append("[bold bright_yellow]⚠ Worktree preserved[/]")
        return "\n".join(lines) if lines else "[dim]No links available[/]"

    # -- Content Renderers -----------------------------------------------------

    def _render_overview(self) -> str:
        m = self._model
        lines: list[str] = []
        lines.append(f"[bold]Issue:[/] {m.issue_id}")
        if m.issue_title:
            lines.append(f"[bold]Title:[/] {m.issue_title}")
        lines.append(f"[bold]State:[/] [{m.status_tone}]{m.state_label}[/]")
        if m.result:
            lines.append(f"[bold]Result:[/] {m.result}")
        if m.summary:
            lines.append(f"[bold]Summary:[/] {m.summary}")
        if m.started_at:
            lines.append(f"[bold]Started:[/] {m.started_at}")
        if m.timestamp:
            lines.append(f"[bold]Timestamp:[/] {m.timestamp}")
        if m.created_at:
            lines.append(f"[bold]Created:[/] {m.created_at}")
        if m.priority:
            lines.append(f"[bold]Priority:[/] {m.priority}")
        if m.dispatch_state:
            lines.append(f"[bold]Dispatch state:[/] {m.dispatch_state}")
        return "\n".join(lines)

    def _render_issue_details(self) -> str:
        m = self._model
        lines: list[str] = []
        if m.description:
            lines.append(f"[bold]Description:[/]\n{m.description}")
        if m.acceptance_criteria:
            lines.append(f"[bold]Acceptance Criteria:[/]\n{m.acceptance_criteria}")
        return "\n\n".join(lines) if lines else "[dim]No details available[/]"

    def _render_failure_details(self) -> str:
        m = self._model
        cat_label = _CATEGORY_LABELS.get(m.failure_category, m.failure_category)
        cat_icon = _CATEGORY_ICONS.get(m.failure_category, "·")
        lines = [
            f"[bold]Category:[/] {cat_icon} {cat_label}",
            f"[bold]Action:[/] {m.failure_action}",
            f"[bold]Attempts:[/] {m.attempts}",
        ]
        if m.failure_summary:
            lines.append(f"[bold]Summary:[/] {m.failure_summary}")
        if m.current_phase:
            lines.append(f"[bold]Failed at stage:[/] {phase_label(m.current_phase)}")
        return "\n".join(lines)

    def _render_amp_result(self) -> str:
        ar = self._model.agent_result
        if not ar:
            return "[dim]No agent result data available[/]"
        lines: list[str] = []
        if ar.get("result"):
            lines.append(f"[bold]Result:[/] {ar['result']}")
        if ar.get("summary"):
            lines.append(f"[bold]Summary:[/] {ar['summary']}")
        if "merge_ready" in ar:
            mr = ar["merge_ready"]
            color = "green" if mr else "red"
            lines.append(f"[bold]Merge ready:[/] [{color}]{mr}[/]")
        if ar.get("context_window_usage_pct") is not None:
            pct = ar["context_window_usage_pct"]
            color = "red" if pct >= 80 else "bright_yellow" if pct >= 50 else "green"
            lines.append(f"[bold]Context usage:[/] [{color}]{pct}%[/]")
        if ar.get("changed_paths"):
            lines.append(f"[bold]Changed files:[/] {len(ar['changed_paths'])}")
            for p in ar["changed_paths"][:10]:
                lines.append(f"  · {p}")
            if len(ar["changed_paths"]) > 10:
                lines.append(f"  … and {len(ar['changed_paths']) - 10} more")
        if ar.get("tests_run"):
            lines.append(f"[bold]Tests run:[/] {', '.join(ar['tests_run'])}")
        if ar.get("blockers"):
            lines.append(f"[bold red]Blockers:[/] {', '.join(ar['blockers'])}")
        if ar.get("followup_bd_issues"):
            lines.append(f"[bold]Follow-up issues:[/] {', '.join(ar['followup_bd_issues'])}")
        return "\n".join(lines) if lines else "[dim]No agent result data available[/]"

    def _render_eval_result(self) -> str:
        er = self._model.evaluation_result
        if not er:
            return "[dim]No evaluation data available[/]"
        lines: list[str] = []
        if er.get("verdict"):
            color = "green" if er["verdict"] == "pass" else "red"
            lines.append(f"[bold]Verdict:[/] [{color}]{er['verdict']}[/]")
        if er.get("summary"):
            lines.append(f"[bold]Summary:[/] {er['summary']}")
        if er.get("evidence"):
            lines.append("[bold]Evidence:[/]")
            for e in er["evidence"]:
                lines.append(f"  · {e}")
        if er.get("tests_run"):
            lines.append(f"[bold]Tests run:[/] {', '.join(er['tests_run'])}")
        if er.get("gaps"):
            lines.append("[bold red]Gaps:[/]")
            for g in er["gaps"]:
                lines.append(f"  · {g}")
        if er.get("task_too_large_signal"):
            lines.append("[bold bright_yellow]⚠ Task too large signal[/]")
        if er.get("context_window_usage_pct") is not None:
            pct = er["context_window_usage_pct"]
            color = "red" if pct >= 80 else "bright_yellow" if pct >= 50 else "green"
            lines.append(f"[bold]Context usage:[/] [{color}]{pct}%[/]")
        return "\n".join(lines) if lines else "[dim]No evaluation data available[/]"

    def _render_merge_details(self) -> str:
        md = self._model.merge_details
        if not md:
            return "[dim]No merge data available[/]"
        lines: list[str] = []
        if md.get("stage"):
            lines.append(f"[bold]Failed at:[/] {md['stage']}")
        if md.get("error"):
            lines.append(f"[bold red]Error:[/] {md['error']}")
        if md.get("conflict_resolved"):
            lines.append("[bold green]Conflict auto-resolved: True[/]")

        diag = md.get("diagnostics")
        if diag:
            lines.append("")
            lines.append("[bold]Diagnostics:[/]")
            if diag.get("reason"):
                lines.append(f"  Reason: {diag['reason']}")
            if diag.get("command"):
                lines.append(f"  Command: {' '.join(diag['command'])}")
            if diag.get("returncode") is not None:
                lines.append(f"  Return code: {diag['returncode']}")
            if diag.get("stdout"):
                stdout = diag["stdout"][:200]
                if len(diag["stdout"]) > 200:
                    stdout += "…"
                lines.append(f"  Stdout: {stdout}")
            if diag.get("stderr"):
                stderr = diag["stderr"][:200]
                if len(diag["stderr"]) > 200:
                    stderr += "…"
                lines.append(f"  Stderr: {stderr}")
            git_state = diag.get("git_state")
            if git_state:
                dirty = git_state.get("repo_root_dirty", [])
                wt_dirty = git_state.get("worktree_dirty", [])
                if dirty:
                    lines.append(f"  Dirty repo-root paths: {', '.join(dirty[:10])}")
                if wt_dirty:
                    lines.append(f"  Dirty worktree paths: {', '.join(wt_dirty[:10])}")

        return "\n".join(lines) if lines else "[dim]No merge data available[/]"

    def _render_hints(self) -> str:
        parts: list[str] = ["[bold]q[/]/[bold]Esc[/] close"]
        m = self._model
        if m.amp_log_path:
            parts.append("[bold]a[/] amp log")
        if m.branch:
            parts.append("[bold]b[/] copy branch")
        if m.worktree_path:
            parts.append("[bold]w[/] copy worktree")
        if m.thread_id:
            parts.append("[bold]t[/] copy thread ID")
            parts.append("[bold]u[/] copy thread URL")
            parts.append("[bold]d[/] copy debug cmd")
        return "  ".join(parts)

    # -- Actions ---------------------------------------------------------------

    def _open_log(self, log_path: str, title: str) -> None:
        if not Path(log_path).exists():
            self.app.notify("Log file not found on disk", severity="warning")
            return
        header_lines = [f"Issue: {self._model.issue_id}"]
        if self._model.current_phase:
            header_lines.append(f"Phase: {phase_label(self._model.current_phase)}")
        self.app.push_screen(
            AmpStreamModal(
                title=title,
                log_path=log_path,
                header_lines=header_lines,
            )
        )

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
        if self._model.failure_summary:
            header_lines.append(f"Failure: {self._model.failure_summary}")
        self.app.push_screen(
            AmpStreamModal(
                title=f"Amp Log: {self._model.issue_id}",
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
