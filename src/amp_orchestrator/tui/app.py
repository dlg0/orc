"""Textual TUI application for amp-orchestrator."""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.tui.snapshot import (
    DashboardSnapshot,
    load_snapshot,
    load_snapshot_fast,
)
from amp_orchestrator.tui.widgets import (
    ActiveIssuePanel,
    ConfigPanel,
    ControlsPanel,
    EventsLog,
    HistoryTable,
    NotConnectedBanner,
    QueueTable,
    StatusPanel,
)


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
        ("s", "start", "Start"),
        ("p", "pause", "Pause"),
        ("u", "resume", "Resume"),
        ("x", "stop", "Stop"),
        ("tab", "focus_next", "Next Panel"),
        ("shift+tab", "focus_previous", "Prev Panel"),
        ("c", "toggle_config", "Config"),
        ("question_mark", "help", "Help"),
    ]

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

    def compose(self) -> ComposeResult:
        yield Header()
        yield NotConnectedBanner()
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
        else:
            self._show_no_project()

    def _show_no_project(self) -> None:
        """Switch all panels to the no-project placeholder state."""
        self.query_one(NotConnectedBanner).add_class("visible")
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
        if not self._state_dir:
            return
        snap = load_snapshot_fast(self._state_dir, self._config)
        self.call_from_thread(self._apply_fast_snapshot, snap)

    @work(thread=True)
    def _do_queue_refresh(self) -> None:
        """Refresh queue (runs in thread)."""
        if not self._repo_root or not self._state_dir:
            return
        snap = load_snapshot(self._repo_root, self._state_dir)
        self.call_from_thread(self._apply_snapshot, snap)

    @work(thread=True)
    def _do_full_refresh(self) -> None:
        """Full refresh (runs in thread)."""
        if not self._repo_root or not self._state_dir:
            return
        snap = load_snapshot(self._repo_root, self._state_dir)
        self.call_from_thread(self._apply_snapshot, snap)

    def action_refresh(self) -> None:
        """Manual refresh triggered by 'r' key."""
        self._do_full_refresh()

    def action_toggle_config(self) -> None:
        """Toggle ConfigPanel visibility."""
        self.query_one(ConfigPanel).toggle_class("visible")

    def action_help(self) -> None:
        """Toggle help overlay."""
        from amp_orchestrator.tui.modals import HelpModal

        self.push_screen(HelpModal())

    def _apply_fast_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update only state/events/history panels."""
        self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(ControlsPanel).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)

    def _apply_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update all panels."""
        self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(ConfigPanel).update_snapshot(snap)
        self.query_one(ControlsPanel).update_snapshot(snap)
        self.query_one(QueueTable).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)

    # -- Control actions -------------------------------------------------------

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
        self._run_control_action("start")

    def action_pause(self) -> None:
        if not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        self._run_control_action("pause")

    def action_resume(self) -> None:
        if not self._repo_root or not self._state_dir:
            self.notify("No project detected", severity="error")
            return
        self._run_control_action("resume")

    def action_stop(self) -> None:
        if not self._state_dir:
            self.notify("No project detected", severity="error")
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

    def _show_transitional_feedback(self, action: str) -> None:
        """Immediately disable controls and show transitional status."""
        label = self._ACTION_LABELS.get(action, (f"{action.capitalize()}\u2026",))[0]
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
            self.call_from_thread(
                self.notify, str(exc.message), severity="error"
            )
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"{action} failed: {exc}", severity="error"
            )
        self.call_from_thread(self._do_full_refresh)
