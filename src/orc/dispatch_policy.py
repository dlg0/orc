"""Dispatch-safety policy derived from Beads exploration."""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_WORKER_TYPES = frozenset({"task", "bug", "feature", "chore"})
SUPPORTED_CONTAINER_TYPES = frozenset({"epic", "integration"})


@dataclass(frozen=True)
class IssueNode:
    """Minimal issue metadata needed to classify dispatch safety."""

    id: str
    issue_type: str = ""
    status: str = ""
    parent_id: str | None = None
    child_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatchSkip:
    """A raw Beads-ready item that Orc intentionally skipped."""

    issue_id: str
    issue_type: str
    status: str
    category: str
    reason: str
    suppressed_by: str | None = None


def classify_issue(node: IssueNode) -> str:
    """Classify an issue as worker, container, or unsupported."""

    if node.issue_type in SUPPORTED_CONTAINER_TYPES:
        return "container"
    if node.issue_type in SUPPORTED_WORKER_TYPES:
        if node.child_ids:
            return "container"
        return "worker"
    return "unsupported"


def build_dispatch_frontier(
    ready_ids: list[str],
    issues_by_id: dict[str, IssueNode],
    *,
    include_in_progress: bool = False,
) -> tuple[list[str], list[DispatchSkip]]:
    """Return dispatchable ready IDs and skipped items in Beads order."""

    unsupported_ancestor_by_issue_id = {
        issue_id: unsupported_ancestor_id
        for issue_id in issues_by_id
        if (unsupported_ancestor_id := _find_unsupported_ancestor(issue_id, issues_by_id)) is not None
    }

    dispatchable_ids: list[str] = []
    skipped: list[DispatchSkip] = []

    for issue_id in ready_ids:
        node = issues_by_id[issue_id]

        if issue_id in unsupported_ancestor_by_issue_id:
            skipped.append(
                DispatchSkip(
                    issue_id=issue_id,
                    issue_type=node.issue_type,
                    status=node.status,
                    category="unsupported subtree",
                    reason=(
                        "inside unsupported container subtree; classify that Beads type before "
                        "dispatching descendants"
                    ),
                    suppressed_by=unsupported_ancestor_by_issue_id[issue_id],
                )
            )
            continue

        classification = classify_issue(node)
        if classification == "worker":
            if node.status == "in_progress" and not include_in_progress:
                skipped.append(
                    DispatchSkip(
                        issue_id=issue_id,
                        issue_type=node.issue_type,
                        status=node.status,
                        category="in progress",
                        reason="already in progress; excluded from a new dispatch frontier",
                    )
                )
                continue
            dispatchable_ids.append(issue_id)
            continue

        if classification == "container":
            if node.child_ids and node.issue_type in SUPPORTED_WORKER_TYPES:
                reason = (
                    "issue has children; Orc treats it as a control/container node instead "
                    "of dispatching it directly"
                )
            else:
                reason = "container/control issue; Orc does not dispatch containers directly"
            skipped.append(
                DispatchSkip(
                    issue_id=issue_id,
                    issue_type=node.issue_type,
                    status=node.status,
                    category="container/control",
                    reason=reason,
                )
            )
            continue

        reason = "unsupported Beads issue type; Orc fails closed"
        if node.child_ids:
            reason = (
                "unsupported container/control issue; Orc suppresses this subtree until the "
                "type is classified"
            )
        skipped.append(
            DispatchSkip(
                issue_id=issue_id,
                issue_type=node.issue_type,
                status=node.status,
                category="unsupported type",
                reason=reason,
            )
        )

    return dispatchable_ids, skipped


def _find_unsupported_ancestor(issue_id: str, issues_by_id: dict[str, IssueNode]) -> str | None:
    """Return the nearest unsupported ancestor for *issue_id*, if any."""

    seen: set[str] = set()
    current_id = issues_by_id[issue_id].parent_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        current = issues_by_id.get(current_id)
        if current is None:
            return None
        if classify_issue(current) == "unsupported":
            return current_id
        current_id = current.parent_id
    return None
