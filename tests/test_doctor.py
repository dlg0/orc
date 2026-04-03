"""Tests for orc.doctor."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch


from orc.config import OrchestratorConfig
from orc.doctor import (
    DoctorContext,
    Finding,
    check_config_and_env,
    check_git_state,
    check_held_issues,
    check_state_consistency,
    check_worktrees,
    run_doctor,
)
from orc.state import (
    OrchestratorMode,
    OrchestratorState,
    RunCheckpoint,
    RunStage,
    StateStore,
)
from orc.worktree import WorktreeInfo


def _make_ctx(
    tmp_path: Path,
    state: OrchestratorState | None = None,
    lock_held: bool = False,
    config: OrchestratorConfig | None = None,
    config_error: str | None = None,
    ready_issue_ids: set[str] | None = None,
    worktrees: list[WorktreeInfo] | None = None,
    stale_days: int = 7,
) -> DoctorContext:
    state_dir = tmp_path / ".orc"
    state_dir.mkdir(exist_ok=True)
    store = StateStore(state_dir)
    st = state or OrchestratorState()
    store.save(st)
    return DoctorContext(
        repo_root=tmp_path,
        state_dir=state_dir,
        state=st,
        store=store,
        lock_held=lock_held,
        config=config or OrchestratorConfig(),
        config_error=config_error,
        ready_issue_ids=ready_issue_ids or set(),
        worktrees=worktrees or [],
        stale_days=stale_days,
    )


# -- check_state_consistency -------------------------------------------------

class TestStateConsistency:
    def test_clean_state_no_findings(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert check_state_consistency(ctx) == []

    def test_stale_running_no_lock(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.running)
        ctx = _make_ctx(tmp_path, state=state, lock_held=False)
        findings = check_state_consistency(ctx)
        assert len(findings) == 1
        assert findings[0].code == "state.stale_running_no_lock"
        assert findings[0].severity == "error"
        assert findings[0].auto_fixable

    def test_stale_running_fix_resets_idle(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.running)
        ctx = _make_ctx(tmp_path, state=state, lock_held=False)
        findings = check_state_consistency(ctx)
        result = findings[0].fix(ctx)
        assert ctx.state.mode == OrchestratorMode.idle
        assert "idle" in result.lower()

    def test_stale_stopping_no_lock(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.stopping)
        ctx = _make_ctx(tmp_path, state=state, lock_held=False)
        findings = check_state_consistency(ctx)
        assert len(findings) == 1
        assert findings[0].code == "state.stale_running_no_lock"
        assert findings[0].severity == "error"
        assert findings[0].auto_fixable

    def test_stale_stopping_fix_moves_resume_candidate(self, tmp_path: Path) -> None:
        checkpoint = RunCheckpoint(
            issue_id="ISSUE-STOP",
            issue_title="Test",
            branch="amp/test-stop",
            worktree_path="/tmp/wt-stop",
            stage=RunStage.amp_running,
        )
        state = OrchestratorState(
            mode=OrchestratorMode.stopping,
            active_run=checkpoint.to_dict(),
        )
        ctx = _make_ctx(tmp_path, state=state, lock_held=False)
        findings = check_state_consistency(ctx)
        result = findings[0].fix(ctx)
        assert ctx.state.mode == OrchestratorMode.idle
        assert ctx.state.active_run is None
        assert ctx.state.resume_candidate is not None
        assert ctx.state.resume_candidate["issue_id"] == "ISSUE-STOP"
        assert "resume_candidate" in result

    def test_stale_running_with_resumable_active_run(self, tmp_path: Path) -> None:
        checkpoint = RunCheckpoint(
            issue_id="ISSUE-1",
            issue_title="Test",
            branch="amp/test",
            worktree_path="/tmp/wt",
            stage=RunStage.amp_running,
        )
        state = OrchestratorState(
            mode=OrchestratorMode.running,
            active_run=checkpoint.to_dict(),
        )
        ctx = _make_ctx(tmp_path, state=state, lock_held=False)
        findings = check_state_consistency(ctx)
        assert findings[0].code == "state.stale_running_no_lock"
        result = findings[0].fix(ctx)
        assert ctx.state.active_run is None
        assert ctx.state.resume_candidate is not None
        assert ctx.state.resume_candidate["issue_id"] == "ISSUE-1"
        assert "resume_candidate" in result

    def test_running_with_lock_no_finding(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.running)
        ctx = _make_ctx(tmp_path, state=state, lock_held=True)
        findings = check_state_consistency(ctx)
        assert not any(f.code == "state.stale_running_no_lock" for f in findings)

    def test_stopping_with_lock_no_finding(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.stopping)
        ctx = _make_ctx(tmp_path, state=state, lock_held=True)
        findings = check_state_consistency(ctx)
        assert not any(f.code == "state.stale_running_no_lock" for f in findings)

    def test_active_run_in_idle(self, tmp_path: Path) -> None:
        checkpoint = RunCheckpoint(
            issue_id="ISSUE-2",
            issue_title="Test",
            branch="amp/test",
            worktree_path="/tmp/wt",
            stage=RunStage.amp_finished,
        )
        state = OrchestratorState(
            mode=OrchestratorMode.idle,
            active_run=checkpoint.to_dict(),
        )
        ctx = _make_ctx(tmp_path, state=state)
        findings = check_state_consistency(ctx)
        codes = [f.code for f in findings]
        assert "state.active_run_in_non_running_mode" in codes

    def test_active_run_fix_moves_to_resume(self, tmp_path: Path) -> None:
        checkpoint = RunCheckpoint(
            issue_id="ISSUE-2",
            issue_title="Test",
            branch="amp/test",
            worktree_path="/tmp/wt",
            stage=RunStage.amp_running,
        )
        state = OrchestratorState(
            mode=OrchestratorMode.idle,
            active_run=checkpoint.to_dict(),
        )
        ctx = _make_ctx(tmp_path, state=state)
        findings = [f for f in check_state_consistency(ctx) if f.code == "state.active_run_in_non_running_mode"]
        findings[0].fix(ctx)
        assert ctx.state.active_run is None
        assert ctx.state.resume_candidate is not None

    def test_invalid_resume_candidate(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            mode=OrchestratorMode.idle,
            resume_candidate={"issue_id": "ISSUE-3", "stage": "amp_running"},
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_state_consistency(ctx)
        codes = [f.code for f in findings]
        assert "state.resume_candidate_invalid" in codes

    def test_valid_resume_candidate_info(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            mode=OrchestratorMode.idle,
            resume_candidate={
                "issue_id": "ISSUE-4",
                "branch": "amp/test",
                "worktree_path": "/tmp/wt",
                "stage": "ready_to_merge",
                "resume_attempts": 0,
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_state_consistency(ctx)
        codes = [f.code for f in findings]
        assert "state.resume_candidate_ready" in codes
        assert "state.resume_candidate_invalid" not in codes


# -- check_held_issues -------------------------------------------------------

class TestHeldIssues:
    def test_no_held_issues(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert check_held_issues(ctx) == []

    def test_closed_held_issue_detected(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-10": {
                    "category": "issue_needs_rework",
                    "action": "hold_until_backlog_changes",
                    "stage": "amp",
                    "summary": "test",
                    "timestamp": "2026-03-01T00:00:00+00:00",
                    "attempts": 1,
                    "branch": None,
                    "worktree_path": None,
                    "preserve_worktree": False,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.closed):
            findings = check_held_issues(ctx)
        assert any(f.code == "held.bd_closed_or_missing" for f in findings)
        assert findings[0].auto_fixable

    def test_closed_held_issue_fix_prunes(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-10": {
                    "category": "issue_needs_rework",
                    "action": "hold_until_backlog_changes",
                    "stage": "amp",
                    "summary": "test",
                    "timestamp": "2026-03-01T00:00:00+00:00",
                    "attempts": 1,
                    "branch": None,
                    "worktree_path": None,
                    "preserve_worktree": False,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.closed):
            findings = check_held_issues(ctx)
        findings[0].fix(ctx)
        assert "ISSUE-10" not in ctx.state.issue_failures

    def test_ready_but_locally_held(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-11": {
                    "category": "blocked_by_dependency",
                    "action": "hold_until_backlog_changes",
                    "stage": "amp",
                    "summary": "blocked",
                    "timestamp": "2026-03-25T00:00:00+00:00",
                    "attempts": 1,
                    "branch": None,
                    "worktree_path": None,
                    "preserve_worktree": False,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state, ready_issue_ids={"ISSUE-11"})
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_held_issues(ctx)
        assert any(f.code == "held.ready_but_locally_held" for f in findings)

    def test_stale_held_issue(self, tmp_path: Path) -> None:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        state = OrchestratorState(
            issue_failures={
                "ISSUE-12": {
                    "category": "issue_needs_rework",
                    "action": "hold_until_backlog_changes",
                    "stage": "amp",
                    "summary": "old failure",
                    "timestamp": old_ts,
                    "attempts": 1,
                    "branch": None,
                    "worktree_path": None,
                    "preserve_worktree": False,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state, stale_days=7)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_held_issues(ctx)
        assert any(f.code == "held.stale" for f in findings)

    def test_high_attempt_count(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-13": {
                    "category": "issue_needs_rework",
                    "action": "hold_until_backlog_changes",
                    "stage": "amp",
                    "summary": "repeated failure",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "attempts": 5,
                    "branch": None,
                    "worktree_path": None,
                    "preserve_worktree": False,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_held_issues(ctx)
        assert any(f.code == "held.high_attempt_count" for f in findings)

    def test_merge_diagnostics_finding(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-15": {
                    "category": "stale_or_conflicted",
                    "action": "hold_for_retry",
                    "stage": "merge/rebase",
                    "summary": "rebase conflict",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "attempts": 1,
                    "branch": "amp/test",
                    "worktree_path": "/tmp/wt",
                    "preserve_worktree": True,
                    "extra": {
                        "merge_stage": "rebase",
                        "merge_error": "conflict",
                        "merge_diagnostics": {
                            "reason": "rebase_conflict",
                            "command": ["git", "rebase", "main"],
                            "returncode": 1,
                            "stdout": "",
                            "stderr": "CONFLICT (content): Merge conflict in foo.py",
                        },
                    },
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_held_issues(ctx)
        diag_findings = [f for f in findings if f.code == "held.merge_diagnostics_available"]
        assert len(diag_findings) == 1
        assert "rebase_conflict" in diag_findings[0].summary
        assert "orc inspect" in diag_findings[0].recommendation
        assert "orc unhold" in diag_findings[0].recommendation

    def test_merge_diagnostics_dirty_repo_root(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-16": {
                    "category": "stale_or_conflicted",
                    "action": "hold_for_retry",
                    "stage": "merge/pre-check",
                    "summary": "dirty repo root",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "attempts": 1,
                    "branch": "amp/test",
                    "worktree_path": "/tmp/wt",
                    "preserve_worktree": True,
                    "extra": {
                        "merge_stage": "pre-check",
                        "merge_error": "repo root dirty",
                        "merge_diagnostics": {
                            "reason": "repo_root_dirty_tracked",
                            "command": ["git", "status", "--porcelain"],
                            "returncode": 0,
                            "stdout": " M dirty_file.txt",
                            "stderr": "",
                            "git_state": {
                                "repo_root_dirty": ["dirty_file.txt"],
                            },
                        },
                    },
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("orc.doctor.get_issue_state", return_value=__import__("orc.queue", fromlist=["IssueState"]).IssueState.open):
            findings = check_held_issues(ctx)
        dirty_findings = [f for f in findings if f.code == "held.repo_root_dirty_at_merge"]
        assert len(dirty_findings) == 1
        assert "dirty_file.txt" in dirty_findings[0].summary
        assert dirty_findings[0].severity == "warn"


# -- check_worktrees ---------------------------------------------------------

class TestWorktrees:
    def test_no_worktrees_no_findings(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert check_worktrees(ctx) == []

    def test_orphaned_clean_worktree(self, tmp_path: Path) -> None:
        wt_path = tmp_path / ".worktrees" / "ISSUE-20"
        wt_path.mkdir(parents=True)
        (wt_path / ".git").touch()
        wt = WorktreeInfo(issue_id="ISSUE-20", worktree_path=wt_path, branch_name="amp/test")
        ctx = _make_ctx(tmp_path, worktrees=[wt])
        with patch("subprocess.run") as mock_run:
            # git status --porcelain returns empty (clean)
            mock_run.return_value.stdout = ""
            mock_run.return_value.returncode = 1  # REBASE_HEAD/MERGE_HEAD not found
            findings = check_worktrees(ctx)
        orphan_findings = [f for f in findings if f.code == "worktree.orphaned_clean"]
        assert len(orphan_findings) == 1
        assert orphan_findings[0].auto_fixable

    def test_referenced_worktree_not_orphaned(self, tmp_path: Path) -> None:
        wt_path = tmp_path / ".worktrees" / "ISSUE-21"
        wt_path.mkdir(parents=True)
        (wt_path / ".git").touch()
        wt = WorktreeInfo(issue_id="ISSUE-21", worktree_path=wt_path, branch_name="amp/test")
        state = OrchestratorState(
            issue_failures={
                "ISSUE-21": {
                    "category": "stale_or_conflicted",
                    "action": "hold_for_retry",
                    "stage": "merge",
                    "summary": "test",
                    "timestamp": "",
                    "attempts": 1,
                    "branch": "amp/test",
                    "worktree_path": str(wt_path),
                    "preserve_worktree": True,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state, worktrees=[wt])
        findings = check_worktrees(ctx)
        # Should not report orphaned worktree for a referenced issue
        assert not any(f.code.startswith("worktree.orphaned") for f in findings)

    def test_held_missing_worktree_with_branch(self, tmp_path: Path) -> None:
        state = OrchestratorState(
            issue_failures={
                "ISSUE-22": {
                    "category": "stale_or_conflicted",
                    "action": "hold_for_retry",
                    "stage": "merge",
                    "summary": "conflict",
                    "timestamp": "",
                    "attempts": 1,
                    "branch": "amp/test",
                    "worktree_path": str(tmp_path / "nonexistent"),
                    "preserve_worktree": True,
                    "extra": None,
                },
            },
        )
        ctx = _make_ctx(tmp_path, state=state)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0  # branch exists
            findings = check_worktrees(ctx)
        assert any(f.code == "worktree.held_missing_recoverable" for f in findings)


# -- check_git_state ----------------------------------------------------------

class TestGitState:
    def test_clean_repo_no_findings(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            findings = check_git_state(ctx)
        # No in-progress ops found
        assert not any(f.code.startswith("git.repo_rebase") or f.code.startswith("git.repo_merge") for f in findings)


# -- check_config_and_env ----------------------------------------------------

class TestConfigAndEnv:
    def test_config_error_reported(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=None, config_error="bad yaml")
        with patch("subprocess.run"), patch("shutil.which", return_value="/usr/bin/dummy"):
            findings = check_config_and_env(ctx)
        assert any(f.code == "config.load_failed" for f in findings)

    def test_missing_amp_detected(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with patch("shutil.which", side_effect=lambda t: None if t == "amp" else "/usr/bin/bd"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                findings = check_config_and_env(ctx)
        assert any(f.code == "env.amp_missing" for f in findings)

    def test_missing_bd_detected(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with patch("shutil.which", side_effect=lambda t: None if t == "bd" else "/usr/bin/amp"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                findings = check_config_and_env(ctx)
        assert any(f.code == "env.bd_missing" for f in findings)


# -- run_doctor ---------------------------------------------------------------

class TestRunDoctor:
    def test_run_doctor_collects_findings(self, tmp_path: Path) -> None:
        """run_doctor runs all checks and returns their findings."""
        ctx = _make_ctx(tmp_path)
        with patch("orc.doctor.get_issue_state"), \
             patch("subprocess.run") as mock_run, \
             patch("shutil.which", return_value="/usr/bin/dummy"):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            findings = run_doctor(ctx)
        # At minimum, should include the config.missing_file info
        assert any(f.code == "config.missing_file" for f in findings)

    def test_fix_refused_when_locked(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.running)
        ctx = _make_ctx(tmp_path, state=state, lock_held=True)
        findings = run_doctor(ctx, apply_fixes=True)
        assert any(f.code == "doctor.fix_refused" for f in findings)

    def test_fixes_applied(self, tmp_path: Path) -> None:
        state = OrchestratorState(mode=OrchestratorMode.running)
        ctx = _make_ctx(tmp_path, state=state, lock_held=False)
        with patch("orc.doctor.get_issue_state"):
            findings = run_doctor(ctx, apply_fixes=True)
        # Should have fixed stale_running_no_lock
        fixed = [f for f in findings if f.code == "state.stale_running_no_lock"]
        assert len(fixed) == 1
        assert "FIXED" in fixed[0].recommendation
        assert ctx.state.mode == OrchestratorMode.idle


class TestFindingToDict:
    def test_basic_serialization(self) -> None:
        f = Finding(
            code="test.code",
            severity="warn",
            summary="A test finding",
            recommendation="Do something",
            issue_id="ISSUE-1",
        )
        d = f.to_dict()
        assert d["code"] == "test.code"
        assert d["severity"] == "warn"
        assert d["issue_id"] == "ISSUE-1"
        assert d["auto_fixable"] is False
