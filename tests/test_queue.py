"""Tests for the queue manager."""

from __future__ import annotations

from amp_orchestrator.queue import BdIssue, select_next_issue


def _issue(
    id: str = "i1",
    title: str = "t",
    priority: int = 3,
    created: str = "2026-01-01T00:00:00Z",
) -> BdIssue:
    return BdIssue(id=id, title=title, priority=priority, created=created)


class TestSelectNextIssue:
    def test_empty_list_returns_none(self) -> None:
        assert select_next_issue([]) is None

    def test_single_issue(self) -> None:
        issue = _issue()
        assert select_next_issue([issue]) is issue

    def test_selects_highest_priority(self) -> None:
        low = _issue(id="low", priority=4)
        high = _issue(id="high", priority=1)
        normal = _issue(id="normal", priority=3)
        assert select_next_issue([low, high, normal]) is high

    def test_breaks_ties_by_oldest_created(self) -> None:
        newer = _issue(id="new", priority=2, created="2026-03-01T00:00:00Z")
        older = _issue(id="old", priority=2, created="2026-01-01T00:00:00Z")
        assert select_next_issue([newer, older]) is older

    def test_priority_zero_treated_as_lowest(self) -> None:
        none_pri = _issue(id="none", priority=0)
        low = _issue(id="low", priority=4)
        assert select_next_issue([none_pri, low]) is low

    def test_priority_zero_only(self) -> None:
        a = _issue(id="a", priority=0, created="2026-01-01T00:00:00Z")
        b = _issue(id="b", priority=0, created="2026-02-01T00:00:00Z")
        assert select_next_issue([b, a]) is a

    def test_skip_ids_filters_issues(self) -> None:
        a = _issue(id="a", priority=1)
        b = _issue(id="b", priority=2)
        assert select_next_issue([a, b], skip_ids={"a"}) is b

    def test_skip_ids_all_filtered_returns_none(self) -> None:
        a = _issue(id="a")
        assert select_next_issue([a], skip_ids={"a"}) is None

    def test_skip_ids_none_is_noop(self) -> None:
        a = _issue(id="a", priority=1)
        assert select_next_issue([a], skip_ids=None) is a

    def test_mixed_priorities_and_dates(self) -> None:
        issues = [
            _issue(id="u2", priority=1, created="2026-03-01T00:00:00Z"),
            _issue(id="u1", priority=1, created="2026-01-01T00:00:00Z"),
            _issue(id="h1", priority=2, created="2026-01-01T00:00:00Z"),
            _issue(id="n0", priority=0, created="2026-01-01T00:00:00Z"),
        ]
        result = select_next_issue(issues)
        assert result is not None
        assert result.id == "u1"
