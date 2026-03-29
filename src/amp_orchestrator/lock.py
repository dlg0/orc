"""File-based locking for amp-orchestrator."""

from __future__ import annotations

import os
from pathlib import Path


class OrchestratorLock:
    """Simple file-based lock using PID to detect stale locks."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._lock_file = state_dir / "lock"

    def _pid_alive(self, pid: int) -> bool:
        """Check whether *pid* refers to a running process."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't own it – still alive.
            return True
        return True

    def acquire(self) -> bool:
        """Create lock file with current PID.

        Returns ``False`` if the lock is already held by a live process.
        Cleans up stale locks (dead PID) automatically.
        """
        if self._lock_file.exists():
            try:
                existing_pid = int(self._lock_file.read_text().strip())
            except (ValueError, OSError):
                # Corrupt lock file – treat as stale.
                self._lock_file.unlink(missing_ok=True)
            else:
                if self._pid_alive(existing_pid):
                    return False
                # Stale lock – previous holder is dead.
                self._lock_file.unlink(missing_ok=True)

        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file.write_text(str(os.getpid()))
        return True

    def release(self) -> None:
        """Remove the lock file."""
        self._lock_file.unlink(missing_ok=True)

    def is_locked(self) -> bool:
        """Return ``True`` if the lock is held by a live process."""
        if not self._lock_file.exists():
            return False
        try:
            pid = int(self._lock_file.read_text().strip())
        except (ValueError, OSError):
            return False
        return self._pid_alive(pid)

    def __enter__(self) -> OrchestratorLock:
        if not self.acquire():
            raise RuntimeError("Failed to acquire orchestrator lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.release()
