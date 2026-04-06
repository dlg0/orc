"""Core scheduler loop for orc."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from orc.already_implemented import (
    AlreadyImplementedChecker,
    AmpAlreadyImplementedChecker,
)
from orc.amp_runner import AmpRunner, IssueContext, ResultType
from orc.config import OrchestratorConfig
from orc.evaluator import IssueEvaluator
from orc.events import EventLog, EventType
from orc.queue import (
    IssueState,
    claim_issue,
    close_issue,
    reopen_issue,
    compute_queue_breakdown,
    create_issue,
    get_children_all_closed,
    get_children_ids,
    get_issue_parent,
    get_issue_state,
    get_issue_status,
    get_ready_issues,
    reconcile_issue_failures,
    rewrite_parent_as_integration_issue,
    select_next_issue,
    summarize_skipped_issues,
    unclaim_issue,
)
from orc.state import (
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    RunCheckpoint,
    StateStore,
    apply_requests,
)
from orc.workflow import WorkflowPhase
from orc.worktree import WorktreeInfo, WorktreeManager

logger = logging.getLogger(__name__)

# Local alias for conciseness in checkpoint calls.
RunStage = WorkflowPhase


_ISSUE_DIVIDER = "-" * 60

_RECOVERY_PROMPT = (
    "\n\n---\n"
    "**RECOVERY RUN**: This is a recovery run for an interrupted previous attempt.\n"
    "Existing work may be present in the worktree/branch.\n"
    "Before making changes, inspect: `git status`, `git log --oneline -10`, `git diff --stat`\n"
    "Build on existing work; do not restart from scratch unless changes are clearly wrong.\n"
    "---\n"
)
_QUEUE_RETRY_MAX = 3
_QUEUE_RETRY_DELAY = 5  # seconds


def _sync_repo_root(repo_root: Path, base_branch: str) -> tuple[bool, str | None]:
    """Sync the repo root to the latest base branch after agent merge.

    Returns (success, error_message).
    """
    import subprocess

    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_root, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return False, f"git fetch failed: {e}"

    try:
        subprocess.run(
            ["git", "checkout", base_branch],
            cwd=repo_root, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return False, f"git checkout {base_branch} failed: {e}"

    try:
        subprocess.run(
            ["git", "pull", "--ff-only", "origin", base_branch],
            cwd=repo_root, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        return False, f"git pull failed: {e}"

    return True, None


def _create_followup_issue(
    original_issue_id: str,
    original_title: str,
    original_description: str,
    original_acceptance_criteria: str,
    eval_summary: str,
    eval_gaps: list[str],
    repo_root: Path,
    state: OrchestratorState,
    store: StateStore,
    state_dir: Path,
    events: EventLog,
) -> str | None:
    """Create a follow-up bd issue when post-merge evaluation fails.

    The follow-up is created as a sibling of the original (same parent).
    The original issue is closed, and Beads priority nudges future ordering.

    Returns the new issue ID, or None if creation failed.
    """
    # Build follow-up content
    short_summary = eval_summary[:100] if eval_summary else "evaluation failed"
    followup_title = f"Follow-up: {original_title} - {short_summary}"

    gaps_text = "\n".join(f"- {g}" for g in eval_gaps) if eval_gaps else "- (no specific gaps reported)"
    followup_description = "\n".join([
        f"## Follow-up to {original_issue_id}",
        "",
        f"The original issue ({original_issue_id}: {original_title}) was implemented and merged,",
        "but post-merge evaluation identified issues that need to be addressed.",
        "",
        "## Evaluation Summary",
        eval_summary or "(no summary)",
        "",
        "## Gaps Identified",
        gaps_text,
        "",
        "## Original Requirements",
        "",
        "### Description",
        original_description or "(no description)",
        "",
        "### Acceptance Criteria",
        original_acceptance_criteria or "(none specified)",
        "",
        "## Notes",
        "- Work from the original issue is already merged into main",
        "- This follow-up should build on top of that work",
        f"- Original issue {original_issue_id} has been closed",
    ])

    # Get parent of original issue to make follow-up a sibling
    parent_id = get_issue_parent(original_issue_id, cwd=repo_root)

    # Create the follow-up issue
    new_id = create_issue(
        title=followup_title,
        description=followup_description,
        parent=parent_id,
        priority="1",  # High priority for follow-ups
        cwd=repo_root,
    )

    if not new_id:
        click.echo(f"[FOLLOWUP] FAILED to create follow-up issue for {original_issue_id}")
        events.record(EventType.error, {
            "issue_id": original_issue_id,
            "stage": "followup_creation",
            "error": "bd create failed",
        })
        return None

    click.echo(f"[FOLLOWUP] Created {new_id} as follow-up to {original_issue_id}")
    events.record(EventType.followup_created, {
        "original_issue_id": original_issue_id,
        "followup_issue_id": new_id,
        "eval_summary": eval_summary,
    })

    # Ensure original is closed
    original_status = get_issue_status(original_issue_id, cwd=repo_root)
    if original_status != "closed":
        close_issue(original_issue_id, cwd=repo_root)

    return new_id


def _save_with_requests(store: StateStore, state: OrchestratorState, state_dir: Path) -> None:
    """Save state after draining pending external requests.

    This ensures that TUI/CLI mutations (unhold, pause, stop, etc.) are
    incorporated into the scheduler's in-memory state before it writes
    to disk, preventing lost-update races.
    """
    apply_requests(state, state_dir)
    store.save(state)


def _check_stop_at_safe_point(
    store: StateStore,
    state_dir: Path,
    state: OrchestratorState,
    events: EventLog,
    repo_root: Path,
    reason: str,
) -> bool:
    """Check for pending pause/stop requests and wind down if found.

    Re-drains the request queue (catching requests that arrived since the
    last save), then inspects ``state.mode``.  If the mode is
    ``pause_requested`` or ``stopping``, the active run is preserved as a
    ``resume_candidate`` so it can be picked up on the next start, and
    the scheduler transitions to ``paused`` or ``idle``.

    Returns ``True`` when the caller should ``return`` (scheduler is stopping).
    """
    # Re-drain in case requests arrived since the last save.
    apply_requests(state, state_dir)

    if state.mode not in (OrchestratorMode.pause_requested, OrchestratorMode.stopping):
        return False

    requested_mode = state.mode
    click.echo(f"[SCHEDULER] {requested_mode.value} detected at {reason} — winding down")

    # Unclaim the issue in beads before preserving the checkpoint.
    _unclaim_active(state, events, repo_root)

    # Preserve the active run as a resume candidate so work is not lost.
    if state.active_run is not None:
        state.resume_candidate = dict(state.active_run)
    state.active_run = None
    store.save(state)

    if requested_mode == OrchestratorMode.pause_requested:
        store.transition(state, OrchestratorMode.paused)
        events.record(EventType.state_changed, {"to": "paused", "reason": reason})
        click.echo("[SCHEDULER] Paused.")
    else:
        store.transition(state, OrchestratorMode.idle)
        events.record(EventType.state_changed, {"to": "idle", "reason": reason})
        click.echo("[SCHEDULER] Stopped.")

    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_failure(
    store: StateStore,
    state_dir: Path,
    state: OrchestratorState,
    issue_id: str,
    category: FailureCategory,
    stage: str,
    summary: str,
    branch: str | None = None,
    worktree_path: str | None = None,
    preserve_worktree: bool = False,
    extra: dict | None = None,
) -> IssueFailure:
    """Persist an IssueFailure into state.issue_failures."""
    action = _action_for_category(category)
    existing = state.issue_failures.get(issue_id)
    attempts = 1
    if existing and isinstance(existing, dict) and "attempts" in existing:
        attempts = existing["attempts"] + 1

    failure = IssueFailure(
        category=category,
        action=action,
        stage=stage,
        summary=summary,
        timestamp=_now_iso(),
        attempts=attempts,
        branch=branch,
        worktree_path=worktree_path,
        preserve_worktree=preserve_worktree,
        extra=extra,
    )
    state.issue_failures[issue_id] = failure.to_dict()
    _save_with_requests(store, state, state_dir)
    return failure


def _action_for_category(category: FailureCategory) -> FailureAction:
    """Map failure category to the default action."""
    return {
        FailureCategory.transient_external: FailureAction.auto_retry,
        FailureCategory.stale_or_conflicted: FailureAction.hold_for_retry,
        FailureCategory.awaiting_subtasks: FailureAction.hold_until_backlog_changes,
        FailureCategory.blocked_by_dependency: FailureAction.hold_until_backlog_changes,
        FailureCategory.agent_failed: FailureAction.pause_orchestrator,
        FailureCategory.agent_crashed: FailureAction.pause_orchestrator,
        FailureCategory.merge_exhausted: FailureAction.pause_orchestrator,
        FailureCategory.resume_failed: FailureAction.pause_orchestrator,
        FailureCategory.sync_failed: FailureAction.pause_orchestrator,
        FailureCategory.fatal_run_error: FailureAction.pause_orchestrator,
    }[category]


def _update_checkpoint(
    store: StateStore,
    state_dir: Path,
    state: OrchestratorState,
    stage: WorkflowPhase,
    *,
    bd_claimed: bool | None = None,
    amp_result: dict | None = None,
    eval_result: dict | None = None,
    events: EventLog | None = None,
) -> None:
    """Update the active run checkpoint's stage and save atomically."""
    if state.active_run is None:
        return
    state.active_run["stage"] = stage.value
    state.active_run["updated_at"] = _now_iso()
    if bd_claimed is not None:
        state.active_run["bd_claimed"] = bd_claimed
    if amp_result is not None:
        state.active_run["amp_result"] = amp_result
    if eval_result is not None:
        state.active_run["eval_result"] = eval_result
    _save_with_requests(store, state, state_dir)
    if events is not None:
        events.set_phase(stage)


