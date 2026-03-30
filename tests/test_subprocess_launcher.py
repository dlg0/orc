"""Tests for the subprocess launcher module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from orc.subprocess_launcher import (
    is_orchestrator_running,
    launch_orchestrator,
)


def test_launch_orchestrator_spawns_subprocess(tmp_path: Path) -> None:
    """launch_orchestrator spawns a Popen with correct args."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with patch("orc.subprocess_launcher.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        proc = launch_orchestrator("start", repo_root, state_dir)

        assert proc.pid == 12345
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[1:] == ["-m", "orc.cli", "start"]
        assert call_args[1]["cwd"] == str(repo_root)
        assert call_args[1]["start_new_session"] is True


def test_launch_orchestrator_resume(tmp_path: Path) -> None:
    """launch_orchestrator passes 'resume' command correctly."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with patch("orc.subprocess_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=99)
        launch_orchestrator("resume", repo_root, state_dir)

        cmd = mock_popen.call_args[0][0]
        assert "resume" in cmd


def test_launch_orchestrator_creates_state_dir(tmp_path: Path) -> None:
    """launch_orchestrator creates state_dir if it doesn't exist."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_dir = tmp_path / "state" / "nested"

    with patch("orc.subprocess_launcher.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=1)
        launch_orchestrator("start", repo_root, state_dir)

    assert state_dir.exists()


def test_is_orchestrator_running_no_pid_file(tmp_path: Path) -> None:
    """Returns False when no PID file exists."""
    assert is_orchestrator_running(tmp_path) is False


def test_is_orchestrator_running_stale_pid(tmp_path: Path) -> None:
    """Returns False and cleans up when PID is stale."""
    pid_path = tmp_path / "orchestrator.pid"
    pid_path.write_text("999999999")

    with patch("os.kill", side_effect=ProcessLookupError):
        result = is_orchestrator_running(tmp_path)

    assert result is False
    assert not pid_path.exists()


def test_is_orchestrator_running_alive(tmp_path: Path) -> None:
    """Returns True when process is alive."""
    pid_path = tmp_path / "orchestrator.pid"
    pid_path.write_text("12345")

    with patch("os.kill") as mock_kill:
        result = is_orchestrator_running(tmp_path)

    assert result is True
    mock_kill.assert_called_once_with(12345, 0)


def test_is_orchestrator_running_invalid_pid(tmp_path: Path) -> None:
    """Returns False for non-numeric PID file content."""
    pid_path = tmp_path / "orchestrator.pid"
    pid_path.write_text("not-a-pid")

    result = is_orchestrator_running(tmp_path)
    assert result is False
