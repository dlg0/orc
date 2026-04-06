"""Project detection and configuration for orc."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import click
import yaml


def _normalize_mode(value: object, *, field_name: str, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_timeout(value: object, *, field_name: str, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return value


@dataclass
class ProjectContext:
    """Detected project context."""

    repo_root: Path
    has_git: bool
    has_beads: bool


@dataclass
class OrchestratorConfig:
    """Runtime configuration for the orchestrator."""

    base_branch: str = "main"
    max_workers: int = 1
    verification_commands: list[str] = field(default_factory=list)
    amp_mode: str = "deep"
    use_already_implemented_preflight: bool = True
    use_decomposition_preflight: bool = True
    enable_evaluation: bool = True
    evaluation_mode: str | None = "rush"
    evaluation_timeout: int = 900
    context_window_warn_threshold: float = 0.85
    summary_mode: str = "self-report"  # "self-report" | "rush-extract" | "stream-json"
    summary_amp_mode: str = "rush"
    fail_fast: bool = False

    def __post_init__(self) -> None:
        self.amp_mode = _normalize_mode(
            self.amp_mode,
            field_name="amp_mode",
            fallback="deep",
        )
        if self.evaluation_mode is not None:
            self.evaluation_mode = _normalize_mode(
                self.evaluation_mode,
                field_name="evaluation_mode",
                fallback=self.amp_mode,
            )
        self.evaluation_timeout = _normalize_timeout(
            self.evaluation_timeout,
            field_name="evaluation_timeout",
            fallback=900,
        )

    @property
    def effective_evaluation_mode(self) -> str:
        return self.evaluation_mode or self.amp_mode

    @property
    def requested_evaluation_mode(self) -> str | None:
        return self.evaluation_mode


CONFIG_DIR = ".orc"
CONFIG_FILE = "config.yaml"


def detect_project(path: Path | None = None) -> ProjectContext:
    """Detect the project root by walking up from *path* (default: cwd).

    Validates that both ``.git`` and ``.beads`` directories exist at the root.
    Raises ``click.ClickException`` if the project is not valid.
    """
    current = (path or Path.cwd()).resolve()

    # Walk up looking for .git
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            repo_root = candidate
            break
    else:
        raise click.ClickException(
            f"Not a git repository (or any parent up to {current})"
        )

    has_git = True
    has_beads = (repo_root / ".beads").exists()

    if not has_beads:
        raise click.ClickException(
            f"No .beads directory found in {repo_root}. Run 'bd onboard' first."
        )

    return ProjectContext(repo_root=repo_root, has_git=has_git, has_beads=has_beads)


def load_config(repo_root: Path) -> OrchestratorConfig:
    """Load configuration from ``.orc/config.yaml``.

    Returns defaults when the file does not exist.
    Raises ``click.ClickException`` if ``max_workers`` is not 1.
    """
    config_path = repo_root / CONFIG_DIR / CONFIG_FILE

    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise click.ClickException(
                f"Invalid config in {config_path}: top-level YAML must be a mapping"
            )
        try:
            config = OrchestratorConfig(**{
                k: v for k, v in data.items() if k in OrchestratorConfig.__dataclass_fields__
            })
        except (TypeError, ValueError) as exc:
            raise click.ClickException(f"Invalid config in {config_path}: {exc}") from exc
    else:
        config = OrchestratorConfig()

    if config.max_workers != 1:
        raise click.ClickException(
            f"max_workers must be 1 for the MVP (got {config.max_workers})"
        )

    return config


def create_default_config(repo_root: Path) -> Path:
    """Write the default configuration to ``.orc/config.yaml``.

    Creates the directory if it does not exist. Returns the path to the file.
    """
    config_dir = repo_root / CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)

    config = OrchestratorConfig()
    data = {
        "base_branch": config.base_branch,
        "max_workers": config.max_workers,
        "verification_commands": config.verification_commands,
        "amp_mode": config.amp_mode,
        "use_already_implemented_preflight": config.use_already_implemented_preflight,
        "use_decomposition_preflight": config.use_decomposition_preflight,
        "enable_evaluation": config.enable_evaluation,
        "evaluation_mode": config.evaluation_mode,
        "evaluation_timeout": config.evaluation_timeout,
        "context_window_warn_threshold": config.context_window_warn_threshold,
        "summary_mode": config.summary_mode,
        "summary_amp_mode": config.summary_amp_mode,
        "fail_fast": config.fail_fast,
    }

    config_path = config_dir / CONFIG_FILE
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return config_path
