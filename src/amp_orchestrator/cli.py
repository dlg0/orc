"""CLI entry point for amp-orchestrator."""

import click


@click.group()
@click.version_option()
def main() -> None:
    """Single-project backlog runner for Amp and bd."""


@main.command()
def status() -> None:
    """Show current orchestrator state, active issue, and queue summary."""
    click.echo("State: idle")


@main.command()
def start() -> None:
    """Begin processing ready issues."""
    click.echo("start: not yet implemented")


@main.command()
def pause() -> None:
    """Finish current issue, then stop scheduling new ones."""
    click.echo("pause: not yet implemented")


@main.command()
def resume() -> None:
    """Continue from paused state."""
    click.echo("resume: not yet implemented")


@main.command()
def stop() -> None:
    """Stop after the current issue reaches a safe checkpoint."""
    click.echo("stop: not yet implemented")


@main.command()
@click.argument("issue_id")
def inspect(issue_id: str) -> None:
    """View last run summary for ISSUE_ID."""
    click.echo(f"inspect {issue_id}: not yet implemented")


@main.command()
@click.option("--tail", "-n", default=20, help="Number of recent events to show.")
def logs(tail: int) -> None:
    """Show recent events from the event log."""
    click.echo(f"logs (last {tail}): not yet implemented")


@main.command("init-config")
def init_config() -> None:
    """Create a local config file with safe defaults."""
    click.echo("init-config: not yet implemented")
