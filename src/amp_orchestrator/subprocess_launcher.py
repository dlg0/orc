"""Subprocess launcher for running the orchestrator from the TUI.

Spawns `amp-orchestrator start` or `amp-orchestrator resume` as a detached
background process so the TUI remains responsive.  Output is redirected to
a log file under the state directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


LOG_FILE = "orchestrator.log"


def launch_orchestrator(
    command: str,
    repo_root: Path,
    state_dir: Path,
) -> subprocess.Popen:
    """Spawn the orchestrator as a background subprocess.

    Args:
        command: Either ``"start"`` or ``"resume"``.
        repo_root: Project repository root.
        state_dir: State directory (for the log file).

    Returns:
        The :class:`subprocess.Popen` handle.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / LOG_FILE

    log_fh = log_path.open("a")

    proc = subprocess.Popen(
        [sys.executable, "-m", "amp_orchestrator.cli", command],
        cwd=str(repo_root),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc


def is_orchestrator_running(state_dir: Path) -> bool:
    """Check whether an orchestrator subprocess is running via the PID file."""
    pid_path = state_dir / "orchestrator.pid"
    if not pid_path.exists():
        return False
    try:
        import os

        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # signal 0 — check if alive
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        pid_path.unlink(missing_ok=True)
        return False
