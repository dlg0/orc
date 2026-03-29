"""Queue manager: reads bd ready issues and selects the next one to work on."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
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


def get_ready_issues(cwd: Path | None = None) -> list[BdIssue]:
    """Run ``bd ready --json`` and return parsed issues.

    Returns an empty list when no issues are ready or the command fails.
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
            return []
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return []
        return [_parse_issue(item) for item in data]
    except (OSError, json.JSONDecodeError):
        return []


def _sort_key(issue: BdIssue) -> tuple[int, str]:
    """Return a sort key so that lower-numbered priorities come first.

    Priority 0 (none) is treated as the lowest priority by mapping it to a
    value higher than any real priority level.
    """
    effective = issue.priority if issue.priority != 0 else 999
    return (effective, issue.created)


def select_next_issue(
    issues: list[BdIssue],
    skip_ids: set[str] | None = None,
) -> BdIssue | None:
    """Pick the highest-priority, oldest issue not in *skip_ids*."""
    skip = skip_ids or set()
    candidates = [i for i in issues if i.id not in skip]
    if not candidates:
        return None
    candidates.sort(key=_sort_key)
    return candidates[0]
