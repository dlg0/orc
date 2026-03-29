"""Tests for the control/service layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from amp_orchestrator.control import (
    pause_orchestrator,
    resume_orchestrator,
    start_orchestrator,
    stop_orchestrator,
)
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore


def _setup(tmp_path: Path, mode: OrchestratorMode = OrchestratorMode.idle) -> Path:
    state_dir = tmp_path / ".amp-orchestrator"
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
    resume_orchestrator(state_dir)
    state = StateStore(state_dir).load()
    assert state.mode == OrchestratorMode.running


def test_resume_from_idle_fails(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    with pytest.raises(Exception, match="Cannot resume"):
        resume_orchestrator(state_dir)


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
        patch("amp_orchestrator.control.load_config"),
        patch("amp_orchestrator.control.run_loop"),
    ):
        start_orchestrator(tmp_path, state_dir)


def test_start_refuses_when_locked(tmp_path: Path) -> None:
    state_dir = _setup(tmp_path, OrchestratorMode.idle)
    from amp_orchestrator.lock import OrchestratorLock
    lock = OrchestratorLock(state_dir)
    lock.acquire()
    try:
        with pytest.raises(Exception, match="lock"):
            start_orchestrator(tmp_path, state_dir)
    finally:
        lock.release()
