"""Textual TUI application for amp-orchestrator."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from amp_orchestrator.tui.snapshot import DashboardSnapshot, load_snapshot
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
            self._refresh_snapshot()

    def _refresh_snapshot(self) -> None:
        if not self._repo_root or not self._state_dir:
            return
        snap = load_snapshot(self._repo_root, self._state_dir)
        self._apply_snapshot(snap)

    def _apply_snapshot(self, snap: DashboardSnapshot) -> None:
        self.query_one(StatusPanel).update_snapshot(snap)
        self.query_one(ActiveIssuePanel).update_snapshot(snap)
        self.query_one(ConfigPanel).update_snapshot(snap)
        self.query_one(QueueTable).update_snapshot(snap)
        self.query_one(EventsLog).update_snapshot(snap)
        self.query_one(HistoryTable).update_snapshot(snap)
