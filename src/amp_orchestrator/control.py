"""Shared control/service layer for orchestrator lifecycle management.

Provides start/pause/resume/stop operations used by both the CLI and TUI.
"""

from __future__ import annotations

from pathlib import Path

import click

from amp_orchestrator.amp_runner import RealAmpRunner
from amp_orchestrator.config import OrchestratorConfig, load_config
from amp_orchestrator.evaluator import AmpEvaluatorRunner
from amp_orchestrator.events import EventLog, EventType
from amp_orchestrator.lock import OrchestratorLock
from amp_orchestrator.scheduler import run_loop
from amp_orchestrator.state import OrchestratorMode, StateStore


def start_orchestrator(repo_root: Path, state_dir: Path) -> None:
    """Begin processing ready issues.

    Acquires the process lock, handles crash recovery, transitions to running,
    and enters the scheduler loop.  Releases the lock on exit.

    Raises ``click.ClickException`` on lock contention or invalid state.
    """
    lock = OrchestratorLock(state_dir)

    if not lock.acquire():
        raise click.ClickException("Orchestrator is already running (lock held)")

    try:
        store = StateStore(state_dir)
        state = store.load()

        # Crash recovery: if state is running/pause_requested but no lock was
        # held (we just acquired it), the previous process must have crashed.
        if state.mode in (OrchestratorMode.running, OrchestratorMode.pause_requested):
            click.echo(
                f"[RECOVERY] Detected stale {state.mode.value} state (previous process crashed)"
            )
            if state.active_issue_id:
                stale_label = state.active_issue_id
                if state.active_issue_title:
                    stale_label += f" -- {state.active_issue_title}"
                click.echo(f"[RECOVERY] stale issue: {stale_label}")
                click.echo(f"[RECOVERY] branch: {state.active_branch}")
                click.echo(f"[RECOVERY] worktree: {state.active_worktree_path}")
            # Reset to idle so we can start fresh
            state.last_error = f"crash recovery from {state.mode.value}"
            state.active_issue_id = None
            state.active_issue_title = None
            state.active_branch = None
            state.active_worktree_path = None
            state.active_stage = None
            state.active_started_at = None
            state.mode = OrchestratorMode.idle
            store.save(state)
            events = EventLog(state_dir)
            events.record(EventType.state_changed, {"to": "idle", "reason": "crash_recovery"})
            click.echo("[RECOVERY] Reset to idle.")

        if state.mode not in (OrchestratorMode.idle, OrchestratorMode.paused):
            lock.release()
            raise click.ClickException(
                f"Cannot start from {state.mode.value} state"
            )

        prev_mode = state.mode.value
        store.transition(state, OrchestratorMode.running)
        events = EventLog(state_dir)
        events.record(EventType.state_changed, {"from": prev_mode, "to": "running"})
        click.echo("[SCHEDULER] Orchestrator started")

        config = load_config(repo_root)
        runner = RealAmpRunner(mode=config.amp_mode)
        evaluator = AmpEvaluatorRunner(
            mode=config.evaluation_mode or config.amp_mode,
            timeout=config.evaluation_timeout,
        ) if config.enable_evaluation else None
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)
    except Exception:
        # Ensure state goes back to error on unexpected failure
        try:
            store = StateStore(state_dir)
            state = store.load()
            if state.mode == OrchestratorMode.running:
                store.transition(state, OrchestratorMode.error)
        except Exception:
            pass
        raise
    finally:
        lock.release()


def pause_orchestrator(state_dir: Path) -> None:
    """Request a pause — the orchestrator will pause after the current issue completes.

    Raises ``click.ClickException`` if not in ``running`` state.
    """
    store = StateStore(state_dir)
    state = store.load()

    if state.mode != OrchestratorMode.running:
        raise click.ClickException(
            f"Cannot pause from {state.mode.value} state (must be running)"
        )

    store.transition(state, OrchestratorMode.pause_requested)
    events = EventLog(state_dir)
    events.record(EventType.pause_requested)
    click.echo("[SCHEDULER] Pause requested -- will pause after current issue completes")


def resume_orchestrator(repo_root: Path, state_dir: Path) -> None:
    """Resume from paused state.

    Acquires the process lock, transitions to running, loads config, creates a
    runner, and enters the scheduler loop.  Releases the lock on exit.
    Handles unexpected failures by transitioning to error.

    Raises ``click.ClickException`` on lock contention or invalid state.
    """
    lock = OrchestratorLock(state_dir)

    if not lock.acquire():
        raise click.ClickException("Orchestrator is already running (lock held)")

    try:
        store = StateStore(state_dir)
        state = store.load()

        if state.mode != OrchestratorMode.paused:
            lock.release()
            raise click.ClickException(
                f"Cannot resume from {state.mode.value} state (must be paused)"
            )

        store.transition(state, OrchestratorMode.running)
        events = EventLog(state_dir)
        events.record(EventType.state_changed, {"from": "paused", "to": "running"})
        click.echo("[SCHEDULER] Orchestrator resumed")

        config = load_config(repo_root)
        runner = RealAmpRunner(mode=config.amp_mode)
        evaluator = AmpEvaluatorRunner(
            mode=config.evaluation_mode or config.amp_mode,
            timeout=config.evaluation_timeout,
        ) if config.enable_evaluation else None
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator)
    except Exception:
        try:
            store = StateStore(state_dir)
            state = store.load()
            if state.mode == OrchestratorMode.running:
                store.transition(state, OrchestratorMode.error)
        except Exception:
            pass
        raise
    finally:
        lock.release()


def stop_orchestrator(state_dir: Path) -> None:
    """Request a stop -- the orchestrator will stop after the current issue reaches a safe checkpoint.

    Raises ``click.ClickException`` if not in ``running`` or ``pause_requested`` state.
    """
    store = StateStore(state_dir)
    state = store.load()

    if state.mode not in (OrchestratorMode.running, OrchestratorMode.pause_requested):
        raise click.ClickException(
            f"Cannot stop from {state.mode.value} state (must be running or pause_requested)"
        )

    store.transition(state, OrchestratorMode.stopping)
    events = EventLog(state_dir)
    events.record(EventType.stop_requested)
    click.echo("[SCHEDULER] Stop requested -- will stop after current issue reaches a safe checkpoint")
