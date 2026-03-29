"""Tests for the CLI entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from amp_orchestrator.cli import main
from amp_orchestrator.control import start_orchestrator
from amp_orchestrator.queue import QueueResult
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal fake project with .git and .beads dirs."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".beads").mkdir()
    return tmp_path


def test_help_shows_all_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ["status", "start", "pause", "resume", "stop", "inspect", "logs", "init-config", "tui", "retry"]:
        assert cmd in result.output


def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_status_shows_mode(tmp_path: Path) -> None:
    _make_project(tmp_path)
    with patch("amp_orchestrator.cli._get_state_dir", return_value=tmp_path / ".amp-orchestrator"):
        with patch("amp_orchestrator.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "idle" in result.output.lower()


def test_status_shows_active_issue(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir()
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.running,
        active_issue_id="bz1.5",
        active_branch="amp/bz1.5-foo",
        active_worktree_path="/tmp/wt",
    )
    store.save(state)

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        with patch("amp_orchestrator.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "bz1.5" in result.output
            assert "running" in result.output.lower()


def test_init_config_creates_file(tmp_path: Path) -> None:
    _make_project(tmp_path)
    with patch("amp_orchestrator.cli.detect_project") as mock_detect:
        from amp_orchestrator.config import ProjectContext
        mock_detect.return_value = ProjectContext(
            repo_root=tmp_path, has_git=True, has_beads=True
        )
        runner = CliRunner()
        result = runner.invoke(main, ["init-config"])
        assert result.exit_code == 0
        assert "Config created" in result.output
        assert (tmp_path / ".amp-orchestrator" / "config.yaml").exists()


def test_pause_from_idle_fails(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["pause"])
        assert result.exit_code != 0
        assert "Cannot pause" in result.output


def test_start_runs_and_goes_idle(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    from amp_orchestrator.config import ProjectContext

    with (
        patch("amp_orchestrator.cli.detect_project", return_value=ProjectContext(repo_root=tmp_path, has_git=True, has_beads=True)),
        patch("amp_orchestrator.cli.start_orchestrator"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["start"])
        assert result.exit_code == 0


def test_start_refuses_when_locked(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    from amp_orchestrator.config import ProjectContext
    from amp_orchestrator.lock import OrchestratorLock
    lock = OrchestratorLock(state_dir)
    lock.acquire()

    try:
        with patch("amp_orchestrator.cli.detect_project", return_value=ProjectContext(repo_root=tmp_path, has_git=True, has_beads=True)):
            runner = CliRunner()
            result = runner.invoke(main, ["start"])
            assert result.exit_code != 0
            assert "lock" in result.output.lower()
    finally:
        lock.release()


def test_stop_from_running(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.running))

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["stop"])
        assert result.exit_code == 0
        assert "stop" in result.output.lower()

        reloaded = store.load()
        assert reloaded.mode == OrchestratorMode.stopping


def test_resume_from_paused(tmp_path: Path) -> None:
    _make_project(tmp_path)
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.paused))

    from amp_orchestrator.config import ProjectContext

    with (
        patch("amp_orchestrator.cli.detect_project", return_value=ProjectContext(repo_root=tmp_path, has_git=True, has_beads=True)),
        patch("amp_orchestrator.cli.resume_orchestrator"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["resume"])
        assert result.exit_code == 0


def test_logs_empty(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["logs"])
        assert result.exit_code == 0
        assert "No events" in result.output


def test_retry_clears_rework(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        issue_failures={"bz5": {"summary": "Missing tests", "timestamp": "2026-01-01T00:00:00+00:00"}},
    )
    store.save(state)

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["retry", "bz5"])
        assert result.exit_code == 0
        assert "Cleared rework status for bz5" in result.output

    reloaded = store.load()
    assert "bz5" not in reloaded.issue_failures


def test_retry_not_in_rework(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["retry", "bz99"])
        assert result.exit_code != 0
        assert "not in rework state" in result.output


def test_status_shows_rework(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        issue_failures={
            "bz7": {"summary": "Tests failing", "timestamp": "2026-01-01T00:00:00+00:00"},
        },
    )
    store.save(state)

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        with patch("amp_orchestrator.cli.get_ready_issues", return_value=QueueResult()):
            runner = CliRunner()
            result = runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "Rework: 1 issue(s) need rework" in result.output
            assert "bz7: Tests failing" in result.output


def test_inspect_not_found(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=OrchestratorMode.idle))

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["inspect", "bz99"])
        assert result.exit_code != 0
        assert "No run history" in result.output


def test_inspect_shows_entry(tmp_path: Path) -> None:
    state_dir = tmp_path / ".amp-orchestrator"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    state = OrchestratorState(
        mode=OrchestratorMode.idle,
        run_history=[
            {"issue_id": "bz1", "result": "success", "branch": "amp/bz1-test", "summary": "did stuff"},
        ],
    )
    store.save(state)

    with patch("amp_orchestrator.cli._get_state_dir", return_value=state_dir):
        runner = CliRunner()
        result = runner.invoke(main, ["inspect", "bz1"])
        assert result.exit_code == 0
        assert "success" in result.output
        assert "bz1" in result.output
