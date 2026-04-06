"""Unified workflow phase definitions for the orc orchestrator.

WorkflowPhase is the single source of truth for all issue lifecycle phases.
It serves both as durable checkpoint state (resumable subset) and as
fine-grained event/UI phase annotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkflowPhase(str, Enum):
    """Ordered phases of an issue's lifecycle through the orchestrator."""

    preflight = "preflight"
    already_implemented_check = "already_implemented_check"
    worktree_created = "worktree_created"
    claimed = "claimed"
    amp_running = "amp_running"
    amp_finished = "amp_finished"
    summary_extraction = "summary_extraction"
    post_merge_eval = "post_merge_eval"
    dirty_worktree_check = "dirty_worktree_check"
    evaluation_running = "evaluation_running"
    ready_to_merge = "ready_to_merge"
    merge_running = "merge_running"
    merge_recovery = "merge_recovery"
    conflict_resolution = "conflict_resolution"
    parent_promotion = "parent_promotion"
    claim_release_pending = "claim_release_pending"


@dataclass(frozen=True)
class PhaseInfo:
    label: str
    resumable: bool
    visible_in_timeline: bool = True


PHASE_INFO: dict[WorkflowPhase, PhaseInfo] = {
    WorkflowPhase.preflight: PhaseInfo("Preflight checks", False),
    WorkflowPhase.already_implemented_check: PhaseInfo("Already-implemented check", False),
    WorkflowPhase.worktree_created: PhaseInfo("Worktree created", False),
    WorkflowPhase.claimed: PhaseInfo("Claimed in backlog", True),
    WorkflowPhase.amp_running: PhaseInfo("Agent running", True),
    WorkflowPhase.amp_finished: PhaseInfo("Agent finished", True),
    WorkflowPhase.summary_extraction: PhaseInfo("Summary extraction", False),
    WorkflowPhase.post_merge_eval: PhaseInfo("Post-merge evaluation", False),
    WorkflowPhase.dirty_worktree_check: PhaseInfo("Dirty worktree check", False),
    WorkflowPhase.evaluation_running: PhaseInfo("Evaluation", False),
    WorkflowPhase.ready_to_merge: PhaseInfo("Ready to merge", True),
    WorkflowPhase.merge_running: PhaseInfo("Merge", False),
    WorkflowPhase.merge_recovery: PhaseInfo("Merge recovery", False),
    WorkflowPhase.conflict_resolution: PhaseInfo("Conflict resolution", False),
    WorkflowPhase.parent_promotion: PhaseInfo("Parent promotion", False),
    WorkflowPhase.claim_release_pending: PhaseInfo("Claim release", False),
}

# Ordered list of phases for timeline display.
PHASE_ORDER: list[WorkflowPhase] = list(WorkflowPhase)

# Phases visible in the TUI timeline sidebar.
TIMELINE_PHASE_ORDER: list[WorkflowPhase] = [
    p for p in WorkflowPhase if PHASE_INFO[p].visible_in_timeline
]

# Set of resumable phase values (for checkpoint writes).
RESUMABLE_PHASES: set[str] = {
    p.value for p, info in PHASE_INFO.items() if info.resumable
}


def is_resumable(phase: WorkflowPhase | str) -> bool:
    """Return True if a phase is a valid resume point."""
    value = phase.value if isinstance(phase, WorkflowPhase) else phase
    return value in RESUMABLE_PHASES


def phase_label(value: str | None) -> str:
    """Return the human-readable label for a phase value."""
    if not value:
        return "—"
    try:
        return PHASE_INFO[WorkflowPhase(value)].label
    except (ValueError, KeyError):
        return value


# Mapping from legacy failure stage strings to WorkflowPhase values.
_LEGACY_FAILURE_STAGE_MAP: dict[str, str] = {
    "amp": WorkflowPhase.amp_running.value,
    "evaluation": WorkflowPhase.evaluation_running.value,
    "worktree_dirty": WorkflowPhase.dirty_worktree_check.value,
    "worktree": WorkflowPhase.worktree_created.value,
    "claim": WorkflowPhase.claimed.value,
    "unclaim": WorkflowPhase.claim_release_pending.value,
    "legacy": WorkflowPhase.amp_running.value,
}


def normalize_failure_phase(stage: str | None) -> str:
    """Normalize a failure stage string to a WorkflowPhase value.

    Handles legacy strings like ``"amp"``, ``"worktree_dirty"``, and
    ``"merge/push"`` as well as current WorkflowPhase values.
    """
    if not stage:
        return WorkflowPhase.amp_running.value
    # Already a valid phase value?
    try:
        WorkflowPhase(stage)
        return stage
    except ValueError:
        pass
    # merge/X → merge_running
    if stage.startswith("merge/"):
        return WorkflowPhase.merge_running.value
    return _LEGACY_FAILURE_STAGE_MAP.get(stage, stage)


# Mapping from EventType values to default phase (for legacy events without phase).
_EVENT_TYPE_PHASE_MAP: dict[str, str] = {
    "issue_selected": WorkflowPhase.preflight.value,
    "amp_started": WorkflowPhase.amp_running.value,
    "amp_finished": WorkflowPhase.amp_finished.value,
    "evaluation_started": WorkflowPhase.evaluation_running.value,
    "evaluation_finished": WorkflowPhase.evaluation_running.value,
    "merge_attempt": WorkflowPhase.merge_running.value,
    "issue_closed": WorkflowPhase.merge_running.value,
    "conflict_detected": WorkflowPhase.conflict_resolution.value,
    "conflict_resolution_started": WorkflowPhase.conflict_resolution.value,
    "conflict_resolution_finished": WorkflowPhase.conflict_resolution.value,
    "merge_recovery_started": WorkflowPhase.merge_recovery.value,
    "merge_recovery_finished": WorkflowPhase.merge_recovery.value,
    "followup_created": WorkflowPhase.post_merge_eval.value,
    "parent_promoted": WorkflowPhase.parent_promotion.value,
    "issue_needs_rework": WorkflowPhase.evaluation_running.value,
    "issue_failure_pruned": WorkflowPhase.preflight.value,
}

# Mapping from error data["stage"] to phase (for legacy error events).
_ERROR_STAGE_PHASE_MAP: dict[str, str] = {
    "claim": WorkflowPhase.claimed.value,
    "amp": WorkflowPhase.amp_running.value,
    "evaluation": WorkflowPhase.evaluation_running.value,
    "worktree": WorkflowPhase.worktree_created.value,
    "worktree_dirty": WorkflowPhase.dirty_worktree_check.value,
    "unclaim": WorkflowPhase.claim_release_pending.value,
    "queue": WorkflowPhase.preflight.value,
}


def infer_event_phase(event_type: str, data: dict | None) -> str:
    """Infer the workflow phase for a legacy event that has no ``phase`` field."""
    # Error events: use data.stage if available
    if event_type == "error" and data:
        stage = data.get("stage", "")
        if stage.startswith("merge/") or stage in ("rebase", "merge", "push"):
            return WorkflowPhase.merge_running.value
        if stage in _ERROR_STAGE_PHASE_MAP:
            return _ERROR_STAGE_PHASE_MAP[stage]

    # Resume events: use data.stage if available
    if event_type in ("resume_attempted", "resume_succeeded", "resume_failed") and data:
        stage = data.get("stage", "")
        if stage:
            return normalize_failure_phase(stage)

    return _EVENT_TYPE_PHASE_MAP.get(event_type, "")
