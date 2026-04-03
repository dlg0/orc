"""Tests for the production dispatch-safety policy."""

from __future__ import annotations

from orc.dispatch_policy import DispatchSkip, IssueNode, build_dispatch_frontier


def _node(
    issue_id: str,
    *,
    issue_type: str,
    status: str = "open",
    parent_id: str | None = None,
    child_ids: tuple[str, ...] = (),
) -> IssueNode:
    return IssueNode(
        id=issue_id,
        issue_type=issue_type,
        status=status,
        parent_id=parent_id,
        child_ids=child_ids,
    )


def _skip(skipped: list[DispatchSkip], issue_id: str) -> DispatchSkip:
    return next(item for item in skipped if item.issue_id == issue_id)


def test_preserves_beads_order_for_dispatchable_items() -> None:
    issues_by_id = {
        "b": _node("b", issue_type="bug"),
        "a": _node("a", issue_type="task"),
    }

    dispatchable, skipped = build_dispatch_frontier(["b", "a"], issues_by_id)

    assert dispatchable == ["b", "a"]
    assert skipped == []


def test_skips_supported_containers_and_workers_with_children() -> None:
    issues_by_id = {
        "epic-1": _node("epic-1", issue_type="epic", child_ids=("task-1",)),
        "task-1": _node("task-1", issue_type="task", parent_id="epic-1", child_ids=("task-1.1",)),
        "task-1.1": _node("task-1.1", issue_type="task", parent_id="task-1"),
    }

    dispatchable, skipped = build_dispatch_frontier(["task-1.1", "task-1", "epic-1"], issues_by_id)

    assert dispatchable == ["task-1.1"]
    assert _skip(skipped, "task-1").category == "container/control"
    assert _skip(skipped, "epic-1").category == "container/control"


def test_skips_in_progress_items_even_if_ready() -> None:
    issues_by_id = {
        "work-1": _node("work-1", issue_type="task", status="in_progress"),
    }

    dispatchable, skipped = build_dispatch_frontier(["work-1"], issues_by_id)

    assert dispatchable == []
    assert _skip(skipped, "work-1").category == "in progress"


def test_skips_unsupported_leaf_without_suppressing_unrelated_items() -> None:
    issues_by_id = {
        "odd-1": _node("odd-1", issue_type="decision"),
        "task-1": _node("task-1", issue_type="task"),
    }

    dispatchable, skipped = build_dispatch_frontier(["odd-1", "task-1"], issues_by_id)

    assert dispatchable == ["task-1"]
    assert _skip(skipped, "odd-1").category == "unsupported type"


def test_unsupported_container_suppresses_descendants_that_appear_first() -> None:
    issues_by_id = {
        "x": _node("x", issue_type="decision", child_ids=("x.1",)),
        "x.1": _node("x.1", issue_type="task", parent_id="x"),
        "other": _node("other", issue_type="bug"),
    }

    dispatchable, skipped = build_dispatch_frontier(["x.1", "x", "other"], issues_by_id)

    assert dispatchable == ["other"]
    assert _skip(skipped, "x.1").category == "unsupported subtree"
    assert _skip(skipped, "x.1").suppressed_by == "x"
    assert _skip(skipped, "x").category == "unsupported type"


def test_ready_descendant_is_suppressed_when_unsupported_ancestor_is_not_ready() -> None:
    issues_by_id = {
        "root": _node("root", issue_type="decision", child_ids=("child",)),
        "child": _node("child", issue_type="task", parent_id="root"),
        "safe": _node("safe", issue_type="chore"),
    }

    dispatchable, skipped = build_dispatch_frontier(["child", "safe"], issues_by_id)

    assert dispatchable == ["safe"]
    assert _skip(skipped, "child").category == "unsupported subtree"
