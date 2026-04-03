"""Tests for the dispatch exploration runner."""

from __future__ import annotations

import json
from pathlib import Path

from orc.explore.models import CommandRecord, IssueSpec, ScenarioDefinition
from orc.explore.runner import run_scenario


class FakeSandbox:
    def __init__(self, *, keep: bool = False) -> None:
        self.keep = keep
        self.path = Path("/tmp/fake-sandbox")

    def __enter__(self) -> "FakeSandbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class FakeClient:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.transcript = []
        self.created: list[tuple[IssueSpec, str | None]] = []
        self.updated: list[tuple[str, str | None, str | None]] = []
        self.blockers: list[tuple[str, str]] = []
        self._ids_by_title: dict[str, str] = {}

    def initialize(self, prefix: str = "orcx") -> None:
        self.transcript.append(CommandRecord(command=["init", prefix], returncode=0, stdout="", stderr=""))

    def configure_custom_types(self, issue_types: set[str]) -> None:
        self.transcript.append(CommandRecord(command=["config", *sorted(issue_types)], returncode=0, stdout="", stderr=""))

    def create_issue(self, spec: IssueSpec, *, parent_id: str | None = None) -> str:
        issue_id = f"id-{spec.key}"
        self.created.append((spec, parent_id))
        self._ids_by_title[spec.title] = issue_id
        return issue_id

    def update_issue(self, issue_id: str, *, status: str | None = None, defer_until: str | None = None) -> None:
        self.updated.append((issue_id, status, defer_until))

    def add_blocker(self, issue_id: str, blocked_by_id: str) -> None:
        self.blockers.append((issue_id, blocked_by_id))

    def ready(self) -> list[dict]:
        return [
            {"id": "id-P", "title": "Parent", "issue_type": "epic", "status": "open", "priority": 2},
            {"id": "id-C", "title": "Child", "issue_type": "task", "status": "open", "priority": 2, "parent": "id-P"},
        ]

    def list_all(self) -> list[dict]:
        return [
            {"id": "id-P", "title": "Parent", "issue_type": "epic", "status": "open", "priority": 2},
            {
                "id": "id-C",
                "title": "Child",
                "issue_type": "task",
                "status": "open",
                "priority": 2,
                "parent": "id-P",
                "dependencies": [{"depends_on_id": "id-P", "type": "parent-child"}],
            },
        ]

    def list_tree(self) -> str:
        return "Parent\n  Child\n"


def test_run_scenario_writes_reports_and_applies_relations(tmp_path: Path) -> None:
    scenario = ScenarioDefinition(
        name="fake",
        description="Fake scenario",
        issues=(
            IssueSpec(key="P", title="Parent", issue_type="epic"),
            IssueSpec(key="C", title="Child", parent="P", blockers=("P",), status="in_progress"),
        ),
    )

    result = run_scenario(
        scenario=scenario,
        output_dir=tmp_path,
        sandbox_factory=FakeSandbox,
        client_factory=FakeClient,
    )

    assert result.markdown_path.exists()
    assert result.json_path.exists()
    report = json.loads(result.json_path.read_text())
    assert report["scenario"]["name"] == "fake"
    assert report["created_ids_by_key"] == {"P": "id-P", "C": "id-C"}
    assert result.plan.dispatchable_ids == ["id-C"]
