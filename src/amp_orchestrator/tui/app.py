"""Textual TUI application for amp-orchestrator."""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.tui.snapshot import (
    DashboardSnapshot,
    load_snapshot,
    load_snapshot_fast,
)
from amp_orchestrator.tui.widgets import (
    ActiveIssuePanel,
    ConfigPanel,
    EventsLog,
    HistoryTable,
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
        ("tab", "focus_next", "Next Panel"),
        ("shift+tab", "focus_previous", "Prev Panel"),
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
        with Horizontal(id="main-area"):
            with Vertical(id="left-col"):
                yield StatusPanel()
                yield ActiveIssuePanel()
                yield ConfigPanel()
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

    def _apply_fast_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update only state/events/history panels."""
        self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)

    def _apply_snapshot(self, snap: DashboardSnapshot) -> None:
        """Update all panels."""
        self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(ConfigPanel).update_snapshot(snap)
        self.query_one(QueueTable).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)
