"""Modal screens for the orc TUI."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Static


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


class ConfirmRetryModal(ModalScreen[str | None]):
    """Confirmation modal before retrying a held issue."""

    DEFAULT_CSS = """
    ConfirmRetryModal {
        align: center middle;
    }
    #retry-dialog {
        width: 60;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #retry-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #retry-buttons {
        height: auto;
        margin-top: 1;
    }
    #retry-buttons Button {
        margin: 0 1;
    }
    #retry-buttons Button:focus {
        text-style: bold reverse;
    }
    #retry-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def __init__(self, issue_id: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._issue_id = issue_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="retry-dialog"):
            yield Label(f"Retry {self._issue_id}?", id="retry-title")
            yield Static("This will clear the held/failed status and re-queue the issue for processing.")
            with Horizontal(id="retry-buttons"):
                yield Button("Cancel", variant="default", id="retry-no")
                yield Button("Retry", variant="warning", id="retry-yes")
            yield Static("[b]y[/] retry  [b]n[/]/[b]Esc[/] cancel", id="retry-hint")

    def on_mount(self) -> None:
        self.query_one("#retry-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "retry-yes":
            self.dismiss(self._issue_id)
        else:
            self.dismiss(None)

    def action_confirm(self) -> None:
        self.dismiss(self._issue_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _build_help_bindings() -> list[tuple[str, str]]:
    """Generate help bindings from OrchestratorApp.BINDINGS.

    This keeps the help modal always in sync with actual keybindings.
    """
    from orc.tui.app import OrchestratorApp

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


def build_thread_continue_cmd(thread_id: str, worktree_path: str | None = None) -> str:
    """Build a shell command to continue an AMP thread for debugging."""
    cmd = f"amp threads continue {thread_id}"
    if worktree_path:
        return f"cd {shlex.quote(worktree_path)} && {cmd}"
    return cmd


class AmpStreamModal(ModalScreen[None]):
    """Modal that live-tails a per-run AMP stream-json log file."""

    DEFAULT_CSS = """
    AmpStreamModal {
        align: center middle;
    }
    #stream-dialog {
        width: 90%;
        max-width: 140;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #stream-header {
        height: auto;
        margin-bottom: 1;
    }
    #stream-title {
        text-style: bold;
    }
    #stream-thread-info {
        color: $text-muted;
    }
    #stream-log {
        height: 1fr;
        border: solid $primary-background;
    }
    #stream-copy-hint {
        height: auto;
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
        log_path: str,
        header_lines: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._log_path = Path(log_path)
        self._header_lines = header_lines or []
        self._lines_read = 0
        self._thread_id: str | None = None
        self._copy_key_map: dict[str, CopyableField] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="stream-dialog"):
            with Vertical(id="stream-header"):
                yield Label(self._title, id="stream-title")
                if self._header_lines:
                    yield Static("\n".join(self._header_lines))
                yield Static("Thread: pending…", id="stream-thread-info")
            yield RichLog(id="stream-log", wrap=True, highlight=True, markup=False)
            yield Static("", id="stream-copy-hint")

    def on_mount(self) -> None:
        self._tail_timer = self.set_interval(0.5, self._tail_log)

    def _tail_log(self) -> None:
        """Read new lines from the log file and append to the RichLog."""
        if not self._log_path.exists():
            return
        try:
            all_lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        new_lines = all_lines[self._lines_read:]
        if not new_lines:
            return
        self._lines_read = len(all_lines)

        log_widget = self.query_one("#stream-log", RichLog)
        for line in new_lines:
            # Try to extract thread_id from stream JSON
            if self._thread_id is None:
                try:
                    msg = json.loads(line)
                    tid = msg.get("thread_id") or msg.get("threadId")
                    if tid and isinstance(tid, str):
                        self._thread_id = tid
                        self._update_thread_info()
                except (json.JSONDecodeError, AttributeError):
                    pass
            display_line = self._format_stream_line(line)
            if display_line:
                log_widget.write(display_line)

    @staticmethod
    def _format_stream_line(line: str) -> str:
        """Format a stream-json line for display."""
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return line
        msg_type = msg.get("type", "")
        if msg_type == "assistant":
            content = msg.get("message", {}).get("content", [])
            parts: list[str] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    parts.append(f"→ {block.get('name', '?')}")
            if parts:
                return f"[assistant] {' '.join(parts)}"
            return "[assistant] (no content)"
        if msg_type == "user":
            content = msg.get("message", {}).get("content", [])
            parts = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        parts.append(text[:120])
                elif btype == "tool_result":
                    tool_id = block.get("tool_use_id", "")
                    # Shorten the tool ID to last 8 chars
                    short_id = tool_id[-8:] if tool_id else "?"
                    parts.append(f"← result({short_id})")
            if parts:
                return f"[user] {' | '.join(parts)}"
            return "[user]"
        if msg_type == "tool_result":
            return f"[tool_result] {str(msg.get('content', ''))[:150]}"
        if msg_type == "result":
            is_err = msg.get("is_error", False)
            usage = msg.get("usage", {})
            pct = ""
            if usage:
                inp = usage.get("input_tokens", 0)
                mx = usage.get("max_tokens", 0)
                if mx:
                    pct = f" ctx={round(inp / mx * 100)}%"
            return f"[result] error={is_err}{pct}"
        if msg_type == "session_start":
            return "[session] started"
        return ""

    def _update_thread_info(self) -> None:
        """Update thread info label and copy hints once thread_id is known."""
        if not self._thread_id:
            return
        thread_url = f"{_THREAD_URL_PREFIX}{self._thread_id}"
        continue_cmd = build_thread_continue_cmd(self._thread_id)
        info = self.query_one("#stream-thread-info", Static)
        info.update(
            f"Thread: {self._thread_id}\n"
            f"URL: {thread_url}\n"
            f"Debug: {continue_cmd}"
        )
        self._copy_key_map = {
            "t": CopyableField(label="Thread ID", value=self._thread_id, key="t"),
            "u": CopyableField(label="Thread URL", value=thread_url, key="u"),
            "d": CopyableField(label="Debug cmd", value=continue_cmd, key="d"),
        }
        hints = "  ".join(
            f"[bold]{f.key}[/] copy {f.label.lower()}"
            for f in self._copy_key_map.values()
        )
        self.query_one("#stream-copy-hint", Static).update(hints)

    async def on_key(self, event) -> None:
        """Handle copy key presses."""
        cf = self._copy_key_map.get(event.key)
        if cf is not None:
            event.stop()
            self.app.copy_to_clipboard(cf.value)
            self.app.notify(f"Copied {cf.label}: {cf.value}", timeout=2)
