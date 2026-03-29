"""Modal screens for the amp-orchestrator TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class InspectModal(ModalScreen[None]):
    """Modal that shows detailed info about a queue item or history entry."""

    DEFAULT_CSS = """
    InspectModal {
        align: center middle;
    }
    #inspect-dialog {
        width: 80%;
        max-width: 100;
        height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #inspect-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, title: str, body: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="inspect-dialog"):
            yield Label(self._title, id="inspect-title")
            yield Static(self._body, id="inspect-body")


class ConfirmStopModal(ModalScreen[bool]):
    """Confirmation modal before stopping the orchestrator."""

    DEFAULT_CSS = """
    ConfirmStopModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: auto;
        margin-top: 1;
    }
    #confirm-buttons Button {
        margin: 0 1;
    }
    #confirm-yes:focus {
        text-style: bold reverse;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "confirm", "Confirm"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-dialog"):
            yield Label("Stop Orchestrator?", id="confirm-title")
            yield Static("The orchestrator will stop after the current issue reaches a safe checkpoint.")
            with Horizontal(id="confirm-buttons"):
                yield Button("Stop", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="default", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


_HELP_BINDINGS = [
    ("q", "Quit"),
    ("r", "Refresh"),
    ("s", "Start orchestrator"),
    ("p", "Pause orchestrator"),
    ("u", "Resume orchestrator"),
    ("x", "Stop orchestrator"),
    ("i / Enter", "Inspect selected item"),
    ("Tab", "Next focus"),
    ("Shift+Tab", "Previous focus"),
    ("?", "Toggle this help"),
]


class HelpModal(ModalScreen[None]):
    """Overlay showing all key bindings."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    #help-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #help-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Label("Key Bindings", id="help-title")
            for key, desc in _HELP_BINDINGS:
                yield Static(f"  [bold]{key:<16}[/] {desc}")
