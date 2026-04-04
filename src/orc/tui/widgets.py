"""Dashboard widgets for the orc TUI."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static

from orc.config import OrchestratorConfig
from orc.queue import BdIssue
from orc.state import OrchestratorMode, OrchestratorState
from orc.tui.event_helpers import (
    EVENT_COLORS,
    _CATEGORY_ICONS,
    _CATEGORY_LABELS,
    _event_severity,
    _SEVERITY_STYLE,
    _human_message,
)
from orc.tui.snapshot import DashboardSnapshot

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


class StaleBanner(Static):
    """Banner shown when dashboard data is stale or refresh has failed."""

    DEFAULT_CSS = """
    StaleBanner {
        height: auto;
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
        display: none;
    }
    NotConnectedBanner.visible {
        display: block;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(NO_PROJECT_MSG, id="no-project-banner-text")


class ErrorAlert(Static):
    """Persistent, high-salience error alert shown when last_error exists."""

    DEFAULT_CSS = """
    ErrorAlert {
        height: auto;
        display: none;
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
        from orc.tui.modals import InspectModal

        self.app.push_screen(
            InspectModal(title="Error Details", body=self._full_error)
        )


class StatusPanel(Static):
    """Status panel: mode badge, queue count, last completed/error, error alert."""

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        min-height: 10;
    }
    StatusPanel.error-state {
        border: round red;
    }
    StatusPanel .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._frozen: bool = False
        # Cached queue counts from last full refresh
        self._cached_beads_ready: int | str = 0
        self._cached_runnable: int | str = 0
        self._cached_held_ready: int | str = 0
        self._cached_policy_skipped: int | str = 0

    def compose(self) -> ComposeResult:
        yield Label("Status", classes="panel-title")
        yield Label("[bold white]○ IDLE[/]", id="mode-badge")
        yield Label("", id="refresh-error")
        yield Label("[italic]Last refresh: —[/]", id="last-updated")
        yield Label("[italic]Queue last refreshed: —[/]", id="queue-last-refreshed")
        yield Label(
            "Beads ready: 0 | Skipped: 0 | Runnable: 0 | In-progress: 0 | Held (ready): 0",
            id="counts-summary",
        )
        yield Label("", id="skip-diagnostics")
        yield Label("[italic]Events: —[/]", id="event-severity-counts")
        yield Label("[italic]Last completed: —[/]", id="last-completed")
        yield Label("[italic]Last error: —[/]", id="last-error")
        yield ErrorAlert()

    def show_no_project(self) -> None:
        self.query_one("#mode-badge", Label).update(
            "[bold red]⚠ NOT CONNECTED[/]"
        )
        self.query_one("#refresh-error", Label).update("")
        self.query_one("#last-updated", Label).update("[italic]Last refresh: —[/]")
        self.query_one("#queue-last-refreshed", Label).update("[italic]Queue last refreshed: —[/]")
        self.query_one("#counts-summary", Label).update(NO_PROJECT_PLACEHOLDER)
        self.query_one("#skip-diagnostics", Label).update("")
        self.query_one("#event-severity-counts", Label).update("[italic]Events: —[/]")
        self.query_one("#last-completed", Label).update("[italic]Last completed: —[/]")
        self.query_one("#last-error", Label).update("[italic]Last error: —[/]")
        self.query_one(ErrorAlert).set_error("")

    def update_last_refreshed(self, ts: datetime) -> None:
        """Update the 'Last refresh' display with the given timestamp."""
        time_str = ts.astimezone().strftime("%H:%M:%S")
        self.query_one("#last-updated", Label).update(
            f"[italic]Last refresh: {time_str}[/]"
        )

    def update_queue_last_refreshed(self, ts: datetime) -> None:
        """Update the 'Queue last refreshed' display with the given timestamp."""
        time_str = ts.astimezone().strftime("%H:%M:%S")
        self.query_one("#queue-last-refreshed", Label).update(
            f"[italic]Queue last refreshed: {time_str}[/]"
        )

    def show_refresh_error(self, message: str) -> None:
        """Show a visible refresh error warning below the mode badge."""
        truncated = message if len(message) <= 80 else message[:77] + "…"
        self.query_one("#refresh-error", Label).update(
            f"[bold red]⚠ Refresh error: {truncated}[/]"
        )

    def hide_refresh_error(self) -> None:
        """Clear the refresh error warning."""
        self.query_one("#refresh-error", Label).update("")

    def show_stale(self) -> None:
        """Show STALE badge on the last-refresh label."""
        label = self.query_one("#last-updated", Label)
        current = str(label.render())
        if "STALE" not in current:
            # Extract time from current text
            label.update(
                f"[bold yellow]⚠ STALE[/] {current}"
            )

    def show_transitional(self, text: str) -> None:
        """Show a transitional status like 'Starting…' or 'Pausing…'."""
        self.query_one("#mode-badge", Label).update(f"[bright_yellow]{text}[/]")

    def show_frozen(self) -> None:
        """Show a FROZEN badge on the mode line."""
        self._frozen = True
        badge = self.query_one("#mode-badge", Label)
        current = str(badge.render())
        if "FROZEN" not in current:
            badge.update(f"[bold bright_cyan on #003344]❄ FROZEN[/] {current}")

    def hide_frozen(self) -> None:
        """Remove the FROZEN badge (next update_snapshot will rebuild cleanly)."""
        self._frozen = False

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        color, text = MODE_STYLES.get(
            snap.state.mode, ("bold white", snap.state.mode.value)
        )
        badge = self.query_one("#mode-badge", Label)
        mode_text = f"[{color}]{text}[/]"
        if self._frozen:
            mode_text = f"[bold bright_cyan on #003344]❄ FROZEN[/] {mode_text}"
        badge.update(mode_text)

        if snap.state.mode == OrchestratorMode.error:
            self.add_class("error-state")
        else:
            self.remove_class("error-state")

        # Consolidated counts: Beads ready | Skipped | Runnable | In-progress | Held
        active = 1 if snap.state.active_issue_id else 0
        counts = self.query_one("#counts-summary", Label)
        skip_label = self.query_one("#skip-diagnostics", Label)
        if not snap.is_fast and snap.queue_breakdown is not None:
            bd = snap.queue_breakdown
            self._cached_beads_ready = bd.beads_ready
            self._cached_runnable = bd.runnable
            self._cached_held_ready = bd.held_and_ready
            self._cached_policy_skipped = bd.policy_skipped
            beads_part = str(bd.beads_ready)
            skipped_part = f"[bold orchid1]{bd.policy_skipped}[/]" if bd.policy_skipped else "0"
            runnable_part = f"[bold green]{bd.runnable}[/]" if bd.runnable else "0"
            active_part = f"[bold dodger_blue]{active}[/]" if active else "0"
            held_part = f"[bold bright_yellow]{bd.held_and_ready}[/]" if bd.held_and_ready else "0"
            counts.update(
                f"Beads ready: {beads_part} | Skipped: {skipped_part} | Runnable: {runnable_part} | "
                f"In-progress: {active_part} | Held (ready): {held_part}"
            )
            # Skip diagnostics: grouped reasons
            if snap.queue_skip_summary:
                parts = [
                    f"{cat}: {cnt}"
                    for cat, cnt in snap.queue_skip_summary.items()
                ]
                skip_label.update(
                    f"[dim]Policy-skipped: {', '.join(parts)}[/]"
                )
            else:
                skip_label.update("")
        else:
            # Fast refresh: use cached queue counts with staleness marker
            beads_str = f"~{self._cached_beads_ready}"
            runnable_str = str(self._cached_runnable)
            held_ready_str = str(self._cached_held_ready)
            skipped_str = str(self._cached_policy_skipped)
            skipped_part = f"[bold orchid1]~{skipped_str}[/]" if skipped_str != "0" else "~0"
            runnable_part = f"[bold green]~{runnable_str}[/]" if runnable_str != "0" else "~0"
            active_part = f"[bold dodger_blue]{active}[/]" if active else "0"
            held_part = f"[bold bright_yellow]~{held_ready_str}[/]" if held_ready_str != "0" else "~0"
            counts.update(
                f"Beads ready: [dim]{beads_str}[/] | Skipped: {skipped_part} | Runnable: {runnable_part} | "
                f"In-progress: {active_part} | Held (ready): {held_part}"
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



class HeldIssuesTable(Static):
    """Table of held/failed issues with inspect and retry capabilities."""

    DEFAULT_CSS = """
    HeldIssuesTable {
        height: auto;
        max-height: 12;
        display: none;
    }
    HeldIssuesTable.visible {
        display: block;
    }
    HeldIssuesTable .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "inspect", "Inspect", show=True),
        Binding("i", "inspect", "Inspect", show=False),
        Binding("y", "retry", "Retry", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._held_items: list[tuple[str, dict]] = []  # (issue_id, failure_info)
        self._row_key: list[str] = []
        self._last_snap: DashboardSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Label("Held Issues", classes="panel-title")
        yield DataTable(id="held-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#held-datatable", DataTable)
        table.add_columns("Issue", "Category", "Action", "Attempts", "Summary")

    def show_no_project(self) -> None:
        self.remove_class("visible")

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        self._last_snap = snap
        failures = snap.state.issue_failures
        if not failures:
            if self.has_class("visible"):
                self.remove_class("visible")
            self._held_items = []
            self._row_key = []
            return

        new_keys = sorted(failures.keys())
        if new_keys == self._row_key:
            return

        self._row_key = new_keys
        self._held_items = [(iid, failures[iid]) for iid in new_keys]
        self.add_class("visible")

        table = self.query_one("#held-datatable", DataTable)
        table.clear()
        for issue_id, info in self._held_items:
            if not isinstance(info, dict):
                table.add_row(issue_id, "?", "?", "?", "?")
                continue
            cat = info.get("category", "unknown")
            cat_label = _CATEGORY_LABELS.get(cat, cat)
            cat_icon = _CATEGORY_ICONS.get(cat, "·")
            action = info.get("action", "unknown")
            action_labels = {
                "auto_retry": "Auto retry",
                "hold_for_retry": "Hold for retry",
                "hold_until_backlog_changes": "Hold until backlog changes",
                "pause_orchestrator": "Pause orchestrator",
            }
            action_label = action_labels.get(action, action)
            attempts = str(info.get("attempts", 1))
            summary = info.get("summary", "")
            if len(summary) > 60:
                summary = summary[:57] + "…"

            category_colors = {
                "transient_external": "bright_yellow",
                "stale_or_conflicted": "bold bright_yellow",
                "issue_needs_rework": "bold red",
                "blocked_by_dependency": "orchid1",
                "fatal_run_error": "bold red",
            }
            cc = category_colors.get(cat, "white")
            table.add_row(
                issue_id,
                f"[{cc}]{cat_icon} {cat_label}[/]",
                action_label,
                attempts,
                summary,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_inspect()

    def action_inspect(self) -> None:
        self._show_inspect()

    def _show_inspect(self) -> None:
        table = self.query_one("#held-datatable", DataTable)
        if not self._held_items or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._held_items):
            return
        issue_id, info = self._held_items[row_idx]
        if not isinstance(info, dict):
            return

        state_dir = getattr(self.app, "_state_dir", None)
        snap = self._last_snap
        if not state_dir or not snap:
            return

        from orc.tui.held_inspect import HeldIssueInspectScreen, build_model

        model = build_model(issue_id, info, snap.state, state_dir)
        self.app.push_screen(HeldIssueInspectScreen(model))

    def action_retry(self) -> None:
        """Clear held status for the selected issue so it gets re-queued."""
        table = self.query_one("#held-datatable", DataTable)
        if not self._held_items or table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._held_items):
            return
        issue_id, _info = self._held_items[row_idx]
        from orc.tui.modals import ConfirmRetryModal

        self.app.push_screen(ConfirmRetryModal(issue_id), self._on_retry_confirmed)

    def _on_retry_confirmed(self, result: str | None) -> None:
        if result:
            self.app.retry_held_issue(result)  # type: ignore[attr-defined]


