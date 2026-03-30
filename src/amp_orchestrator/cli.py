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
from amp_orchestrator.queue import get_issue_status, get_ready_issues, reconcile_issue_failures
from amp_orchestrator.state import (
    OrchestratorMode,
    StateStore,
    queue_retry,
)


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
        label = state.active_issue_id
        if state.active_issue_title:
            label += f" — {state.active_issue_title}"
        click.echo(f"Active issue: {label}")
    if state.last_completed_issue:
        click.echo(f"Last completed: {state.last_completed_issue}")
    if state.last_error:
        click.echo(f"Last error: {state.last_error}")

    # Reconcile held issues against beads before displaying
    if state.issue_failures:
        pruned = reconcile_issue_failures(state.issue_failures, cwd=state_dir.parent)
        if pruned:
            store.save(state)
            for issue_id, reason in pruned:
                click.echo(f"Pruned held issue {issue_id} ({reason})")

    queue_result = get_ready_issues(state_dir.parent)
    if queue_result.success:
        click.echo(f"Queue: {len(queue_result.issues)} issue(s) ready")
    else:
        click.echo(f"Queue: error fetching issues ({queue_result.error})")

    if state.issue_failures:
        click.echo(f"Held issues: {len(state.issue_failures)}")
        by_category: dict[str, list[tuple[str, dict]]] = {}
        for rid, info in state.issue_failures.items():
            cat = info.get("category", "unknown")
            by_category.setdefault(cat, []).append((rid, info))
        for cat, items in by_category.items():
            click.echo(f"  [{cat}] ({len(items)})")
            for rid, info in items:
                click.echo(f"    {rid}: {info.get('summary', '(no summary)')}")

    if state.mode == OrchestratorMode.running and state.active_issue_id:
        if state.active_stage:
            click.echo(f"Stage: {state.active_stage}")
        if state.active_started_at:
            click.echo(f"Started at: {state.active_started_at}")
        if state.active_worktree_path:
            click.echo(f"Worktree: {state.active_worktree_path}")
        if state.active_branch:
            click.echo(f"Branch: {state.active_branch}")


@main.command()
@click.option("--fail-fast", is_flag=True, default=False, help="Stop on the first issue failure instead of continuing.")
def start(fail_fast: bool) -> None:
    """Begin processing ready issues."""
    project = detect_project()
    repo_root = project.repo_root
    state_dir = repo_root / CONFIG_DIR
    start_orchestrator(repo_root, state_dir, fail_fast=fail_fast)


@main.command()
def pause() -> None:
    """Finish current issue, then stop scheduling new ones."""
    state_dir = _get_state_dir()
    pause_orchestrator(state_dir)


@main.command()
@click.option("--fail-fast", is_flag=True, default=False, help="Stop on the first issue failure instead of continuing.")
def resume(fail_fast: bool) -> None:
    """Continue from paused state."""
    project = detect_project()
    repo_root = project.repo_root
    state_dir = repo_root / CONFIG_DIR
    resume_orchestrator(repo_root, state_dir, fail_fast=fail_fast)


@main.command()
def stop() -> None:
    """Stop after the current issue reaches a safe checkpoint."""
    state_dir = _get_state_dir()
    stop_orchestrator(state_dir)


@main.command()
@click.argument("issue_id")
def retry(issue_id: str) -> None:
    """Clear held/failed status for ISSUE_ID and re-queue it."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()
    if issue_id not in state.issue_failures:
        raise click.ClickException(f"{issue_id} is not in held/failed state")
    if get_issue_status(issue_id, cwd=state_dir.parent) == "closed":
        del state.issue_failures[issue_id]
        store.save(state)
        click.echo(f"{issue_id} is already closed in beads — removed from held list")
        return
    try:
        message = queue_retry(state, issue_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    store.save(state)
    click.echo(message)


@main.command("retry-merge")
@click.argument("issue_id")
def retry_merge(issue_id: str) -> None:
    """Queue ISSUE_ID to retry only the verify-and-merge step on next run."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()
    if issue_id not in state.issue_failures:
        raise click.ClickException(f"{issue_id} is not in held/failed state")
    if get_issue_status(issue_id, cwd=state_dir.parent) == "closed":
        del state.issue_failures[issue_id]
        store.save(state)
        click.echo(f"{issue_id} is already closed in beads — removed from held list")
        return
    try:
        message = queue_retry(state, issue_id, merge_only=True)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    store.save(state)
    click.echo(message)


@main.command()
@click.argument("issue_id")
def inspect(issue_id: str) -> None:
    """View last run summary for ISSUE_ID."""
    state_dir = _get_state_dir()
    store = StateStore(state_dir)
    state = store.load()

    # Check issue_failures first for richer detail
    failure_info = state.issue_failures.get(issue_id)
    if failure_info is not None:
        click.echo(f"Issue: {issue_id}")
        click.echo(f"Status: held/failed")
        click.echo(f"Category: {failure_info.get('category', 'unknown')}")
        click.echo(f"Stage: {failure_info.get('stage', 'unknown')}")
        click.echo(f"Attempts: {failure_info.get('attempts', 1)}")
        if failure_info.get("branch"):
            click.echo(f"Branch: {failure_info['branch']}")
        if failure_info.get("worktree_path"):
            click.echo(f"Worktree: {failure_info['worktree_path']}")
        if failure_info.get("summary"):
            click.echo(f"Summary: {failure_info['summary']}")
        return

    # Fall back to run_history
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
