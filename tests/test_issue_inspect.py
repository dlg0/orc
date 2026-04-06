"""Tests for the unified IssueInspectModel and builder functions."""

from __future__ import annotations

from orc.queue import BdIssue
from orc.tui.issue_inspect import (
    IssueInspectModel,
    build_from_history,
    build_from_queue,
)


class TestBuildFromQueue:
    def test_basic_runnable(self) -> None:
        issue = BdIssue(
            id="X-1", title="Add feature", priority=2, created="2026-01-01",
            description="Do the thing", acceptance_criteria="It works",
        )
        model = build_from_queue(issue, "Runnable")
        assert model.issue_id == "X-1"
        assert model.source == "queue"
        assert model.state_label == "Runnable"
        assert model.status_tone == "green"
        assert model.dispatch_state == "Runnable"
        assert model.priority == 2
        assert model.created_at == "2026-01-01"
        assert model.description == "Do the thing"
        assert model.acceptance_criteria == "It works"
        assert model.workflow_steps == []
        assert model.events == []

    def test_held_ready(self) -> None:
        issue = BdIssue(id="X-2", title="Fix bug", priority=1, created="2026-02-01")
        model = build_from_queue(issue, "Held (ready)")
        assert model.state_label == "Held (ready)"
        assert model.status_tone == "bright_yellow"


class TestBuildFromHistory:
    def test_completed_run(self) -> None:
        run = {
            "issue_id": "X-3",
            "issue_title": "Ship it",
            "result": "completed",
            "summary": "All good",
            "timestamp": "2026-03-01T10:00:00Z",
            "branch": "orc/X-3",
            "worktree_path": "/tmp/wt",
            "thread_id": "T-abc123",
        }
        model = build_from_history(run)
        assert model.issue_id == "X-3"
        assert model.source == "history"
        assert model.state_label == "Completed"
        assert model.status_tone == "green"
        assert model.result == "completed"
        assert model.summary == "All good"
        assert model.branch == "orc/X-3"
        assert model.thread_id == "T-abc123"

    def test_failed_run(self) -> None:
        run = {"issue_id": "X-4", "result": "failed", "summary": "Boom"}
        model = build_from_history(run)
        assert model.state_label == "Failed"
        assert model.status_tone == "red"

    def test_empty_result(self) -> None:
        run = {"issue_id": "X-5"}
        model = build_from_history(run)
        assert model.state_label == "Unknown"
        assert model.status_tone == "white"

    def test_amp_result_dict_preserved(self) -> None:
        run = {
            "issue_id": "X-6",
            "result": "completed",
            "amp_result": {"summary": "Did stuff", "merge_ready": True},
        }
        model = build_from_history(run)
        assert model.agent_result is not None
        assert model.agent_result["merge_ready"] is True

    def test_amp_result_non_dict_ignored(self) -> None:
        run = {"issue_id": "X-7", "result": "completed", "amp_result": "string-val"}
        model = build_from_history(run)
        assert model.agent_result is None


class TestModelConditionalSections:
    """Test that the model has the right data for conditional rendering."""

    def test_queue_has_no_workflow(self) -> None:
        issue = BdIssue(id="Q-1", title="T", priority=0, created="2026-01-01")
        model = build_from_queue(issue, "Runnable")
        assert model.workflow_steps == []
        assert model.agent_result is None
        assert model.evaluation_result is None
        assert model.merge_details is None

    def test_history_has_no_events(self) -> None:
        run = {"issue_id": "H-1", "result": "completed"}
        model = build_from_history(run)
        assert model.events == []

    def test_queue_description_available(self) -> None:
        issue = BdIssue(
            id="Q-2", title="T", priority=0, created="2026-01-01",
            description="desc", acceptance_criteria="ac",
        )
        model = build_from_queue(issue, "Runnable")
        assert model.description == "desc"
        assert model.acceptance_criteria == "ac"
