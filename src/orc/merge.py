"""Verification and merge management for orc."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orc.events import EventLog, EventType
from orc.worktree import WorktreeInfo, build_worktree_env

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    success: bool
    stage: str
    error: str | None = None
    conflict_resolved: bool = False


def _decode_output(output: str | bytes | None) -> str:
    """Return subprocess output as text for logging and message matching."""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _merge_in_progress(cwd: Path) -> bool:
    """Check whether a merge is still in progress (MERGE_HEAD exists)."""
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=cwd,
        capture_output=True,
    )
    return result.returncode == 0


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


def _stage_conflict_files(cwd: Path, conflict_files: list[str]) -> bool:
    """Stage the files that originally had conflicts after markers are resolved."""
    if not conflict_files:
        return True

    try:
        subprocess.run(
            ["git", "add", "-A", "--", *conflict_files],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        logger.warning(
            "Failed to stage resolved conflict files %s: stderr=%r",
            conflict_files,
            _decode_output(err.stderr),
        )
        return False

    return True


def _rebase_in_progress(cwd: Path) -> bool:
    """Check whether a rebase is still paused and awaiting user action."""
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "REBASE_HEAD"],
        cwd=cwd,
        capture_output=True,
    )
    return result.returncode == 0


def _is_empty_rebase_step(error: subprocess.CalledProcessError) -> bool:
    """Detect when rebase --continue fails because the patch became empty."""
    combined_output = "\n".join(
        part for part in (_decode_output(error.stdout), _decode_output(error.stderr)) if part
    ).lower()
    empty_signals = [
        "no changes",
        "nothing to commit",
        "previous cherry-pick is now empty",
    ]
    return any(signal in combined_output for signal in empty_signals)


def _format_conflict_resolution_error(detail: str | None) -> str:
    """Build a user-facing conflict resolution error with optional diagnostics."""
    detail_text = (detail or "").strip()
    if not detail_text:
        return "conflict resolution failed"
    return f"conflict resolution failed: {detail_text}"


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

    if not _stage_conflict_files(worktree_path, conflict_files):
        if events:
            events.record(EventType.conflict_resolution_finished, {
                "issue_id": issue_id,
                "stage": stage,
                "success": False,
                "reason": "git_add_failed",
                "remaining_files": conflict_files,
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
) -> tuple[bool, str | None]:
    """Resolve merge/rebase conflicts using amp. Returns success and optional failure detail."""
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
        return False, None

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
                    text=True,
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
                        return False, None
                    # Loop back to try --continue again
                elif _is_empty_rebase_step(rebase_err):
                    logger.info(
                        "git rebase --continue reported no changes after conflict resolution; skipping empty commit"
                    )
                    try:
                        subprocess.run(
                            ["git", "rebase", "--skip"],
                            cwd=worktree_path,
                            check=True,
                            capture_output=True,
                            text=True,
                            env=rebase_env,
                        )
                    except subprocess.CalledProcessError as skip_err:
                        logger.warning(
                            "git rebase --skip failed after empty rebase step rc=%s stderr=%r",
                            skip_err.returncode,
                            _decode_output(skip_err.stderr),
                        )
                        if events:
                            events.record(EventType.conflict_resolution_finished, {
                                "issue_id": issue_id,
                                "stage": stage,
                                "success": False,
                                "reason": "rebase_skip_failed",
                            })
                        return False, None

                    if not _rebase_in_progress(worktree_path):
                        break
                else:
                    rebase_stderr = _decode_output(rebase_err.stderr).strip()
                    logger.warning(
                        "git rebase --continue failed after conflict resolution rc=%s stderr=%r",
                        rebase_err.returncode,
                        rebase_stderr,
                    )
                    if events:
                        events.record(EventType.conflict_resolution_finished, {
                            "issue_id": issue_id,
                            "stage": stage,
                            "success": False,
                            "reason": "rebase_continue_failed",
                            "stderr": rebase_stderr,
                            "returncode": rebase_err.returncode,
                        })
                    return False, _format_conflict_resolution_error(rebase_stderr)

    logger.info("Conflict resolution succeeded for %s stage", stage)
    if events:
        events.record(EventType.conflict_resolution_finished, {
            "issue_id": issue_id,
            "stage": stage,
            "success": True,
        })
    return True, None


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

            resolved, resolution_error = _resolve_conflicts_with_amp(
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
                return MergeResult(
                    success=False,
                    stage="rebase",
                    error=resolution_error or "conflict resolution failed",
                )
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

            resolved, resolution_error = _resolve_conflicts_with_amp(
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
                return MergeResult(
                    success=False,
                    stage="merge",
                    error=resolution_error or "conflict resolution failed",
                )
            # After merge conflict resolution, commit the merge — unless amp
            # already completed it (consuming MERGE_HEAD).
            if _merge_in_progress(repo_root):
                try:
                    subprocess.run(
                        ["git", "commit", "--no-edit"],
                        cwd=repo_root,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError as commit_err:
                    logger.error(
                        "git commit --no-edit failed rc=%s stderr=%r",
                        commit_err.returncode,
                        commit_err.stderr,
                    )
                    subprocess.run(
                        ["git", "merge", "--abort"],
                        cwd=repo_root,
                        capture_output=True,
                    )
                    return MergeResult(
                        success=False,
                        stage="merge",
                        error=f"commit after conflict resolution failed: {commit_err.stderr or commit_err.stdout or str(commit_err)}",
                    )
            else:
                logger.warning(
                    "Merge state (MERGE_HEAD) already consumed during amp conflict resolution for %s; "
                    "accepting existing commit",
                    issue_id,
                )
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
