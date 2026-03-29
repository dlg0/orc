"""Tests for the worktree manager module."""

from __future__ import annotations

from pathlib import Path

from amp_orchestrator.worktree import WorktreeManager, slugify


class TestSlugify:
    def test_simple_title(self) -> None:
        assert slugify("Fix the bug") == "fix-the-bug"

    def test_special_characters(self) -> None:
        assert slugify("feat: add login & signup!") == "feat-add-login-signup"

    def test_multiple_spaces_and_hyphens(self) -> None:
        assert slugify("too   many---dashes") == "too-many-dashes"

    def test_leading_trailing_hyphens(self) -> None:
        assert slugify("---hello---") == "hello"

    def test_long_title_truncated(self) -> None:
        title = "a" * 100
        result = slugify(title)
        assert len(result) == 50
        assert result == "a" * 50

    def test_empty_string(self) -> None:
        assert slugify("") == ""

    def test_unicode_characters(self) -> None:
        assert slugify("café résumé") == "caf-r-sum"

    def test_numbers_preserved(self) -> None:
        assert slugify("issue 42 fix") == "issue-42-fix"


class TestBranchNameGeneration:
    def test_branch_name_format(self) -> None:
        mgr = WorktreeManager(repo_root=Path("/repo"))
        slug = slugify("Add user auth")
        branch = f"amp/ISSUE-1-{slug}"
        assert branch == "amp/ISSUE-1-add-user-auth"

    def test_branch_name_with_special_chars_in_title(self) -> None:
        slug = slugify("fix: handle edge-case #99")
        branch = f"amp/BUG-42-{slug}"
        assert branch == "amp/BUG-42-fix-handle-edge-case-99"


class TestWorktreePath:
    def test_worktree_path(self) -> None:
        mgr = WorktreeManager(repo_root=Path("/repo"))
        assert mgr.worktrees_dir == Path("/repo/.worktrees")

    def test_issue_worktree_path(self) -> None:
        mgr = WorktreeManager(repo_root=Path("/repo"))
        path = mgr.worktrees_dir / "ISSUE-5"
        assert path == Path("/repo/.worktrees/ISSUE-5")

    def test_custom_base_branch(self) -> None:
        mgr = WorktreeManager(repo_root=Path("/repo"), base_branch="develop")
        assert mgr.base_branch == "develop"
