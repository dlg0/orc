"""Textual TUI application for amp-orchestrator."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer


class OrchestratorApp(App):
    """TUI dashboard for amp-orchestrator."""

    TITLE = "amp-orchestrator"
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()
