"""Tests for the verification and merge manager module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import call, patch

from amp_orchestrator.merge import MergeResult, verify_and_merge
from amp_orchestrator.worktree import WorktreeInfo


WORKTREE_INFO = WorktreeInfo(
    issue_id="ISSUE-1",
    worktree_path=Path("/repo/.worktrees/ISSUE-1"),
    branch_name="amp/ISSUE-1-fix-bug",
)
REPO_ROOT = Path("/repo")
BASE_BRANCH = "main"
ISSUE_ID = "ISSUE-1"


class TestMergeResult:
    def test_success_result(self) -> None:
        r = MergeResult(success=True, stage="complete")
        assert r.success is True
        assert r.stage == "complete"
        assert r.error is None

    def test_failure_result(self) -> None:
        r = MergeResult(success=False, stage="rebase", error="conflict")
        assert r.success is False
        assert r.stage == "rebase"
        assert r.error == "conflict"

    def test_error_defaults_to_none(self) -> None:
        r = MergeResult(success=True, stage="complete")
        assert r.error is None


class TestVerifyAndMergeSuccess:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_full_success_path(self, mock_run: object) -> None:
        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=["make test", "make lint"],
            auto_push=True,
            issue_id=ISSUE_ID,
        )

        assert result.success is True
        assert result.stage == "complete"
        assert result.error is None

        calls = mock_run.call_args_list
        # fetch
        assert calls[0] == call(
            ["git", "fetch", "origin"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # rebase
        assert calls[1] == call(
            ["git", "rebase", "origin/main"],
            cwd=WORKTREE_INFO.worktree_path, check=True, capture_output=True,
        )
        # verify commands
        assert calls[2] == call(
            "make test",
            cwd=WORKTREE_INFO.worktree_path, check=True, capture_output=True, shell=True,
        )
        assert calls[3] == call(
            "make lint",
            cwd=WORKTREE_INFO.worktree_path, check=True, capture_output=True, shell=True,
        )
        # checkout
        assert calls[4] == call(
            ["git", "checkout", "main"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # pull
        assert calls[5] == call(
            ["git", "pull", "origin", "main"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # merge
        assert calls[6] == call(
            ["git", "merge", "--no-ff", "amp/ISSUE-1-fix-bug", "-m", "Merge amp/ISSUE-1-fix-bug"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # push
        assert calls[7] == call(
            ["git", "push", "origin", "main"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # bd close
        assert calls[8] == call(
            ["bd", "close", "ISSUE-1"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )


class TestRebaseFailure:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_rebase_failure_aborts_and_returns(self, mock_run: object) -> None:
        def side_effect(*args: object, **kwargs: object) -> None:
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
                raise subprocess.CalledProcessError(1, cmd)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=["make test"],
            auto_push=True,
            issue_id=ISSUE_ID,
        )

        assert result.success is False
        assert result.stage == "rebase"
        assert result.error is not None

        # rebase --abort should have been called
        calls = mock_run.call_args_list
        abort_calls = [c for c in calls if c[0][0] == ["git", "rebase", "--abort"]]
        assert len(abort_calls) == 1


class TestVerifyFailure:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_verify_failure_returns_early(self, mock_run: object) -> None:
        def side_effect(*args: object, **kwargs: object) -> None:
            if kwargs.get("shell"):
                raise subprocess.CalledProcessError(1, args[0])

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=["make test"],
            auto_push=True,
            issue_id=ISSUE_ID,
        )

        assert result.success is False
        assert result.stage == "verify"
        assert result.error is not None


class TestBdCloseNotCalledOnMergeFailure:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_bd_close_not_called_when_merge_fails(self, mock_run: object) -> None:
        def side_effect(*args: object, **kwargs: object) -> None:
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "merge"]:
                raise subprocess.CalledProcessError(1, cmd)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=True,
            issue_id=ISSUE_ID,
        )

        assert result.success is False
        assert result.stage == "merge"

        # bd close should NOT have been called
        calls = mock_run.call_args_list
        bd_calls = [c for c in calls if c[0][0] == ["bd", "close", "ISSUE-1"]]
        assert len(bd_calls) == 0


class TestAutoPushFalse:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_push_skipped_when_auto_push_false(self, mock_run: object) -> None:
        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=False,
            issue_id=ISSUE_ID,
        )

        assert result.success is True
        assert result.stage == "complete"

        # No push call should exist
        calls = mock_run.call_args_list
        push_calls = [
            c for c in calls
            if isinstance(c[0][0], list) and c[0][0][:2] == ["git", "push"]
        ]
        assert len(push_calls) == 0

        # bd close should still be called
        bd_calls = [c for c in calls if c[0][0] == ["bd", "close", "ISSUE-1"]]
        assert len(bd_calls) == 1
