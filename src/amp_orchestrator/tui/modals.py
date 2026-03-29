"""Modal screens for the amp-orchestrator TUI."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


@dataclass
class CopyableField:
    """A field whose value can be copied to the clipboard from the modal."""

    label: str
    value: str
    key: str  # single key to trigger copy (e.g. "t" for thread)


# Ampcode thread URL prefix.
_THREAD_URL_PREFIX = "https://ampcode.com/threads/"


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
    #inspect-copy-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(
        self,
        title: str,
        body: str,
        copyable_fields: list[CopyableField] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._body = body
        self._copyable_fields = copyable_fields or []
        self._copy_key_map: dict[str, CopyableField] = {
            f.key: f for f in self._copyable_fields
        }

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="inspect-dialog"):
            yield Label(self._title, id="inspect-title")
            yield Static(self._body, id="inspect-body")
            if self._copyable_fields:
                hints = "  ".join(
                    f"[bold]{f.key}[/] copy {f.label.lower()}"
                    for f in self._copyable_fields
                )
                yield Static(hints, id="inspect-copy-hint")

    async def on_key(self, event) -> None:
        """Handle copy key presses for copyable fields."""
        cf = self._copy_key_map.get(event.key)
        if cf is not None:
            event.stop()
            self.app.copy_to_clipboard(cf.value)
            self.app.notify(f"Copied {cf.label}: {cf.value}", timeout=2)


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
    #confirm-buttons Button:focus {
        text-style: bold reverse;
    }
    #confirm-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-dialog"):
            yield Label("Stop Orchestrator?", id="confirm-title")
            yield Static("The orchestrator will stop after the current issue reaches a safe checkpoint.")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", variant="default", id="confirm-no")
                yield Button("Stop", variant="error", id="confirm-yes")
            yield Static("[b]y[/] stop  [b]n[/]/[b]Esc[/] cancel", id="confirm-hint")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def _build_help_bindings() -> list[tuple[str, str]]:
    """Generate help bindings from OrchestratorApp.BINDINGS.

    This keeps the help modal always in sync with actual keybindings.
    """
    from amp_orchestrator.tui.app import OrchestratorApp

    # Friendly display names for Textual key identifiers
    _KEY_DISPLAY: dict[str, str] = {
        "question_mark": "?",
        "tab": "Tab",
        "shift+tab": "Shift+Tab",
    }

    bindings: list[tuple[str, str]] = []
    for binding in OrchestratorApp.BINDINGS:
        if isinstance(binding, tuple):
            key, _action, description = binding
        else:
            key = binding.key
            description = binding.description or binding.action
        display_key = _KEY_DISPLAY.get(key, key)
        bindings.append((display_key, description))
    return bindings


def get_help_bindings() -> list[tuple[str, str]]:
    """Public accessor for the generated help bindings list."""
    return _build_help_bindings()


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
            for key, desc in _build_help_bindings():
                yield Static(f"  [bold]{key:<16}[/] {desc}")
