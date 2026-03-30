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


def _git_status_porcelain(cwd: Path) -> str:
    """Return porcelain status output for the working tree (empty when clean)."""
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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
    stdout = (error.stdout or b"") if isinstance(error.stdout, bytes) else (error.stdout or "").encode()
    combined = (stderr + b"\n" + stdout).decode(errors="replace").lower()
    conflict_signals = ["conflict", "merge conflict", "could not apply", "fix conflicts"]
    return any(s in combined for s in conflict_signals)


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
        if not _rebase_in_progress(worktree_path):
            logger.warning(
                "Rebase state (REBASE_HEAD) already consumed during amp conflict resolution for %s; "
                "accepting existing state",
                issue_id,
            )
        else:
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

    # Require clean worktree before rebase
    dirty = _git_status_porcelain(worktree_info.worktree_path)
    if dirty:
        return MergeResult(
            success=False,
            stage="preflight",
            error=f"worktree not clean before rebase:\n{dirty[:500]}",
        )

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


def _snapshot_pre_merge(worktree_path: Path, repo_root: Path, base_branch: str, branch_name: str) -> dict:
    """Capture pre-merge state for postcondition verification."""
    remote_base_sha = subprocess.run(
        ["git", "rev-parse", f"origin/{base_branch}"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Compute merge-base and branch-affected paths for scoped checks
    merge_base_sha = subprocess.run(
        ["git", "merge-base", f"origin/{base_branch}", branch_name],
        cwd=worktree_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", f"{merge_base_sha}..{branch_name}"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    affected_paths = {p.strip() for p in diff_result.stdout.splitlines() if p.strip()}
    return {
        "remote_base_sha": remote_base_sha,
        "merge_base_sha": merge_base_sha,
        "affected_paths": affected_paths,
    }


def _check_post_merge(
    worktree_path: Path,
    repo_root: Path,
    base_branch: str,
    branch_name: str,
    snapshot: dict,
    auto_push: bool,
) -> tuple[bool, str | None]:
    """Verify postconditions after agent merge. Returns (ok, error_detail)."""
    # No in-progress git ops in either worktree or repo root
    for cwd, label in [(worktree_path, "worktree"), (repo_root, "repo_root")]:
        if _rebase_in_progress(cwd):
            return False, f"REBASE_HEAD still exists in {label}"
        if _merge_in_progress(cwd):
            return False, f"MERGE_HEAD still exists in {label}"
        conflicts = _get_conflict_files(cwd)
        if conflicts:
            return False, f"unmerged files in {label}: {conflicts}"

    # Scoped cleanliness: only check affected paths, not entire repo
    affected_paths = snapshot.get("affected_paths", set())
    if affected_paths:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *affected_paths],
            cwd=repo_root, capture_output=True, text=True,
        )
        if status.stdout.strip():
            return False, f"affected paths not clean: {status.stdout.strip()[:200]}"

    # Integration check using post-rebase branch tip (not pre-rebase SHA)
    final_branch_sha = subprocess.run(
        ["git", "rev-parse", branch_name],
        cwd=worktree_path, capture_output=True, text=True,
    ).stdout.strip()
    local_base_sha = subprocess.run(
        ["git", "rev-parse", base_branch],
        cwd=repo_root, capture_output=True, text=True,
    ).stdout.strip()
    if final_branch_sha and local_base_sha:
        ancestor_check = subprocess.run(
            ["git", "merge-base", "--is-ancestor", final_branch_sha, local_base_sha],
            cwd=repo_root, capture_output=True,
        )
        if ancestor_check.returncode != 0:
            return False, "branch commits not integrated into base"

    # Verify push landed via ancestor check (tolerates concurrent pushes)
    if auto_push:
        fetch = subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=repo_root, capture_output=True,
        )
        if fetch.returncode != 0:
            return False, "post-merge fetch failed"
        remote_ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", local_base_sha, f"origin/{base_branch}"],
            cwd=repo_root, capture_output=True,
        )
        if remote_ancestor.returncode != 0:
            return False, "merge result not found on remote"

    return True, None


def _build_merge_agent_prompt(
    branch_name: str,
    base_branch: str,
    issue_id: str,
    verification_commands: list[str],
    worktree_path: Path,
    repo_root: Path,
) -> str:
    """Build a positive-criteria prompt for the merge agent."""
    verify_section = ""
    if verification_commands:
        cmds = "\n".join(f"  - `{cmd}`" for cmd in verification_commands)
        verify_section = f"\nAfter rebasing, run these verification commands and ensure they pass:\n{cmds}\n"

    return "\n".join([
        f"You are merging branch `{branch_name}` into `{base_branch}` for issue {issue_id}.",
        "",
        "Your task:",
        f"1. Rebase `{branch_name}` onto the latest `origin/{base_branch}`, resolving any conflicts",
        verify_section,
        f"2. In the main repo at `{repo_root}`, checkout `{base_branch}` and pull latest",
        f"3. Merge `{branch_name}` into `{base_branch}` with `--no-ff`",
        f"4. Push `origin {base_branch}`",
        f"5. Run `bd close {issue_id}`",
        "",
        "Success criteria:",
        f"- `{base_branch}` contains all commits from `{branch_name}`",
        "- All verification commands pass",
        "- Push to origin succeeds",
        "- Working tree is clean",
        "",
        f"Work in the worktree at: {worktree_path}",
        f"Main repo at: {repo_root}",
    ])


