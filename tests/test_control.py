"""Tests for the control/service layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from orc.control import (
    pause_orchestrator,
    resume_orchestrator,
    start_orchestrator,
    stop_orchestrator,
)
from orc.state import OrchestratorMode, OrchestratorState, RunCheckpoint, RunStage, StateStore


def _setup(tmp_path: Path, mode: OrchestratorMode = OrchestratorMode.idle) -> Path:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(parents=True)
    store = StateStore(state_dir)
    store.save(OrchestratorState(mode=mode))
    return state_dir


def test_pause_from_running(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.running)
    pause_orchestrator(state_dir)
    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.pause_requested


def test_pause_from_idle_fails(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    with pytest.raises(Exception, match="Cannot pause"):
        pause_orchestrator(state_dir)


def test_resume_from_paused(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.paused)
    with (
        patch("orc.control.load_config"),
        patch("orc.control.run_loop"),
    ):
        resume_orchestrator(tmp_path, state_dir)
    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.running


def test_resume_from_idle_fails(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    with pytest.raises(Exception, match="Cannot resume"):
        resume_orchestrator(tmp_path, state_dir)


def test_stop_from_running(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.running)
    stop_orchestrator(state_dir)
    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.stopping


def test_stop_from_idle_fails(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    with pytest.raises(Exception, match="Cannot stop"):
        stop_orchestrator(state_dir)


def test_start_acquires_lock_and_runs(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    with (
        patch("orc.control.load_config"),
        patch("orc.control.run_loop"),
    ):
        start_orchestrator(tmp_path, state_dir)


def test_stop_from_pause_requested(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.pause_requested)
    stop_orchestrator(state_dir)
    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.stopping


def test_pause_from_paused_fails(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.paused)
    with pytest.raises(Exception, match="Cannot pause"):
        pause_orchestrator(state_dir)


def test_start_crash_recovery(tmp_path: Path) -> None:
    """Start from stale running state (no lock held) triggers crash recovery."""
    state_dir = _setup(tmp_path, OrchestratorMode.running)
    store = StateStore(state_dir)
    state = store.load()
    checkpoint = RunCheckpoint(
        issue_id="X-stale",
        issue_title="Stale issue",
        branch="amp/stale",
        stage=RunStage.amp_running,
    )
    state.active_run = checkpoint.to_dict()
    store.save(state)

    with (
        patch("orc.control.load_config"),
        patch("orc.control.run_loop"),
    ):
        start_orchestrator(tmp_path, state_dir)

    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.running
    assert state.active_issue_id is None  # cleared by crash recovery
    assert state.active_run is None


def test_start_refuses_when_locked(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    from orc.lock import OrchestratorLock
    lock = OrchestratorLock(state_dir)
    lock.acquire()
    try:
        with pytest.raises(Exception, match="lock"):
            start_orchestrator(tmp_path, state_dir)
    finally:
        lock.release()
