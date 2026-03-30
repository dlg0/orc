"""Verification and merge management for amp-orchestrator."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from amp_orchestrator.events import EventLog, EventType
from amp_orchestrator.worktree import WorktreeInfo, build_worktree_env

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    success: bool
    stage: str
    error: str | None = None
    conflict_resolved: bool = False


def _is_conflict(error: subprocess.CalledProcessError) -> bool:
    """Detect whether a git rebase/merge failure is due to conflicts."""
    stderr = (error.stderr or b"") if isinstance(error.stderr, bytes) else (error.stderr or "").encode()
    stderr_text = stderr.decode(errors="replace").lower()
    conflict_signals = ["conflict", "merge conflict", "could not apply", "fix conflicts"]
    if any(s in stderr_text for s in conflict_signals):
        return True
    # Git uses exit code 1 for conflicts in rebase
    if error.returncode in (1, 2):
        return True
    return False


def _get_conflict_files(cwd: Path) -> list[str]:
    """List files with merge conflicts in the working tree."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def _spawn_amp_for_conflicts(
    worktree_path: Path,
    conflict_files: list[str],
    stage: str,
    base_branch: str,
    issue_id: str,
    events: EventLog | None,
    conflict_resolution_timeout: int = 600,
) -> bool:
    """Spawn amp to resolve conflict markers and stage files. Returns True if all conflicts resolved."""
    amp_path = shutil.which("amp")
    if amp_path is None:
        logger.warning("amp CLI not found — cannot attempt conflict resolution")
        return False

    if events:
        events.record(EventType.conflict_resolution_started, {
            "issue_id": issue_id,
            "stage": stage,
            "conflict_files": conflict_files,
        })

    prompt = _build_conflict_prompt(conflict_files, stage, base_branch, issue_id)

    cmd = [
        amp_path,
        "-x",
        prompt,
        "--dangerously-allow-all",
        "--no-notifications",
        "--no-color",
        "--mode",
        "rush",
    ]

    logger.info("Spawning amp to resolve %s conflicts in %s", stage, worktree_path)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=conflict_resolution_timeout,
            env=build_worktree_env(worktree_path),
        )
    except subprocess.TimeoutExpired:
        logger.error("Conflict resolution timed out after %ds", conflict_resolution_timeout)
        if events:
            events.record(EventType.conflict_resolution_finished, {
                "issue_id": issue_id,
                "stage": stage,
                "success": False,
                "reason": "timeout",
            })
        return False

    if proc.returncode != 0:
        logger.warning("Amp conflict resolution exited with code %d", proc.returncode)

    # Check if conflicts are resolved: no conflict markers remaining
    remaining = _get_conflict_files(worktree_path)
    if remaining:
        logger.warning("Conflicts remain after amp resolution: %s", remaining)
        if events:
            events.record(EventType.conflict_resolution_finished, {
                "issue_id": issue_id,
                "stage": stage,
                "success": False,
                "reason": "conflicts_remain",
                "remaining_files": remaining,
            })
        return False

    return True


def _resolve_conflicts_with_amp(
    worktree_path: Path,
    conflict_files: list[str],
    stage: str,
    base_branch: str,
    issue_id: str,
    events: EventLog | None,
    conflict_resolution_timeout: int = 600,
) -> bool:
    """Resolve merge/rebase conflicts using amp. Handles multi-commit rebases. Returns True if resolved."""
    resolved = _spawn_amp_for_conflicts(
        worktree_path=worktree_path,
        conflict_files=conflict_files,
        stage=stage,
        base_branch=base_branch,
        issue_id=issue_id,
        events=events,
        conflict_resolution_timeout=conflict_resolution_timeout,
    )
    if not resolved:
        return False

    # If rebase, continue — may need multiple rounds for multi-commit rebases
    if stage == "rebase":
        rebase_env = {**subprocess.os.environ, "GIT_EDITOR": "true"}
        max_rebase_rounds = 20  # safety cap
        for _round in range(max_rebase_rounds):
            try:
                subprocess.run(
                    ["git", "rebase", "--continue"],
                    cwd=worktree_path,
                    check=True,
                    capture_output=True,
                    env=rebase_env,
                )
                break  # rebase finished successfully
            except subprocess.CalledProcessError as rebase_err:
                next_conflicts = _get_conflict_files(worktree_path)
                if next_conflicts and _is_conflict(rebase_err):
                    logger.info(
                        "Rebase --continue hit new conflicts (round %d): %s",
                        _round + 1,
                        next_conflicts,
                    )
                    re_resolved = _spawn_amp_for_conflicts(
                        worktree_path=worktree_path,
                        conflict_files=next_conflicts,
                        stage=stage,
                        base_branch=base_branch,
                        issue_id=issue_id,
                        events=events,
                        conflict_resolution_timeout=conflict_resolution_timeout,
                    )
                    if not re_resolved:
                        logger.warning("Failed to resolve conflicts on round %d", _round + 1)
                        return False
                    # Loop back to try --continue again
                else:
                    logger.warning("git rebase --continue failed after conflict resolution")
                    if events:
                        events.record(EventType.conflict_resolution_finished, {
                            "issue_id": issue_id,
                            "stage": stage,
                            "success": False,
                            "reason": "rebase_continue_failed",
                        })
                    return False

    logger.info("Conflict resolution succeeded for %s stage", stage)
    if events:
        events.record(EventType.conflict_resolution_finished, {
            "issue_id": issue_id,
            "stage": stage,
            "success": True,
        })
    return True


