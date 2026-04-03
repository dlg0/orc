"""Tests for the Beads dispatch exploration planner."""

from __future__ import annotations

from orc.explore.models import ObservedIssue, ObservedState
from orc.explore.planner import build_trial_plan


def _issue(
    issue_id: str,
    *,
    key: str,
    issue_type: str,
    status: str = "open",
    parent_id: str | None = None,
    child_ids: list[str] | None = None,
) -> ObservedIssue:
    return ObservedIssue(
        id=issue_id,
        key=key,
        title=key,
        issue_type=issue_type,
        status=status,
        priority=2,
        parent_id=parent_id,
        child_ids=child_ids or [],
    )


def test_worker_only_plan_dispatches_ready_workers_in_order() -> None:
    observations = ObservedState(
        ids_by_key={"A": "a", "B": "b"},
        keys_by_id={"a": "A", "b": "B"},
        ready_ids_in_order=["b", "a"],
        issues_by_id={
            "a": _issue("a", key="A", issue_type="task"),
            "b": _issue("b", key="B", issue_type="feature"),
        },
        descendants_by_id={"a": [], "b": []},
    )

    plan = build_trial_plan(observations)

    assert plan.dispatchable_ids == ["b", "a"]
    assert not plan.invalid


def test_ready_epic_expands_descendants_without_dispatching_container() -> None:
    observations = ObservedState(
        ids_by_key={"E": "e", "E.1": "e1", "E.2": "e2"},
        keys_by_id={"e": "E", "e1": "E.1", "e2": "E.2"},
        ready_ids_in_order=["e", "e1", "e2"],
        issues_by_id={
            "e": _issue("e", key="E", issue_type="epic", child_ids=["e1", "e2"]),
            "e1": _issue("e1", key="E.1", issue_type="task"),
            "e2": _issue("e2", key="E.2", issue_type="task"),
        },
        descendants_by_id={"e": ["e1", "e2"], "e1": [], "e2": []},
    )

    plan = build_trial_plan(observations)

    assert plan.dispatchable_ids == ["e1", "e2"]
    assert plan.entries[0].classification == "container"
    assert plan.entries[0].dispatchable is False
    assert plan.entries[1].suppressed_by == "e"
    assert plan.entries[2].suppressed_by == "e"


def test_integration_is_treated_as_supported_container() -> None:
    observations = ObservedState(
        ids_by_key={"I": "i", "I.1": "i1"},
        keys_by_id={"i": "I", "i1": "I.1"},
        ready_ids_in_order=["i1", "i"],
        issues_by_id={
            "i": _issue("i", key="I", issue_type="integration", child_ids=["i1"]),
            "i1": _issue("i1", key="I.1", issue_type="task", parent_id="i"),
        },
        descendants_by_id={"i": ["i1"], "i1": []},
    )

    plan = build_trial_plan(observations)

    assert plan.invalid is False
    assert plan.dispatchable_ids == ["i1"]
    assert plan.entries[1].classification == "container"


def test_unsupported_container_suppresses_ready_descendants_even_when_child_appears_first() -> None:
    observations = ObservedState(
        ids_by_key={"P": "p", "X": "x", "X.1": "x1"},
        keys_by_id={"p": "P", "x": "X", "x1": "X.1"},
        ready_ids_in_order=["x1", "x", "p"],
        issues_by_id={
            "p": _issue("p", key="P", issue_type="epic", child_ids=["x"]),
            "x": _issue("x", key="X", issue_type="mystery", parent_id="p", child_ids=["x1"]),
            "x1": _issue("x1", key="X.1", issue_type="task", parent_id="x"),
        },
        descendants_by_id={"p": ["x", "x1"], "x": ["x1"], "x1": []},
    )

    plan = build_trial_plan(observations)

    assert plan.invalid is True
    assert [finding.issue_type for finding in plan.unsupported_types] == ["mystery"]
    assert plan.dispatchable_ids == []
    assert plan.entries[0].dispatchable is False
    assert plan.entries[0].suppressed_by == "x"
    assert "unsupported container subtree" in plan.entries[0].reason


def test_in_progress_issue_is_observed_but_not_dispatched_by_default() -> None:
    observations = ObservedState(
        ids_by_key={"Doing": "d", "Open": "o"},
        keys_by_id={"d": "Doing", "o": "Open"},
        ready_ids_in_order=["d", "o"],
        issues_by_id={
            "d": _issue("d", key="Doing", issue_type="task", status="in_progress"),
            "o": _issue("o", key="Open", issue_type="task"),
        },
        descendants_by_id={"d": [], "o": []},
    )

    plan = build_trial_plan(observations)

    assert plan.dispatchable_ids == ["o"]
    assert plan.entries[0].dispatchable is False
    assert "already in progress" in plan.entries[0].reason
