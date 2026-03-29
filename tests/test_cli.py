"""Tests for the CLI entry point."""

from click.testing import CliRunner

from amp_orchestrator.cli import main


def test_status_shows_idle() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "idle" in result.output.lower()


def test_help_shows_all_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ["status", "start", "pause", "resume", "stop", "inspect", "logs", "init-config"]:
        assert cmd in result.output


def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output
