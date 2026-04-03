"""Tests for the exploration CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from orc.cli import main
from orc.explore.models import ExplorationSummary


def test_explore_dispatch_requires_selection() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["explore", "dispatch"])
    assert result.exit_code != 0
    assert "Select at least one scenario" in result.output


def test_explore_dispatch_runs_named_scenario(tmp_path: Path) -> None:
    summary = ExplorationSummary(output_dir=tmp_path, results=[])
    runner = CliRunner()

    with patch("orc.explore.cli.run_dispatch_exploration", return_value=summary) as mock_run:
        result = runner.invoke(main, ["explore", "dispatch", "--scenario", "simple-independent-workers", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    mock_run.assert_called_once()
    assert "Wrote exploration artifacts" in result.output


def test_explore_dispatch_rejects_unknown_scenario() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["explore", "dispatch", "--scenario", "not-a-scenario"])
    assert result.exit_code != 0
    assert "Unknown scenario" in result.output
