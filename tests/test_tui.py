"""Tests for the TUI module."""
from __future__ import annotations

from click.testing import CliRunner

from amp_orchestrator.cli import main
from amp_orchestrator.tui.app import OrchestratorApp


def test_orchestrator_app_instantiates() -> None:
    app = OrchestratorApp()
    assert app.TITLE == "amp-orchestrator"


def test_tui_command_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "tui" in result.output
