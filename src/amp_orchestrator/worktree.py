"""Git worktree management for amp-orchestrator."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def slugify(title: str) -> str:
    """Convert an issue title to a URL-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    return slug[:50]


@dataclass
class WorktreeInfo:
    issue_id: str
    worktree_path: Path
    branch_name: str


class WorktreeManager:
    def __init__(self, repo_root: Path, base_branch: str = "main") -> None:
        self.repo_root = repo_root
        self.base_branch = base_branch
        self.worktrees_dir = repo_root / ".worktrees"

    def create_worktree(self, issue_id: str, title: str) -> WorktreeInfo:
        """Create a git worktree for the given issue."""
        slug = slugify(title)
        branch_name = f"amp/{issue_id}-{slug}"
        worktree_path = self.worktrees_dir / issue_id

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
        )

        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=self.repo_root,
                check=True,
                capture_output=True,
            )

        subprocess.run(
            [
                "git", "worktree", "add",
                "-b", branch_name,
                str(worktree_path),
                f"origin/{self.base_branch}",
            ],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
        )

        return WorktreeInfo(
            issue_id=issue_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

    def cleanup_worktree(self, info: WorktreeInfo) -> None:
        """Remove a worktree and delete its branch."""
        subprocess.run(
            ["git", "worktree", "remove", str(info.worktree_path), "--force"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", info.branch_name],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
        )

    def list_worktrees(self) -> list[WorktreeInfo]:
        """List worktrees managed by amp-orchestrator."""
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

        worktrees: list[WorktreeInfo] = []
        worktree_path: Path | None = None
        branch_name: str | None = None

        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                worktree_path = Path(line.split(" ", 1)[1])
                branch_name = None
            elif line.startswith("branch "):
                ref = line.split(" ", 1)[1]
                branch_name = ref.removeprefix("refs/heads/")
            elif line == "":
                if (
                    worktree_path is not None
                    and branch_name is not None
                    and self.worktrees_dir in worktree_path.parents
                ):
                    issue_id = worktree_path.name
                    worktrees.append(
                        WorktreeInfo(
                            issue_id=issue_id,
                            worktree_path=worktree_path,
                            branch_name=branch_name,
                        )
                    )
                worktree_path = None
                branch_name = None

        # Handle last entry if output doesn't end with blank line
        if (
            worktree_path is not None
            and branch_name is not None
            and self.worktrees_dir in worktree_path.parents
        ):
            issue_id = worktree_path.name
            worktrees.append(
                WorktreeInfo(
                    issue_id=issue_id,
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                )
            )

        return worktrees
