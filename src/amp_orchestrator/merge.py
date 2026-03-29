"""Verification and merge management for amp-orchestrator."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from amp_orchestrator.events import EventLog, EventType
from amp_orchestrator.worktree import WorktreeInfo


@dataclass
class MergeResult:
    success: bool
    stage: str
    error: str | None = None


def verify_and_merge(
    worktree_info: WorktreeInfo,
    repo_root: Path,
    base_branch: str,
    verification_commands: list[str],
    auto_push: bool,
    issue_id: str,
    state_dir: Path | None = None,
) -> MergeResult:
    """Verify a worktree branch and merge it into the base branch."""
    # Fetch
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # Rebase
    try:
        subprocess.run(
            ["git", "rebase", f"origin/{base_branch}"],
            cwd=worktree_info.worktree_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_info.worktree_path,
            capture_output=True,
        )
        return MergeResult(success=False, stage="rebase", error=str(e))

    # Verify
    events = EventLog(state_dir) if state_dir else None
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
            )
        except subprocess.CalledProcessError as e:
            if events:
                events.record(EventType.verification_run, {"issue_id": issue_id, "command": cmd, "result": "fail"})
            return MergeResult(success=False, stage="verify", error=str(e))
        if events:
            events.record(EventType.verification_run, {"issue_id": issue_id, "command": cmd, "result": "pass"})

    # Checkout base branch
    subprocess.run(
        ["git", "checkout", base_branch],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # Pull latest
    subprocess.run(
        ["git", "pull", "origin", base_branch],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

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
        return MergeResult(success=False, stage="merge", error=str(e))

    # Push
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

    return MergeResult(success=True, stage="complete")
