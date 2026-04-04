"""Queue manager: reads ``bd ready`` and derives Orc's dispatch frontier."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from orc.dispatch_policy import DispatchSkip, IssueNode, build_dispatch_frontier


@dataclass
class BdIssue:
    """A bd issue parsed from Beads JSON output."""

    id: str
    title: str
    priority: int  # 1=urgent, 2=high, 3=normal, 4=low, 0=none
    created: str  # ISO date string
    description: str = ""
    acceptance_criteria: str = ""
    parent: str = ""  # parent issue ID, if this is a child issue
    issue_type: str = ""
    status: str = ""


@dataclass
class QueueResult:
    """Result of fetching the ready-issue queue.

    ``issues`` is the Orc-dispatchable subsequence of the raw Beads-ready list.
    ``raw_issues`` keeps the original Beads-ready set for operator diagnostics.
    """

    issues: list[BdIssue] = field(default_factory=list)
    raw_issues: list[BdIssue] = field(default_factory=list)
    skipped: list[DispatchSkip] = field(default_factory=list)
    success: bool = True
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.raw_issues and self.issues:
            self.raw_issues = list(self.issues)

    @property
    def beads_ready(self) -> int:
        """Count of raw issues returned by ``bd ready`` before Orc filtering."""

        return len(self.raw_issues)

    @property
    def policy_skipped(self) -> int:
        """Count of raw ready items skipped by Orc's dispatch policy."""

        return len(self.skipped)


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
        parent=raw.get("parent", ""),
        issue_type=raw.get("issue_type", ""),
        status=raw.get("status", ""),
    )


def _run_bd_json_list(
    command: list[str],
    *,
    cwd: Path | None,
    failure_message: str,
) -> tuple[list[dict] | None, str | None]:
    """Run a bd JSON command that is expected to return a list."""

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
    except OSError as exc:
        return None, str(exc)

    if result.returncode != 0:
        error = result.stderr.strip() if result.stderr and result.stderr.strip() else failure_message
        return None, error

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, str(exc)

    if not isinstance(data, list):
        return None, f"{command[1]} returned non-list JSON"
    return data, None


def _build_issue_nodes(all_rows: list[dict], ready_issues: list[BdIssue]) -> dict[str, IssueNode]:
    """Build the minimal issue graph needed for dispatch filtering."""

    row_by_id: dict[str, dict] = {}
    child_ids_by_parent: dict[str, list[str]] = {}

    def note_parent(issue_id: str, parent_id: str | None) -> None:
        if not parent_id:
            return
        child_ids = child_ids_by_parent.setdefault(parent_id, [])
        if issue_id not in child_ids:
            child_ids.append(issue_id)

    for row in all_rows:
        issue_id = row.get("id")
        if not issue_id:
            continue
        row_by_id[issue_id] = row
        note_parent(issue_id, row.get("parent") or None)

    for issue in ready_issues:
        row_by_id.setdefault(
            issue.id,
            {
                "id": issue.id,
                "issue_type": issue.issue_type,
                "status": issue.status,
                "parent": issue.parent,
            },
        )
        note_parent(issue.id, issue.parent or None)

    issues_by_id: dict[str, IssueNode] = {}
    for issue_id, row in row_by_id.items():
        issues_by_id[issue_id] = IssueNode(
            id=issue_id,
            issue_type=row.get("issue_type", ""),
            status=row.get("status", ""),
            parent_id=row.get("parent") or None,
            child_ids=tuple(child_ids_by_parent.get(issue_id, [])),
        )
    return issues_by_id