def _build_conflict_prompt(
    conflict_files: list[str],
    stage: str,
    base_branch: str,
    issue_id: str,
) -> str:
    files_list = "\n".join(f"- {f}" for f in conflict_files)
    return "\n".join([
        f"You are resolving {stage} conflicts for issue {issue_id}.",
        f"The branch was being {stage}d onto origin/{base_branch} and hit conflicts.",
        "",
        "Files with conflicts:",
        files_list,
        "",
        "INSTRUCTIONS:",
        "1. Open each conflicted file and resolve the conflict markers (<<<<<<< ======= >>>>>>>)",
        "2. Stage each resolved file with `git add <file>`",
        "3. Do NOT run `git rebase --continue` or `git merge --continue` — that will be handled automatically",
        "4. Do NOT commit — just resolve and stage",
        "5. Make sure the resolved code is correct and compiles",
        "",
        "Resolve ALL conflicts now.",
    ])


def verify_and_merge(
    worktree_info: WorktreeInfo,
    repo_root: Path,
    base_branch: str,
    verification_commands: list[str],
    auto_push: bool,
    issue_id: str,
    state_dir: Path | None = None,
    conflict_resolution_timeout: int = 600,
) -> MergeResult:
    """Verify a worktree branch and merge it into the base branch."""
    events = EventLog(state_dir) if state_dir else None

    # Fetch
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return MergeResult(success=False, stage="fetch", error=str(e))

    # Check branch has commits ahead of base
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"origin/{base_branch}..{worktree_info.branch_name}"],
        cwd=worktree_info.worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    if int(ahead.stdout.strip()) == 0:
        return MergeResult(success=False, stage="preflight", error="branch has no commits ahead of base")

    diff_check = subprocess.run(
        ["git", "diff", "--quiet", f"origin/{base_branch}..{worktree_info.branch_name}"],
        cwd=worktree_info.worktree_path,
        capture_output=True,
    )
    if diff_check.returncode == 0:
        return MergeResult(success=False, stage="preflight", error="branch has no diff vs base")

    # Rebase
    conflict_resolved = False
    try:
        subprocess.run(
            ["git", "rebase", f"origin/{base_branch}"],
            cwd=worktree_info.worktree_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        if _is_conflict(e):
            conflict_files = _get_conflict_files(worktree_info.worktree_path)
            if events:
                events.record(EventType.conflict_detected, {
                    "issue_id": issue_id,
                    "stage": "rebase",
                    "conflict_files": conflict_files,
                })
            logger.info("Rebase conflict detected, attempting resolution for %s", issue_id)

            resolved = _resolve_conflicts_with_amp(
                worktree_path=worktree_info.worktree_path,
                conflict_files=conflict_files,
                stage="rebase",
                base_branch=base_branch,
                issue_id=issue_id,
                events=events,
                conflict_resolution_timeout=conflict_resolution_timeout,
            )
            if not resolved:
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=worktree_info.worktree_path,
                    capture_output=True,
                )
                return MergeResult(success=False, stage="rebase", error="conflict resolution failed")
            conflict_resolved = True
        else:
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=worktree_info.worktree_path,
                capture_output=True,
            )
            return MergeResult(success=False, stage="rebase", error=str(e))

    # Verify (re-run after conflict resolution too)
    for cmd in verification_commands:
        if events:
            events.record(EventType.verification_run, {"issue_id": issue_id, "command": cmd})
        try:
            subprocess.run(
                cmd,
                cwd=worktree_info.worktree_path,
                check=True,
                capture_output=True,
                shell=True,
                env=build_worktree_env(worktree_info.worktree_path),
            )
        except subprocess.CalledProcessError as e:
            if events:
                events.record(EventType.verification_run, {"issue_id": issue_id, "command": cmd, "result": "fail"})
            return MergeResult(success=False, stage="verify", error=str(e))
        if events:
            events.record(EventType.verification_run, {"issue_id": issue_id, "command": cmd, "result": "pass"})

    # Checkout base branch
    try:
        subprocess.run(
            ["git", "checkout", base_branch],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return MergeResult(success=False, stage="checkout", error=str(e))

    # Pull latest
    try:
        subprocess.run(
            ["git", "pull", "origin", base_branch],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return MergeResult(success=False, stage="pull", error=str(e))

    # Merge
    branch_name = worktree_info.branch_name
    try:
        subprocess.run(
            ["git", "merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        if _is_conflict(e):
            conflict_files = _get_conflict_files(repo_root)
            if events:
                events.record(EventType.conflict_detected, {
                    "issue_id": issue_id,
                    "stage": "merge",
                    "conflict_files": conflict_files,
                })
            logger.info("Merge conflict detected, attempting resolution for %s", issue_id)

            resolved = _resolve_conflicts_with_amp(
                worktree_path=repo_root,
                conflict_files=conflict_files,
                stage="merge",
                base_branch=base_branch,
                issue_id=issue_id,
                events=events,
                conflict_resolution_timeout=conflict_resolution_timeout,
            )
            if not resolved:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=repo_root,
                    capture_output=True,
                )
                return MergeResult(success=False, stage="merge", error="conflict resolution failed")
            # After merge conflict resolution, commit the merge
            try:
                subprocess.run(
                    ["git", "commit", "--no-edit"],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=repo_root,
                    capture_output=True,
                )
                return MergeResult(success=False, stage="merge", error="commit after conflict resolution failed")
            conflict_resolved = True
        else:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_root,
                capture_output=True,
            )
            return MergeResult(success=False, stage="merge", error=str(e))

    # Push and close bd issue
    if auto_push:
        try:
            subprocess.run(
                ["git", "push", "origin", base_branch],
                cwd=repo_root,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            return MergeResult(success=False, stage="push", error=str(e))

        # Close bd issue
        try:
            subprocess.run(
                ["bd", "close", issue_id],
                cwd=repo_root,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            return MergeResult(success=False, stage="close", error=str(e))

    return MergeResult(success=True, stage="complete", conflict_resolved=conflict_resolved)
