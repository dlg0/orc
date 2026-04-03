"""CLI entry points for the Beads dispatch exploration harness."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from orc.config import CONFIG_DIR, detect_project
from orc.explore.runner import run_dispatch_exploration
from orc.explore.scenarios import get_scenarios


@click.group()
def explore() -> None:
    """Run exploration harnesses that compare raw Beads behavior with Orc policy."""


@explore.command("dispatch")
@click.option("--scenario", "scenario_names", multiple=True, help="Run a named scenario. Repeat to run more than one.")
@click.option("--all", "run_all", is_flag=True, default=False, help="Run all registered scenarios.")
@click.option("--keep-sandbox", is_flag=True, default=False, help="Keep each temporary Beads sandbox after the run.")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None, help="Directory for markdown and JSON artifacts.")
@click.option("--include-in-progress", is_flag=True, default=False, help="Treat in-progress issues as dispatchable if Beads includes them in the ready set.")
def dispatch(
    scenario_names: tuple[str, ...],
    run_all: bool,
    keep_sandbox: bool,
    output_dir: Path | None,
    include_in_progress: bool,
) -> None:
    """Compare raw ``bd ready`` output with Orc's dispatch-safety filters."""

    registry = get_scenarios()
    if run_all and scenario_names:
        raise click.ClickException("Use either --all or --scenario, not both")
    if run_all:
        selected_names = list(registry)
    elif scenario_names:
        unknown = [name for name in scenario_names if name not in registry]
        if unknown:
            raise click.ClickException(f"Unknown scenario(s): {', '.join(unknown)}")
        selected_names = list(scenario_names)
    else:
        raise click.ClickException("Select at least one scenario with --scenario, or use --all")

    if output_dir is None:
        repo_root = detect_project().repo_root
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_dir = repo_root / CONFIG_DIR / "explore" / f"dispatch-{stamp}"

    summary = run_dispatch_exploration(
        output_dir=output_dir,
        scenario_names=selected_names,
        keep_sandbox=keep_sandbox,
        include_in_progress=include_in_progress,
    )

    click.echo(f"Wrote exploration artifacts to {summary.output_dir}")
    for result in summary.results:
        click.echo(
            f"- {result.scenario.name}: {result.status} "
            f"(markdown: {result.markdown_path}, json: {result.json_path})"
        )
    raise click.exceptions.Exit(summary.exit_code)