def resolve_issue_id(issue_id: str, cwd: Path | None = None) -> str:
    """Resolve a possibly-suffix-only issue ID to its full prefixed form.

    If *issue_id* already contains a ``-`` (i.e. looks like a full ID such as
    ``orc-1wf``), it is returned unchanged.  Otherwise, calls
    ``bd show <suffix> --json`` to discover the canonical ID.

    Raises ``ValueError`` if the suffix cannot be resolved.
    """
    if "-" in issue_id:
        return issue_id
    try:
        result = subprocess.run(
            ["bd", "show", issue_id, "--json"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            raise ValueError(f"Cannot resolve issue suffix '{issue_id}': {stderr or 'bd show failed'}")
        data = json.loads(result.stdout)
        if isinstance(data, list) and len(data) == 1:
            return data[0]["id"]
        if isinstance(data, list) and len(data) > 1:
            ids = [item["id"] for item in data]
            raise ValueError(f"Ambiguous suffix '{issue_id}' matches multiple issues: {', '.join(ids)}")
        raise ValueError(f"Cannot resolve issue suffix '{issue_id}': no results")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError(f"Cannot resolve issue suffix '{issue_id}': {exc}") from exc


def get_ready_issues(cwd: Path | None = None) -> QueueResult:
    """Return Orc's dispatch frontier derived from raw ``bd ready`` output.

    Beads owns readiness and ordering; Orc applies only dispatch-safety filters.
    """
    ready_rows, error = _run_bd_json_list(
        ["bd", "ready", "--json", "--limit", "0"],
        cwd=cwd,
        failure_message="bd ready failed",
    )
    if error is not None or ready_rows is None:
        return QueueResult(issues=[], raw_issues=[], skipped=[], success=False, error=error)

    raw_ready_issues = [_parse_issue(item) for item in ready_rows]

    all_rows, error = _run_bd_json_list(
        ["bd", "list", "--all", "--json", "--limit", "0"],
        cwd=cwd,
        failure_message="bd list failed",
    )
    if error is not None or all_rows is None:
        return QueueResult(issues=[], raw_issues=raw_ready_issues, skipped=[], success=False, error=error)

    issues_by_id = _build_issue_nodes(all_rows, raw_ready_issues)
    dispatchable_ids, skipped = build_dispatch_frontier(
        [issue.id for issue in raw_ready_issues],
        issues_by_id,
    )
    dispatchable_id_set = set(dispatchable_ids)
    dispatchable_issues = [issue for issue in raw_ready_issues if issue.id in dispatchable_id_set]

    return QueueResult(
        issues=dispatchable_issues,
        raw_issues=raw_ready_issues,
        skipped=skipped,
        success=True,
        error=None,
    )


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
    """Pick the first eligible issue in Beads order.

    If *priority_id* is set and present in *issues* (and not skipped),
    it is returned immediately.
    """
    skip = skip_ids or set()
    candidates = [i for i in issues if i.id not in skip]
    if not candidates:
        return None
    if priority_id:
        for c in candidates:
            if c.id == priority_id:
                return c
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


@dataclass
class QueueBreakdown:
    """Breakdown of beads-ready issues into raw, skipped, held, and runnable counts."""

    beads_ready: int
    policy_skipped: int
    held_and_ready: int
    runnable: int

    @property
    def has_held_blocking(self) -> bool:
        """True when there are beads-ready issues but none are runnable."""
        return self.beads_ready > 0 and self.runnable == 0 and self.held_and_ready > 0


def compute_queue_breakdown(
    ready_issues: QueueResult | list[BdIssue],
    issue_failures: dict[str, object],
) -> QueueBreakdown:
    """Compute the operator-facing queue breakdown.

    *ready_issues* may be a raw list of issues or a full :class:`QueueResult`.
    *issue_failures* is the held-issue dict from orchestrator state.
    """
    held_ids = set(issue_failures)

    if isinstance(ready_issues, QueueResult):
        dispatchable_issues = ready_issues.issues
        beads_ready = ready_issues.beads_ready
        policy_skipped = ready_issues.policy_skipped
    else:
        dispatchable_issues = ready_issues
        beads_ready = len(ready_issues)
        policy_skipped = 0

    held_and_ready = sum(1 for i in dispatchable_issues if i.id in held_ids)
    runnable = len(dispatchable_issues) - held_and_ready
    return QueueBreakdown(
        beads_ready=beads_ready,
        policy_skipped=policy_skipped,
        held_and_ready=held_and_ready,
        runnable=runnable,
    )


def summarize_skipped_issues(skipped: list[DispatchSkip]) -> dict[str, int]:
    """Return grouped skip counts for operator-facing diagnostics."""

    counts = Counter(skip.category for skip in skipped)
    return {category: counts[category] for category in sorted(counts)}


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


def create_issue(
    title: str,
    description: str,
    *,
    parent: str | None = None,
    priority: int | None = None,
    cwd: Path | None = None,
) -> str | None:
    """Create a new bd issue and return its ID, or None on failure.

    Runs ``bd create "<title>" --description "<description>" --silent``
    with optional ``--parent`` and ``--priority`` flags.
    """
    cmd: list[str] = ["bd", "create", title, "--description", description, "--silent"]
    if parent is not None:
        cmd.extend(["--parent", parent])
    if priority is not None:
        cmd.extend(["--priority", str(priority)])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except OSError:
        return None


def close_issue(issue_id: str, cwd: Path | None = None) -> bool:
    """Run ``bd close <issue_id>`` to close an issue.

    Returns True if the close succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["bd", "close", issue_id],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def reopen_issue(issue_id: str, cwd: Path | None = None) -> bool:
    """Reopen a closed issue via ``bd update --status open``.

    Returns True if the update succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["bd", "update", issue_id, "--status", "open"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def rewrite_parent_as_integration_issue(
    issue_id: str,
    child_ids: list[str],
    cwd: Path | None = None,
) -> bool:
    """Rewrite a decomposed parent into a verification/integration issue.

    Called by the scheduler after a worker returns a ``decomposed`` result.
    Updates the parent's title, description, and acceptance criteria so it
    becomes a verification gate for its children.

    Returns True if the update succeeded, False otherwise.
    """
    children_list = ", ".join(child_ids) if child_ids else "(see child issues)"
    new_title = f"[integration] Verify decomposed children of {issue_id}"
    new_description = (
        f"This issue was automatically rewritten by orc after decomposition.\n\n"
        f"Verify that the child issues ({children_list}) collectively satisfy "
        f"the original requirements. Run integration tests and confirm no "
        f"regressions."
    )
    new_acceptance = (
        "All child issues are closed and their changes are landed. "
        "Integration tests pass. No regressions introduced."
    )
    try:
        result = subprocess.run(
            [
                "bd", "update", issue_id,
                "--title", new_title,
                "--description", new_description,
                "--acceptance", new_acceptance,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def get_children_ids(parent_id: str, cwd: Path | None = None) -> list[str]:
    """Return a list of child issue IDs for *parent_id*.

    Returns an empty list if the parent has no children or the query fails.
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
            return []
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return []
        return [child["id"] for child in data if "id" in child]
    except (OSError, json.JSONDecodeError):
        return []


def get_issue_details(issue_id: str, cwd: Path | None = None) -> dict | None:
    """Return the full issue dict for *issue_id*, or None on failure.

    Calls ``bd show <id> --json`` and returns the first element of the
    resulting list.
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
            return data[0]
        return None
    except (OSError, json.JSONDecodeError):
        return None
