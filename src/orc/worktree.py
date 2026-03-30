"""Git worktree management for orc."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def build_worktree_env(worktree_path: Path) -> dict[str, str]:
    """Build a subprocess env with the worktree's ``src/`` prepended to PYTHONPATH.

    This ensures subprocesses import the worktree's modified source rather than
    the main checkout's editable install.
    """
    env = os.environ.copy()
    src_path = worktree_path / "src"
    if src_path.is_dir():
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else str(src_path)
    return env


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
                "-B", branch_name,
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

    def ensure_resumable_worktree(self, branch: str, worktree_path: str) -> bool:
        """Check if a worktree/branch combination is usable for resume.

        Returns True if the worktree exists (or was recreated from the branch).
        Returns False if neither worktree nor branch exist.
        """
        wt_path = Path(worktree_path)
        if wt_path.exists() and (wt_path / ".git").exists():
            return True

        # Check if the branch still exists
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=self.repo_root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return False

        # Branch exists but worktree is gone — recreate it
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=self.repo_root,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

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
        """List worktrees managed by orc."""
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
