"""Report rendering for dispatch exploration runs."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from orc.explore.models import PlanEntry, ScenarioRunResult


def build_json_report(result: ScenarioRunResult) -> dict[str, Any]:
    """Build the machine-readable report structure."""

    return _to_jsonable(
        {
            "scenario": result.scenario,
            "status": result.status,
            "sandbox_path": result.sandbox_path,
            "created_ids_by_key": result.created_ids_by_key,
            "observations": result.observations,
            "plan": result.plan,
            "mismatches": result.mismatches,
            "setup_error": result.setup_error,
        }
    )


def render_markdown_report(result: ScenarioRunResult) -> str:
    """Render the human-readable markdown report."""

    lines: list[str] = [
        f"# {result.scenario.name}",
        "",
        result.scenario.description,
        "",
        f"Status: `{result.status}`",
        f"Sandbox: `{result.sandbox_path}`",
        "",
    ]

    if result.setup_error is not None:
        lines.extend([
            "## Setup Error",
            "",
            result.setup_error,
            "",
        ])

    lines.extend([
        "## Scenario Definition",
        "",
    ])
    for spec in result.scenario.issues:
        parts = [f"`{spec.key}`: `{spec.issue_type}` — {spec.title}"]
        if spec.parent:
            parts.append(f"parent=`{spec.parent}`")
        if spec.blockers:
            parts.append(f"blocked-by={', '.join(f'`{key}`' for key in spec.blockers)}")
        if spec.priority is not None:
            parts.append(f"priority=`{spec.priority}`")
        if spec.status != "open":
            parts.append(f"status=`{spec.status}`")
        if spec.defer_until is not None:
            parts.append(f"defer=`{spec.defer_until}`")
        lines.append(f"- {'; '.join(parts)}")

    lines.extend([
        "",
        "## Hypotheses",
        "",
    ])
    for hypothesis in result.scenario.hypotheses:
        lines.append(f"- {hypothesis}")

    lines.extend([
        "",
        "## Created Issues",
        "",
    ])
    for key, issue_id in result.created_ids_by_key.items():
        lines.append(f"- `{key}` -> `{issue_id}`")

    lines.extend([
        "",
        "## Expected Behavior",
        "",
    ])
    lines.extend(_render_expectations(result))

    lines.extend([
        "",
        "## Raw Beads Observations",
        "",
        f"- Ready order: {', '.join(_display_issue_id(result, issue_id) for issue_id in result.observations.ready_ids_in_order) or '(none)' }",
        "- Tree output:",
        "",
        "```text",
        result.observations.list_tree.rstrip() or "(no tree output)",
        "```",
        "",
        "### Issue Snapshots",
        "",
    ])
    for issue in result.observations.issues_by_id.values():
        child_list = ", ".join(_display_issue_id(result, child_id) for child_id in issue.child_ids) or "(none)"
        blocker_list = ", ".join(_display_issue_id(result, blocker_id) for blocker_id in issue.blocker_ids) or "(none)"
        parent = _display_issue_id(result, issue.parent_id) if issue.parent_id else "(none)"
        lines.append(
            f"- {_display_issue_id(result, issue.id)}: type=`{issue.issue_type}` status=`{issue.status}` priority=`{issue.priority}` parent={parent}; children={child_list}; blockers={blocker_list}"
        )

    lines.extend([
        "",
        "## Orc Trial Plan",
        "",
    ])
    plan_lines = _render_plan_entries(result, result.plan.entries)
    lines.extend(plan_lines or ["- No plan entries"])
    lines.extend([
        "",
        f"Dispatch frontier: {', '.join(_display_issue_id(result, issue_id) for issue_id in result.plan.dispatchable_ids) or '(none)'}",
        f"Plan invalid: `{result.plan.invalid}`",
        "",
    ])
    if result.plan.unsupported_types:
        lines.append("Unsupported types:")
        for finding in result.plan.unsupported_types:
            key_label = f"`{finding.key}`" if finding.key else "(no key)"
            lines.append(f"- {key_label}: type=`{finding.issue_type}` — {finding.reason}")
        lines.append("")

    lines.extend([
        "## Mismatches",
        "",
    ])
    if result.mismatches:
        for mismatch in result.mismatches:
            lines.append(f"- {mismatch}")
    else:
        lines.append("- None")

    lines.extend([
        "",
        "## Recommended Next Refinement",
        "",
        _recommended_next_refinement(result),
        "",
        "## Command Transcript",
        "",
    ])
    if not result.observations.command_transcript:
        lines.append("- None recorded")
    else:
        for record in result.observations.command_transcript:
            lines.append(f"- `{ ' '.join(record.command) }` -> rc={record.returncode}")

    return "\n".join(lines).rstrip() + "\n"


def _render_expectations(result: ScenarioRunResult) -> list[str]:
    expected = result.scenario.expectations
    lines: list[str] = []
    if expected.ready_contains:
        lines.append(f"- Ready contains: {', '.join(f'`{key}`' for key in expected.ready_contains)}")
    if expected.ready_excludes:
        lines.append(f"- Ready excludes: {', '.join(f'`{key}`' for key in expected.ready_excludes)}")
    if expected.dispatch_contains:
        lines.append(f"- Dispatch frontier contains: {', '.join(f'`{key}`' for key in expected.dispatch_contains)}")
    if expected.dispatch_excludes:
        lines.append(f"- Dispatch frontier excludes: {', '.join(f'`{key}`' for key in expected.dispatch_excludes)}")
    if expected.invalid_due_to_types:
        lines.append(f"- Unsupported types expected: {', '.join(f'`{issue_type}`' for issue_type in expected.invalid_due_to_types)}")
    if expected.dispatch_preserves_ready_order:
        lines.append("- Dispatch frontier should preserve Beads ready order")
    return lines or ["- No explicit expectations recorded"]


def _render_plan_entries(result: ScenarioRunResult, entries: list[PlanEntry], *, indent: int = 0) -> list[str]:
    lines: list[str] = []
    prefix = "  " * indent
    for entry in entries:
        state = "dispatchable" if entry.dispatchable else "not dispatchable"
        lines.append(
            f"{prefix}- {_display_issue_id(result, entry.issue_id)}: `{entry.issue_type}` / `{entry.classification}` / {state} — {entry.reason}"
        )
        if entry.ready_descendant_ids:
            descendant_labels = ", ".join(_display_issue_id(result, issue_id) for issue_id in entry.ready_descendant_ids)
            lines.append(f"{prefix}  ready descendants: {descendant_labels}")
        if entry.already_accounted_for_ids:
            accounted_for = ", ".join(_display_issue_id(result, issue_id) for issue_id in entry.already_accounted_for_ids)
            lines.append(f"{prefix}  already seen earlier in Beads order: {accounted_for}")
        if entry.suppressed_by:
            lines.append(f"{prefix}  suppressed by: {_display_issue_id(result, entry.suppressed_by)}")
        lines.extend(_render_plan_entries(result, entry.nested_entries, indent=indent + 1))
    return lines


def _display_issue_id(result: ScenarioRunResult, issue_id: str | None) -> str:
    if issue_id is None:
        return "(none)"
    key = result.observations.keys_by_id.get(issue_id)
    if key is None:
        return f"`{issue_id}`"
    return f"`{key}` (`{issue_id}`)"


def _recommended_next_refinement(result: ScenarioRunResult) -> str:
    if result.setup_error is not None:
        return "Fix sandbox or Beads setup errors before trusting this scenario."
    if result.plan.unsupported_types:
        return "Classify the unsupported Beads types explicitly before promoting this policy into production dispatching."
    if result.mismatches:
        return "Review the observed Beads behavior and adjust either the scenario expectations or the Orc V0 policy with evidence from this run."
    return "This scenario currently matches the V0 exploration policy and can serve as a regression check for future Beads changes."


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(inner_value) for key, inner_value in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
