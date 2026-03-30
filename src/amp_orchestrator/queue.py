"""Queue manager: reads bd ready issues and selects the next one to work on."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


@dataclass
class BdIssue:
    """A bd issue parsed from ``bd ready --json`` output."""

    id: str
    title: str
    priority: int  # 1=urgent, 2=high, 3=normal, 4=low, 0=none
    created: str  # ISO date string
    description: str = ""
    acceptance_criteria: str = ""


@dataclass
class QueueResult:
    """Result of fetching the ready-issue queue.

    Distinguishes an empty queue (success=True, issues=[]) from a fetch
    failure (success=False, error=...).
    """

    issues: list[BdIssue] = field(default_factory=list)
    success: bool = True
    error: str | None = None


def _extract_acceptance_criteria(description: str) -> str:
    """Extract the Acceptance Criteria section from a bd issue description."""
    marker = "## Acceptance Criteria"
    idx = description.find(marker)
    if idx == -1:
        return ""
    after = description[idx + len(marker) :]
    # Take everything until the next heading or end of string.
    next_heading = after.find("\n## ")
    if next_heading != -1:
        return after[:next_heading].strip()
    return after.strip()


def _parse_issue(raw: dict) -> BdIssue:
    """Convert a single JSON object from bd into a BdIssue."""
    description = raw.get("description", "")
    return BdIssue(
        id=raw["id"],
        title=raw["title"],
        priority=raw.get("priority", 0),
        created=raw.get("created_at", ""),
        description=description,
        acceptance_criteria=_extract_acceptance_criteria(description),
    )


def get_ready_issues(cwd: Path | None = None) -> QueueResult:
    """Run ``bd ready --json`` and return a structured result.

    Returns a :class:`QueueResult` that distinguishes an empty queue
    (``success=True``) from a fetch failure (``success=False``).
    """
    try:
        result = subprocess.run(
            ["bd", "ready", "--json"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            error = result.stderr.strip() if result.stderr and result.stderr.strip() else "bd ready failed"
            return QueueResult(issues=[], success=False, error=error)
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return QueueResult(issues=[], success=False, error="bd ready returned non-list JSON")
        return QueueResult(issues=[_parse_issue(item) for item in data], success=True, error=None)
    except OSError as exc:
        return QueueResult(issues=[], success=False, error=str(exc))
    except json.JSONDecodeError as exc:
        return QueueResult(issues=[], success=False, error=str(exc))


def _sort_key(issue: BdIssue) -> tuple[int, str]:
    """Return a sort key so that lower-numbered priorities come first.

    Priority 0 (none) is treated as the lowest priority by mapping it to a
    value higher than any real priority level.
    """
    effective = issue.priority if issue.priority != 0 else 999
    return (effective, issue.created)


def claim_issue(issue_id: str, cwd: Path | None = None) -> bool:
    """Run ``bd update <id> --claim`` to atomically claim an issue.

    Returns True if the claim succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["bd", "update", issue_id, "--claim"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def unclaim_issue(issue_id: str, cwd: Path | None = None) -> bool:
    """Release a bd issue claim by resetting status to open.

    Returns True if the unclaim succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["bd", "update", issue_id, "--status", "open", "--assignee", ""],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def select_next_issue(
    issues: list[BdIssue],
    skip_ids: set[str] | None = None,
    priority_id: str | None = None,
) -> BdIssue | None:
    """Pick the highest-priority, oldest issue not in *skip_ids*.

    If *priority_id* is set and present in *issues* (and not skipped),
    it is returned immediately regardless of priority/date ordering.
    This supports parent-promotion: when all children of a parent are
    closed, the parent is force-selected as the next issue.
    """
    skip = skip_ids or set()
    candidates = [i for i in issues if i.id not in skip]
    if not candidates:
        return None
    if priority_id:
        for c in candidates:
            if c.id == priority_id:
                return c
    candidates.sort(key=_sort_key)
    return candidates[0]


def get_issue_parent(issue_id: str, cwd: Path | None = None) -> str | None:
    """Return the parent issue ID for *issue_id*, or None if it has no parent.

    Calls ``bd show <id> --json`` and reads the ``parent`` field.
    """
    try:
        result = subprocess.run(
            ["bd", "show", issue_id, "--json"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if isinstance(data, list) and len(data) > 0:
            return data[0].get("parent") or None
        return None
    except (OSError, json.JSONDecodeError):
        return None


def get_issue_status(issue_id: str, cwd: Path | None = None) -> str | None:
    """Return the beads status for *issue_id*, or ``None`` if unavailable."""
    try:
        result = subprocess.run(
            ["bd", "show", issue_id, "--json"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if isinstance(data, list) and len(data) > 0:
            status = data[0].get("status")
            return status if isinstance(status, str) else None
        return None
    except (OSError, json.JSONDecodeError):
        return None


class IssueState(Enum):
    """Broad state of a beads issue for reconciliation purposes."""

    open = "open"
    closed = "closed"
    missing = "missing"
    unknown = "unknown"  # transient bd failure — keep the entry


def get_issue_state(issue_id: str, cwd: Path | None = None) -> IssueState:
    """Return the broad state of a beads issue.

    Distinguishes between closed/missing issues (safe to prune) and
    transient failures (should keep the failure entry).
    """
    try:
        result = subprocess.run(
            ["bd", "show", issue_id, "--json"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip().lower() if result.stderr else ""
            if "no issue" in stderr or "not found" in stderr:
                return IssueState.missing
            return IssueState.unknown
        data = json.loads(result.stdout)
        if isinstance(data, list) and len(data) > 0:
            status = data[0].get("status", "")
            if status == "closed":
                return IssueState.closed
            return IssueState.open
        return IssueState.missing
    except (OSError, json.JSONDecodeError):
        return IssueState.unknown


def reconcile_issue_failures(
    issue_failures: dict[str, dict],
    cwd: Path | None = None,
) -> list[tuple[str, str]]:
    """Prune issue_failures entries whose beads issue is closed or missing.

    Returns a list of ``(issue_id, reason)`` tuples for each pruned entry.
    Mutates *issue_failures* in place.
    """
    pruned: list[tuple[str, str]] = []
    for issue_id in list(issue_failures):
        bd_state = get_issue_state(issue_id, cwd=cwd)
        if bd_state in (IssueState.closed, IssueState.missing):
            del issue_failures[issue_id]
            pruned.append((issue_id, bd_state.value))
    return pruned


def get_children_all_closed(parent_id: str, cwd: Path | None = None) -> bool | None:
    """Check whether all children of *parent_id* are closed.

    Returns True if the parent has children and all are closed,
    False if any child is not closed,
    None if the parent has no children or the query fails.
    """
    try:
        result = subprocess.run(
            ["bd", "children", parent_id, "--json"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not isinstance(data, list) or len(data) == 0:
            return None
        return all(child.get("status") == "closed" for child in data)
    except (OSError, json.JSONDecodeError):
        return None
