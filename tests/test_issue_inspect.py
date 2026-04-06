"""Tests for the unified IssueInspectModel and builder functions."""

from __future__ import annotations

from orc.queue import BdIssue
from orc.tui.issue_inspect import (
    _build_active_timeline,
    _build_held_timeline,
    _build_history_timeline,
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

    def test_preflight_log_preserved(self) -> None:
        run = {
            "issue_id": "X-8",
            "result": "skipped_already_implemented",
            "preflight_log_path": "/tmp/preflight.jsonl",
        }
        model = build_from_history(run)
        assert model.preflight_log_path == "/tmp/preflight.jsonl"

    def test_eval_log_and_result_preserved(self) -> None:
        run = {
            "issue_id": "X-9",
            "result": "completed",
            "eval_log_path": "/tmp/eval.log",
            "eval_result": {
                "verdict": "pass",
                "summary": "done",
                "classification": "verdict",
            },
        }
        model = build_from_history(run)
        assert model.eval_log_path == "/tmp/eval.log"
        assert model.evaluation_result is not None
        assert model.evaluation_result["summary"] == "done"


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


class TestPreflightShownInTimeline:
    """Preflight should appear as the first visible workflow step."""

    def test_active_timeline_includes_preflight(self) -> None:
        steps = _build_active_timeline("amp_running", "/tmp/log")
        phases = [s.phase for s in steps]
        assert phases[0] == "preflight"
        assert "preflight" in phases

    def test_active_timeline_marks_evaluation_log(self) -> None:
        steps = _build_active_timeline("evaluation_running", "/tmp/amp.log", None, "/tmp/eval.log")
        eval_step = next(step for step in steps if step.phase == "evaluation_running")
        assert eval_step.has_log is True

    def test_held_timeline_includes_preflight(self) -> None:
        failure = {"stage": "amp", "summary": "Boom"}
        steps = _build_held_timeline(failure, had_evaluation=False)
        phases = [s.phase for s in steps]
        assert phases[0] == "preflight"
        assert "preflight" in phases

    def test_history_timeline_includes_preflight(self) -> None:
        steps = _build_history_timeline("merge_running", "completed")
        phases = [s.phase for s in steps]
        assert phases[0] == "preflight"
        assert "preflight" in phases

    def test_active_at_preflight_phase_marks_preflight_active(self) -> None:
        steps = _build_active_timeline("preflight", None)
        phases = [s.phase for s in steps]
        assert phases[0] == "preflight"
        assert steps[0].status == "active"
        assert all(s.status == "pending" for s in steps[1:])
