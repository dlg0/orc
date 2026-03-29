"""Tests for OrchestratorLock."""

import os

from amp_orchestrator.lock import OrchestratorLock


def test_acquire_fresh(tmp_path):
    lock = OrchestratorLock(tmp_path)
    assert lock.acquire() is True
    assert lock.is_locked() is True
    lock.release()


def test_acquire_fails_when_held(tmp_path):
    lock = OrchestratorLock(tmp_path)
    assert lock.acquire() is True

    lock2 = OrchestratorLock(tmp_path)
    assert lock2.acquire() is False

    lock.release()


def test_release_clears_lock(tmp_path):
    lock = OrchestratorLock(tmp_path)
    lock.acquire()
    lock.release()
    assert lock.is_locked() is False
    assert not (tmp_path / "lock").exists()


def test_stale_lock_cleaned_up(tmp_path):
    lock_file = tmp_path / "lock"
    # Write a PID that cannot exist (very high number).
    lock_file.write_text("9999999")

    lock = OrchestratorLock(tmp_path)
    assert lock.acquire() is True
    assert int(lock_file.read_text()) == os.getpid()
    lock.release()


def test_context_manager(tmp_path):
    with OrchestratorLock(tmp_path) as lock:
        assert lock.is_locked() is True

    assert not (tmp_path / "lock").exists()


def test_context_manager_fails_when_held(tmp_path):
    lock = OrchestratorLock(tmp_path)
    lock.acquire()

    try:
        with OrchestratorLock(tmp_path):
            assert False, "Should not reach here"
    except RuntimeError:
        pass

    lock.release()
