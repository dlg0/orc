"""Tests for the queue manager."""

from __future__ import annotations

from unittest.mock import patch

from amp_orchestrator.queue import (
    BdIssue,
    QueueResult,
    claim_issue,
    get_children_all_closed,
    get_issue_parent,
    get_issue_status,
    get_ready_issues,
    select_next_issue,
    unclaim_issue,
)


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


class TestClaimIssue:
    def test_claim_success(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert claim_issue("test-1", cwd=tmp_path) is True
            mock_run.assert_called_once_with(
                ["bd", "update", "test-1", "--claim"],
                capture_output=True,
                text=True,
                cwd=tmp_path,
                check=False,
            )

    def test_claim_failure(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert claim_issue("test-1", cwd=tmp_path) is False

    def test_claim_oserror(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run", side_effect=OSError("no bd")):
            assert claim_issue("test-1", cwd=tmp_path) is False


class TestGetReadyIssues:
    """Tests for get_ready_issues returning QueueResult."""

    def test_success_with_issues(self, tmp_path) -> None:
        import json

        data = [
            {"id": "i1", "title": "First", "priority": 2, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "i2", "title": "Second", "priority": 3, "created_at": "2026-02-01T00:00:00Z"},
        ]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            result = get_ready_issues(tmp_path)

        assert isinstance(result, QueueResult)
        assert result.success is True
        assert result.error is None
        assert len(result.issues) == 2
        assert result.issues[0].id == "i1"
        assert result.issues[1].id == "i2"

    def test_success_empty_queue(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            result = get_ready_issues(tmp_path)

        assert result.success is True
        assert result.error is None
        assert result.issues == []

    def test_failure_nonzero_returncode(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "bd: not initialized"
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert result.error == "bd: not initialized"
        assert result.issues == []

    def test_failure_nonzero_returncode_empty_stderr(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = ""
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert result.error == "bd ready failed"
        assert result.issues == []

    def test_failure_oserror(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run", side_effect=OSError("no bd")):
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert "no bd" in result.error
        assert result.issues == []

    def test_failure_json_decode_error(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert result.error is not None
        assert result.issues == []

    def test_failure_non_list_json(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '{"key": "value"}'
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert "non-list" in result.error
        assert result.issues == []


class TestUnclaimIssue:
    def test_success(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert unclaim_issue("X-1") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["bd", "update", "X-1", "--status", "open", "--assignee", ""]

    def test_failure_returns_false(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert unclaim_issue("X-1") is False

    def test_os_error_returns_false(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run", side_effect=OSError("no bd")):
            assert unclaim_issue("X-1") is False

    def test_passes_cwd(self, tmp_path) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            unclaim_issue("X-1", cwd=tmp_path)
            assert mock_run.call_args[1]["cwd"] == tmp_path


class TestSelectNextIssuePriorityId:
    def test_priority_id_selected_over_higher_priority(self) -> None:
        urgent = _issue(id="urgent", priority=1)
        target = _issue(id="target", priority=4)
        assert select_next_issue([urgent, target], priority_id="target") is target

    def test_priority_id_not_in_list_falls_back(self) -> None:
        a = _issue(id="a", priority=2)
        b = _issue(id="b", priority=3)
        assert select_next_issue([a, b], priority_id="missing") is a

    def test_priority_id_in_skip_ids_not_selected(self) -> None:
        a = _issue(id="a", priority=2)
        b = _issue(id="b", priority=3)
        assert select_next_issue([a, b], skip_ids={"a"}, priority_id="a") is b

    def test_priority_id_none_has_no_effect(self) -> None:
        a = _issue(id="a", priority=1)
        b = _issue(id="b", priority=2)
        assert select_next_issue([a, b], priority_id=None) is a


class TestGetIssueParent:
    def test_returns_parent_id(self) -> None:
        import json
        data = [{"id": "child-1", "title": "Child", "parent": "parent-1"}]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("child-1") == "parent-1"

    def test_returns_none_when_no_parent(self) -> None:
        import json
        data = [{"id": "top-1", "title": "Top level"}]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("top-1") is None

    def test_returns_none_on_failure(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_issue_parent("x") is None

    def test_returns_none_on_oserror(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run", side_effect=OSError("no bd")):
            assert get_issue_parent("x") is None

    def test_passes_cwd(self, tmp_path) -> None:
        import json
        data = [{"id": "c", "parent": "p"}]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            get_issue_parent("c", cwd=tmp_path)
            assert mock_run.call_args[1]["cwd"] == tmp_path


class TestGetIssueStatus:
    def test_returns_status(self) -> None:
        import json

        data = [{"id": "issue-1", "status": "closed"}]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_status("issue-1") == "closed"

    def test_returns_none_on_failure(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_issue_status("issue-1") is None

    def test_passes_cwd(self, tmp_path) -> None:
        import json

        data = [{"id": "issue-1", "status": "open"}]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            get_issue_status("issue-1", cwd=tmp_path)
            assert mock_run.call_args[1]["cwd"] == tmp_path


class TestGetChildrenAllClosed:
    def test_all_closed_returns_true(self) -> None:
        import json
        data = [
            {"id": "c1", "status": "closed"},
            {"id": "c2", "status": "closed"},
        ]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_children_all_closed("parent-1") is True

    def test_some_open_returns_false(self) -> None:
        import json
        data = [
            {"id": "c1", "status": "closed"},
            {"id": "c2", "status": "open"},
        ]
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_children_all_closed("parent-1") is False

    def test_no_children_returns_none(self) -> None:
        import json
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert get_children_all_closed("parent-1") is None

    def test_failure_returns_none(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_children_all_closed("parent-1") is None

    def test_oserror_returns_none(self) -> None:
        with patch("amp_orchestrator.queue.subprocess.run", side_effect=OSError("no bd")):
            assert get_children_all_closed("parent-1") is None
