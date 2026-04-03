"""Trial Orc planner for dispatch exploration."""

from __future__ import annotations

from orc.explore.models import (
    IssueClassification,
    ObservedIssue,
    ObservedState,
    PlanEntry,
    TrialPlan,
    UnsupportedTypeFinding,
)

DEFAULT_TYPE_POLICY: dict[str, IssueClassification] = {
    "task": "worker",
    "bug": "worker",
    "feature": "worker",
    "chore": "worker",
    "epic": "container",
    "integration": "container",
}


def build_trial_plan(
    observations: ObservedState,
    *,
    include_in_progress: bool = False,
    type_policy: dict[str, IssueClassification] | None = None,
) -> TrialPlan:
    """Build the V0 trial dispatch plan from observed Beads state."""

    policy = dict(type_policy or DEFAULT_TYPE_POLICY)
    ready_ids = list(observations.ready_ids_in_order)
    ready_index = {issue_id: index for index, issue_id in enumerate(ready_ids)}
    suppressed_by: dict[str, str] = {}
    entries: list[PlanEntry] = []
    unsupported_types: list[UnsupportedTypeFinding] = []
    unsupported_ancestor_by_issue_id = {
        issue_id: unsupported_ancestor_id
        for issue_id in observations.issues_by_id
        if (unsupported_ancestor_id := _find_unsupported_ancestor(issue_id, observations, policy)) is not None
    }

    for issue_id in ready_ids:
        if issue_id in suppressed_by:
            issue = observations.issues_by_id[issue_id]
            entries.append(
                PlanEntry(
                    issue_id=issue.id,
                    key=issue.key,
                    title=issue.title,
                    issue_type=issue.issue_type,
                    status=issue.status,
                    classification=_classify_issue(issue, policy),
                    dispatchable=False,
                    reason=f"already represented under container {suppressed_by[issue_id]}",
                    ready_index=ready_index[issue_id],
                    suppressed_by=suppressed_by[issue_id],
                )
            )
            continue

        if issue_id in unsupported_ancestor_by_issue_id:
            issue = observations.issues_by_id[issue_id]
            entries.append(
                PlanEntry(
                    issue_id=issue.id,
                    key=issue.key,
                    title=issue.title,
                    issue_type=issue.issue_type,
                    status=issue.status,
                    classification=_classify_issue(issue, policy),
                    dispatchable=False,
                    reason=(
                        "inside unsupported container subtree; classify that Beads type before "
                        "dispatching any descendants"
                    ),
                    ready_index=ready_index[issue_id],
                    suppressed_by=unsupported_ancestor_by_issue_id[issue_id],
                )
            )
            continue

        entry, newly_suppressed, new_findings = _plan_issue(
            issue_id,
            observations,
            ready_ids,
            ready_index,
            policy,
            include_in_progress=include_in_progress,
        )
        entries.append(entry)
        unsupported_types.extend(new_findings)
        for covered_issue_id, covering_issue_id in newly_suppressed.items():
            suppressed_by.setdefault(covered_issue_id, covering_issue_id)

    dispatchable_ids = [
        entry.issue_id
        for entry in sorted(_flatten_entries(entries), key=lambda item: item.ready_index if item.ready_index is not None else -1)
        if entry.dispatchable
    ]
    return TrialPlan(
        entries=entries,
        dispatchable_ids=dispatchable_ids,
        invalid=bool(unsupported_types),
        unsupported_types=unsupported_types,
        type_policy=policy,
    )


