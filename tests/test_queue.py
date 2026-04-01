"""Tests for the queue manager."""

from __future__ import annotations

from unittest.mock import patch

from orc.queue import (
    BdIssue,
    IssueState,
    QueueResult,
    claim_issue,
    compute_queue_breakdown,
    create_issue,
    get_children_all_closed,
    get_children_ids,
    get_issue_parent,
    get_issue_state,
    get_issue_status,
    get_ready_issues,
    reconcile_issue_failures,
    rewrite_parent_as_integration_issue,
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
        with patch("orc.queue.subprocess.run") as mock_run:
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
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert claim_issue("test-1", cwd=tmp_path) is False

    def test_claim_oserror(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError("no bd")):
            assert claim_issue("test-1", cwd=tmp_path) is False


class TestGetReadyIssues:
    """Tests for get_ready_issues returning QueueResult."""

    def test_success_with_issues(self, tmp_path) -> None:
        import json

        data = [
            {"id": "i1", "title": "First", "priority": 2, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "i2", "title": "Second", "priority": 3, "created_at": "2026-02-01T00:00:00Z"},
        ]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            result = get_ready_issues(tmp_path)

        assert isinstance(result, QueueResult)
        assert result.success is True
        assert result.error is None
        assert len(result.issues) == 2
        assert result.issues[0].id == "i1"
        assert result.issues[1].id == "i2"

    def test_passes_unlimited_limit(self, tmp_path) -> None:
        """Ensure get_ready_issues requests the full result set (--limit 0)."""
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            get_ready_issues(tmp_path)
            cmd = mock_run.call_args[0][0]
            assert cmd == ["bd", "ready", "--json", "--limit", "0"]

    def test_success_empty_queue(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            result = get_ready_issues(tmp_path)

        assert result.success is True
        assert result.error is None
        assert result.issues == []

    def test_failure_nonzero_returncode(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "bd: not initialized"
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert result.error == "bd: not initialized"
        assert result.issues == []

    def test_failure_nonzero_returncode_empty_stderr(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = ""
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert result.error == "bd ready failed"
        assert result.issues == []

    def test_failure_oserror(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError("no bd")):
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert "no bd" in result.error
        assert result.issues == []

    def test_failure_json_decode_error(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert result.error is not None
        assert result.issues == []

    def test_failure_non_list_json(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '{"key": "value"}'
            result = get_ready_issues(tmp_path)

        assert result.success is False
        assert "non-list" in result.error
        assert result.issues == []


class TestUnclaimIssue:
    def test_success(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert unclaim_issue("X-1") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["bd", "update", "X-1", "--status", "open", "--assignee", ""]

    def test_failure_returns_false(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert unclaim_issue("X-1") is False

    def test_os_error_returns_false(self) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError("no bd")):
            assert unclaim_issue("X-1") is False

    def test_passes_cwd(self, tmp_path) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
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
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("child-1") == "parent-1"

    def test_returns_none_when_no_parent(self) -> None:
        import json
        data = [{"id": "top-1", "title": "Top level"}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("top-1") is None

    def test_returns_none_on_failure(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_issue_parent("x") is None

    def test_returns_none_on_oserror(self) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError("no bd")):
            assert get_issue_parent("x") is None

    def test_passes_cwd(self, tmp_path) -> None:
        import json
        data = [{"id": "c", "parent": "p"}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            get_issue_parent("c", cwd=tmp_path)
            assert mock_run.call_args[1]["cwd"] == tmp_path


class TestGetIssueStatus:
    def test_returns_status(self) -> None:
        import json

        data = [{"id": "issue-1", "status": "closed"}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_status("issue-1") == "closed"

    def test_returns_none_on_failure(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_issue_status("issue-1") is None

    def test_passes_cwd(self, tmp_path) -> None:
        import json

        data = [{"id": "issue-1", "status": "open"}]
        with patch("orc.queue.subprocess.run") as mock_run:
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
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_children_all_closed("parent-1") is True

    def test_some_open_returns_false(self) -> None:
        import json
        data = [
            {"id": "c1", "status": "closed"},
            {"id": "c2", "status": "open"},
        ]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_children_all_closed("parent-1") is False

    def test_no_children_returns_none(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert get_children_all_closed("parent-1") is None

    def test_failure_returns_none(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_children_all_closed("parent-1") is None

    def test_oserror_returns_none(self) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError("no bd")):
            assert get_children_all_closed("parent-1") is None


class TestGetIssueState:
    def test_returns_open(self) -> None:
        import json

        data = [{"id": "i1", "status": "open"}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_state("i1") is IssueState.open

    def test_returns_closed(self) -> None:
        import json

        data = [{"id": "i1", "status": "closed"}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_state("i1") is IssueState.closed

    def test_returns_in_progress_as_open(self) -> None:
        import json

        data = [{"id": "i1", "status": "in_progress"}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_state("i1") is IssueState.open

    def test_returns_missing_when_not_found(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = 'Error: no issue found matching "xyz"'
            assert get_issue_state("xyz") is IssueState.missing

    def test_returns_missing_for_not_found_variant(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "not found"
            assert get_issue_state("xyz") is IssueState.missing

    def test_returns_unknown_on_generic_failure(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "database connection error"
            assert get_issue_state("i1") is IssueState.unknown

    def test_returns_unknown_on_oserror(self) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError("no bd")):
            assert get_issue_state("i1") is IssueState.unknown

    def test_returns_missing_for_empty_list(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert get_issue_state("i1") is IssueState.missing


class TestReconcileIssueFailures:
    def _failure_entry(self) -> dict:
        return {"category": "stale_or_conflicted", "action": "hold_for_retry", "summary": "merge failed"}

    def test_prunes_closed_issues(self) -> None:
        failures = {"i1": self._failure_entry(), "i2": self._failure_entry()}
        with patch("orc.queue.get_issue_state") as mock_state:
            mock_state.side_effect = lambda id, cwd=None: (
                IssueState.closed if id == "i1" else IssueState.open
            )
            pruned = reconcile_issue_failures(failures)
        assert "i1" not in failures
        assert "i2" in failures
        assert pruned == [("i1", "closed")]

    def test_prunes_missing_issues(self) -> None:
        failures = {"gone": self._failure_entry()}
        with patch("orc.queue.get_issue_state", return_value=IssueState.missing):
            pruned = reconcile_issue_failures(failures)
        assert failures == {}
        assert pruned == [("gone", "missing")]

    def test_keeps_open_issues(self) -> None:
        failures = {"i1": self._failure_entry()}
        with patch("orc.queue.get_issue_state", return_value=IssueState.open):
            pruned = reconcile_issue_failures(failures)
        assert "i1" in failures
        assert pruned == []

    def test_keeps_unknown_issues(self) -> None:
        failures = {"i1": self._failure_entry()}
        with patch("orc.queue.get_issue_state", return_value=IssueState.unknown):
            pruned = reconcile_issue_failures(failures)
        assert "i1" in failures
        assert pruned == []

    def test_empty_failures_is_noop(self) -> None:
        failures: dict = {}
        pruned = reconcile_issue_failures(failures)
        assert pruned == []

    def test_mixed_states(self) -> None:
        failures = {
            "closed1": self._failure_entry(),
            "open1": self._failure_entry(),
            "missing1": self._failure_entry(),
            "unknown1": self._failure_entry(),
        }
        state_map = {
            "closed1": IssueState.closed,
            "open1": IssueState.open,
            "missing1": IssueState.missing,
            "unknown1": IssueState.unknown,
        }
        with patch("orc.queue.get_issue_state") as mock_state:
            mock_state.side_effect = lambda id, cwd=None: state_map[id]
            pruned = reconcile_issue_failures(failures)
        assert set(failures.keys()) == {"open1", "unknown1"}
        assert len(pruned) == 2
        pruned_ids = {p[0] for p in pruned}
        assert pruned_ids == {"closed1", "missing1"}


class TestCreateIssueParentSemantics:
    """Regression: create_issue must use --parent, not --deps 'parent:<id>'.

    A real-world decomposition produced pseudo-parent dependencies instead of
    true parent-child relationships, silently breaking bd children and parent
    promotion.  These tests assert the canonical CLI invocation.
    """

    def test_uses_parent_flag(self) -> None:

        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "child-1\n"
            create_issue("Child task", "desc", parent="parent-1")
            cmd = mock_run.call_args[0][0]
            assert "--parent" in cmd
            parent_idx = cmd.index("--parent")
            assert cmd[parent_idx + 1] == "parent-1"

    def test_does_not_use_deps_flag(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "child-1\n"
            create_issue("Child task", "desc", parent="parent-1")
            cmd = mock_run.call_args[0][0]
            assert "--deps" not in cmd
            # Also check no 'parent:' substring in any argument
            assert not any("parent:" in arg for arg in cmd if isinstance(arg, str) and arg != "parent-1")

    def test_no_parent_flag_when_parent_is_none(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "top-1\n"
            create_issue("Top-level task", "desc")
            cmd = mock_run.call_args[0][0]
            assert "--parent" not in cmd


class TestParentFieldNotDeps:
    """Regression: get_issue_parent reads 'parent' field, not 'deps'.

    If bd show returns a dependency-only relationship (no 'parent' field),
    get_issue_parent must return None — not infer a parent from deps.
    """

    def test_ignores_deps_with_parent_prefix(self) -> None:
        """Issue has deps containing 'parent:X' but no actual parent field."""
        import json

        data = [{"id": "child-1", "title": "Child", "deps": ["parent:pseudo-parent"]}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("child-1") is None

    def test_returns_parent_only_from_parent_field(self) -> None:
        """Issue has both parent field and deps — parent field wins."""
        import json

        data = [{"id": "child-1", "parent": "real-parent", "deps": ["parent:fake-parent"]}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("child-1") == "real-parent"

    def test_empty_parent_field_returns_none(self) -> None:
        """An empty-string parent field should be treated as no parent."""
        import json

        data = [{"id": "child-1", "parent": ""}]
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            assert get_issue_parent("child-1") is None


class TestGetChildrenPseudoParentRegression:
    """Regression: get_children_all_closed returns None for pseudo-parents.

    When a pseudo-parent (dependency-only, not true --parent) has no real
    children, bd children returns an empty list.  The function must return
    None (no children) rather than True (vacuously all closed).
    """

    def test_empty_children_returns_none_not_true(self) -> None:
        """A pseudo-parent with no bd children must not report all_closed=True."""
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            result = get_children_all_closed("pseudo-parent-1")
            assert result is None, (
                "Empty children list should return None, not True. "
                "A pseudo-parent dependency must not trigger parent promotion."
            )


class TestPaginatedHeldOverlap:
    """Regression test: first page entirely held but later issues are runnable.

    Before the fix, get_ready_issues used ``bd ready --json`` without
    ``--limit 0``, so only the first 10 issues (the default page) were
    returned.  If all 10 were in the local held set, the scheduler
    incorrectly concluded the queue was exhausted even though runnable
    issues existed beyond page 1.
    """

    def test_scheduler_finds_runnable_beyond_held_page(self) -> None:
        """Simulates a 15-issue ready queue where the first 10 are held."""
        held_ids = {f"held-{i}" for i in range(10)}
        all_issues = [
            _issue(id=f"held-{i}", priority=3) for i in range(10)
        ] + [
            _issue(id=f"runnable-{i}", priority=2) for i in range(5)
        ]

        issue_failures = {iid: {"category": "stale", "action": "hold_for_retry"} for iid in held_ids}

        breakdown = compute_queue_breakdown(all_issues, issue_failures)
        assert breakdown.beads_ready == 15
        assert breakdown.held_and_ready == 10
        assert breakdown.runnable == 5
        assert not breakdown.has_held_blocking

        selected = select_next_issue(all_issues, skip_ids=held_ids)
        assert selected is not None
        assert selected.id.startswith("runnable-")

    def test_all_held_reports_blocking(self) -> None:
        """When every ready issue is held, has_held_blocking should be True."""
        all_issues = [_issue(id=f"held-{i}", priority=3) for i in range(10)]
        issue_failures = {f"held-{i}": {"category": "stale"} for i in range(10)}

        breakdown = compute_queue_breakdown(all_issues, issue_failures)
        assert breakdown.beads_ready == 10
        assert breakdown.runnable == 0
        assert breakdown.has_held_blocking


# --- rewrite_parent_as_integration_issue ---


class TestRewriteParentAsIntegrationIssue:
    def test_success(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert rewrite_parent_as_integration_issue("parent-1", ["child-a", "child-b"]) is True
            args = mock_run.call_args[0][0]
            assert args[:3] == ["bd", "update", "parent-1"]
            assert "--title" in args
            assert "--description" in args
            assert "--acceptance" in args
            # Verify child IDs appear in the description
            desc_idx = args.index("--description") + 1
            assert "child-a" in args[desc_idx]
            assert "child-b" in args[desc_idx]

    def test_failure(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert rewrite_parent_as_integration_issue("parent-1", []) is False

    def test_os_error(self) -> None:
        with patch("orc.queue.subprocess.run", side_effect=OSError):
            assert rewrite_parent_as_integration_issue("parent-1", []) is False


# --- get_children_ids ---


class TestGetChildrenIds:
    def test_returns_ids(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '[{"id": "c1"}, {"id": "c2"}]'
            assert get_children_ids("parent-1") == ["c1", "c2"]

    def test_no_children(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert get_children_ids("parent-1") == []

    def test_failure(self) -> None:
        with patch("orc.queue.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert get_children_ids("parent-1") == []
