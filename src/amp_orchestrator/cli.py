"""CLI entry point for amp-orchestrator."""

from __future__ import annotations

from pathlib import Path

import click

from amp_orchestrator.config import CONFIG_DIR, create_default_config, detect_project
from amp_orchestrator.events import EventLog, EventType
from amp_orchestrator.lock import OrchestratorLock
from amp_orchestrator.queue import get_ready_issues
from amp_orchestrator.state import OrchestratorMode, StateStore


def _get_state_dir(path: Path | None = None) -> Path:
    """Detect the project and return the state directory path."""
    ctx = detect_project(path)
    return ctx.repo_root / CONFIG_DIR


@click.group()
@click.version_option()
def main() -> None:
    """Single-project backlog runner for Amp and bd."""


@main.command()
def status() -> None:
    """Show current orchestrator state, active issue, and queue summary."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()

    click.echo(f"Mode: {state.mode.value}")

    if state.active_issue_id:
        click.echo(f"Active issue: {state.active_issue_id}")
    if state.last_completed_issue:
        click.echo(f"Last completed: {state.last_completed_issue}")
    if state.last_error:
        click.echo(f"Last error: {state.last_error}")

    ready = get_ready_issues(state_dir.parent)
    click.echo(f"Queue: {len(ready)} issue(s) ready")

    if state.mode == OrchestratorMode.running and state.active_issue_id:
        if state.active_worktree_path:
            click.echo(f"Worktree: {state.active_worktree_path}")
        if state.active_branch:
            click.echo(f"Branch: {state.active_branch}")


@main.command()
def start() -> None:
    """Begin processing ready issues."""
    state_dir = _get_state_dir()
    lock = OrchestratorLock(state_dir)

    if not lock.acquire():
        raise click.ClickException("Orchestrator is already running (lock held)")

    try:
        store = StateStore(state_dir)
        state = store.load()

        if state.mode not in (OrchestratorMode.idle, OrchestratorMode.paused):
            lock.release()
            raise click.ClickException(
                f"Cannot start from {state.mode.value} state"
            )

        store.transition(state, OrchestratorMode.running)
        events = EventLog(state_dir)
        events.record(EventType.state_changed, {"from": "idle", "to": "running"})
        click.echo("Orchestrator started")
    except Exception:
        lock.release()
        raise


@main.command()
def pause() -> None:
    """Finish current issue, then stop scheduling new ones."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()

    if state.mode != OrchestratorMode.running:
        raise click.ClickException(
            f"Cannot pause from {state.mode.value} state (must be running)"
        )

    store.transition(state, OrchestratorMode.pause_requested)
    events = EventLog(state_dir)
    events.record(EventType.pause_requested)
    click.echo("Pause requested — will pause after current issue completes")


@main.command()
def resume() -> None:
    """Continue from paused state."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()

    if state.mode != OrchestratorMode.paused:
        raise click.ClickException(
            f"Cannot resume from {state.mode.value} state (must be paused)"
        )

    store.transition(state, OrchestratorMode.running)
    events = EventLog(state_dir)
    events.record(EventType.state_changed, {"from": "paused", "to": "running"})
    click.echo("Orchestrator resumed")


@main.command()
def stop() -> None:
    """Stop after the current issue reaches a safe checkpoint."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()

    if state.mode not in (OrchestratorMode.running, OrchestratorMode.pause_requested):
        raise click.ClickException(
            f"Cannot stop from {state.mode.value} state (must be running or pause_requested)"
        )

    store.transition(state, OrchestratorMode.stopping)
    events = EventLog(state_dir)
    events.record(EventType.stop_requested)
    click.echo("Stop requested — will stop after current issue reaches a safe checkpoint")


@main.command()
@click.argument("issue_id")
def inspect(issue_id: str) -> None:
    """View last run summary for ISSUE_ID."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()

    entry = None
    for run in reversed(state.run_history):
        if run.get("issue_id") == issue_id:
            entry = run
            break

    if entry is None:
        raise click.ClickException(f"No run history found for {issue_id}")

    click.echo(f"Issue: {issue_id}")
    if "result" in entry:
        click.echo(f"Result: {entry['result']}")
    if "branch" in entry:
        click.echo(f"Branch: {entry['branch']}")
    if "worktree_path" in entry:
        click.echo(f"Worktree: {entry['worktree_path']}")
    if "summary" in entry:
        click.echo(f"Summary: {entry['summary']}")


@main.command()
@click.option("--tail", "-n", default=20, help="Number of recent events to show.")
def logs(tail: int) -> None:
    """Show recent events from the event log."""
    state_dir = _get_state_dir()
    event_log = EventLog(state_dir)
    entries = event_log.recent(tail)

    if not entries:
        click.echo("No events recorded yet.")
        return

    for entry in entries:
        ts = entry.get("timestamp", "?")
        etype = entry.get("event_type", "?")
        data = entry.get("data")
        line = f"[{ts}] {etype}"
        if data:
            line += f"  {data}"
        click.echo(line)


@main.command("init-config")
def init_config() -> None:
    """Create a local config file with safe defaults."""
    ctx = detect_project()
    config_path = create_default_config(ctx.repo_root)
    click.echo(f"Config created: {config_path}")
