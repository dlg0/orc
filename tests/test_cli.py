"""Tests for the CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from orc.cli import main
from orc.control import start_orchestrator
from orc.queue import QueueResult
from orc.state import OrchestratorMode, OrchestratorState, RunCheckpoint, RunStage, StateStore


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal fake project with .git and .beads dirs."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".beads").mkdir()
    return tmp_path


def test_help_shows_all_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ["status", "start", "pause", "resume", "stop", "inspect", "logs", "init-config", "tui", "unhold"]:
        assert cmd in result.output


def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_status_shows_mode(tmp_path: Path) -> None:
    _make_project(tmp_path)
    with patch("orc.cli._get_state_dir", return_value=tmp_path / ".orc"):
        with patch("orc.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "idle" in result.output.lower()


def test_status_shows_active_issue(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".orc"
    state_dir.mkdir()
    store = StateStore(state_dir)
    checkpoint = RunCheckpoint(
        issue_id="bz1.5",
        issue_title="Foo",
        branch="amp/bz1.5-foo",
        worktree_path="/tmp/wt",
        stage=RunStage.amp_running,
    )
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_run=checkpoint.to_dict(),
    )
    store.save(state)

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        with patch("orc.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "bz1.5" in result.output
            assert "running" in result.output.lower()


def test_init_config_creates_file(tmp_path: Path) -> None:
    _make_project(tmp_path)
    with patch("orc.cli.detect_project") as mock_detect:
        from orc.config import ProjectContext
        mock_detect.return_value = ProjectContext(
            repo_root=tmp_path, has_git=True, has_beads=True
        )
        runner = CliRunner()
        result = runner.invoke(main, ["init-config"])
        assert result.exit_code == 0
        assert "Config created" in result.output
        assert (tmp_path / ".orc" / "config.yaml").exists()


def test_pause_from_idle_fails(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["pause"])
        assert result.exit_code != 0
        assert "Cannot pause" in result.output


def test_start_runs_and_goes_idle(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    from orc.config import ProjectContext

    with (
        patch("orc.cli.detect_project", return_value=ProjectContext(repo_root=tmp_path, has_git=True, has_beads=True)),
        patch("orc.cli.start_orchestrator"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["start"])
        assert result.exit_code == 0


def test_start_refuses_when_locked(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    from orc.config import ProjectContext
    from orc.lock import OrchestratorLock
    lock = OrchestratorLock(state_dir)
    lock.acquire()

    try:
        with patch("orc.cli.detect_project", return_value=ProjectContext(repo_root=tmp_path, has_git=True, has_beads=True)):
            runner = CliRunner()
            result = runner.invoke(main, ["start"])
            assert result.exit_code != 0
            assert "lock" in result.output.lower()
    finally:
        lock.release()


def test_stop_from_running(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.running))

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["stop"])
        assert result.exit_code == 0
        assert "stop" in result.output.lower()

        reloaded = store.load()
        assert reloaded.mode == OrchestratorMode.stopping


def test_resume_from_paused(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.paused))

    from orc.config import ProjectContext

    with (
        patch("orc.cli.detect_project", return_value=ProjectContext(repo_root=tmp_path, has_git=True, has_beads=True)),
        patch("orc.cli.resume_orchestrator"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0


def test_logs_empty(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["logs"])
        assert result.exit_code == 0
        assert "No events" in result.output


def test_unhold_clears_failure(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        issue_failures={"bz5": {
            "category": "issue_needs_rework",
            "action": "hold_for_retry",
            "stage": "evaluation",
            "summary": "Missing tests",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "attempts": 1,
        }},
    )
    store.save(state)

    with (
        patch("orc.cli._get_state_dir", return_value=state_dir),
        patch("orc.cli.get_issue_status", return_value="open"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["unhold", "bz5"])
        assert result.exit_code == 0
        assert "Removed hold for bz5" in result.output

    reloaded = store.load()
    assert "bz5" not in reloaded.issue_failures


def test_unhold_not_in_failures(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["unhold", "bz99"])
        assert result.exit_code != 0
        assert "not in held/failed state" in result.output


def test_unhold_clears_hold_for_conflict_failure(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        issue_failures={"bz6": {
            "category": "stale_or_conflicted",
            "action": "hold_for_retry",
            "stage": "merge/rebase",
            "summary": "Rebase conflict",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "attempts": 1,
            "branch": "amp/bz6-conflict",
            "worktree_path": "/tmp/wt-bz6",
            "preserve_worktree": True,
        }},
    )
    store.save(state)

    with (
        patch("orc.cli._get_state_dir", return_value=state_dir),
        patch("orc.cli.get_issue_status", return_value="open"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["unhold", "bz6"])
        assert result.exit_code == 0
        assert "Removed hold for bz6" in result.output

    reloaded = store.load()
    assert "bz6" not in reloaded.issue_failures
    assert reloaded.resume_candidate is None



def test_status_shows_held_issues(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        issue_failures={
            "bz7": {
                "category": "issue_needs_rework",
                "action": "hold_for_retry",
                "stage": "evaluation",
                "summary": "Tests failing",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "attempts": 1,
            },
        },
    )
    store.save(state)

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        with patch("orc.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "Held issues: 1" in result.output
            assert "[issue_needs_rework]" in result.output
            assert "bz7: Tests failing" in result.output


def test_status_normalizes_legacy_held_issues(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(json.dumps({
        "mode": "idle",
        "last_completed_issue": None,
        "last_error": None,
        "run_history": [],
        "issue_failures": {
            "bz7": {"summary": "Tests failing", "timestamp": "2026-01-01T00:00:00+00:00"},
        },
    }))

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        with patch("orc.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "[issue_needs_rework]" in result.output
            assert "[unknown]" not in result.output


def test_inspect_shows_failure_details(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        issue_failures={
            "bz10": {
                "category": "transient_external",
                "action": "hold_for_retry",
                "stage": "amp_run",
                "summary": "Rate limited",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "attempts": 3,
                "branch": "amp/bz10-fix",
                "worktree_path": "/tmp/wt-bz10",
            },
        },
    )
    store.save(state)

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["inspect", "bz10"])
        assert result.exit_code == 0
        assert "Category: transient_external" in result.output
        assert "Stage: amp_run" in result.output
        assert "Attempts: 3" in result.output
        assert "Branch: amp/bz10-fix" in result.output
        assert "Worktree: /tmp/wt-bz10" in result.output
        assert "Summary: Rate limited" in result.output


def test_inspect_not_found(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["inspect", "bz99"])
        assert result.exit_code != 0
        assert "No run history" in result.output


def test_inspect_shows_entry(tmp_path: Path) -> None:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        run_history=[
            {"issue_id": "bz1", "result": "success", "branch": "amp/bz1-test", "summary": "did stuff"},
        ],
    )
    store.save(state)

    with patch("orc.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["inspect", "bz1"])
        assert result.exit_code == 0
        assert "success" in result.output
        assert "bz1" in result.output