def _attempt_resume(
    repo_root: Path,
    state_dir: Path,
    config: OrchestratorConfig,
    runner: AmpRunner,
    evaluator: IssueEvaluator | None = None,
    store: StateStore | None = None,
    state: OrchestratorState | None = None,
    events: EventLog | None = None,
    worktree_mgr: WorktreeManager | None = None,
    fail_fast: bool = False,
) -> bool:
    """Attempt to resume an interrupted run from resume_candidate.

    Returns True if resume was attempted (success or failure),
    False if candidate was discarded without running.
    """
    assert store is not None and state is not None and events is not None
    assert worktree_mgr is not None

    candidate = state.resume_candidate
    if not candidate:
        return False

    issue_id = candidate["issue_id"]
    stage = candidate.get("stage", "")
    branch = candidate.get("branch")
    wt_path = candidate.get("worktree_path")
    attempts = candidate.get("resume_attempts", 0)

    click.echo("")
    click.echo(_ISSUE_DIVIDER)
    click.echo(f"[RESUME] {issue_id} -- attempting recovery (attempt {attempts})")
    click.echo(_ISSUE_DIVIDER)
    events.record(EventType.resume_attempted, {
        "issue_id": issue_id, "stage": stage, "attempt": attempts,
    })

    # Validate the resume is still possible
    if not branch or not wt_path:
        click.echo(f"[RESUME] {issue_id} no branch/worktree — discarding candidate")
        state.resume_candidate = None
        _save_with_requests(store, state, state_dir)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "no_branch_or_worktree"})
        return False

    if not worktree_mgr.ensure_resumable_worktree(branch, wt_path):
        click.echo(f"[RESUME] {issue_id} worktree/branch not recoverable — discarding")
        # Unclaim if we had a claim
        if candidate.get("bd_claimed"):
            unclaim_issue(issue_id, cwd=repo_root)
        state.resume_candidate = None
        _save_with_requests(store, state, state_dir)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "worktree_not_recoverable"})
        return False

    # Validate the issue is still relevant in beads before resuming
    bd_state = get_issue_state(issue_id, cwd=repo_root)
    if bd_state in (IssueState.closed, IssueState.missing):
        click.echo(f"[RESUME] {issue_id} is {bd_state.value} in beads — discarding candidate")
        state.resume_candidate = None
        _save_with_requests(store, state, state_dir)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": f"issue_{bd_state.value}"})
        return False
    if bd_state == IssueState.unknown:
        click.echo(f"[RESUME] {issue_id} beads state unknown (transient failure) — deferring")
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "bd_state_unknown"})
        time.sleep(_QUEUE_RETRY_DELAY)
        return False

    # Promote resume_candidate to active_run
    state.active_run = candidate
    state.resume_candidate = None
    _save_with_requests(store, state, state_dir)

    wt_info = WorktreeInfo(
        issue_id=issue_id,
        worktree_path=Path(wt_path),
        branch_name=branch,
    )
    wt_path_str = str(wt_info.worktree_path)

    # Phase-based resume
    if stage in (RunStage.claimed.value, RunStage.amp_running.value):
        # Re-run amp with recovery prompt
        click.echo(f"[RESUME] {issue_id} re-running amp (was at stage={stage})")
        _update_checkpoint(store, state_dir, state, RunStage.amp_running, events=events)

        description = candidate.get("issue_title", "")
        ctx = IssueContext(
            issue_id=issue_id,
            title=candidate.get("issue_title", ""),
            description=description + _RECOVERY_PROMPT,
            acceptance_criteria="",
            worktree_path=wt_info.worktree_path,
            repo_root=repo_root,
            base_branch=config.base_branch,
        )
        events.record(EventType.amp_started, {"issue_id": issue_id, "recovery": True, "mode": config.amp_mode})
        click.echo(f"[AMP] {issue_id} running (recovery, mode={config.amp_mode}) ...")

        try:
            result = runner.run(ctx)
        except Exception as exc:
            click.echo(f"[RESUME] {issue_id} amp failed during recovery: {exc}")
            _record_failure(
                store, state_dir, state, issue_id, FailureCategory.resume_failed, WorkflowPhase.amp_running.value,
                str(exc), branch, wt_path_str,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "amp_exception"})
            return True

        events.record(EventType.amp_finished, {
            "issue_id": issue_id, "result": result.result.value,
            "summary": result.summary, "recovery": True,
        })
        click.echo(f"[AMP] {issue_id} result={result.result.value} -- {result.summary}")

        if not result.merge_ready or result.result != ResultType.completed:
            click.echo(f"[RESUME] {issue_id} not merge-ready after recovery — marking needs_human")
            _record_failure(
                store, state_dir, state, issue_id, FailureCategory.resume_failed, WorkflowPhase.amp_running.value,
                result.summary, branch, wt_path_str,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            _record_run(store, state_dir, state, issue_id, result.result.value, result.summary,
                        branch, wt_path_str, amp_mode=config.amp_mode)
            events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "not_merge_ready"})
            return True

        _update_checkpoint(
            store, state_dir, state, RunStage.amp_finished,
            amp_result={"result": result.result.value, "summary": result.summary,
                        "merge_ready": result.merge_ready},
            events=events,
        )

        # Check for pause/stop after recovery amp
        if _check_stop_at_safe_point(store, state_dir, state, events, repo_root, "after_resume_amp"):
            return True

    elif stage == RunStage.amp_finished.value:
        click.echo(f"[RESUME] {issue_id} skipping amp (already finished)")

    elif stage == RunStage.ready_to_merge.value:
        click.echo(f"[RESUME] {issue_id} skipping amp (legacy ready_to_merge — treating as finished)")

    else:
        click.echo(f"[RESUME] {issue_id} unknown/non-resumable stage={stage} — discarding")
        _unclaim_active(state, events, repo_root)
        _clear_active(store, state_dir, state)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "unknown_stage"})
        return False

    # Check for pause/stop before post-merge evaluation
    if _check_stop_at_safe_point(store, state_dir, state, events, repo_root, "before_resume_eval"):
        return True

    # Sync repo root
    click.echo(f"[SYNC] {issue_id} syncing repo root ...")
    sync_ok, sync_error = _sync_repo_root(repo_root, config.base_branch)
    if not sync_ok:
        click.echo(f"[SYNC] {issue_id} sync failed: {sync_error}")
        _record_failure(
            store, state_dir, state, issue_id, FailureCategory.sync_failed,
            WorkflowPhase.post_merge_eval.value,
            f"repo sync failed: {sync_error}", branch, wt_path_str,
        )
        _unclaim_active(state, events, repo_root)
        _clear_active(store, state_dir, state)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "sync_failed"})
        return True

    # Post-merge evaluation
    if evaluator is not None:
        _update_checkpoint(store, state_dir, state, RunStage.post_merge_eval, events=events)
        _eval_mode = config.evaluation_mode or config.amp_mode
        events.record(EventType.evaluation_started, {"issue_id": issue_id, "recovery": True, "mode": _eval_mode})
        click.echo(f"[EVAL] {issue_id} running post-merge evaluation (mode={_eval_mode}) ...")

        ctx_for_eval = IssueContext(
            issue_id=issue_id,
            title=candidate.get("issue_title", ""),
            description=candidate.get("issue_description", ""),
            acceptance_criteria=candidate.get("issue_acceptance_criteria", ""),
            worktree_path=wt_info.worktree_path,
            repo_root=repo_root,
            base_branch=config.base_branch,
        )
        try:
            eval_result = evaluator.evaluate(
                context=ctx_for_eval,
                base_branch=config.base_branch,
                verification_commands=config.verification_commands,
            )
        except Exception as exc:
            from orc.evaluator import EvaluationResult
            eval_result = EvaluationResult.fail(f"Evaluator crashed: {exc}")

        events.record(EventType.evaluation_finished, {
            "issue_id": issue_id, "verdict": eval_result.verdict.value,
            "summary": eval_result.summary, "recovery": True,
        })

        if eval_result.passed:
            click.echo(f"[EVAL] {issue_id} PASSED: {eval_result.summary}")
        else:
            click.echo(f"[EVAL] {issue_id} FAILED: {eval_result.summary}")
            followup_id = _create_followup_issue(
                original_issue_id=issue_id,
                original_title=candidate.get("issue_title", ""),
                original_description=candidate.get("issue_description", ""),
                original_acceptance_criteria=candidate.get("issue_acceptance_criteria", ""),
                eval_summary=eval_result.summary,
                eval_gaps=eval_result.gaps,
                repo_root=repo_root,
                state=state,
                store=store,
                state_dir=state_dir,
                events=events,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            _record_run(store, state_dir, state, issue_id,
                        "completed_with_followup" if followup_id else "followup_failed",
                        eval_result.summary, branch, wt_path_str,
                        amp_mode=config.amp_mode)
            events.record(EventType.resume_succeeded if followup_id else EventType.resume_failed,
                          {"issue_id": issue_id})
            return True

    # Success
    click.echo(f"[COMPLETE] {issue_id} OK (recovered)")
    events.record(EventType.issue_closed, {"issue_id": issue_id, "recovery": True})
    events.record(EventType.resume_succeeded, {"issue_id": issue_id})
    state.last_completed_issue = issue_id
    state.issue_failures.pop(issue_id, None)
    _clear_active(store, state_dir, state)
    _record_run(store, state_dir, state, issue_id, "completed", "recovered from interrupted run",
                branch, wt_path_str, amp_mode=config.amp_mode)
    _try_cleanup(worktree_mgr, wt_info)
    _check_parent_promotion(issue_id, repo_root, store, state_dir, state, events)

    return True


def run_loop(
    repo_root: Path,
    state_dir: Path,
    config: OrchestratorConfig,
    runner: AmpRunner,
    evaluator: IssueEvaluator | None = None,
    already_implemented_checker: AlreadyImplementedChecker | None = None,
    fail_fast: bool = False,
    only_issue: str | None = None,
) -> None:
    """Run the main scheduler loop until the queue is empty or stopped.

    When *only_issue* is set the loop processes **at most one issue** and then
    transitions to idle.  If a ``resume_candidate`` exists but belongs to a
    different issue, the loop exits with an error message instead of silently
    running the wrong issue.
    """
    store = StateStore(state_dir)
    events = EventLog(state_dir)
    worktree_mgr = WorktreeManager(repo_root, config.base_branch)
    state = store.load()
    issue_num = 0
    # CLI flag takes precedence; fall back to config file setting
    fail_fast = fail_fast or config.fail_fast

    if only_issue:
        click.echo(f"[SCHEDULER] Single-issue mode: {only_issue}")
    click.echo(f"[SCHEDULER] Entering run loop (fail_fast={fail_fast})")

    while True:
        state = store.load()

        if state.mode in (
            OrchestratorMode.pause_requested,
            OrchestratorMode.stopping,
        ):
            if state.mode == OrchestratorMode.pause_requested:
                store.transition(state, OrchestratorMode.paused)
                events.record(EventType.state_changed, {"to": "paused"})
                click.echo("[SCHEDULER] Paused.")
            else:
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle"})
                click.echo("[SCHEDULER] Stopped.")
            return

        if state.mode != OrchestratorMode.running:
            return

        # Check for resume candidate before queue selection
        if state.resume_candidate:
            candidate_id = state.resume_candidate.get("issue_id")
            if only_issue and candidate_id != only_issue:
                click.echo(
                    f"[SCHEDULER] --only {only_issue} but resume candidate is "
                    f"{candidate_id} — aborting"
                )
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "only_issue_mismatch"})
                return
            resumed = _attempt_resume(
                repo_root, state_dir, config, runner, evaluator,
                store, state, events, worktree_mgr, fail_fast,
            )
            # After resume attempt, re-enter loop to check state/queue
            if resumed:
                issue_num += 1
                if only_issue:
                    click.echo("[SCHEDULER] --only: single issue processed — stopping")
                    state = store.load()
                    if state.mode == OrchestratorMode.running:
                        store.transition(state, OrchestratorMode.idle)
                        events.record(EventType.state_changed, {"to": "idle", "reason": "only_issue_done"})
                    return
            continue

        # Reconcile issue_failures against beads state
        if state.issue_failures:
            click.echo(f"[SCHEDULER] Reconciling {len(state.issue_failures)} held issue(s) against beads ...")
            pruned = reconcile_issue_failures(state.issue_failures, cwd=repo_root)
            if pruned:
                _save_with_requests(store, state, state_dir)
                for issue_id, reason in pruned:
                    click.echo(f"[SCHEDULER] Pruned held issue {issue_id} ({reason})")
                    events.record(EventType.issue_failure_pruned, {"issue_id": issue_id, "reason": reason})

        # Derive skip_ids from persisted issue_failures
        failed_ids: set[str] = set(state.issue_failures.keys())

        # Select next issue — with queue-failure retry
        click.echo("[SCHEDULER] Fetching ready issues from backlog ...")
        queue_result = None
        for attempt in range(1, _QUEUE_RETRY_MAX + 1):
            queue_result = get_ready_issues(repo_root)
            if queue_result.success:
                break
            click.echo(f"[SCHEDULER] Queue fetch failed (attempt {attempt}/{_QUEUE_RETRY_MAX}): {queue_result.error}")
            events.record(EventType.error, {"stage": "queue", "error": queue_result.error, "attempt": attempt})
            if attempt < _QUEUE_RETRY_MAX:
                time.sleep(_QUEUE_RETRY_DELAY)

        assert queue_result is not None  # always set after loop

        if not queue_result.success:
            click.echo("[SCHEDULER] Queue fetch failed after retries — continuing loop")
            events.record(EventType.error, {"stage": "queue", "error": queue_result.error, "retries_exhausted": True})
            continue

        breakdown = compute_queue_breakdown(queue_result, state.issue_failures)
        click.echo(
            f"[SCHEDULER] Backlog: {breakdown.beads_ready} beads-ready, "
            f"{breakdown.policy_skipped} skipped by policy, "
            f"{breakdown.held_and_ready} held, {breakdown.runnable} runnable"
        )
        if queue_result.skipped:
            skip_summary = ", ".join(
                f"{count} {category}"
                for category, count in summarize_skipped_issues(queue_result.skipped).items()
            )
            click.echo(f"[SCHEDULER] Policy skips: {skip_summary}")

        click.echo("[SCHEDULER] Selecting next issue ...")
        issue = select_next_issue(queue_result.issues, skip_ids=failed_ids, priority_id=only_issue)
        # Clear any legacy local queue override after selection attempt.
        if state.promoted_parent:
            state.promoted_parent = None
            _save_with_requests(store, state, state_dir)

        if issue is None:
            if breakdown.beads_ready > 0:
                reasons: list[str] = []
                if breakdown.policy_skipped:
                    reasons.append(f"{breakdown.policy_skipped} skipped by dispatch policy")
                if breakdown.held_and_ready:
                    reasons.append(f"{breakdown.held_and_ready} held locally")
                click.echo(
                    f"[SCHEDULER] No runnable issues -- {breakdown.beads_ready} beads-ready "
                    f"but {' and '.join(reasons)}. Use 'orc status' or 'orc unhold' to inspect."
                )
            else:
                click.echo("[SCHEDULER] No ready issues -- queue exhausted.")
            store.transition(state, OrchestratorMode.idle)
            events.record(EventType.state_changed, {"to": "idle", "reason": "queue_empty"})
            return

        # --only guard: skip issues that don't match
        if only_issue and issue.id != only_issue:
            click.echo(f"[SCHEDULER] --only {only_issue} but selected {issue.id} — stopping")
            store.transition(state, OrchestratorMode.idle)
            events.record(EventType.state_changed, {"to": "idle", "reason": "only_issue_not_found"})
            return

        issue_num += 1
        events.set_phase(WorkflowPhase.preflight)
        events.record(EventType.issue_selected, {"issue_id": issue.id, "title": issue.title})
        click.echo("")
        click.echo(_ISSUE_DIVIDER)
        click.echo(f"[SELECT] #{issue_num} {issue.id} -- {issue.title}")
        click.echo(_ISSUE_DIVIDER)

        # Set active_run immediately so the TUI shows the issue during preflight
        preflight_checkpoint = RunCheckpoint(
            issue_id=issue.id,
            issue_title=issue.title,
            issue_description=issue.description,
            issue_acceptance_criteria=issue.acceptance_criteria,
            stage=RunStage.preflight,
            updated_at=_now_iso(),
        )
        state.active_run = preflight_checkpoint.to_dict()
        _save_with_requests(store, state, state_dir)

        # Already-implemented preflight check
        if already_implemented_checker is not None:
            # Create a dedicated log file for the preflight amp call
            preflight_logs_dir = state_dir / "amp-runs"
            preflight_logs_dir.mkdir(parents=True, exist_ok=True)
            preflight_ts = _now_iso().replace(":", "-").replace("+", "p")
            preflight_log_path = preflight_logs_dir / f"{preflight_ts}-{issue.id}-preflight.jsonl"
            state.active_run["preflight_log_path"] = str(preflight_log_path)
            _update_checkpoint(store, state_dir, state, RunStage.already_implemented_check, events=events)
            click.echo(f"[PREFLIGHT] {issue.id} checking if already implemented (mode=rush) ...")
            click.echo(f"[PREFLIGHT] {issue.id} log={preflight_log_path}")
            ai_result = already_implemented_checker.check(
                issue_id=issue.id,
                title=issue.title,
                description=issue.description,
                acceptance_criteria=issue.acceptance_criteria,
                cwd=repo_root,
                log_path=preflight_log_path,
            )
            if ai_result.should_skip:
                click.echo(
                    f"[PREFLIGHT] {issue.id} {ai_result.confidence.value}: {ai_result.summary}"
                )
                events.record(EventType.already_implemented_detected, {
                    "issue_id": issue.id,
                    "confidence": ai_result.confidence.value,
                    "summary": ai_result.summary,
                    "evidence": ai_result.evidence,
                })
                close_issue(issue.id, cwd=repo_root)
                click.echo(f"[PREFLIGHT] {issue.id} closed (already implemented)")
                _clear_active(store, state_dir, state)
                _record_run(
                    store, state_dir, state, issue.id,
                    "skipped_already_implemented",
                    ai_result.summary,
                    amp_mode=config.amp_mode,
                    extra={
                        "confidence": ai_result.confidence.value,
                        "evidence": ai_result.evidence,
                    },
                )
                if fail_fast:
                    click.echo("[SCHEDULER] Fail-fast: stopping after already-implemented detection")
                    store.transition(state, OrchestratorMode.idle)
                    events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                    return
                continue
            click.echo(f"[PREFLIGHT] {issue.id} not already implemented — proceeding")

        # Create worktree
        _update_checkpoint(store, state_dir, state, RunStage.worktree_created, events=events)
        click.echo(f"[WORKTREE] {issue.id} creating worktree ...")
        try:
            wt_info = worktree_mgr.create_worktree(issue.id, issue.title)
        except Exception as exc:
            click.echo(f"[WORKTREE] {issue.id} FAILED: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "worktree", "error": str(exc)})
            wt_category = FailureCategory.transient_external if isinstance(exc, OSError) else FailureCategory.fatal_run_error
            _clear_active(store, state_dir, state)
            _record_failure(store, state_dir, state, issue.id, wt_category, WorkflowPhase.worktree_created.value, str(exc))
            _record_run(store, state_dir, state, issue.id, "failed", str(exc), amp_mode=config.amp_mode)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after worktree failure")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        click.echo(f"[WORKTREE] {issue.id} branch={wt_info.branch_name}")
        click.echo(f"[WORKTREE] {issue.id} path={wt_info.worktree_path}")

        # Prepare per-run AMP log path for live monitoring
        amp_logs_dir = state_dir / "amp-runs"
        amp_logs_dir.mkdir(parents=True, exist_ok=True)
        ts_slug = _now_iso().replace(":", "-").replace("+", "p")
        amp_log_path = amp_logs_dir / f"{ts_slug}-{issue.id}.jsonl"

        # Update checkpoint with worktree details
        state.active_run["branch"] = wt_info.branch_name
        state.active_run["worktree_path"] = str(wt_info.worktree_path)
        state.active_run["amp_log_path"] = str(amp_log_path)
        _save_with_requests(store, state, state_dir)

        # Claim the issue in bd so it shows as in-progress
        click.echo(f"[CLAIM] {issue.id} claiming in backlog ...")
        claimed = claim_issue(issue.id, cwd=repo_root)
        if claimed:
            click.echo(f"[CLAIM] {issue.id} claimed")
        else:
            click.echo(f"[CLAIM] {issue.id} WARNING: bd update --claim failed (continuing)")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "claim", "error": "bd update --claim failed"})

        _update_checkpoint(store, state_dir, state, RunStage.claimed, bd_claimed=claimed, events=events)

        # Check for pause/stop before starting amp (may take minutes)
        if _check_stop_at_safe_point(store, state_dir, state, events, repo_root, "before_amp"):
            return

        # Invoke Amp
        click.echo(f"[AMP] {issue.id} spawning agent (mode={config.amp_mode}) ...")
        _update_checkpoint(store, state_dir, state, RunStage.amp_running, events=events)
        ctx = IssueContext(
            issue_id=issue.id,
            title=issue.title,
            description=issue.description,
            acceptance_criteria=issue.acceptance_criteria,
            worktree_path=wt_info.worktree_path,
            repo_root=repo_root,
            base_branch=config.base_branch,
        )
        events.record(EventType.amp_started, {"issue_id": issue.id, "mode": config.amp_mode})
        click.echo(f"[AMP] {issue.id} running (mode={config.amp_mode}) ...")
        click.echo(f"[AMP] {issue.id} log={amp_log_path}")

        try:
            result = runner.run(ctx, log_path=amp_log_path)
        except Exception as exc:
            click.echo(f"[AMP] {issue.id} FAILED: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "amp", "error": str(exc)})
            _record_failure(
                store, state_dir, state, issue.id, FailureCategory.agent_crashed, WorkflowPhase.amp_running.value,
                str(exc), wt_info.branch_name, str(wt_info.worktree_path),
                extra={"amp_log_path": str(amp_log_path)} if amp_log_path.exists() else None,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            _record_run(store, state_dir, state, issue.id, "failed", str(exc), worktree_path=str(wt_info.worktree_path), amp_mode=config.amp_mode)
            _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after amp failure")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        _update_checkpoint(
            store, state_dir, state, RunStage.amp_finished,
            amp_result={"result": result.result.value, "summary": result.summary,
                        "merge_ready": result.merge_ready},
            events=events,
        )

        # Check for pause/stop requests (first safe point after runner.run()).
        if _check_stop_at_safe_point(store, state_dir, state, events, repo_root, "after_amp"):
            return

        amp_finished_data: dict = {
            "issue_id": issue.id,
            "result": result.result.value,
            "summary": result.summary,
        }
        if result.thread_id:
            amp_finished_data["thread_id"] = result.thread_id
        if result.context_window_usage_pct is not None:
            amp_finished_data["context_window_usage_pct"] = result.context_window_usage_pct
        events.record(EventType.amp_finished, amp_finished_data)
        click.echo(f"[AMP] {issue.id} result={result.result.value} -- {result.summary}")
        if result.thread_id:
            click.echo(f"[AMP] {issue.id} thread_id={result.thread_id}")

        # Optional rush-mode summary extraction
        if (
            config.summary_mode == "rush-extract"
            and result.thread_id
            and result.result == ResultType.completed
        ):
            from orc.amp_runner import RealAmpRunner

            events.set_phase(WorkflowPhase.summary_extraction)
            click.echo(f"[SUMMARY] {issue.id} extracting rush summary (mode={config.summary_amp_mode}) ...")
            rush_summary = RealAmpRunner.extract_rush_summary(
                thread_id=result.thread_id,
                cwd=wt_info.worktree_path,
                mode=config.summary_amp_mode,
            )
            if rush_summary:
                click.echo(f"[SUMMARY] {issue.id} {rush_summary}")
                result.summary = rush_summary
            else:
                click.echo(f"[SUMMARY] {issue.id} rush extraction failed, using self-report")
        if result.context_window_usage_pct is not None and result.context_window_usage_pct >= config.context_window_warn_threshold * 100:
            click.echo(f"[AMP] {issue.id} WARNING: context window usage high: {result.context_window_usage_pct}%")

        if result.merge_ready:
            click.echo(f"[AMP] {issue.id} merge_ready=true")
        else:
            click.echo(f"[AMP] {issue.id} merge_ready=false")

        wt_path = str(wt_info.worktree_path)

        # Build extra dict with context usage, thread ID, and log path
        ctx_extra: dict = {}
        if result.context_window_usage_pct is not None:
            ctx_extra["context_window_usage_pct"] = result.context_window_usage_pct
        if result.thread_id:
            ctx_extra["thread_id"] = result.thread_id
        if amp_log_path.exists():
            ctx_extra["amp_log_path"] = str(amp_log_path)

        # Handle non-merge outcomes
        if result.result == ResultType.decomposed:
            click.echo(f"[AMP] {issue.id} decomposed -- skipping merge")
            # Rewrite the parent into a verification/integration issue
            child_ids = get_children_ids(issue.id, cwd=repo_root)
            if rewrite_parent_as_integration_issue(issue.id, child_ids, cwd=repo_root):
                click.echo(f"[AMP] {issue.id} rewritten as integration issue")
            else:
                click.echo(f"[AMP] {issue.id} WARNING: failed to rewrite parent as integration issue")
                events.record(EventType.error, {"issue_id": issue.id, "stage": "decomposition_rewrite", "error": "rewrite_parent_as_integration_issue failed"})
            # Inline decomposition: don't hold the parent in issue_failures.
            # The parent stays in the queue; _check_parent_promotion will
            # auto-close it once all children complete.
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            _record_run(store, state_dir, state, issue.id, "decomposed", result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after decomposed result")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        if result.result == ResultType.blocked:
            click.echo(f"[AMP] {issue.id} {result.result.value} -- moving on")
            _record_failure(
                store, state_dir, state, issue.id, FailureCategory.blocked_by_dependency, WorkflowPhase.amp_finished.value,
                result.summary, wt_info.branch_name, wt_path,
                extra=ctx_extra or None,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            _record_run(store, state_dir, state, issue.id, result.result.value, result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after blocked result")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        if result.result in (ResultType.failed, ResultType.needs_human):
            click.echo(f"[AMP] {issue.id} {result.result.value} -- moving on")
            _record_failure(
                store, state_dir, state, issue.id, FailureCategory.agent_failed, WorkflowPhase.amp_finished.value,
                result.summary, wt_info.branch_name, wt_path,
                extra=ctx_extra or None,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state_dir, state)
            _record_run(store, state_dir, state, issue.id, result.result.value, result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo(f"[SCHEDULER] Fail-fast: stopping after {result.result.value} result")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        # --- Post-amp: sync main and evaluate ---
        click.echo(f"[AMP] {issue.id} agent finished — processing result ...")

        # Sync repo root to latest main (agent should have merged+pushed)
        click.echo(f"[SYNC] {issue.id} syncing repo root to {config.base_branch} ...")
        sync_ok, sync_error = _sync_repo_root(repo_root, config.base_branch)
        if not sync_ok:
            click.echo(f"[SYNC] {issue.id} WARNING: sync failed: {sync_error}")
            events.record(EventType.error, {
                "issue_id": issue.id,
                "stage": "post_merge_sync",
                "error": sync_error,
            })

            # --- Merge recovery: launch a rush-mode agent to land the work ---
            click.echo(f"[MERGE-RECOVERY] {issue.id} launching agent to land work (mode=rush) ...")
            _update_checkpoint(store, state_dir, state, RunStage.merge_recovery, events=events)
            events.record(EventType.merge_recovery_started, {"issue_id": issue.id})

            from orc.amp_runner import RealAmpRunner

            recovery_ok, recovery_msg = RealAmpRunner.run_merge_recovery(
                issue_id=issue.id,
                thread_id=result.thread_id,
                worktree_path=wt_info.worktree_path,
                repo_root=repo_root,
                base_branch=config.base_branch,
            )
            events.record(EventType.merge_recovery_finished, {
                "issue_id": issue.id,
                "success": recovery_ok,
                "summary": recovery_msg,
            })

            if recovery_ok:
                click.echo(f"[MERGE-RECOVERY] {issue.id} agent finished — re-syncing ...")
                sync_ok, sync_error = _sync_repo_root(repo_root, config.base_branch)

            if not sync_ok:
                # Recovery exhausted — reopen the issue and stop orc
                click.echo(f"[MERGE-RECOVERY] {issue.id} FAILED: merge not landed after recovery")
                reopen_issue(issue.id, cwd=repo_root)
                click.echo(f"[MERGE-RECOVERY] {issue.id} reopened — stopping orchestrator")
                _record_failure(
                    store, state_dir, state, issue.id, FailureCategory.merge_exhausted,
                    WorkflowPhase.merge_recovery.value,
                    f"merge recovery exhausted: {sync_error}", wt_info.branch_name, wt_path,
                    extra=ctx_extra or None,
                )
                _unclaim_active(state, events, repo_root)
                _clear_active(store, state_dir, state)
                _record_run(store, state_dir, state, issue.id, "failed",
                            f"merge recovery exhausted: {sync_error}",
                            wt_info.branch_name, wt_path, amp_mode=config.amp_mode,
                            extra=ctx_extra or None)
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {
                    "to": "idle", "reason": "merge_recovery_exhausted",
                })
                return

            click.echo(f"[MERGE-RECOVERY] {issue.id} sync OK after recovery")

        # Check for pause/stop before evaluation
        if _check_stop_at_safe_point(store, state_dir, state, events, repo_root, "before_eval"):
            return

        # Post-merge evaluation on main
        if evaluator is not None:
            _update_checkpoint(store, state_dir, state, RunStage.post_merge_eval, events=events)
            _eval_mode = config.evaluation_mode or config.amp_mode
            events.record(EventType.evaluation_started, {"issue_id": issue.id, "mode": _eval_mode})
            click.echo(f"[EVAL] {issue.id} running post-merge evaluation on {config.base_branch} (mode={_eval_mode}) ...")

            try:
                eval_result = evaluator.evaluate(
                    context=ctx,
                    base_branch=config.base_branch,
                    verification_commands=config.verification_commands,
                )
            except Exception as exc:
                from orc.evaluator import EvaluationResult
                eval_result = EvaluationResult.fail(f"Evaluator crashed: {exc}")

            eval_finished_data: dict = {
                "issue_id": issue.id,
                "verdict": eval_result.verdict.value,
                "summary": eval_result.summary,
                "task_too_large_signal": eval_result.task_too_large_signal,
            }
            if eval_result.context_window_usage_pct is not None:
                eval_finished_data["context_window_usage_pct"] = eval_result.context_window_usage_pct
            events.record(EventType.evaluation_finished, eval_finished_data)

            if eval_result.passed:
                click.echo(f"[EVAL] {issue.id} PASSED: {eval_result.summary}")
            else:
                click.echo(f"[EVAL] {issue.id} FAILED: {eval_result.summary}")
                events.record(EventType.issue_needs_rework, {
                    "issue_id": issue.id,
                    "summary": eval_result.summary,
                })
                # Create follow-up issue instead of holding the original
                followup_id = _create_followup_issue(
                    original_issue_id=issue.id,
                    original_title=issue.title,
                    original_description=issue.description,
                    original_acceptance_criteria=issue.acceptance_criteria,
                    eval_summary=eval_result.summary,
                    eval_gaps=eval_result.gaps,
                    repo_root=repo_root,
                    state=state,
                    store=store,
                    state_dir=state_dir,
                    events=events,
                )
                if followup_id is None:
                    # Follow-up creation failed — pause orchestrator
                    click.echo("[SCHEDULER] Pausing: follow-up issue creation failed")
                    _unclaim_active(state, events, repo_root)
                    _clear_active(store, state_dir, state)
                    _record_run(
                        store, state_dir, state, issue.id, "followup_failed",
                        eval_result.summary, wt_info.branch_name, wt_path,
                        amp_mode=config.amp_mode, extra=ctx_extra or None,
                    )
                    store.transition(state, OrchestratorMode.paused)
                    events.record(EventType.state_changed, {"to": "paused", "reason": "followup_creation_failed"})
                    return

                _unclaim_active(state, events, repo_root)
                _clear_active(store, state_dir, state)
                _record_run(
                    store, state_dir, state, issue.id, "completed_with_followup",
                    eval_result.summary, wt_info.branch_name, wt_path,
                    amp_mode=config.amp_mode,
                    extra={**(ctx_extra or {}), "followup_issue_id": followup_id,
                           "evaluation": eval_result.to_dict()},
                )
                _try_cleanup(worktree_mgr, wt_info)
                if fail_fast:
                    click.echo("[SCHEDULER] Fail-fast: stopping after eval failure with follow-up")
                    store.transition(state, OrchestratorMode.idle)
                    events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                    return
                continue

        # Success path
        click.echo(f"[COMPLETE] {issue.id} OK")
        events.record(EventType.issue_closed, {"issue_id": issue.id})
        state.last_completed_issue = issue.id
        state.issue_failures.pop(issue.id, None)
        _clear_active(store, state_dir, state)
        _record_run(store, state_dir, state, issue.id, "completed", result.summary,
                    wt_info.branch_name, wt_path, amp_mode=config.amp_mode,
                    extra=ctx_extra or None)
        _try_cleanup(worktree_mgr, wt_info)
        _check_parent_promotion(issue.id, repo_root, store, state_dir, state, events)

        if only_issue:
            click.echo("[SCHEDULER] --only: single issue processed — stopping")
            state = store.load()
            if state.mode == OrchestratorMode.running:
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "only_issue_done"})
            return


def _check_parent_promotion(
    issue_id: str,
    repo_root: Path,
    store: StateStore,
    state_dir: Path,
    state: OrchestratorState,
    events: EventLog,
) -> None:
    """After closing *issue_id*, auto-close its parent if all children are done.

    When all children of a decomposed parent are closed, the parent is
    automatically closed and any stale local holds are cleared.  This
    implements inline decomposition: the parent stays in the queue behind
    its children and is resolved without a separate held/blocked list.
    """
    parent_id = get_issue_parent(issue_id, cwd=repo_root)
    if not parent_id:
        return

    all_closed = get_children_all_closed(parent_id, cwd=repo_root)
    if not all_closed:
        return

    # Clear any stale local hold (may exist from legacy state).
    if parent_id in state.issue_failures:
        state.issue_failures.pop(parent_id, None)

    # Auto-close the parent now that all children are done.
    parent_status = get_issue_status(parent_id, cwd=repo_root)
    if parent_status != "closed":
        if close_issue(parent_id, cwd=repo_root):
            click.echo(f"[PARENT] {parent_id} all children closed — auto-closed")
            events.record(EventType.issue_closed, {"issue_id": parent_id, "auto_closed": True})
        else:
            click.echo(f"[PARENT] {parent_id} all children closed — close failed")
            events.record(EventType.error, {"issue_id": parent_id, "stage": "parent_auto_close", "error": "bd close failed"})

    _save_with_requests(store, state, state_dir)


def _clear_active(store: StateStore, state_dir: Path, state: OrchestratorState) -> None:
    """Clear active run checkpoint and save."""
    state.active_run = None
    _save_with_requests(store, state, state_dir)


def _unclaim_active(
    state: OrchestratorState,
    events: EventLog,
    repo_root: Path,
) -> None:
    """Release the bd claim for the active run if one was acquired.

    Checks issue status first — does not reopen a closed issue.
    """
    if state.active_run and state.active_run.get("bd_claimed"):
        issue_id = state.active_run["issue_id"]
        # Don't reopen a closed issue
        status = get_issue_status(issue_id, cwd=repo_root)
        if status == "closed":
            state.active_run["bd_claimed"] = False
            return
        if unclaim_issue(issue_id, cwd=repo_root):
            state.active_run["bd_claimed"] = False
        else:
            events.record(EventType.error, {
                "issue_id": issue_id,
                "stage": "unclaim",
                "error": "unclaim_issue failed",
            })


def _record_run(
    store: StateStore,
    state_dir: Path,
    state: OrchestratorState,
    issue_id: str,
    result: str,
    summary: str,
    branch: str | None = None,
    worktree_path: str | None = None,
    amp_mode: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append a run record to history and save."""
    entry: dict = {
        "issue_id": issue_id,
        "result": result,
        "summary": summary,
        "timestamp": _now_iso(),
    }
    if branch:
        entry["branch"] = branch
    if worktree_path:
        entry["worktree_path"] = worktree_path
    if amp_mode:
        entry["amp_mode"] = amp_mode
    if extra:
        entry.update(extra)
    state.run_history.append(entry)
    _save_with_requests(store, state, state_dir)


def _try_cleanup(worktree_mgr: WorktreeManager, wt_info) -> None:
    """Best-effort worktree cleanup."""
    try:
        worktree_mgr.cleanup_worktree(wt_info)
    except Exception:
        logger.debug("Worktree cleanup failed for %s", wt_info, exc_info=True)
