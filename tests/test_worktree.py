"""Tests for the worktree manager module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from orc.worktree import WorktreeManager, build_worktree_env, slugify


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


class TestBuildWorktreeEnv:
    def test_prepends_pythonpath_when_src_exists(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        env = build_worktree_env(tmp_path)
        assert env["PYTHONPATH"].startswith(str(src))

    def test_preserves_existing_pythonpath(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        with patch.dict(os.environ, {"PYTHONPATH": "/existing/path"}):
            env = build_worktree_env(tmp_path)
        assert env["PYTHONPATH"] == f"{src}{os.pathsep}/existing/path"

    def test_no_pythonpath_when_src_missing(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = build_worktree_env(tmp_path)
        assert "PYTHONPATH" not in env

    def test_sets_pythonpath_without_existing(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        with patch.dict(os.environ, {}, clear=True):
            env = build_worktree_env(tmp_path)
        assert env["PYTHONPATH"] == str(src)
