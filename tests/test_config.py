"""Tests for project detection and configuration."""

from pathlib import Path

import pytest
import yaml
from click import ClickException

from amp_orchestrator.config import (
    OrchestratorConfig,
    create_default_config,
    detect_project,
    load_config,
)


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project structure with .git and .beads."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".beads").mkdir()
    return tmp_path


def test_detect_project_finds_repo_root(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    sub = root / "a" / "b"
    sub.mkdir(parents=True)

    ctx = detect_project(sub)
    assert ctx.repo_root == root
    assert ctx.has_git is True
    assert ctx.has_beads is True


def test_detect_project_raises_when_git_missing(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()

    with pytest.raises(ClickException, match="Not a git repository"):
        detect_project(tmp_path)


def test_detect_project_raises_when_beads_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    with pytest.raises(ClickException, match="No .beads directory"):
        detect_project(tmp_path)


def test_load_config_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config == OrchestratorConfig()


def test_load_config_reads_existing_config(tmp_path: Path) -> None:
    cfg_dir = tmp_path / ".amp-orchestrator"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        yaml.dump({"base_branch": "develop", "auto_push": False})
    )

    config = load_config(tmp_path)
    assert config.base_branch == "develop"
    assert config.auto_push is False
    # Defaults for unset fields
    assert config.max_workers == 1
    assert config.require_clean_worktree is True


def test_create_default_config_writes_file(tmp_path: Path) -> None:
    path = create_default_config(tmp_path)

    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data["base_branch"] == "main"
    assert data["max_workers"] == 1
    assert data["amp_mode"] == "smart"


def test_context_window_warn_threshold_default(tmp_path: Path) -> None:
    config = OrchestratorConfig()
    assert config.context_window_warn_threshold == 0.85


def test_create_default_config_includes_context_window_warn_threshold(tmp_path: Path) -> None:
    path = create_default_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    assert data["context_window_warn_threshold"] == 0.85


def test_max_workers_gt_1_rejected(tmp_path: Path) -> None:
    cfg_dir = tmp_path / ".amp-orchestrator"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.dump({"max_workers": 4}))

    with pytest.raises(ClickException, match="max_workers must be 1"):
        load_config(tmp_path)
