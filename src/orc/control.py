"""Shared control/service layer for orchestrator lifecycle management.

Provides start/pause/resume/stop operations used by both the CLI and TUI.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

logger = logging.getLogger(__name__)

from orc.already_implemented import AmpAlreadyImplementedChecker
from orc.amp_runner import RealAmpRunner
from orc.config import OrchestratorConfig, load_config
from orc.evaluator import AmpEvaluatorRunner
from orc.events import EventLog, EventType
from orc.lock import OrchestratorLock
from orc.queue import IssueState, get_issue_state, unclaim_issue
from orc.scheduler import run_loop
from orc.state import OrchestratorMode, RequestQueue, StateStore, _MAX_RESUME_ATTEMPTS, _RESUMABLE_STAGES
from orc.worktree import WorktreeManager


def start_orchestrator(
    repo_root: Path,
    state_dir: Path,
    *,
    fail_fast: bool = False,
    only_issue: str | None = None,
) -> None:
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

        # Crash recovery: if state is an in-flight mode but no lock was
        # held (we just acquired it), the previous process must have crashed.
        if state.mode in (OrchestratorMode.running, OrchestratorMode.pause_requested, OrchestratorMode.stopping):
            click.echo(
                f"[RECOVERY] Detected stale {state.mode.value} state (previous process crashed)"
            )
            events = EventLog(state_dir)
            events.record(EventType.state_changed, {
                "to": "idle", "reason": "interrupted_run_detected",
                "issue_id": state.active_issue_id,
            })

            if state.active_run:
                stale_label = state.active_issue_id or "unknown"
                if state.active_issue_title:
                    stale_label += f" -- {state.active_issue_title}"
                click.echo(f"[RECOVERY] stale issue: {stale_label}")
                click.echo(f"[RECOVERY] branch: {state.active_branch}")
                click.echo(f"[RECOVERY] worktree: {state.active_worktree_path}")
                click.echo(f"[RECOVERY] stage: {state.active_stage}")

                # Determine if the run is resumable
                stage = state.active_run.get("stage", "")
                branch = state.active_run.get("branch")
                wt_path = state.active_run.get("worktree_path")
                attempts = state.active_run.get("resume_attempts", 0)
                is_resumable = (
                    stage in _RESUMABLE_STAGES
                    and branch
                    and wt_path
                    and attempts < _MAX_RESUME_ATTEMPTS
                )

                if is_resumable:
                    # Check if worktree/branch still exist
                    try:
                        config = load_config(repo_root)
                    except Exception:
                        logger.debug("Failed to load config during recovery, using defaults", exc_info=True)
                        config = OrchestratorConfig()
                    worktree_mgr = WorktreeManager(repo_root, config.base_branch)
                    if worktree_mgr.ensure_resumable_worktree(branch, wt_path):
                        click.echo(f"[RECOVERY] Resumable run detected (stage={stage}, attempts={attempts})")
                        state.active_run["resume_attempts"] = attempts + 1
                        state.resume_candidate = state.active_run
                        state.active_run = None
                        state.last_error = f"crash recovery from {state.mode.value}"
                        state.mode = OrchestratorMode.idle
                        store.save(state)
                        events.record(EventType.state_changed, {
                            "to": "idle", "reason": "crash_recovery",
                            "resume_candidate": state.resume_candidate.get("issue_id"),
                        })
                        click.echo("[RECOVERY] Resume candidate saved — will attempt recovery.")
                    else:
                        is_resumable = False
                        click.echo("[RECOVERY] Worktree/branch not recoverable — discarding.")

                if not is_resumable:
                    # Not resumable — unclaim and discard
                    if state.active_run.get("bd_claimed"):
                        issue_id = state.active_run["issue_id"]
                        # Don't reopen a closed/missing issue
                        bd_state = get_issue_state(issue_id, cwd=repo_root)
                        if bd_state in (IssueState.closed, IssueState.missing):
                            click.echo(f"[RECOVERY] {issue_id} already {bd_state.value} — skipping unclaim")
                        elif unclaim_issue(issue_id, cwd=repo_root):
                            click.echo(f"[RECOVERY] Unclaimed {issue_id}")
                        else:
                            click.echo(f"[RECOVERY] WARNING: failed to unclaim {issue_id}")
                    state.last_error = f"crash recovery from {state.mode.value}"
                    state.active_run = None
                    state.resume_candidate = None
                    state.mode = OrchestratorMode.idle
                    store.save(state)
                    events.record(EventType.state_changed, {"to": "idle", "reason": "crash_recovery"})
                    click.echo("[RECOVERY] Reset to idle.")
            else:
                state.last_error = f"crash recovery from {state.mode.value}"
                state.mode = OrchestratorMode.idle
                store.save(state)
                events.record(EventType.state_changed, {"to": "idle", "reason": "crash_recovery"})
                click.echo("[RECOVERY] Reset to idle (no active run).")

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
        ai_checker = AmpAlreadyImplementedChecker() if config.use_already_implemented_preflight else None
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator, already_implemented_checker=ai_checker, fail_fast=fail_fast, only_issue=only_issue)
    except Exception:
        # Ensure state goes back to error on unexpected failure
        try:
            store = StateStore(state_dir)
            state = store.load()
            if state.mode == OrchestratorMode.running:
                store.transition(state, OrchestratorMode.error)
        except Exception:
            logger.warning("Failed to transition state to error during cleanup", exc_info=True)
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

    if OrchestratorLock(state_dir).is_locked():
        # Scheduler is running — enqueue instead of direct transition to
        # avoid a lost-update race where the scheduler overwrites our write.
        RequestQueue(state_dir).enqueue("pause")
        events = EventLog(state_dir)
        events.record(EventType.pause_requested)
        click.echo("[SCHEDULER] Pause requested -- will pause after current issue completes")
    else:
        store.transition(state, OrchestratorMode.pause_requested)
        events = EventLog(state_dir)
        events.record(EventType.pause_requested)
        click.echo("[SCHEDULER] Pause requested -- will pause after current issue completes")


def resume_orchestrator(repo_root: Path, state_dir: Path, *, fail_fast: bool = False) -> None:
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
        ai_checker = AmpAlreadyImplementedChecker() if config.use_already_implemented_preflight else None
        run_loop(repo_root, state_dir, config, runner, evaluator=evaluator, already_implemented_checker=ai_checker, fail_fast=fail_fast)
    except Exception:
        try:
            store = StateStore(state_dir)
            state = store.load()
            if state.mode == OrchestratorMode.running:
                store.transition(state, OrchestratorMode.error)
        except Exception:
            logger.warning("Failed to transition state to error during cleanup", exc_info=True)
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

    if OrchestratorLock(state_dir).is_locked():
        RequestQueue(state_dir).enqueue("stop")
        events = EventLog(state_dir)
        events.record(EventType.stop_requested)
        click.echo("[SCHEDULER] Stop requested -- will stop after current issue reaches a safe checkpoint")
    else:
        store.transition(state, OrchestratorMode.stopping)
        events = EventLog(state_dir)
        events.record(EventType.stop_requested)
        click.echo("[SCHEDULER] Stop requested -- will stop after current issue reaches a safe checkpoint")
