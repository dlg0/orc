"""Tests for the verification and merge manager module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import call, patch

from amp_orchestrator.merge import (
    MergeResult,
    _is_conflict,
    _build_conflict_prompt,
    verify_and_merge,
)
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
        assert r.conflict_resolved is False

    def test_conflict_resolved_result(self) -> None:
        r = MergeResult(success=True, stage="complete", conflict_resolved=True)
        assert r.success is True
        assert r.conflict_resolved is True

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
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
                result = subprocess.CompletedProcess(cmd, 0, stdout="3\n", stderr="")
                return result
            if isinstance(cmd, list) and cmd[:2] == ["git", "diff"] and "--quiet" in cmd:
                return subprocess.CompletedProcess(cmd, 1)  # has diff
            return subprocess.CompletedProcess(cmd if isinstance(cmd, list) else [cmd], 0, stdout="", stderr="")

        mock_run.side_effect = side_effect

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
        # rev-list preflight
        assert calls[1] == call(
            ["git", "rev-list", "--count", f"origin/{BASE_BRANCH}..{WORKTREE_INFO.branch_name}"],
            cwd=WORKTREE_INFO.worktree_path, capture_output=True, text=True, check=True,
        )
        # diff preflight
        assert calls[2] == call(
            ["git", "diff", "--quiet", f"origin/{BASE_BRANCH}..{WORKTREE_INFO.branch_name}"],
            cwd=WORKTREE_INFO.worktree_path, capture_output=True,
        )
        # rebase
        assert calls[3] == call(
            ["git", "rebase", "origin/main"],
            cwd=WORKTREE_INFO.worktree_path, check=True, capture_output=True,
        )
        # verify commands
        assert calls[4] == call(
            "make test",
            cwd=WORKTREE_INFO.worktree_path, check=True, capture_output=True, shell=True,
        )
        assert calls[5] == call(
            "make lint",
            cwd=WORKTREE_INFO.worktree_path, check=True, capture_output=True, shell=True,
        )
        # checkout
        assert calls[6] == call(
            ["git", "checkout", "main"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # pull
        assert calls[7] == call(
            ["git", "pull", "origin", "main"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # merge
        assert calls[8] == call(
            ["git", "merge", "--no-ff", "amp/ISSUE-1-fix-bug", "-m", "Merge amp/ISSUE-1-fix-bug"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # push
        assert calls[9] == call(
            ["git", "push", "origin", "main"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        # bd close
        assert calls[10] == call(
            ["bd", "close", "ISSUE-1"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )


def _preflight_side_effect(*args, **kwargs):
    """Default side_effect that passes preflight checks (commits ahead, has diff)."""
    cmd = args[0]
    if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="3\n", stderr="")
    if isinstance(cmd, list) and cmd[:2] == ["git", "diff"] and "--quiet" in cmd:
        return subprocess.CompletedProcess(cmd, 1)  # has diff
    return subprocess.CompletedProcess(cmd if isinstance(cmd, list) else [cmd], 0, stdout="", stderr="")


class TestRebaseFailure:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_rebase_failure_aborts_and_returns(self, mock_run: object) -> None:
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return _preflight_side_effect(*args, **kwargs)

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
        def side_effect(*args, **kwargs):
            if kwargs.get("shell"):
                raise subprocess.CalledProcessError(1, args[0])
            return _preflight_side_effect(*args, **kwargs)

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
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
                # Use exit code 128 (non-conflict git error)
                raise subprocess.CalledProcessError(128, cmd, stderr=b"fatal: some error")
            return _preflight_side_effect(*args, **kwargs)

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


class TestVerificationRunEvents:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_verification_run_events_emitted(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.side_effect = _preflight_side_effect
        state_dir = tmp_path / ".amp-orchestrator"
        state_dir.mkdir()

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=["make test", "make lint"],
            auto_push=False,
            issue_id=ISSUE_ID,
            state_dir=state_dir,
        )

        assert result.success is True

        from amp_orchestrator.events import EventLog
        events = EventLog(state_dir).all()
        vr_events = [e for e in events if e["event_type"] == "verification_run"]
        # 2 commands → 2 "before" events + 2 "pass" events = 4 total
        assert len(vr_events) == 4
        assert vr_events[0]["data"]["command"] == "make test"
        assert "result" not in vr_events[0]["data"]  # before-run event
        assert vr_events[1]["data"]["command"] == "make test"
        assert vr_events[1]["data"]["result"] == "pass"

    @patch("amp_orchestrator.merge.subprocess.run")
    def test_verification_run_fail_event(self, mock_run: object, tmp_path: Path) -> None:
        state_dir = tmp_path / ".amp-orchestrator"
        state_dir.mkdir()

        def side_effect(*args, **kwargs):
            if kwargs.get("shell"):
                raise subprocess.CalledProcessError(1, args[0])
            return _preflight_side_effect(*args, **kwargs)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=["make test"],
            auto_push=False,
            issue_id=ISSUE_ID,
            state_dir=state_dir,
        )

        assert result.success is False
        assert result.stage == "verify"

        from amp_orchestrator.events import EventLog
        events = EventLog(state_dir).all()
        vr_events = [e for e in events if e["event_type"] == "verification_run"]
        assert len(vr_events) == 2  # before + fail
        assert vr_events[1]["data"]["result"] == "fail"

    @patch("amp_orchestrator.merge.subprocess.run")
    def test_no_events_without_state_dir(self, mock_run: object) -> None:
        """When state_dir is None, no events are emitted and merge still works."""
        mock_run.side_effect = _preflight_side_effect
        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=["make test"],
            auto_push=False,
            issue_id=ISSUE_ID,
        )
        assert result.success is True


class TestPreflightChecks:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_no_commits_ahead_rejects(self, mock_run: object) -> None:
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
            return subprocess.CompletedProcess(cmd if isinstance(cmd, list) else [cmd], 0, stdout="", stderr="")

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
        assert result.stage == "preflight"
        assert "no commits" in result.error

    @patch("amp_orchestrator.merge.subprocess.run")
    def test_no_diff_rejects(self, mock_run: object) -> None:
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
            if isinstance(cmd, list) and cmd[:2] == ["git", "diff"] and "--quiet" in cmd:
                return subprocess.CompletedProcess(cmd, 0)  # no diff
            return subprocess.CompletedProcess(cmd if isinstance(cmd, list) else [cmd], 0, stdout="", stderr="")

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
        assert result.stage == "preflight"
        assert "no diff" in result.error


class TestAutoPushFalse:
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_push_and_close_skipped_when_auto_push_false(self, mock_run: object) -> None:
        mock_run.side_effect = _preflight_side_effect
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

        # bd close should NOT be called when auto_push=False
        bd_calls = [c for c in calls if c[0][0] == ["bd", "close", "ISSUE-1"]]
        assert len(bd_calls) == 0


class TestIsConflict:
    def test_conflict_in_stderr(self) -> None:
        e = subprocess.CalledProcessError(1, ["git", "rebase"], stderr=b"CONFLICT (content): Merge conflict in foo.py")
        assert _is_conflict(e) is True

    def test_could_not_apply_in_stderr(self) -> None:
        e = subprocess.CalledProcessError(1, ["git", "rebase"], stderr=b"error: could not apply abc123")
        assert _is_conflict(e) is True

    def test_exit_code_1_is_conflict(self) -> None:
        e = subprocess.CalledProcessError(1, ["git", "rebase"], stderr=b"")
        assert _is_conflict(e) is True

    def test_exit_code_2_is_conflict(self) -> None:
        e = subprocess.CalledProcessError(2, ["git", "merge"], stderr=b"")
        assert _is_conflict(e) is True

    def test_exit_code_128_not_conflict(self) -> None:
        e = subprocess.CalledProcessError(128, ["git", "rebase"], stderr=b"fatal: not a git repo")
        assert _is_conflict(e) is False

    def test_none_stderr(self) -> None:
        e = subprocess.CalledProcessError(1, ["git", "rebase"], stderr=None)
        assert _is_conflict(e) is True


class TestBuildConflictPrompt:
    def test_prompt_contains_files_and_stage(self) -> None:
        prompt = _build_conflict_prompt(["foo.py", "bar.py"], "rebase", "main", "ISSUE-1")
        assert "foo.py" in prompt
        assert "bar.py" in prompt
        assert "rebase" in prompt
        assert "ISSUE-1" in prompt
        assert "main" in prompt

    def test_prompt_instructs_not_to_continue(self) -> None:
        prompt = _build_conflict_prompt(["a.py"], "rebase", "main", "X")
        assert "git rebase --continue" in prompt


class TestRebaseConflictResolution:
    @patch("amp_orchestrator.merge.shutil.which", return_value="/usr/bin/amp")
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_rebase_conflict_resolved_by_amp(self, mock_run, mock_which) -> None:
        """When rebase conflicts occur and amp resolves them, merge succeeds."""
        conflict_attempt = [False]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd and "--continue" not in cmd:
                if not conflict_attempt[0]:
                    conflict_attempt[0] = True
                    raise subprocess.CalledProcessError(1, cmd, stderr=b"CONFLICT (content): Merge conflict in foo.py")
                return subprocess.CompletedProcess(cmd, 0)
            if isinstance(cmd, list) and cmd[:3] == ["git", "diff", "--name-only"]:
                # First call returns conflicts, second call (after resolution) returns empty
                if conflict_attempt[0] and kwargs.get("text"):
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="foo.py\n", stderr="")
            if isinstance(cmd, list) and cmd[0].endswith("amp"):
                # Simulate amp resolving conflicts
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--continue" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            return _preflight_side_effect(*args, **kwargs)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=False,
            issue_id=ISSUE_ID,
        )

        assert result.success is True
        assert result.conflict_resolved is True

    @patch("amp_orchestrator.merge.shutil.which", return_value="/usr/bin/amp")
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_rebase_conflict_unresolved_aborts(self, mock_run, mock_which) -> None:
        """When amp fails to resolve conflicts, rebase is aborted."""
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd and "--continue" not in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr=b"CONFLICT")
            if isinstance(cmd, list) and cmd[:3] == ["git", "diff", "--name-only"]:
                # Conflicts persist
                return subprocess.CompletedProcess(cmd, 0, stdout="foo.py\n", stderr="")
            if isinstance(cmd, list) and cmd[0].endswith("amp"):
                return subprocess.CompletedProcess(cmd, 0)
            return _preflight_side_effect(*args, **kwargs)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=False,
            issue_id=ISSUE_ID,
        )

        assert result.success is False
        assert result.stage == "rebase"
        assert "conflict resolution failed" in result.error

        # rebase --abort should have been called
        calls = mock_run.call_args_list
        abort_calls = [c for c in calls if isinstance(c[0][0], list) and c[0][0] == ["git", "rebase", "--abort"]]
        assert len(abort_calls) >= 1

    @patch("amp_orchestrator.merge.shutil.which", return_value=None)
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_no_amp_cli_skips_resolution(self, mock_run, mock_which) -> None:
        """When amp CLI is not found, conflict resolution is skipped."""
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr=b"CONFLICT")
            if isinstance(cmd, list) and cmd[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="foo.py\n", stderr="")
            return _preflight_side_effect(*args, **kwargs)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=False,
            issue_id=ISSUE_ID,
        )

        assert result.success is False
        assert result.stage == "rebase"


class TestConflictResolutionEvents:
    @patch("amp_orchestrator.merge.shutil.which", return_value="/usr/bin/amp")
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_conflict_events_emitted(self, mock_run, mock_which, tmp_path: Path) -> None:
        """Conflict detection and resolution events are recorded."""
        conflict_attempt = [False]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd and "--continue" not in cmd:
                if not conflict_attempt[0]:
                    conflict_attempt[0] = True
                    raise subprocess.CalledProcessError(1, cmd, stderr=b"CONFLICT")
                return subprocess.CompletedProcess(cmd, 0)
            if isinstance(cmd, list) and cmd[:3] == ["git", "diff", "--name-only"]:
                if conflict_attempt[0] and kwargs.get("text"):
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="foo.py\n", stderr="")
            if isinstance(cmd, list) and cmd[0].endswith("amp"):
                return subprocess.CompletedProcess(cmd, 0)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--continue" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            return _preflight_side_effect(*args, **kwargs)

        mock_run.side_effect = side_effect
        state_dir = tmp_path / ".amp-orchestrator"
        state_dir.mkdir()

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=False,
            issue_id=ISSUE_ID,
            state_dir=state_dir,
        )

        assert result.success is True

        from amp_orchestrator.events import EventLog
        all_events = EventLog(state_dir).all()
        event_types = [e["event_type"] for e in all_events]
        assert "conflict_detected" in event_types
        assert "conflict_resolution_started" in event_types
        assert "conflict_resolution_finished" in event_types

        # Check the finished event has success=True
        finished = [e for e in all_events if e["event_type"] == "conflict_resolution_finished"]
        assert len(finished) == 1
        assert finished[0]["data"]["success"] is True


class TestMergeConflictResolution:
    @patch("amp_orchestrator.merge.shutil.which", return_value="/usr/bin/amp")
    @patch("amp_orchestrator.merge.subprocess.run")
    def test_merge_conflict_resolved_by_amp(self, mock_run, mock_which) -> None:
        """When merge (not rebase) conflicts occur and amp resolves them, merge succeeds."""
        merge_attempt = [False]
        diff_call_count = [0]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            # merge --no-ff raises conflict on first attempt
            if isinstance(cmd, list) and cmd[:2] == ["git", "merge"] and "--no-ff" in cmd:
                if not merge_attempt[0]:
                    merge_attempt[0] = True
                    raise subprocess.CalledProcessError(1, cmd, stderr=b"CONFLICT (content)")
                return subprocess.CompletedProcess(cmd, 0)
            # git diff --name-only --diff-filter=U to list conflict files
            if isinstance(cmd, list) and cmd[:3] == ["git", "diff", "--name-only"]:
                diff_call_count[0] += 1
                if merge_attempt[0] and diff_call_count[0] > 1:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="bar.py\n", stderr="")
            if isinstance(cmd, list) and cmd[0].endswith("amp"):
                return subprocess.CompletedProcess(cmd, 0)
            if isinstance(cmd, list) and cmd[:2] == ["git", "commit"]:
                return subprocess.CompletedProcess(cmd, 0)
            return _preflight_side_effect(*args, **kwargs)

        mock_run.side_effect = side_effect

        result = verify_and_merge(
            worktree_info=WORKTREE_INFO,
            repo_root=REPO_ROOT,
            base_branch=BASE_BRANCH,
            verification_commands=[],
            auto_push=False,
            issue_id=ISSUE_ID,
        )

        assert result.success is True
        assert result.conflict_resolved is True
