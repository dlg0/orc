"""Textual TUI application for orc."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header

from orc.config import OrchestratorConfig
from orc.queue import (
    QueueResult,
    compute_queue_breakdown,
    get_issue_status,
    summarize_skipped_issues,
)
from orc.state import OrchestratorMode, StateStore, RequestQueue
from orc.tui.snapshot import (
    DashboardSnapshot,
    load_snapshot,
    load_snapshot_fast,
)
from orc.tui.widgets import (
    _ACTION_ENABLED,
    ActiveIssuePanel,
    ConfigPanel,
    EventsLog,
    HeldIssuesTable,
    HistoryTable,
    NotConnectedBanner,
    QueueTable,
    StaleBanner,
    StatusPanel,
)


_STALE_THRESHOLD_SECS = 10


class OrchestratorApp(App):
    """TUI dashboard for orc."""

    TITLE = "orc"

    CSS = """
    Screen {
        background: #0b0f14;
        color: #d7dde8;
    }

    Header {
        background: #111827;
        color: #d7dde8;
    }

    Footer {
        background: #0f172a;
        color: #93a4b8;
    }

    #main-area {
        height: 1fr;
        overflow-y: auto;
    }

    /* ── shared panel chrome ── */
    .dashboard-panel {
        background: #11161f;
        color: #d7dde8;
        border: round #263041;
        padding: 0 1;
    }

    .dashboard-panel .panel-title {
        padding: 0 1;
        background: #161d2a;
        color: #94a3b8;
        text-style: bold;
    }

    .dashboard-panel:focus,
    .dashboard-panel:focus-within {
        border: round #5ea1ff;
        background: #141b26;
    }

    .dashboard-panel:focus .panel-title,
    .dashboard-panel:focus-within .panel-title {
        background: #1d2a3d;
        color: #ffffff;
    }

    /* ── per-panel accents ── */
    .panel-status { border: round #294236; }
    .panel-status .panel-title { color: #7ee787; }

    .panel-active { border: round #263b5a; }
    .panel-active .panel-title { color: #8fb2ff; }

    .panel-config { border: round #3a3150; }
    .panel-config .panel-title { color: #c4a7e7; }

    .panel-queue { border: round #24404a; }
    .panel-queue .panel-title { color: #7dcfff; }

    .panel-held { border: round #4b3526; }
    .panel-held .panel-title { color: #ffb86c; }

    .panel-events { border: round #30384a; }
    .panel-events .panel-title { color: #9fb0c8; }

    .panel-history { border: round #29423b; }
    .panel-history .panel-title { color: #73daca; }

    /* error state override */
    StatusPanel.error-state {
        border: round #f7768e;
    }

    /* ── filter inputs ── */
    Input.filter-bar {
        background: #0d131c;
        color: #d7dde8;
        border: round #22304a;
    }

    Input.filter-bar:focus {
        border: round #5ea1ff;
    }

    /* ── tables / logs ── */
    DataTable {
        background: #0d131c;
    }

    RichLog {
        background: #0d131c;
    }

    DataTable > .datatable--header {
        background: #182235;
        color: #8fb2ff;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #22304a;
        color: #ffffff;
    }

    /* ── banners ── */
    StaleBanner {
        background: #3b2d12;
        color: #ffe5a3;
    }

    NotConnectedBanner {
        background: #3a151a;
        color: #ffd6db;
    }

    ErrorAlert {
        background: #2b1318;
        color: #ffd7d7;
        border: round #6b1f2b;
    }
    """

    BINDINGS = [
        ("ctrl+c", "ctrl_c", "Quit (×2)"),
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("f", "freeze", "Freeze"),
        ("s", "start", "Start"),
        ("p", "pause", "Pause"),
        ("u", "resume", "Resume"),
        ("x", "stop", "Stop"),
        ("tab", "focus_next", "Next Focus"),
        ("shift+tab", "focus_previous", "Previous Focus"),
        ("c", "toggle_config", "Config"),
        ("question_mark", "help", "Help"),
    ]

    _EXPECTED_MODES: dict[str, set[OrchestratorMode]] = {
        "start": {OrchestratorMode.running},
        "pause": {OrchestratorMode.pause_requested, OrchestratorMode.paused},
        "resume": {OrchestratorMode.running},
        "stop": {OrchestratorMode.stopping, OrchestratorMode.idle},
    }

    def __init__(
        self,
        repo_root: Path | None = None,
        state_dir: Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._repo_root = repo_root
        self._state_dir = state_dir
        self._config = OrchestratorConfig()
        self._pending_action: str | None = None
        self._orch_mode: OrchestratorMode = OrchestratorMode.idle
        self._frozen: bool = False
        self._last_ctrl_c: float = 0.0
        self._last_successful_refresh: datetime | None = None
        self._last_queue_refresh: datetime | None = None
        self._last_refresh_error: str | None = None
        self._last_queue_error: str | None = None
        self._last_config_error: str | None = None
        self._last_good_queue_result: QueueResult | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield NotConnectedBanner()
        yield StaleBanner()
        with Vertical(id="main-area"):
            yield StatusPanel(classes="dashboard-panel panel-status")
            yield ActiveIssuePanel(classes="dashboard-panel panel-active")
            yield ConfigPanel(classes="dashboard-panel panel-config")
            yield QueueTable(classes="dashboard-panel panel-queue")
            yield HeldIssuesTable(classes="dashboard-panel panel-held")
            yield EventsLog(classes="dashboard-panel panel-events")
            yield HistoryTable(classes="dashboard-panel panel-history")
        yield Footer()

    def on_mount(self) -> None:
        if self._repo_root and self._state_dir:
            self._load_config()
            self._do_full_refresh()
            self.set_interval(1.0, self._do_fast_refresh)
            self.set_interval(5.0, self._do_queue_refresh)
            self.set_interval(1.0, self._check_staleness)
        else:
            self._show_no_project()

    def _show_no_project(self) -> None:
        """Switch all panels to the no-project placeholder state."""
        self.query_one(NotConnectedBanner).add_class("visible")
        self.query_one(StatusPanel).show_no_project()
        self.query_one(ActiveIssuePanel).show_no_project()
        self.query_one(ConfigPanel).show_no_project()
        self.query_one(QueueTable).show_no_project()
        self.query_one(HeldIssuesTable).show_no_project()
        self.query_one(EventsLog).show_no_project()
        self.query_one(HistoryTable).show_no_project()

    def _load_config(self) -> None:
        """Load config once on startup."""
        if not self._repo_root:
            return
        try:
            from orc.config import load_config

            self._config = load_config(self._repo_root)
        except Exception as exc:
            self._config = OrchestratorConfig()
            self._last_config_error = str(exc)
            self.notify(
                f"Config load failed: {exc}", severity="warning"
            )

    @work(thread=True)
    def _do_fast_refresh(self) -> None:
        """Refresh state and events (runs in thread)."""
        if not self._state_dir or self._frozen:
            return
        try:
            snap = load_snapshot_fast(self._state_dir, self._config)
            self.call_from_thread(self._apply_loaded_fast_snapshot, snap)
        except Exception as exc:
            self.call_from_thread(self._mark_refresh_error, str(exc))

    @work(thread=True)
    def _do_queue_refresh(self) -> None:
        """Refresh queue (runs in thread)."""
        if not self._repo_root or not self._state_dir or self._frozen:
            return
        try:
            snap = load_snapshot(self._repo_root, self._state_dir)
            self.call_from_thread(self._apply_loaded_snapshot, snap)
        except Exception as exc:
            self.call_from_thread(self._mark_refresh_error, str(exc))

    @work(thread=True)
    def _do_full_refresh(self) -> None:
        """Full refresh (runs in thread)."""
        if not self._repo_root or not self._state_dir:
            return
        try:
            snap = load_snapshot(self._repo_root, self._state_dir)
            self.call_from_thread(self._apply_loaded_snapshot, snap)
        except Exception as exc:
            self.call_from_thread(self._mark_refresh_error, str(exc))

    def _mark_refresh_success(
        self,
        includes_queue: bool = False,
    ) -> None:
        """Record a successful refresh and update the status display."""
        now = datetime.now(timezone.utc)
        self._last_successful_refresh = now
        self._last_refresh_error = None
        status_panel = self.query_one(StatusPanel)
        status_panel.update_last_refreshed(now)
        if includes_queue:
            self._last_queue_refresh = now
            status_panel.update_queue_last_refreshed(now)
        self.query_one(StaleBanner).hide()

    def _mark_refresh_error(self, message: str) -> None:
        """Record a failed refresh and show warning in StatusPanel."""
        self._last_refresh_error = message
        self._update_refresh_error_display()

    def _update_refresh_error_display(self) -> None:
        """Render queue/config warnings without marking the dashboard stale."""
        messages: list[str] = []
        if self._last_refresh_error:
            messages.append(self._last_refresh_error)
        if self._last_queue_error:
            messages.append(f"Queue: {self._last_queue_error}")
        if self._last_config_error:
            messages.append(f"Config: {self._last_config_error}")

        status_panel = self.query_one(StatusPanel)
        if messages:
            status_panel.show_refresh_error(" | ".join(messages))
        else:
            status_panel.hide_refresh_error()

    def _remember_queue_snapshot(self, snap: DashboardSnapshot) -> None:
        """Cache the last successful queue payload for transient queue failures."""
        if (
            snap.queue_result is None
            or not snap.queue_result.success
            or snap.queue_breakdown is None
        ):
            return
        self._last_good_queue_result = snap.queue_result

    def _snapshot_for_display(self, snap: DashboardSnapshot) -> DashboardSnapshot:
        """Reuse the last good queue view when the latest queue refresh failed."""
        if (
            snap.queue_result is None
            or snap.queue_result.success
            or self._last_good_queue_result is None
        ):
            return snap
        return replace(
            snap,
            ready_issues=list(self._last_good_queue_result.issues),
            queue_result=self._last_good_queue_result,
            queue_breakdown=compute_queue_breakdown(
                self._last_good_queue_result,
                snap.state.issue_failures,
            ),
            queue_skip_summary=(
                summarize_skipped_issues(self._last_good_queue_result.skipped)
                if self._last_good_queue_result.skipped
                else None
            ),
        )

    def _apply_loaded_fast_snapshot(self, snap: DashboardSnapshot) -> None:
        """Apply a fast snapshot and preserve queue/config warnings."""
        self._mark_refresh_success()
        self._apply_fast_snapshot(snap)
        self._update_refresh_error_display()

    def _apply_loaded_snapshot(self, snap: DashboardSnapshot) -> None:
        """Apply a full snapshot while preserving queue failure semantics."""
        queue_refresh_ok = snap.queue_result is None or snap.queue_result.success
        self._last_queue_error = None if queue_refresh_ok else snap.queue_error
        self._last_config_error = snap.config_error

        if queue_refresh_ok:
            self._remember_queue_snapshot(snap)

        self._mark_refresh_success(includes_queue=queue_refresh_ok)
        self._apply_snapshot(self._snapshot_for_display(snap))
        self._update_refresh_error_display()

    def _check_staleness(self) -> None:
        """Show/hide the stale banner based on time since last successful refresh."""
        if self._frozen:
            return
        banner = self.query_one(StaleBanner)
        status_panel = self.query_one(StatusPanel)
        if self._last_refresh_error:
            banner.show_error(self._last_refresh_error)
            status_panel.show_stale()
            return
        if self._last_successful_refresh is None:
            return
        elapsed = (datetime.now(timezone.utc) - self._last_successful_refresh).total_seconds()
        if elapsed >= _STALE_THRESHOLD_SECS:
            banner.show_stale(int(elapsed))
            status_panel.show_stale()
        else:
            banner.hide()

    def action_refresh(self) -> None:
        """Manual refresh triggered by 'r' key."""
        was_frozen = self._frozen
        self._frozen = False
        self.notify("Refreshing\u2026")
        self._do_full_refresh()
        self._frozen = was_frozen
        if was_frozen:
            self.query_one(StatusPanel).show_frozen()

    def action_freeze(self) -> None:
        """Toggle freeze/unfreeze of live dashboard updates."""
        self._frozen = not self._frozen
        if self._frozen:
            self.notify("Live updates frozen", severity="warning")
            self.query_one(StatusPanel).show_frozen()
        else:
            self.notify("Live updates resumed")
            self.query_one(StatusPanel).hide_frozen()
            self._do_full_refresh()

    def action_toggle_config(self) -> None:
        """Toggle ConfigPanel visibility."""
        self.query_one(ConfigPanel).toggle_class("visible")

    def action_help(self) -> None:
        """Show help modal with key bindings."""
        from orc.tui.modals import HelpModal

        self.push_screen(HelpModal())

    _CTRL_C_TIMEOUT = 2.0

    def action_ctrl_c(self) -> None:
        """Quit on double Ctrl+C within the timeout window."""
        now = monotonic()
        if now - self._last_ctrl_c <= self._CTRL_C_TIMEOUT:
            self.exit()
            return
        self._last_ctrl_c = now
        self.notify("Press Ctrl+C again to quit", severity="warning")

    def _check_pending_action(self, snap: DashboardSnapshot) -> bool:
        """Check if a pending action's expected mode has been reached.

        Returns True if controls/status should be suppressed (still pending).
        """
        if not self._pending_action:
            return False
        expected = self._EXPECTED_MODES.get(self._pending_action, set())
        if snap.state.mode in expected:
            self._pending_action = None
            return False
        return True

    def _apply_fast_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update only state/events/history panels."""
        self._orch_mode = snap.state.mode
        suppress = self._check_pending_action(snap)
        if not suppress:
            self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(HeldIssuesTable).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)
        self._refresh_open_issue_inspector(snap)

    def _apply_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update all panels."""
        self._orch_mode = snap.state.mode
        suppress = self._check_pending_action(snap)
        if not suppress:
            self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(ConfigPanel).update_snapshot(snap)
        self.query_one(QueueTable).update_snapshot(snap)
        self.query_one(HeldIssuesTable).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)
        self._refresh_open_issue_inspector(snap)

    def _refresh_open_issue_inspector(self, snap: DashboardSnapshot) -> None:
        """Keep an open active issue inspector in sync with the latest snapshot."""
        if not self._state_dir:
            return

        from orc.tui.issue_inspect import IssueInspectScreen

        screen = self.screen
        if isinstance(screen, IssueInspectScreen):
            screen.refresh_active_run(snap.state, self._state_dir)

    # -- Control actions -------------------------------------------------------

    def _is_action_allowed(self, action: str) -> bool:
        """Check if *action* is valid for the current mode.

        Returns ``True`` when the action may proceed.  When it is not
        allowed, a notification is shown and ``False`` is returned so the
        caller can short-circuit.
        """
        allowed_modes = _ACTION_ENABLED.get(action)
        if allowed_modes is None or self._orch_mode in allowed_modes:
            return True
        self.notify(
            f"Cannot {action} while {self._orch_mode.value}",
            severity="warning",
        )
        return False

    def action_start(self) -> None:
        if not self._repo_root or not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        if not self._is_action_allowed("start"):
            return
        self._run_control_action("start")

    def action_pause(self) -> None:
        if not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        if not self._is_action_allowed("pause"):
            return
        self._run_control_action("pause")

    def action_resume(self) -> None:
        if not self._repo_root or not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        if not self._is_action_allowed("resume"):
            return
        self._run_control_action("resume")

    def action_stop(self) -> None:
        if not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        if not self._is_action_allowed("stop"):
            return
        from orc.tui.modals import ConfirmStopModal

        self.push_screen(ConfirmStopModal(), self._on_stop_confirmed)

    def _on_stop_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            self._run_control_action("stop")

    def retry_held_issue(self, issue_id: str) -> None:
        """Clear held/failed status for an issue so it gets re-queued."""
        if not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        self._do_retry_held_issue(issue_id)

    @work(thread=True)
    def _do_retry_held_issue(self, issue_id: str) -> None:
        try:
            rq = RequestQueue(self._state_dir)  # type: ignore[arg-type]
            store = StateStore(self._state_dir)  # type: ignore[arg-type]
            state = store.load()
            if issue_id not in state.issue_failures:
                self.call_from_thread(
                    self.notify, f"{issue_id} is not in held/failed state", severity="warning"
                )
                return
            issue_status = get_issue_status(issue_id, cwd=self._repo_root or self._state_dir.parent)
            if issue_status == "closed":
                rq.enqueue("unhold", issue_id=issue_id)
                message = f"{issue_id} is already closed in beads — queued removal from held list"
            else:
                rq.enqueue("unhold", issue_id=issue_id)
                message = f"Queued retry for {issue_id} — will be cleared on next scheduler save"
            self.call_from_thread(
                self.notify, message
            )
            self.call_from_thread(self._do_full_refresh)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Retry failed: {exc}", severity="error"
            )

    _ACTION_LABELS: dict[str, tuple[str, str]] = {
        "start": ("Starting\u2026", "Orchestrator started"),
        "pause": ("Pausing\u2026", "Orchestrator paused"),
        "resume": ("Resuming\u2026", "Orchestrator resumed"),
        "stop": ("Stopping\u2026", "Orchestrator stopped"),
    }

    def _clear_pending_action(self) -> None:
        """Clear the pending action guard so refreshes resume normally."""
        self._pending_action = None

    def _show_transitional_feedback(self, action: str) -> None:
        """Immediately disable controls and show transitional status."""
        self._pending_action = action
        label = self._ACTION_LABELS.get(action, (f"{action.capitalize()}\u2026",))[0]
        self.query_one(StatusPanel).show_transitional(label)

    @work(thread=True)
    def _run_control_action(self, action: str) -> None:
        """Run a control action in a background thread."""
        import click

        self.call_from_thread(self._show_transitional_feedback, action)
        success_msg = self._ACTION_LABELS.get(action, ("", f"{action.capitalize()} succeeded"))[1]

        try:
            if action in ("start", "resume"):
                from orc.subprocess_launcher import (
                    launch_orchestrator,
                )

                proc = launch_orchestrator(
                    action,
                    self._repo_root,  # type: ignore[arg-type]
                    self._state_dir,  # type: ignore[arg-type]
                )
                self.call_from_thread(
                    self.notify,
                    f"{success_msg} (pid {proc.pid})",
                )
            else:
                from orc.control import (
                    pause_orchestrator,
                    stop_orchestrator,
                )

                if action == "pause":
                    pause_orchestrator(self._state_dir)  # type: ignore[arg-type]
                elif action == "stop":
                    stop_orchestrator(self._state_dir)  # type: ignore[arg-type]
                self.call_from_thread(
                    self.notify, success_msg
                )
        except click.ClickException as exc:
            self.call_from_thread(self._clear_pending_action)
            self.call_from_thread(
                self.notify, str(exc.message), severity="error"
            )
        except Exception as exc:
            self.call_from_thread(self._clear_pending_action)
            self.call_from_thread(
                self.notify, f"{action} failed: {exc}", severity="error"
            )
        self.call_from_thread(self._do_full_refresh)
