"""Full-screen held issue inspection with workflow timeline and diagnostics."""

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
    _CATEGORY_ICONS,
    _CATEGORY_LABELS,
    _SEVERITY_STYLE,
    _event_severity,
    _human_message,
)
from orc.tui.modals import AmpStreamModal, _THREAD_URL_PREFIX, build_thread_continue_cmd
from orc.workflow import PHASE_INFO, PHASE_ORDER, normalize_failure_phase, phase_label


@dataclass
class WorkflowStep:
    stage: str
    label: str
    status: Literal["passed", "failed", "pending", "skipped"]
    detail: str | None = None


@dataclass
class HeldIssueModel:
    issue_id: str
    failure: dict
    related_runs: list[dict]
    events: list[dict]
    amp_log_path: str | None
    thread_id: str | None
    branch: str | None
    worktree_path: str | None
    amp_result: dict | None
    eval_result: dict | None
    merge_details: dict | None
    timeline: list[WorkflowStep] = field(default_factory=list)


def build_model(
    issue_id: str,
    failure: dict,
    state: OrchestratorState,
    state_dir: Path,
) -> HeldIssueModel:
    """Assemble a HeldIssueModel from available data sources."""
    # Find related runs (all runs for this issue, newest first)
    related_runs = [
        r for r in reversed(state.run_history) if r.get("issue_id") == issue_id
    ]

    # Load issue-specific events
    events = [
        e
        for e in EventLog(state_dir).all()
        if isinstance(e.get("data"), dict) and e["data"].get("issue_id") == issue_id
    ]

    # Resolve structured outputs from failure.extra, then from run history
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

    timeline = _build_timeline(failure, eval_result is not None)

    return HeldIssueModel(
        issue_id=issue_id,
        failure=failure,
        related_runs=related_runs,
        events=events,
        amp_log_path=amp_log_path,
        thread_id=thread_id,
        branch=branch,
        worktree_path=worktree_path,
        amp_result=amp_result,
        eval_result=eval_result,
        merge_details=merge_details,
        timeline=timeline,
    )


def _build_timeline(failure: dict, had_evaluation: bool) -> list[WorkflowStep]:
    """Build workflow timeline from the failure record."""
    stage = failure.get("stage", "legacy")
    fail_at = normalize_failure_phase(stage)

    # Find the index of the failure phase in the ordered list
    phase_keys = [p.value for p in PHASE_ORDER]
    try:
        fail_idx = phase_keys.index(fail_at)
    except ValueError:
        fail_idx = -1  # unknown phase — mark everything pending

    steps: list[WorkflowStep] = []
    for i, phase in enumerate(PHASE_ORDER):
        info = PHASE_INFO[phase]
        key = phase.value
        label = info.label

        if fail_idx < 0:
            status: Literal["passed", "failed", "pending", "skipped"] = "pending"
        elif i < fail_idx:
            status = "passed"
        elif i == fail_idx:
            status = "failed"
        else:
            # Optional phases are skipped unless the failure is at that phase
            if key == "evaluation_running" and not had_evaluation and fail_at != key:
                status = "skipped"
            elif key in ("summary_extraction", "dirty_worktree_check", "conflict_resolution", "parent_promotion"):
                status = "skipped"
            else:
                status = "pending"

        detail = failure.get("summary") if status == "failed" else None
        steps.append(WorkflowStep(stage=key, label=label, status=status, detail=detail))

    return steps


# -- Screen --------------------------------------------------------------------

_STATUS_GLYPHS = {
    "passed": "[green]✔[/]",
    "failed": "[bold red]✖[/]",
    "pending": "[dim]○[/]",
    "skipped": "[dim]↷[/]",
}


