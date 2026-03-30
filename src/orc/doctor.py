"""Diagnostic checks for orc orchestrator health."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orc.config import OrchestratorConfig, load_config
from orc.lock import OrchestratorLock
from orc.queue import get_issue_state, get_ready_issues, IssueState, reconcile_issue_failures
from orc.state import (
    OrchestratorMode,
    OrchestratorState,
    StateStore,
    _MAX_RESUME_ATTEMPTS,
    _RESUMABLE_STAGES,
    can_retry_merge,
)
from orc.worktree import WorktreeInfo, WorktreeManager


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """A single diagnostic finding."""

    code: str
    severity: str  # "error", "warn", "info"
    summary: str
    recommendation: str
    issue_id: str | None = None
    path: str | None = None
    auto_fixable: bool = False
    fix: Callable[[DoctorContext], str] | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "recommendation": self.recommendation,
        }
        if self.issue_id:
            d["issue_id"] = self.issue_id
        if self.path:
            d["path"] = self.path
        d["auto_fixable"] = self.auto_fixable
        return d


@dataclass
class DoctorContext:
    """Snapshot of orchestrator state for diagnostic checks."""

    repo_root: Path
    state_dir: Path
    state: OrchestratorState
    store: StateStore
    lock_held: bool
    config: OrchestratorConfig | None
    config_error: str | None
    ready_issue_ids: set[str]
    worktrees: list[WorktreeInfo]
    stale_days: int = 7


DoctorCheck = Callable[[DoctorContext], list[Finding]]


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_context(
    repo_root: Path,
    state_dir: Path,
    stale_days: int = 7,
) -> DoctorContext:
    """Build a DoctorContext by probing the current environment."""
    store = StateStore(state_dir)
    state = store.load()
    lock = OrchestratorLock(state_dir)
    lock_held = lock.is_locked()

    config: OrchestratorConfig | None = None
    config_error: str | None = None
    try:
        config = load_config(repo_root)
    except Exception as exc:
        config_error = str(exc)

    ready_ids: set[str] = set()
    queue_result = get_ready_issues(repo_root)
    if queue_result.success:
        ready_ids = {i.id for i in queue_result.issues}

    worktrees: list[WorktreeInfo] = []
    try:
        base = config.base_branch if config else "main"
        mgr = WorktreeManager(repo_root, base)
        worktrees = mgr.list_worktrees()
    except Exception:
        pass

    return DoctorContext(
        repo_root=repo_root,
        state_dir=state_dir,
        state=state,
        store=store,
        lock_held=lock_held,
        config=config,
        config_error=config_error,
        ready_issue_ids=ready_ids,
        worktrees=worktrees,
        stale_days=stale_days,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_state_consistency(ctx: DoctorContext) -> list[Finding]:
    """Check for state/lifecycle inconsistencies."""
    findings: list[Finding] = []
    state = ctx.state

    # 1. Mode is running/pause_requested but no lock held
    if state.mode in (OrchestratorMode.running, OrchestratorMode.pause_requested) and not ctx.lock_held:
        def fix_stale_running(c: DoctorContext) -> str:
            c.state.mode = OrchestratorMode.idle
            if c.state.active_run:
                stage = c.state.active_run.get("stage", "")
                branch = c.state.active_run.get("branch")
                wt_path = c.state.active_run.get("worktree_path")
                attempts = c.state.active_run.get("resume_attempts", 0)
                is_resumable = (
                    stage in _RESUMABLE_STAGES
                    and branch and wt_path
                    and attempts < _MAX_RESUME_ATTEMPTS
                )
                if is_resumable:
                    c.state.active_run["resume_attempts"] = attempts + 1
                    c.state.resume_candidate = c.state.active_run
                    c.state.active_run = None
                    c.store.save(c.state)
                    return "Reset to idle; active run moved to resume_candidate"
                else:
                    c.state.active_run = None
                    c.store.save(c.state)
                    return "Reset to idle; non-resumable active run cleared"
            c.store.save(c.state)
            return "Reset to idle"

        findings.append(Finding(
            code="state.stale_running_no_lock",
            severity="error",
            summary=f"Mode is '{state.mode.value}' but no live lock is held — previous process likely crashed.",
            recommendation="Run 'orc doctor --fix' to recover state, or 'orc start' which handles crash recovery.",
            auto_fixable=True,
            fix=fix_stale_running,
        ))

    # 2. active_run set in non-running mode
    if state.active_run and state.mode in (OrchestratorMode.idle, OrchestratorMode.paused, OrchestratorMode.error):
        issue_id = state.active_run.get("issue_id", "unknown")

        def fix_orphan_active(c: DoctorContext) -> str:
            stage = c.state.active_run.get("stage", "") if c.state.active_run else ""
            branch = c.state.active_run.get("branch") if c.state.active_run else None
            wt_path = c.state.active_run.get("worktree_path") if c.state.active_run else None
            attempts = (c.state.active_run.get("resume_attempts", 0) if c.state.active_run else 0)
            is_resumable = (
                stage in _RESUMABLE_STAGES
                and branch and wt_path
                and attempts < _MAX_RESUME_ATTEMPTS
            )
            if is_resumable and not c.state.resume_candidate:
                c.state.resume_candidate = c.state.active_run
                c.state.active_run = None
                c.store.save(c.state)
                return "Moved active_run to resume_candidate"
            c.state.active_run = None
            c.store.save(c.state)
            return "Cleared orphaned active_run"

        findings.append(Finding(
            code="state.active_run_in_non_running_mode",
            severity="error",
            summary=f"active_run is set (issue {issue_id}) but mode is '{state.mode.value}'.",
            recommendation="Run 'orc doctor --fix' to clear or convert to resume_candidate.",
            issue_id=issue_id,
            auto_fixable=True,
            fix=fix_orphan_active,
        ))

    # 3. Invalid resume_candidate
    if state.resume_candidate:
        rc = state.resume_candidate
        issue_id = rc.get("issue_id", "unknown")
        problems: list[str] = []

        if not rc.get("branch") or not rc.get("worktree_path"):
            problems.append("missing branch or worktree_path")
        if rc.get("resume_attempts", 0) >= _MAX_RESUME_ATTEMPTS:
            problems.append(f"resume_attempts ({rc.get('resume_attempts')}) >= max ({_MAX_RESUME_ATTEMPTS})")

        bd_state = get_issue_state(issue_id, cwd=ctx.repo_root)
        if bd_state in (IssueState.closed, IssueState.missing):
            problems.append(f"issue is {bd_state.value} in beads")

        if problems:
            def fix_invalid_resume(c: DoctorContext) -> str:
                c.state.resume_candidate = None
                c.store.save(c.state)
                return "Cleared invalid resume_candidate"

            findings.append(Finding(
                code="state.resume_candidate_invalid",
                severity="error",
                summary=f"resume_candidate for {issue_id} is invalid: {'; '.join(problems)}.",
                recommendation="Run 'orc doctor --fix' to clear it.",
                issue_id=issue_id,
                auto_fixable=True,
                fix=fix_invalid_resume,
            ))
        elif state.mode == OrchestratorMode.idle:
            findings.append(Finding(
                code="state.resume_candidate_ready",
                severity="info",
                summary=f"Resume candidate {issue_id} is queued and valid.",
                recommendation="Run 'orc start' to attempt recovery.",
                issue_id=issue_id,
            ))

    return findings


def check_held_issues(ctx: DoctorContext) -> list[Finding]:
    """Check held issues for stale/pruneable/retryable states."""
    findings: list[Finding] = []
    now = datetime.now(timezone.utc)

    for issue_id, info in list(ctx.state.issue_failures.items()):
        category = info.get("category", "unknown")
        action = info.get("action", "unknown")
        summary = info.get("summary", "(no summary)")
        timestamp = info.get("timestamp", "")
        attempts = info.get("attempts", 1)

        # Check if issue is closed/missing in beads
        bd_state = get_issue_state(issue_id, cwd=ctx.repo_root)
        if bd_state in (IssueState.closed, IssueState.missing):
            def fix_prune_closed(c: DoctorContext, _iid: str = issue_id) -> str:
                c.state.issue_failures.pop(_iid, None)
                c.store.save(c.state)
                return f"Pruned {_iid} ({bd_state.value})"

            findings.append(Finding(
                code="held.bd_closed_or_missing",
                severity="warn",
                summary=f"Held issue {issue_id} is {bd_state.value} in beads.",
                recommendation=f"Safe to remove from held list.",
                issue_id=issue_id,
                auto_fixable=True,
                fix=fix_prune_closed,
            ))
            continue

        # Check if issue is now in the ready queue (backlog changed)
        if issue_id in ctx.ready_issue_ids:
            findings.append(Finding(
                code="held.ready_but_locally_held",
                severity="warn",
                summary=f"Held issue {issue_id} [{category}] is now in the bd ready queue.",
                recommendation=f"Run 'orc retry {issue_id}' to re-queue it.",
                issue_id=issue_id,
            ))

        # Check if merge-retryable
        if can_retry_merge(info):
            branch = info.get("branch", "")
            wt_path = info.get("worktree_path", "")
            findings.append(Finding(
                code="held.retry_merge_ready",
                severity="info",
                summary=f"Held issue {issue_id} has a preserved worktree and is eligible for merge retry.",
                recommendation=f"Run 'orc retry-merge {issue_id}' to retry the merge step only.",
                issue_id=issue_id,
                path=wt_path,
            ))

        # Check for stale holds
        if timestamp:
            try:
                held_at = datetime.fromisoformat(timestamp)
                age_days = (now - held_at).days
                if age_days >= ctx.stale_days:
                    findings.append(Finding(
                        code="held.stale",
                        severity="warn",
                        summary=f"Issue {issue_id} has been held for {age_days} days [{category}]: {summary}",
                        recommendation=f"Run 'orc inspect {issue_id}' to review, then 'orc retry {issue_id}' or close it.",
                        issue_id=issue_id,
                    ))
            except (ValueError, TypeError):
                pass

        # High attempt count
        if attempts >= 3:
            findings.append(Finding(
                code="held.high_attempt_count",
                severity="warn",
                summary=f"Issue {issue_id} has failed {attempts} times [{category}].",
                recommendation=f"Inspect manually with 'orc inspect {issue_id}' — repeated failures suggest a deeper problem.",
                issue_id=issue_id,
            ))

    return findings


def check_worktrees(ctx: DoctorContext) -> list[Finding]:
    """Check for orphaned or problematic worktrees."""
    findings: list[Finding] = []

    # Build set of issue IDs referenced by state
    referenced_ids: set[str] = set(ctx.state.issue_failures.keys())
    if ctx.state.active_run:
        referenced_ids.add(ctx.state.active_run.get("issue_id", ""))
    if ctx.state.resume_candidate:
        referenced_ids.add(ctx.state.resume_candidate.get("issue_id", ""))

    for wt in ctx.worktrees:
        if wt.issue_id not in referenced_ids:
            # Orphaned worktree — check if clean
            is_dirty = False
            has_git_op = False
            try:
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=wt.worktree_path,
                    capture_output=True, text=True, timeout=10,
                )
                is_dirty = bool(status.stdout.strip())

                for ref in ("REBASE_HEAD", "MERGE_HEAD"):
                    check = subprocess.run(
                        ["git", "rev-parse", "-q", "--verify", ref],
                        cwd=wt.worktree_path,
                        capture_output=True, timeout=5,
                    )
                    if check.returncode == 0:
                        has_git_op = True
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

            if has_git_op or is_dirty:
                findings.append(Finding(
                    code="worktree.orphaned_dirty",
                    severity="warn",
                    summary=f"Orphaned worktree at {wt.worktree_path} (branch {wt.branch_name}) has uncommitted changes or in-progress git operation.",
                    recommendation="Inspect manually — may contain useful work that wasn't merged.",
                    issue_id=wt.issue_id,
                    path=str(wt.worktree_path),
                ))
            else:
                def fix_orphan_clean(c: DoctorContext, _wt: WorktreeInfo = wt) -> str:
                    base = c.config.base_branch if c.config else "main"
                    mgr = WorktreeManager(c.repo_root, base)
                    mgr.cleanup_worktree(_wt)
                    return f"Removed orphaned worktree {_wt.worktree_path}"

                findings.append(Finding(
                    code="worktree.orphaned_clean",
                    severity="info",
                    summary=f"Orphaned worktree at {wt.worktree_path} (branch {wt.branch_name}) is clean and not referenced by state.",
                    recommendation="Run 'orc doctor --fix' to clean up.",
                    issue_id=wt.issue_id,
                    path=str(wt.worktree_path),
                    auto_fixable=True,
                    fix=fix_orphan_clean,
                ))

    # Check held issues that reference missing worktrees
    for issue_id, info in ctx.state.issue_failures.items():
        wt_path = info.get("worktree_path")
        branch = info.get("branch")
        if wt_path and not Path(wt_path).exists():
            if branch:
                # Check if branch still exists
                try:
                    result = subprocess.run(
                        ["git", "rev-parse", "--verify", branch],
                        cwd=ctx.repo_root,
                        capture_output=True, timeout=5,
                    )
                    branch_exists = result.returncode == 0
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    branch_exists = False

                if branch_exists:
                    findings.append(Finding(
                        code="worktree.held_missing_recoverable",
                        severity="warn",
                        summary=f"Held issue {issue_id} references missing worktree {wt_path}, but branch {branch} still exists.",
                        recommendation=f"Worktree can be recreated. Run 'orc retry {issue_id}' or 'orc retry-merge {issue_id}'.",
                        issue_id=issue_id,
                        path=wt_path,
                    ))
                else:
                    findings.append(Finding(
                        code="worktree.held_missing_unrecoverable",
                        severity="warn",
                        summary=f"Held issue {issue_id} references missing worktree {wt_path} and branch {branch} is also gone.",
                        recommendation=f"Previous work is lost. Run 'orc retry {issue_id}' to start fresh.",
                        issue_id=issue_id,
                        path=wt_path,
                    ))

    return findings


def check_git_state(ctx: DoctorContext) -> list[Finding]:
    """Check for leftover git operations in repo root."""
    findings: list[Finding] = []

    for ref, label in [("REBASE_HEAD", "rebase"), ("MERGE_HEAD", "merge")]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "-q", "--verify", ref],
                cwd=ctx.repo_root,
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                findings.append(Finding(
                    code=f"git.repo_{label}_in_progress",
                    severity="error",
                    summary=f"Repo root has {ref} — a {label} operation is in progress.",
                    recommendation=f"Run 'git {label} --abort' in {ctx.repo_root} or resolve and continue.",
                    path=str(ctx.repo_root),
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Check for unmerged files in repo root
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=ctx.repo_root,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            files = result.stdout.strip().splitlines()
            findings.append(Finding(
                code="git.repo_unmerged_files",
                severity="error",
                summary=f"Repo root has {len(files)} unmerged file(s): {', '.join(files[:5])}",
                recommendation="Resolve merge conflicts in the repo root before running orc.",
                path=str(ctx.repo_root),
            ))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return findings


def check_config_and_env(ctx: DoctorContext) -> list[Finding]:
    """Check configuration and environment prerequisites."""
    findings: list[Finding] = []

    # Config load error
    if ctx.config_error:
        findings.append(Finding(
            code="config.load_failed",
            severity="error",
            summary=f"Failed to load config: {ctx.config_error}",
            recommendation=f"Fix {ctx.state_dir / 'config.yaml'} or run 'orc init-config' to create defaults.",
        ))

    # Missing config file (informational)
    config_path = ctx.state_dir / "config.yaml"
    if not config_path.exists() and ctx.config_error is None:
        findings.append(Finding(
            code="config.missing_file",
            severity="info",
            summary="No config file found — using defaults.",
            recommendation="Run 'orc init-config' to create an explicit configuration.",
        ))

    # Check base_branch exists
    if ctx.config:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{ctx.config.base_branch}"],
                cwd=ctx.repo_root,
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                findings.append(Finding(
                    code="config.base_branch_missing",
                    severity="error",
                    summary=f"Configured base_branch 'origin/{ctx.config.base_branch}' does not exist.",
                    recommendation="Update base_branch in .orc/config.yaml or push the branch to origin.",
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Check amp and bd on PATH
    for tool in ("amp", "bd"):
        if shutil.which(tool) is None:
            findings.append(Finding(
                code=f"env.{tool}_missing",
                severity="error",
                summary=f"'{tool}' is not on PATH.",
                recommendation=f"Install {tool} or add it to your PATH.",
            ))

    return findings


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

CHECKS: list[DoctorCheck] = [
    check_state_consistency,
    check_held_issues,
    check_worktrees,
    check_git_state,
    check_config_and_env,
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_doctor(
    ctx: DoctorContext,
    *,
    apply_fixes: bool = False,
) -> list[Finding]:
    """Run all checks and optionally apply safe fixes.

    Returns the list of findings. When *apply_fixes* is True, findings with
    ``auto_fixable=True`` have their ``fix`` callback invoked and a result
    message is appended to ``finding.recommendation``.
    """
    all_findings: list[Finding] = []
    for check in CHECKS:
        all_findings.extend(check(ctx))

    if apply_fixes and ctx.lock_held:
        # Refuse to fix while orchestrator is running
        all_findings.insert(0, Finding(
            code="doctor.fix_refused",
            severity="error",
            summary="Cannot apply fixes while orchestrator lock is held.",
            recommendation="Stop the orchestrator first, then re-run 'orc doctor --fix'.",
        ))
        return all_findings

    if apply_fixes:
        for f in all_findings:
            if f.auto_fixable and f.fix is not None:
                try:
                    result = f.fix(ctx)
                    f.recommendation = f"FIXED: {result}"
                except Exception as exc:
                    f.recommendation = f"FIX FAILED: {exc}"

    return all_findings