_MERGE_AGENT_TIMEOUT = 900  # 15 minutes


def agent_merge(
    worktree_info: WorktreeInfo,
    repo_root: Path,
    base_branch: str,
    verification_commands: list[str],
    auto_push: bool,
    issue_id: str,
    state_dir: Path | None = None,
    merge_agent_timeout: int = _MERGE_AGENT_TIMEOUT,
    merge_agent_mode: str = "smart",
) -> MergeResult:
    """Merge via a dedicated amp agent thread with programmatic pre/post checks."""
    events = EventLog(state_dir) if state_dir else None

    # --- Preflight ---
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_root, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return MergeResult(success=False, stage="fetch", error=str(e))

    # Check branch has commits ahead
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"origin/{base_branch}..{worktree_info.branch_name}"],
        cwd=worktree_info.worktree_path,
        capture_output=True, text=True, check=True,
    )
    if int(ahead.stdout.strip()) == 0:
        return MergeResult(success=False, stage="preflight", error="branch has no commits ahead of base")

    diff_check = subprocess.run(
        ["git", "diff", "--quiet", f"origin/{base_branch}..{worktree_info.branch_name}"],
        cwd=worktree_info.worktree_path, capture_output=True,
    )
    if diff_check.returncode == 0:
        return MergeResult(success=False, stage="preflight", error="branch has no diff vs base")

    # Require clean worktree
    dirty = _git_status_porcelain(worktree_info.worktree_path)
    if dirty:
        return MergeResult(
            success=False,
            stage="preflight",
            error=f"worktree not clean before merge:\n{dirty[:500]}",
        )

    # Assert clean state
    for check_cmd, check_name in [
        (["git", "rev-parse", "-q", "--verify", "REBASE_HEAD"], "rebase"),
        (["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], "merge"),
    ]:
        result = subprocess.run(check_cmd, cwd=worktree_info.worktree_path, capture_output=True)
        if result.returncode == 0:
            return MergeResult(
                success=False, stage="preflight",
                error=f"{check_name} already in progress in worktree",
            )

    # Snapshot
    try:
        snapshot = _snapshot_pre_merge(
            worktree_info.worktree_path, repo_root, base_branch, worktree_info.branch_name,
        )
    except subprocess.CalledProcessError as e:
        return MergeResult(success=False, stage="preflight", error=f"snapshot failed: {e}")

    if events:
        events.record(EventType.merge_attempt, {
            "issue_id": issue_id,
            "method": "agent",
            "snapshot": snapshot,
        })

    # --- Spawn merge agent ---
    amp_path = shutil.which("amp")
    if amp_path is None:
        return MergeResult(success=False, stage="merge_agent", error="amp CLI not found")

    prompt = _build_merge_agent_prompt(
        branch_name=worktree_info.branch_name,
        base_branch=base_branch,
        issue_id=issue_id,
        verification_commands=verification_commands if auto_push else [],
        worktree_path=worktree_info.worktree_path,
        repo_root=repo_root,
    )

    cmd = [
        amp_path, "-x", prompt,
        "--dangerously-allow-all", "--no-notifications", "--no-color",
        "--mode", merge_agent_mode,
    ]

    logger.info("Spawning merge agent for %s in %s", issue_id, worktree_info.worktree_path)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree_info.worktree_path),
            capture_output=True, text=True,
            timeout=merge_agent_timeout,
            env=build_worktree_env(worktree_info.worktree_path),
        )
    except subprocess.TimeoutExpired:
        logger.error("Merge agent timed out after %ds", merge_agent_timeout)
        return MergeResult(success=False, stage="merge_agent", error=f"merge agent timed out after {merge_agent_timeout}s")

    if proc.returncode != 0:
        logger.warning("Merge agent exited with code %d", proc.returncode)

    # --- Postcondition checks ---
    ok, error_detail = _check_post_merge(
        worktree_path=worktree_info.worktree_path,
        repo_root=repo_root,
        base_branch=base_branch,
        branch_name=worktree_info.branch_name,
        snapshot=snapshot,
        auto_push=auto_push,
    )
    if not ok:
        logger.warning("Merge agent postcondition failed: %s", error_detail)
        if events:
            events.record(EventType.error, {
                "issue_id": issue_id,
                "stage": "merge_agent_postcheck",
                "error": error_detail,
            })
        return MergeResult(success=False, stage="merge_agent", error=error_detail)

    logger.info("Merge agent succeeded for %s", issue_id)
    if events:
        events.record(EventType.issue_closed, {"issue_id": issue_id, "method": "agent"})

    return MergeResult(success=True, stage="complete")