def _plan_issue(
    issue_id: str,
    observations: ObservedState,
    ready_ids: list[str],
    ready_index: dict[str, int],
    type_policy: dict[str, IssueClassification],
    *,
    include_in_progress: bool,
) -> tuple[PlanEntry, dict[str, str], list[UnsupportedTypeFinding]]:
    issue = observations.issues_by_id[issue_id]
    type_classification = _classify(issue.issue_type, type_policy)
    classification = _classify_issue(issue, type_policy)
    ready_descendants = [
        descendant_id
        for descendant_id in ready_ids
        if descendant_id in observations.descendants_by_id.get(issue_id, [])
    ]

    if classification == "unsupported":
        finding = UnsupportedTypeFinding(
            issue_id=issue.id,
            key=issue.key,
            issue_type=issue.issue_type,
            reason="unsupported Beads issue type encountered; plan marked invalid",
        )
        covered = {
            descendant_id: issue.id
            for descendant_id in ready_descendants
            if ready_index[descendant_id] > ready_index[issue_id]
        }
        reason = "unsupported Beads issue type; Orc fails closed"
        if issue.child_ids:
            reason = "unsupported container/control issue; Orc suppresses this subtree until the type is classified"
        return (
            PlanEntry(
                issue_id=issue.id,
                key=issue.key,
                title=issue.title,
                issue_type=issue.issue_type,
                status=issue.status,
                classification=classification,
                dispatchable=False,
                reason=reason,
                ready_index=ready_index[issue_id],
                ready_descendant_ids=ready_descendants,
            ),
            covered,
            [finding],
        )

    if classification == "worker":
        if issue.status == "in_progress" and not include_in_progress:
            return (
                PlanEntry(
                    issue_id=issue.id,
                    key=issue.key,
                    title=issue.title,
                    issue_type=issue.issue_type,
                    status=issue.status,
                    classification=classification,
                    dispatchable=False,
                    reason="already in progress; observed but excluded from the dispatch frontier",
                    ready_index=ready_index[issue_id],
                ),
                {},
                [],
            )
        return (
            PlanEntry(
                issue_id=issue.id,
                key=issue.key,
                title=issue.title,
                issue_type=issue.issue_type,
                status=issue.status,
                classification=classification,
                dispatchable=True,
                reason="ready worker issue",
                ready_index=ready_index[issue_id],
            ),
            {},
            [],
        )

    past_descendants = [
        descendant_id
        for descendant_id in ready_descendants
        if ready_index[descendant_id] < ready_index[issue_id]
    ]
    future_descendants = [
        descendant_id
        for descendant_id in ready_descendants
        if ready_index[descendant_id] > ready_index[issue_id]
    ]
    nested_entries: list[PlanEntry] = []
    nested_suppressed: dict[str, str] = {}
    unsupported_types: list[UnsupportedTypeFinding] = []

    for descendant_id in future_descendants:
        if descendant_id in nested_suppressed:
            nested_issue = observations.issues_by_id[descendant_id]
            nested_entries.append(
                PlanEntry(
                    issue_id=nested_issue.id,
                    key=nested_issue.key,
                    title=nested_issue.title,
                    issue_type=nested_issue.issue_type,
                    status=nested_issue.status,
                    classification=_classify_issue(nested_issue, type_policy),
                    dispatchable=False,
                    reason=f"already represented under container {nested_suppressed[descendant_id]}",
                    ready_index=ready_index[descendant_id],
                    suppressed_by=nested_suppressed[descendant_id],
                )
            )
            continue

        nested_entry, child_suppressed, child_findings = _plan_issue(
            descendant_id,
            observations,
            ready_ids,
            ready_index,
            type_policy,
            include_in_progress=include_in_progress,
        )
        nested_entries.append(nested_entry)
        unsupported_types.extend(child_findings)
        for covered_issue_id, covering_issue_id in child_suppressed.items():
            nested_suppressed.setdefault(covered_issue_id, covering_issue_id)

    covered = {descendant_id: nested_suppressed.get(descendant_id, issue.id) for descendant_id in future_descendants}
    reason = "container/control issue; inspect ready descendants instead of dispatching directly"
    if issue.child_ids and type_classification == "worker":
        reason = "issue has children; Orc treats it as a control/container node instead of dispatching it directly"
    return (
        PlanEntry(
            issue_id=issue.id,
            key=issue.key,
            title=issue.title,
            issue_type=issue.issue_type,
            status=issue.status,
            classification=classification,
            dispatchable=False,
            reason=reason,
            ready_index=ready_index[issue_id],
            nested_entries=nested_entries,
            ready_descendant_ids=ready_descendants,
            already_accounted_for_ids=past_descendants,
        ),
        covered,
        unsupported_types,
    )


def _classify(issue_type: str, type_policy: dict[str, IssueClassification]) -> IssueClassification:
    return type_policy.get(issue_type, "unsupported")


def _classify_issue(issue: ObservedIssue, type_policy: dict[str, IssueClassification]) -> IssueClassification:
    classification = _classify(issue.issue_type, type_policy)
    if classification == "worker" and issue.child_ids:
        return "container"
    return classification


def _find_unsupported_ancestor(
    issue_id: str,
    observations: ObservedState,
    type_policy: dict[str, IssueClassification],
) -> str | None:
    parent_id = observations.issues_by_id[issue_id].parent_id
    while parent_id is not None:
        parent = observations.issues_by_id.get(parent_id)
        if parent is None:
            return None
        if _classify_issue(parent, type_policy) == "unsupported":
            return parent_id
        parent_id = parent.parent_id
    return None


def _flatten_entries(entries: list[PlanEntry]) -> list[PlanEntry]:
    flattened: list[PlanEntry] = []
    for entry in entries:
        flattened.append(entry)
        flattened.extend(_flatten_entries(entry.nested_entries))
    return flattened