class ActiveIssuePanel(Static):
    """Active issue panel: id, title, stage, elapsed, branch, worktree."""

    DEFAULT_CSS = """
    ActiveIssuePanel {
        height: auto;
        min-height: 7;
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
        yield Label("Active Issue", classes="panel-title")
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

        # If there's an active AMP log file, open the live stream modal
        amp_log_path = state.active_amp_log_path
        if amp_log_path and state.active_stage == "amp_running":
            from pathlib import Path

            if Path(amp_log_path).exists() or state.active_stage == "amp_running":
                header_lines = []
                if state.active_issue_title:
                    header_lines.append(f"Title: {state.active_issue_title}")
                if state.active_stage:
                    color, label = _STAGE_STYLES.get(
                        state.active_stage,
                        ("yellow", state.active_stage),
                    )
                    elapsed = ""
                    if state.active_started_at:
                        elapsed = f" ({_format_elapsed(state.active_started_at)})"
                    header_lines.append(f"Stage: {label}{elapsed}")
                if state.active_branch:
                    header_lines.append(f"Branch: {state.active_branch}")
                from orc.tui.modals import AmpStreamModal

                self.app.push_screen(
                    AmpStreamModal(
                        title=f"Live: {state.active_issue_id}",
                        log_path=amp_log_path,
                        header_lines=header_lines,
                    )
                )
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
        from orc.tui.modals import CopyableField, InspectModal

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
        yield Label("Config", classes="panel-title")
        yield Label("", id="config-detail")

    def show_no_project(self) -> None:
        self.query_one("#config-detail", Label).update(NO_PROJECT_PLACEHOLDER)

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        self._last_config = snap.config
        cfg = snap.config
        lines = [
            f"Base branch: {cfg.base_branch}",
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
            lines.append("\n[bold]Verification commands:[/]")
            for cmd in cfg.verification_commands:
                lines.append(f"  • {cmd}")
        else:
            lines.append("\n[bold]Verification commands:[/] (none)")
        from orc.tui.modals import InspectModal

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
    }
    ControlsPanel .panel-title {
        text-style: bold;
    }
    """

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Label("Controls", classes="panel-title")
        with Horizontal(id="controls-buttons"):
            yield Button("▶ Start", id="btn-start", classes="control-start")
            yield Button("⏸ Pause", id="btn-pause", classes="control-pause")
            yield Button("↻ Resume", id="btn-resume", classes="control-resume")
            yield Button("■ Stop", id="btn-stop", classes="control-stop")

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
    """Dispatch frontier table with view and search/filter support."""

    DEFAULT_CSS = """
    QueueTable {
        height: 2fr;
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
        Binding("o", "cycle_view", "View", show=True),
        Binding("slash", "toggle_filter", "Filter", show=True),
    ]

    # View modes cycle: backend Beads order (default) → newest → oldest
    _VIEW_MODES = ("beads", "age_newest", "age_oldest")
    _VIEW_LABELS = {
        "beads": "Beads order",
        "age_newest": "Newest first",
        "age_oldest": "Oldest first",
    }

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._issues: list[BdIssue] = []
        self._filtered_issues: list[BdIssue] = []
        self._held_issue_ids: set[str] = set()
        self._render_key: tuple[object, ...] = ()
        self._view_mode: str = "beads"
        self._filter_text: str = ""
        self._policy_skipped: int = 0
        self._empty_message: str = "[italic]No issues in dispatch frontier[/]"

    def compose(self) -> ComposeResult:
        yield Label(self._panel_title_text(), classes="panel-title")
        yield Label("[italic]Dispatch frontier: —[/]", id="queue-summary")
        yield Label("[italic]Dispatch diagnostics: —[/]", id="queue-diagnostics")
        yield Input(
            placeholder="Filter frontier by issue ID or title…",
            id="queue-filter",
            classes="filter-bar",
        )
        yield DataTable(id="queue-datatable", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#queue-datatable", DataTable)
        table.add_columns("ID", "State", "Pri", "Title", "Created")

    def _panel_title_text(self) -> str:
        """Return the queue panel title for the current local view."""
        return f"Dispatch Frontier [View: {self._VIEW_LABELS[self._view_mode]}]"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "queue-filter":
            self._filter_text = event.value.strip()
            self._render_key = ()  # force re-render
            self._rebuild_table()

    def show_no_project(self) -> None:
        self.query_one("#queue-summary", Label).update(NO_PROJECT_PLACEHOLDER)
        self.query_one("#queue-diagnostics", Label).update("")
        table = self.query_one("#queue-datatable", DataTable)
        table.clear()
        table.add_row("-", "-", "-", NO_PROJECT_PLACEHOLDER, "-")

    def _apply_view(self, issues: list[BdIssue]) -> list[BdIssue]:
        """Apply the selected local view to the incoming dispatch frontier."""
        if self._view_mode == "age_newest":
            return sorted(issues, key=lambda i: i.created, reverse=True)
        if self._view_mode == "age_oldest":
            return sorted(issues, key=lambda i: i.created)
        return list(issues)

    def _apply_filter(self, issues: list[BdIssue]) -> list[BdIssue]:
        """Filter issues by ID or title substring."""
        if not self._filter_text:
            return issues
        needle = self._filter_text.lower()
        return [i for i in issues if needle in i.id.lower() or needle in i.title.lower()]

    def _rebuild_table(self) -> None:
        """Re-render the table with current view and filter settings."""
        table = self.query_one("#queue-datatable", DataTable)
        saved_cursor = table.cursor_row
        filtered = self._apply_filter(self._issues)
        self._filtered_issues = self._apply_view(filtered)
        table.clear()
        if not self._filtered_issues:
            if self._filter_text:
                msg = f"[italic]No matches for '{self._filter_text}'[/]"
            else:
                msg = self._empty_message
            table.add_row("-", "-", "-", msg, "-")
            return
        for issue in self._filtered_issues:
            state_label = (
                "[bold bright_yellow]Held (ready)[/]"
                if issue.id in self._held_issue_ids
                else "[bold green]Runnable[/]"
            )
            pri = str(issue.priority) if issue.priority else "-"
            table.add_row(issue.id, state_label, pri, issue.title, issue.created)
        if table.row_count > 0:
            table.move_cursor(row=min(saved_cursor, table.row_count - 1))

    def _empty_frontier_message(self, snap: DashboardSnapshot) -> str:
        """Return the most helpful empty-state message for the queue panel."""
        if snap.queue_error and snap.queue_breakdown is None:
            return "[italic]Queue refresh failed — no cached frontier[/]"
        if snap.queue_breakdown is None:
            return "[italic]No issues in dispatch frontier[/]"
        if snap.queue_breakdown.beads_ready == 0:
            return "[italic]No Beads-ready issues[/]"
        if snap.queue_breakdown.runnable == 0:
            return "[italic]No runnable issues — see dispatch diagnostics above[/]"
        return "[italic]No issues in dispatch frontier[/]"

    def _update_diagnostics(self, snap: DashboardSnapshot) -> None:
        """Show grouped skip reasons and held-ready diagnostics."""
        summary = self.query_one("#queue-summary", Label)
        diagnostics = self.query_one("#queue-diagnostics", Label)

        if snap.queue_breakdown is None:
            if snap.queue_error:
                summary.update("[italic]Dispatch frontier unavailable[/]")
                diagnostics.update(
                    f"[bold yellow]Queue refresh failed[/] — {snap.queue_error}"
                )
            else:
                summary.update("[italic]Dispatch frontier: —[/]")
                diagnostics.update("[italic]Dispatch diagnostics: —[/]")
            return

        bd = snap.queue_breakdown
        summary.update(
            "[italic]Dispatch frontier: "
            f"{bd.beads_ready} beads-ready | {bd.runnable} runnable | "
            f"{bd.held_and_ready} held-ready | {bd.policy_skipped} skipped by policy[/]"
        )

        lines: list[str] = []
        if snap.queue_error:
            lines.append(
                "[bold yellow]Queue refresh failed[/] — showing last good queue view."
            )
        if bd.beads_ready == 0:
            lines.append("[italic]No Beads-ready issues.[/]")
        elif bd.runnable == 0:
            reasons: list[str] = []
            if bd.policy_skipped:
                reasons.append(f"{bd.policy_skipped} skipped by dispatch policy")
            if bd.held_and_ready:
                reasons.append(f"{bd.held_and_ready} held locally")
            lines.append(
                "[bold yellow]No runnable issues[/]"
                + (f" — {' and '.join(reasons)}." if reasons else ".")
            )
        if snap.queue_skip_summary:
            lines.append(
                "[italic]Policy skips:[/] "
                + ", ".join(
                    f"{category}: {count}"
                    for category, count in snap.queue_skip_summary.items()
                )
            )
        held_ready_ids = [
            issue.id for issue in snap.ready_issues if issue.id in self._held_issue_ids
        ]
        if held_ready_ids:
            preview = ", ".join(held_ready_ids[:3])
            extra = ""
            if len(held_ready_ids) > 3:
                extra = f" (+{len(held_ready_ids) - 3} more)"
            lines.append(f"[italic]Held-ready:[/] {preview}{extra}")

        diagnostics.update(
            "\n".join(lines) if lines else "[italic]Dispatch diagnostics: —[/]"
        )

    def update_snapshot(self, snap: DashboardSnapshot) -> None:
        held_issue_ids = set(snap.state.issue_failures)
        self._held_issue_ids = held_issue_ids
        self._update_diagnostics(snap)
        self._empty_message = self._empty_frontier_message(snap)
        render_key = (
            tuple(
                (
                    issue.id,
                    issue.title,
                    issue.priority,
                    issue.created,
                    issue.id in held_issue_ids,
                )
                for issue in snap.ready_issues
            ),
            self._empty_message,
        )
        if render_key == self._render_key:
            return
        self._issues = list(snap.ready_issues)
        self._render_key = render_key
        if snap.queue_breakdown is not None:
            self._policy_skipped = snap.queue_breakdown.policy_skipped
        self._rebuild_table()

    def action_cycle_view(self) -> None:
        """Cycle local views: Beads order → newest → oldest."""
        idx = self._VIEW_MODES.index(self._view_mode)
        self._view_mode = self._VIEW_MODES[(idx + 1) % len(self._VIEW_MODES)]
        self.query_one(".panel-title", Label).update(self._panel_title_text())
        self._render_key = ()  # force re-render
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
            self._render_key = ()
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
        state_label = "Held (ready)" if issue.id in self._held_issue_ids else "Runnable"
        title = f"Issue: {issue.id}"
        lines = [
            f"[bold]Title:[/] {issue.title}",
            f"[bold]Dispatch state:[/] {state_label}",
            f"[bold]Priority:[/] {issue.priority}",
            f"[bold]Created:[/] {issue.created}",
        ]
        if issue.description:
            lines.append(f"\n[bold]Description:[/]\n{issue.description}")
        if issue.acceptance_criteria:
            lines.append(
                f"\n[bold]Acceptance Criteria:[/]\n{issue.acceptance_criteria}"
            )
        from orc.tui.modals import InspectModal

        self.app.push_screen(InspectModal(title=title, body="\n".join(lines)))


class EventsLog(Static):
    """Live events log with delta-based appending and error-only toggle."""

    DEFAULT_CSS = """
    EventsLog {
        height: 1fr;
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
        return f"[italic]{ts}[/] [{sev_style}]\\[{severity}][/] [{color}]{message}[/]"

    def _filter_events(self, events: list[dict]) -> list[dict]:
        """Filter events based on current error-only setting."""
        if not self._errors_only:
            return events
        return [e for e in events if _event_severity(e.get("event_type", "")) == "ERR"]

    def compose(self) -> ComposeResult:
        yield Label("Events", classes="panel-title")
        yield RichLog(id="events-richlog", wrap=True, max_lines=200, markup=True)

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
        yield Label("Run History", classes="panel-title")
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
            from orc.tui.modals import _THREAD_URL_PREFIX

            thread_url = f"{_THREAD_URL_PREFIX}{run['thread_id']}"
            lines.append(f"[bold]Thread:[/] {run['thread_id']}")
            lines.append(f"[bold]Thread URL:[/] {thread_url}")
            from orc.tui.modals import build_thread_continue_cmd

            continue_cmd = build_thread_continue_cmd(
                run["thread_id"], run.get("worktree_path")
            )
            lines.append(f"[bold]Debug cmd:[/] {continue_cmd}")
        if run.get("summary"):
            lines.append(f"\n[bold]Summary:[/]\n{run['summary']}")
        from orc.tui.modals import CopyableField, InspectModal, _THREAD_URL_PREFIX

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
            copyable.append(
                CopyableField(
                    label="Debug cmd",
                    value=build_thread_continue_cmd(
                        run["thread_id"], run.get("worktree_path")
                    ),
                    key="d",
                )
            )
        if run.get("branch"):
            copyable.append(CopyableField(label="Branch", value=run["branch"], key="b"))
        if run.get("worktree_path"):
            copyable.append(CopyableField(label="Worktree", value=run["worktree_path"], key="w"))

        self.app.push_screen(
            InspectModal(title=title, body="\n".join(lines), copyable_fields=copyable)
        )
