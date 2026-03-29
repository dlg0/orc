"""CLI entry point for amp-orchestrator."""

from __future__ import annotations

from pathlib import Path

import click

from amp_orchestrator.config import CONFIG_DIR, create_default_config, detect_project
from amp_orchestrator.control import (
    pause_orchestrator,
    resume_orchestrator,
    start_orchestrator,
    stop_orchestrator,
)
from amp_orchestrator.events import EventLog
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
    project = detect_project()
    repo_root = project.repo_root
    state_dir = repo_root / CONFIG_DIR
    start_orchestrator(repo_root, state_dir)


@main.command()
def pause() -> None:
    """Finish current issue, then stop scheduling new ones."""
    state_dir = _get_state_dir()
    pause_orchestrator(state_dir)


@main.command()
def resume() -> None:
    """Continue from paused state."""
    project = detect_project()
    repo_root = project.repo_root
    state_dir = repo_root / CONFIG_DIR
    resume_orchestrator(repo_root, state_dir)


@main.command()
def stop() -> None:
    """Stop after the current issue reaches a safe checkpoint."""
    state_dir = _get_state_dir()
    stop_orchestrator(state_dir)


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


@main.command()
def tui() -> None:
    """Launch the TUI dashboard."""
    from amp_orchestrator.tui.app import OrchestratorApp

    project = detect_project()
    repo_root = project.repo_root
    state_dir = repo_root / CONFIG_DIR
    app = OrchestratorApp(repo_root=repo_root, state_dir=state_dir)
    app.run()


@main.command("init-config")
def init_config() -> None:
    """Create a local config file with safe defaults."""
    ctx = detect_project()
    config_path = create_default_config(ctx.repo_root)
    click.echo(f"Config created: {config_path}")
