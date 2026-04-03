"""End-to-end runner for the Beads dispatch exploration harness."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from orc.explore.bd import BdClient, Sandbox
from orc.explore.models import ExplorationSummary, ObservedIssue, ObservedState, ScenarioDefinition, ScenarioRunResult, TrialPlan
from orc.explore.planner import build_trial_plan
from orc.explore.report import build_json_report, render_markdown_report
from orc.explore.scenarios import get_scenarios


def run_dispatch_exploration(
    *,
    output_dir: Path,
    scenario_names: list[str],
    keep_sandbox: bool = False,
    include_in_progress: bool = False,
) -> ExplorationSummary:
    """Run the requested dispatch exploration scenarios."""

    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = get_scenarios()
    results = [
        run_scenario(
            scenario=scenarios[scenario_name],
            output_dir=output_dir / scenario_name,
            keep_sandbox=keep_sandbox,
            include_in_progress=include_in_progress,
        )
        for scenario_name in scenario_names
    ]
    return ExplorationSummary(output_dir=output_dir, results=results)


def run_scenario(
    *,
    scenario: ScenarioDefinition,
    output_dir: Path,
    keep_sandbox: bool = False,
    include_in_progress: bool = False,
    sandbox_factory=Sandbox,
    client_factory=BdClient,
) -> ScenarioRunResult:
    """Run one scenario in its own Beads sandbox and write both reports."""

    output_dir.mkdir(parents=True, exist_ok=True)
    observations = ObservedState()
    plan = TrialPlan()
    created_ids_by_key: dict[str, str] = {}
    setup_error: str | None = None

    with sandbox_factory(keep=keep_sandbox) as sandbox:
        markdown_path = output_dir / "report.md"
        json_path = output_dir / "report.json"
        client = client_factory(sandbox.path)

        try:
            client.initialize(prefix="orcx")
            client.configure_custom_types({spec.issue_type for spec in scenario.issues})
            created_ids_by_key = _build_scenario(scenario, client)
            observations = _observe_state(client, created_ids_by_key)
            plan = build_trial_plan(observations, include_in_progress=include_in_progress)
            mismatches = _compare_expectations(scenario, observations, plan)
        except Exception as exc:  # pragma: no cover - exercised in runner failure test
            observations.command_transcript = list(client.transcript)
            setup_error = str(exc)
            mismatches = []
        result = ScenarioRunResult(
            scenario=scenario,
            sandbox_path=sandbox.path,
            created_ids_by_key=created_ids_by_key,
            observations=observations,
            plan=plan,
            mismatches=mismatches,
            markdown_path=markdown_path,
            json_path=json_path,
            setup_error=setup_error,
        )
        markdown_path.write_text(render_markdown_report(result))
        json_path.write_text(json.dumps(build_json_report(result), indent=2) + "\n")
        return result


def _build_scenario(scenario: ScenarioDefinition, client: BdClient) -> dict[str, str]:
    ids_by_key: dict[str, str] = {}
    for spec in scenario.issues:
        parent_id = ids_by_key.get(spec.parent) if spec.parent else None
        if spec.parent and parent_id is None:
            raise ValueError(f"Scenario {scenario.name}: parent {spec.parent!r} must be declared before {spec.key!r}")
        ids_by_key[spec.key] = client.create_issue(spec, parent_id=parent_id)
        if spec.create_delay_seconds > 0:
            time.sleep(spec.create_delay_seconds)

    for spec in scenario.issues:
        issue_id = ids_by_key[spec.key]
        for blocker_key in spec.blockers:
            blocker_id = ids_by_key.get(blocker_key)
            if blocker_id is None:
                raise ValueError(f"Scenario {scenario.name}: blocker {blocker_key!r} not found for {spec.key!r}")
            client.add_blocker(issue_id, blocker_id)
        if spec.status != "open" or spec.defer_until is not None:
            client.update_issue(issue_id, status=spec.status if spec.status != "open" else None, defer_until=spec.defer_until)

    return ids_by_key


def _observe_state(client: BdClient, ids_by_key: dict[str, str]) -> ObservedState:
    ready_raw = client.ready()
    list_raw = client.list_all()
    list_tree = client.list_tree()
    keys_by_id = {issue_id: key for key, issue_id in ids_by_key.items()}
    child_ids_by_parent: dict[str, list[str]] = defaultdict(list)
    issues_by_id: dict[str, ObservedIssue] = {}

    for raw_issue in list_raw:
        issue_id = raw_issue["id"]
        parent_id = raw_issue.get("parent") or None
        if parent_id is not None:
            child_ids_by_parent[parent_id].append(issue_id)
        blocker_ids = [
            dependency["depends_on_id"]
            for dependency in raw_issue.get("dependencies", [])
            if dependency.get("type") == "blocks"
        ]
        issues_by_id[issue_id] = ObservedIssue(
            id=issue_id,
            key=keys_by_id.get(issue_id),
            title=raw_issue.get("title", issue_id),
            issue_type=raw_issue.get("issue_type", "task"),
            status=raw_issue.get("status", "open"),
            priority=raw_issue.get("priority"),
            parent_id=parent_id,
            blocker_ids=blocker_ids,
            raw=raw_issue,
        )

    for parent_id, child_ids in child_ids_by_parent.items():
        if parent_id in issues_by_id:
            issues_by_id[parent_id].child_ids = list(child_ids)

    descendants_by_id = {
        issue_id: _descendants_for(issue_id, issues_by_id)
        for issue_id in issues_by_id
    }
    return ObservedState(
        ids_by_key=dict(ids_by_key),
        keys_by_id=keys_by_id,
        ready_ids_in_order=[item["id"] for item in ready_raw],
        issues_by_id=issues_by_id,
        descendants_by_id=descendants_by_id,
        list_tree=list_tree,
        ready_raw=ready_raw,
        list_raw=list_raw,
        command_transcript=list(client.transcript),
    )


def _descendants_for(issue_id: str, issues_by_id: dict[str, ObservedIssue]) -> list[str]:
    descendants: list[str] = []
    stack = list(issues_by_id[issue_id].child_ids)
    while stack:
        child_id = stack.pop(0)
        descendants.append(child_id)
        child_issue = issues_by_id.get(child_id)
        if child_issue is not None:
            stack.extend(child_issue.child_ids)
    return descendants


def _compare_expectations(
    scenario: ScenarioDefinition,
    observations: ObservedState,
    plan: TrialPlan,
) -> list[str]:
    expected = scenario.expectations
    ready_keys = [observations.keys_by_id.get(issue_id, issue_id) for issue_id in observations.ready_ids_in_order]
    dispatch_keys = [observations.keys_by_id.get(issue_id, issue_id) for issue_id in plan.dispatchable_ids]
    unsupported_types = sorted({finding.issue_type for finding in plan.unsupported_types})
    mismatches: list[str] = []

    for key in expected.ready_contains:
        if key not in ready_keys:
            mismatches.append(f"expected ready set to contain {key}, but it was absent")
    for key in expected.ready_excludes:
        if key in ready_keys:
            mismatches.append(f"expected ready set to exclude {key}, but it was present")
    for key in expected.dispatch_contains:
        if key not in dispatch_keys:
            mismatches.append(f"expected dispatch frontier to contain {key}, but it was absent")
    for key in expected.dispatch_excludes:
        if key in dispatch_keys:
            mismatches.append(f"expected dispatch frontier to exclude {key}, but it was present")

    expected_unsupported = sorted(expected.invalid_due_to_types)
    if expected_unsupported != unsupported_types:
        if expected_unsupported or unsupported_types:
            mismatches.append(
                "expected unsupported types "
                f"{expected_unsupported or ['(none)']} but observed {unsupported_types or ['(none)']}"
            )

    if expected.dispatch_preserves_ready_order:
        filtered_ready = [key for key in ready_keys if key in dispatch_keys]
        if dispatch_keys != filtered_ready:
            mismatches.append(
                f"expected dispatch frontier to preserve ready order; ready-filtered order was {filtered_ready} but dispatch order was {dispatch_keys}"
            )

    return mismatches