class HeldIssueInspectScreen(Screen[None]):
    """Full-screen diagnostic view for a held/failed issue."""

    DEFAULT_CSS = """
    HeldIssueInspectScreen {
        background: #0b0f14;
        color: #d7dde8;
    }
    #hi-root {
        height: 1fr;
    }
    #hi-header {
        height: auto;
        padding: 0 1;
        background: #111827;
    }
    #hi-header-title {
        text-style: bold;
    }
    #hi-header-meta {
        color: #93a4b8;
    }
    #hi-main {
        height: 1fr;
    }
    #hi-sidebar {
        width: 36;
        border-right: solid #263041;
        padding: 0 1;
    }
    #hi-timeline-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
        color: #94a3b8;
    }
    #hi-timeline {
        height: auto;
    }
    #hi-links {
        height: auto;
        margin-top: 1;
        color: #93a4b8;
    }
    #hi-content {
        width: 1fr;
        padding: 0 1;
    }
    #hi-output-scroll {
        height: 1fr;
        min-height: 8;
    }
    .hi-section-title {
        text-style: bold;
        margin-top: 1;
        color: #8fb2ff;
    }
    .hi-section-body {
        height: auto;
        margin-bottom: 1;
    }
    #hi-events-table {
        height: 1fr;
        min-height: 6;
        border-top: solid #263041;
    }
    #hi-hints {
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

    def __init__(
        self,
        model: HeldIssueModel,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model = model

    def compose(self) -> ComposeResult:
        m = self._model
        f = m.failure

        # Header
        cat_label = _CATEGORY_LABELS.get(f.get("category", ""), f.get("category", "?"))
        cat_icon = _CATEGORY_ICONS.get(f.get("category", ""), "·")

        with Vertical(id="hi-root"):
            with Vertical(id="hi-header"):
                yield Label(
                    f"{cat_icon} Held Issue: {m.issue_id}",
                    id="hi-header-title",
                )
                meta_parts = [
                    f"Category: {cat_label}",
                    f"Stage: {f.get('stage', '?')}",
                    f"Attempts: {f.get('attempts', 1)}",
                    f"Action: {f.get('action', '?')}",
                ]
                if f.get("timestamp"):
                    meta_parts.append(f"Time: {f['timestamp']}")
                yield Label(" │ ".join(meta_parts), id="hi-header-meta")

            with Horizontal(id="hi-main"):
                # Left sidebar: timeline + links
                with Vertical(id="hi-sidebar"):
                    yield Label("Workflow", id="hi-timeline-title")
                    yield Static(self._render_timeline(), id="hi-timeline")
                    yield Static(self._render_links(), id="hi-links")

                # Right content: output sections + events
                with Vertical(id="hi-content"):
                    with VerticalScroll(id="hi-output-scroll"):
                        # Summary section
                        yield Label("Summary", classes="hi-section-title")
                        yield Static(
                            f.get("summary", "(no summary)"),
                            classes="hi-section-body",
                        )

                        # AMP result section
                        yield Label("Agent Result", classes="hi-section-title")
                        yield Static(
                            self._render_amp_result(),
                            classes="hi-section-body",
                        )

                        # Evaluation section
                        yield Label("Evaluation", classes="hi-section-title")
                        yield Static(
                            self._render_eval_result(),
                            classes="hi-section-body",
                        )

                        # Merge section
                        yield Label("Merge", classes="hi-section-title")
                        yield Static(
                            self._render_merge_details(),
                            classes="hi-section-body",
                        )

                    # Events table
                    yield DataTable(id="hi-events-table", cursor_type="row")

            yield Static(self._render_hints(), id="hi-hints")

    def on_mount(self) -> None:
        table = self.query_one("#hi-events-table", DataTable)
        table.add_columns("Time", "Sev", "Phase", "Type", "Message")
        for event in self._model.events:
            ts = event.get("timestamp", "")
            if "T" in ts:
                # Show HH:MM:SS only
                ts = ts.split("T")[1].split(".")[0] if "." in ts.split("T")[1] else ts.split("T")[1].split("+")[0]
            event_type = event.get("event_type", "")
            sev = _event_severity(event_type)
            sev_style = _SEVERITY_STYLE.get(sev, "bright_white")
            color = EVENT_COLORS.get(event_type, "white")
            phase_str = phase_label(event.get("phase"))
            message = _human_message(event_type, event.get("data"))
            if len(message) > 100:
                message = message[:97] + "…"
            table.add_row(
                ts,
                f"[{sev_style}]{sev}[/]",
                phase_str,
                f"[{color}]{event_type}[/]",
                message,
            )
        if table.row_count == 0:
            table.add_row("-", "-", "-", "-", "[italic]No events recorded for this issue[/]")

    def _render_timeline(self) -> str:
        lines: list[str] = []
        for step in self._model.timeline:
            glyph = _STATUS_GLYPHS.get(step.status, "?")
            line = f"  {glyph} {step.label}"
            if step.detail and step.status == "failed":
                # Truncate detail for sidebar
                detail = step.detail if len(step.detail) <= 40 else step.detail[:37] + "…"
                line += f"\n     [dim]{detail}[/]"
            lines.append(line)
        return "\n".join(lines)

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
        if m.failure.get("preserve_worktree"):
            lines.append("[bold bright_yellow]⚠ Worktree preserved[/]")
        return "\n".join(lines) if lines else "[dim]No links available[/]"

    def _render_amp_result(self) -> str:
        ar = self._model.amp_result
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
        er = self._model.eval_result
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

    def action_open_amp_log(self) -> None:
        if not self._model.amp_log_path:
            self.app.notify("No amp log available", severity="warning")
            return
        if not Path(self._model.amp_log_path).exists():
            self.app.notify("Amp log file not found on disk", severity="warning")
            return
        header_lines = [
            f"Issue: {self._model.issue_id}",
        ]
        if self._model.branch:
            header_lines.append(f"Branch: {self._model.branch}")
        if self._model.failure.get("summary"):
            header_lines.append(f"Failure: {self._model.failure['summary']}")
        self.app.push_screen(
            AmpStreamModal(
                title=f"Amp Log: {self._model.issue_id}",
                log_path=self._model.amp_log_path,
                header_lines=header_lines,
            )
        )

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
