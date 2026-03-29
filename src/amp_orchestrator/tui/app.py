"""Textual TUI application for amp-orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.state import OrchestratorMode
from amp_orchestrator.tui.snapshot import (
    DashboardSnapshot,
    load_snapshot,
    load_snapshot_fast,
)
from amp_orchestrator.tui.widgets import (
    _ACTION_ENABLED,
    ActiveIssuePanel,
    ConfigPanel,
    ControlsPanel,
    EventsLog,
    HistoryTable,
    NotConnectedBanner,
    QueueTable,
    StaleBanner,
    StatusPanel,
    SummaryStrip,
)


_STALE_THRESHOLD_SECS = 10


class OrchestratorApp(App):
    """TUI dashboard for amp-orchestrator."""

    TITLE = "amp-orchestrator"

    CSS = """
    #left-col {
        width: 1fr;
    }
    #right-col {
        width: 2fr;
    }
    #main-area {
        height: 1fr;
    }
    """

    BINDINGS = [
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
        self._last_successful_refresh: datetime | None = None
        self._last_queue_refresh: datetime | None = None
        self._last_refresh_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield SummaryStrip()
        yield NotConnectedBanner()
        yield StaleBanner()
        with Horizontal(id="main-area"):
            with Vertical(id="left-col"):
                yield StatusPanel()
                yield ActiveIssuePanel()
                yield ConfigPanel()
                yield ControlsPanel()
            with Vertical(id="right-col"):
                yield QueueTable()
                yield EventsLog()
                yield HistoryTable()
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
        self.query_one(SummaryStrip).show_no_project()
        self.query_one(StatusPanel).show_no_project()
        self.query_one(ActiveIssuePanel).show_no_project()
        self.query_one(ConfigPanel).show_no_project()
        self.query_one(ControlsPanel).show_no_project()
        self.query_one(QueueTable).show_no_project()
        self.query_one(EventsLog).show_no_project()
        self.query_one(HistoryTable).show_no_project()

    def _load_config(self) -> None:
        """Load config once on startup."""
        if not self._repo_root:
            return
        try:
            from amp_orchestrator.config import load_config

            self._config = load_config(self._repo_root)
        except Exception:
            pass

    @work(thread=True)
    def _do_fast_refresh(self) -> None:
        """Refresh state and events (runs in thread)."""
        if not self._state_dir or self._frozen:
            return
        try:
            snap = load_snapshot_fast(self._state_dir, self._config)
            self.call_from_thread(self._mark_refresh_success)
            self.call_from_thread(self._apply_fast_snapshot, snap)
        except Exception as exc:
            self.call_from_thread(self._mark_refresh_error, str(exc))

    @work(thread=True)
    def _do_queue_refresh(self) -> None:
        """Refresh queue (runs in thread)."""
        if not self._repo_root or not self._state_dir or self._frozen:
            return
        try:
            snap = load_snapshot(self._repo_root, self._state_dir)
            self.call_from_thread(self._mark_refresh_success, True)
            self.call_from_thread(self._apply_snapshot, snap)
        except Exception as exc:
            self.call_from_thread(self._mark_refresh_error, str(exc))

    @work(thread=True)
    def _do_full_refresh(self) -> None:
        """Full refresh (runs in thread)."""
        if not self._repo_root or not self._state_dir:
            return
        try:
            snap = load_snapshot(self._repo_root, self._state_dir)
            self.call_from_thread(self._mark_refresh_success, True)
            self.call_from_thread(self._apply_snapshot, snap)
        except Exception as exc:
            self.call_from_thread(self._mark_refresh_error, str(exc))

    def _mark_refresh_success(self, includes_queue: bool = False) -> None:
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
        """Record a failed refresh."""
        self._last_refresh_error = message

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
        self._do_full_refresh()
        self._frozen = was_frozen
        if was_frozen:
            self.query_one(SummaryStrip).show_frozen()

    def action_freeze(self) -> None:
        """Toggle freeze/unfreeze of live dashboard updates."""
        self._frozen = not self._frozen
        if self._frozen:
            self.notify("Live updates frozen", severity="warning")
            self.query_one(SummaryStrip).show_frozen()
        else:
            self.notify("Live updates resumed")
            self.query_one(SummaryStrip).hide_frozen()
            self._do_full_refresh()

    def action_toggle_config(self) -> None:
        """Toggle ConfigPanel visibility."""
        self.query_one(ConfigPanel).toggle_class("visible")

    def action_help(self) -> None:
        """Show help modal with key bindings."""
        from amp_orchestrator.tui.modals import HelpModal

        self.push_screen(HelpModal())

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
            self.query_one(SummaryStrip).update_snapshot(snap)
            self.query_one(StatusPanel).update_snapshot(snap)
            self.query_one(ControlsPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)

    def _apply_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update all panels."""
        self._orch_mode = snap.state.mode
        suppress = self._check_pending_action(snap)
        if not suppress:
            self.query_one(SummaryStrip).update_snapshot(snap)
            self.query_one(StatusPanel).update_snapshot(snap)
            self.query_one(ControlsPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(ConfigPanel).update_snapshot(snap)
        self.query_one(QueueTable).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle control panel button clicks."""
        actions = {
            "btn-start": self.action_start,
            "btn-pause": self.action_pause,
            "btn-resume": self.action_resume,
            "btn-stop": self.action_stop,
        }
        handler = actions.get(event.button.id or "")
        if handler:
            handler()

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
        from amp_orchestrator.tui.modals import ConfirmStopModal

        self.push_screen(ConfirmStopModal(), self._on_stop_confirmed)

    def _on_stop_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            self._run_control_action("stop")

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
        self.query_one(SummaryStrip).show_transitional(label)
        self.query_one(StatusPanel).show_transitional(label)
        self.query_one(ControlsPanel).disable_all()

    @work(thread=True)
    def _run_control_action(self, action: str) -> None:
        """Run a control action in a background thread."""
        import click

        self.call_from_thread(self._show_transitional_feedback, action)
        success_msg = self._ACTION_LABELS.get(action, ("", f"{action.capitalize()} succeeded"))[1]

        try:
            if action in ("start", "resume"):
                from amp_orchestrator.subprocess_launcher import (
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
                from amp_orchestrator.control import (
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
