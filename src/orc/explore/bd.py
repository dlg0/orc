"""Sandbox and Beads CLI helpers for the exploration harness."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from orc.explore.models import CommandRecord, IssueSpec

BUILTIN_BEADS_TYPES = {
    "bug",
    "chore",
    "decision",
    "epic",
    "feature",
    "task",
}


class BdCommandError(RuntimeError):
    """Raised when a required Beads CLI command fails."""


class Sandbox:
    """Temporary isolated repo for Beads experiments."""

    def __init__(self, keep: bool = False) -> None:
        self.keep = keep
        self.path = Path(tempfile.mkdtemp(prefix="orc-explore-"))

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.keep:
            shutil.rmtree(self.path, ignore_errors=True)


class BdClient:
    """Small Beads subprocess wrapper with transcript capture."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.transcript: list[CommandRecord] = []

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            args,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        self.transcript.append(
            CommandRecord(
                command=list(args),
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "command failed"
            raise BdCommandError(f"{' '.join(args)}: {detail}")
        return proc

    def initialize(self, prefix: str = "orcx") -> None:
        self._run(["git", "init"])
        self._run([
            "bd",
            "init",
            "--skip-agents",
            "--skip-hooks",
            "--quiet",
            "--prefix",
            prefix,
        ])

    def configure_custom_types(self, issue_types: set[str]) -> None:
        custom_types = sorted(issue_type for issue_type in issue_types if issue_type not in BUILTIN_BEADS_TYPES)
        if not custom_types:
            return
        self._run(["bd", "config", "set", "types.custom", ",".join(custom_types)])

    def create_issue(self, spec: IssueSpec, *, parent_id: str | None = None) -> str:
        cmd = [
            "bd",
            "create",
            spec.title,
            "--type",
            spec.issue_type,
            "--silent",
        ]
        if parent_id is not None:
            cmd.extend(["--parent", parent_id])
        if spec.priority is not None:
            cmd.extend(["--priority", str(spec.priority)])
        if spec.description:
            cmd.extend(["--description", spec.description])
        proc = self._run(cmd)
        issue_id = proc.stdout.strip()
        if not issue_id:
            raise BdCommandError(f"{' '.join(cmd)}: missing issue id in output")
        return issue_id

    def update_issue(
        self,
        issue_id: str,
        *,
        status: str | None = None,
        defer_until: str | None = None,
    ) -> None:
        cmd = ["bd", "update", issue_id]
        if status is not None:
            cmd.extend(["--status", status])
        if defer_until is not None:
            cmd.extend(["--defer", defer_until])
        if len(cmd) == 3:
            return
        self._run(cmd)

    def add_blocker(self, issue_id: str, blocked_by_id: str) -> None:
        self._run(["bd", "dep", "add", issue_id, blocked_by_id])

    def ready(self) -> list[dict]:
        proc = self._run(["bd", "ready", "--json", "--limit", "0"])
        return self._parse_json_list(proc.stdout, "bd ready")

    def list_all(self) -> list[dict]:
        proc = self._run(["bd", "list", "--all", "--json", "--limit", "0"])
        return self._parse_json_list(proc.stdout, "bd list")

    def list_tree(self) -> str:
        proc = self._run(["bd", "list", "--all", "--pretty", "--limit", "0"])
        return proc.stdout

    @staticmethod
    def _parse_json_list(stdout: str, command_name: str) -> list[dict]:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive only
            raise BdCommandError(f"{command_name}: invalid JSON output ({exc})") from exc
        if not isinstance(data, list):
            raise BdCommandError(f"{command_name}: expected JSON list")
        return data
