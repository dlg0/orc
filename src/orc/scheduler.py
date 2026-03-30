"""Core scheduler loop for orc."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import click

from orc.amp_runner import AmpRunner, IssueContext, ResultType
from orc.config import OrchestratorConfig
from orc.evaluator import IssueEvaluator
from orc.events import EventLog, EventType
from orc.merge import _git_status_porcelain, verify_and_merge
from orc.queue import (
    claim_issue,
    get_children_all_closed,
    get_issue_parent,
    get_ready_issues,
    reconcile_issue_failures,
    select_next_issue,
    unclaim_issue,
)
from orc.state import (
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    RunCheckpoint,
    RunStage,
    StateStore,
    _MAX_RESUME_ATTEMPTS,
)
from orc.worktree import WorktreeInfo, WorktreeManager


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_failure(
    store: StateStore,
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
    store.save(state)
    return failure


def _action_for_category(category: FailureCategory) -> FailureAction:
    """Map failure category to the default action."""
    return {
        FailureCategory.transient_external: FailureAction.auto_retry,
        FailureCategory.stale_or_conflicted: FailureAction.hold_for_retry,
        FailureCategory.issue_needs_rework: FailureAction.hold_until_backlog_changes,
        FailureCategory.blocked_by_dependency: FailureAction.hold_until_backlog_changes,
        FailureCategory.fatal_run_error: FailureAction.pause_orchestrator,
    }[category]


def _update_checkpoint(
    store: StateStore,
    state: OrchestratorState,
    stage: RunStage,
    *,
    bd_claimed: bool | None = None,
    amp_result: dict | None = None,
    eval_result: dict | None = None,
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
    store.save(state)


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
        store.save(state)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "no_branch_or_worktree"})
        return False

    if not worktree_mgr.ensure_resumable_worktree(branch, wt_path):
        click.echo(f"[RESUME] {issue_id} worktree/branch not recoverable — discarding")
        # Unclaim if we had a claim
        if candidate.get("bd_claimed"):
            unclaim_issue(issue_id, cwd=repo_root)
        state.resume_candidate = None
        store.save(state)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "worktree_not_recoverable"})
        return False

    # Promote resume_candidate to active_run
    state.active_run = candidate
    state.resume_candidate = None
    store.save(state)

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
        _update_checkpoint(store, state, RunStage.amp_running)

        description = candidate.get("issue_title", "")
        ctx = IssueContext(
            issue_id=issue_id,
            title=candidate.get("issue_title", ""),
            description=description + _RECOVERY_PROMPT,
            acceptance_criteria="",
            worktree_path=wt_info.worktree_path,
            repo_root=repo_root,
        )
        events.record(EventType.amp_started, {"issue_id": issue_id, "recovery": True})
        click.echo(f"[AMP] {issue_id} running (recovery) ...")

        try:
            result = runner.run(ctx)
        except Exception as exc:
            click.echo(f"[RESUME] {issue_id} amp failed during recovery: {exc}")
            _record_failure(
                store, state, issue_id, FailureCategory.issue_needs_rework, "amp",
                str(exc), branch, wt_path_str,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
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
                store, state, issue_id, FailureCategory.issue_needs_rework, "amp",
                result.summary, branch, wt_path_str,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue_id, result.result.value, result.summary,
                        branch, wt_path_str, amp_mode=config.amp_mode)
            events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "not_merge_ready"})
            return True

        _update_checkpoint(
            store, state, RunStage.amp_finished,
            amp_result={"result": result.result.value, "summary": result.summary,
                        "merge_ready": result.merge_ready},
        )

    elif stage == RunStage.amp_finished.value:
        # Amp already finished — check stored result
        amp_result = candidate.get("amp_result", {})
        if not amp_result.get("merge_ready"):
            click.echo(f"[RESUME] {issue_id} amp_finished but not merge-ready — discarding")
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "not_merge_ready"})
            return False
        click.echo(f"[RESUME] {issue_id} skipping amp (already finished with merge-ready)")

    elif stage == RunStage.ready_to_merge.value:
        click.echo(f"[RESUME] {issue_id} skipping amp+eval (ready to merge)")

    else:
        click.echo(f"[RESUME] {issue_id} unknown/non-resumable stage={stage} — discarding")
        _unclaim_active(state, events, repo_root)
        _clear_active(store, state)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "unknown_stage"})
        return False

    # Run evaluation if needed and stage hasn't passed it
    if evaluator is not None and stage != RunStage.ready_to_merge.value:
        _update_checkpoint(store, state, RunStage.evaluation_running)
        events.record(EventType.evaluation_started, {"issue_id": issue_id, "recovery": True})
        click.echo(f"[EVAL] {issue_id} running ...")

        ctx_for_eval = IssueContext(
            issue_id=issue_id,
            title=candidate.get("issue_title", ""),
            description="",
            acceptance_criteria="",
            worktree_path=wt_info.worktree_path,
            repo_root=repo_root,
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
            _update_checkpoint(store, state, RunStage.ready_to_merge, eval_result=eval_result.to_dict())
        else:
            click.echo(f"[EVAL] {issue_id} FAILED: {eval_result.summary}")
            _record_failure(
                store, state, issue_id, FailureCategory.issue_needs_rework, "evaluation",
                eval_result.summary, branch, wt_path_str,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "eval_failed"})
            return True

    # Merge
    _update_checkpoint(store, state, RunStage.merge_running)
    click.echo(f"[MERGE] {issue_id} starting verify-and-merge ...")
    events.record(EventType.merge_attempt, {"issue_id": issue_id, "recovery": True})
    merge_result = verify_and_merge(
        worktree_info=wt_info,
        repo_root=repo_root,
        base_branch=config.base_branch,
        verification_commands=config.verification_commands,
        auto_push=config.auto_push,
        issue_id=issue_id,
        state_dir=state_dir,
    )

    if merge_result.success:
        click.echo(f"[MERGE] {issue_id} OK (recovered)")
        events.record(EventType.issue_closed, {"issue_id": issue_id, "recovery": True})
        events.record(EventType.resume_succeeded, {"issue_id": issue_id})
        state.last_completed_issue = issue_id
        state.issue_failures.pop(issue_id, None)
        _clear_active(store, state)
        _record_run(store, state, issue_id, "completed", "recovered from interrupted run",
                    branch, wt_path_str, amp_mode=config.amp_mode)
        _try_cleanup(worktree_mgr, wt_info)
        _check_parent_promotion(issue_id, repo_root, store, state, events)
    else:
        click.echo(f"[MERGE] {issue_id} FAILED at {merge_result.stage}: {merge_result.error}")
        _record_failure(
            store, state, issue_id, FailureCategory.stale_or_conflicted,
            f"merge/{merge_result.stage}",
            f"merge failed at {merge_result.stage}", branch, wt_path_str,
        )
        _unclaim_active(state, events, repo_root)
        _clear_active(store, state)
        _record_run(store, state, issue_id, "failed",
                    f"merge failed at {merge_result.stage} (recovery)",
                    branch, wt_path_str, amp_mode=config.amp_mode)
        events.record(EventType.resume_failed, {"issue_id": issue_id, "reason": "merge_failed"})

    return True


def run_loop(
    repo_root: Path,
    state_dir: Path,
    config: OrchestratorConfig,
    runner: AmpRunner,
    evaluator: IssueEvaluator | None = None,
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
            pruned = reconcile_issue_failures(state.issue_failures, cwd=repo_root)
            if pruned:
                store.save(state)
                for issue_id, reason in pruned:
                    click.echo(f"[SCHEDULER] Pruned held issue {issue_id} ({reason})")
                    events.record(EventType.issue_failure_pruned, {"issue_id": issue_id, "reason": reason})

        # Derive skip_ids from persisted issue_failures
        failed_ids: set[str] = set(state.issue_failures.keys())

        # Select next issue — with queue-failure retry
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

        # If a parent was promoted, prioritize it in selection
        priority_id = state.promoted_parent
        issue = select_next_issue(queue_result.issues, skip_ids=failed_ids, priority_id=priority_id)
        # Clear promoted_parent after selection attempt (one-shot)
        if state.promoted_parent:
            state.promoted_parent = None
            store.save(state)

        if issue is None:
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
        events.record(EventType.issue_selected, {"issue_id": issue.id, "title": issue.title})
        click.echo("")
        click.echo(_ISSUE_DIVIDER)
        click.echo(f"[SELECT] #{issue_num} {issue.id} -- {issue.title}")
        click.echo(_ISSUE_DIVIDER)

        # Create worktree
        try:
            wt_info = worktree_mgr.create_worktree(issue.id, issue.title)
        except Exception as exc:
            click.echo(f"[WORKTREE] {issue.id} FAILED: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "worktree", "error": str(exc)})
            wt_category = FailureCategory.transient_external if isinstance(exc, OSError) else FailureCategory.fatal_run_error
            _record_failure(store, state, issue.id, wt_category, "worktree", str(exc))
            _record_run(store, state, issue.id, "failed", str(exc), amp_mode=config.amp_mode)
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

        # Update state with active run checkpoint
        checkpoint = RunCheckpoint(
            issue_id=issue.id,
            issue_title=issue.title,
            branch=wt_info.branch_name,
            worktree_path=str(wt_info.worktree_path),
            stage=RunStage.worktree_created,
            amp_log_path=str(amp_log_path),
            updated_at=_now_iso(),
        )
        state.active_run = checkpoint.to_dict()
        store.save(state)

        # Claim the issue in bd so it shows as in-progress
        claimed = claim_issue(issue.id, cwd=repo_root)
        if not claimed:
            click.echo(f"[CLAIM] {issue.id} WARNING: bd update --claim failed (continuing)")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "claim", "error": "bd update --claim failed"})

        _update_checkpoint(store, state, RunStage.claimed, bd_claimed=claimed)

        # Invoke Amp
        _update_checkpoint(store, state, RunStage.amp_running)
        ctx = IssueContext(
            issue_id=issue.id,
            title=issue.title,
            description=issue.description,
            acceptance_criteria=issue.acceptance_criteria,
            worktree_path=wt_info.worktree_path,
            repo_root=repo_root,
        )
        events.record(EventType.amp_started, {"issue_id": issue.id})
        click.echo(f"[AMP] {issue.id} running ...")
        click.echo(f"[AMP] {issue.id} log={amp_log_path}")

        try:
            result = runner.run(ctx, log_path=amp_log_path)
        except Exception as exc:
            click.echo(f"[AMP] {issue.id} FAILED: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "amp", "error": str(exc)})
            _record_failure(
                store, state, issue.id, FailureCategory.issue_needs_rework, "amp",
                str(exc), wt_info.branch_name, str(wt_info.worktree_path),
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "failed", str(exc), worktree_path=str(wt_info.worktree_path), amp_mode=config.amp_mode)
            _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after amp failure")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        _update_checkpoint(
            store, state, RunStage.amp_finished,
            amp_result={"result": result.result.value, "summary": result.summary,
                        "merge_ready": result.merge_ready},
        )

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

            click.echo(f"[SUMMARY] {issue.id} extracting rush summary ...")
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
            _record_failure(
                store, state, issue.id, FailureCategory.blocked_by_dependency, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "decomposed", result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
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
                store, state, issue.id, FailureCategory.blocked_by_dependency, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue.id, result.result.value, result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
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
                store, state, issue.id, FailureCategory.issue_needs_rework, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue.id, result.result.value, result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo(f"[SCHEDULER] Fail-fast: stopping after {result.result.value} result")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        if not result.merge_ready:
            click.echo(f"[AMP] {issue.id} completed but not merge-ready -- skipping merge")
            _record_failure(
                store, state, issue.id, FailureCategory.issue_needs_rework, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "completed_no_merge", result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after completed_no_merge result")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return
            continue

        # Enforce clean worktree before merge/evaluation
        if config.require_clean_worktree:
            dirty = _git_status_porcelain(wt_info.worktree_path)
            if dirty:
                click.echo(f"[WORKTREE] {issue.id} dirty after amp -- attempting finalize commit")
                _finalize_dirty_worktree(wt_info.worktree_path, issue.id)
                dirty = _git_status_porcelain(wt_info.worktree_path)
            if dirty:
                click.echo(f"[WORKTREE] {issue.id} still dirty after finalize:\n{dirty[:300]}")
                _record_failure(
                    store, state, issue.id, FailureCategory.issue_needs_rework, "worktree_dirty",
                    f"worktree not clean: {dirty[:200]}", wt_info.branch_name, wt_path,
                )
                _unclaim_active(state, events, repo_root)
                _clear_active(store, state)
                _record_run(store, state, issue.id, "failed", f"worktree not clean: {dirty[:200]}", wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
                if fail_fast:
                    click.echo("[SCHEDULER] Fail-fast: stopping after dirty worktree")
                    store.transition(state, OrchestratorMode.idle)
                    events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                    return
                continue

        # Independent evaluation
        if evaluator is not None:
            _update_checkpoint(store, state, RunStage.evaluation_running)
            events.record(EventType.evaluation_started, {"issue_id": issue.id})
            click.echo(f"[EVAL] {issue.id} running ...")

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
                _update_checkpoint(
                    store, state, RunStage.ready_to_merge,
                    eval_result=eval_result.to_dict(),
                )
            else:
                click.echo(f"[EVAL] {issue.id} FAILED: {eval_result.summary}")
                events.record(EventType.issue_needs_rework, {
                    "issue_id": issue.id,
                    "summary": eval_result.summary,
                })
                _record_failure(
                    store, state, issue.id, FailureCategory.issue_needs_rework, "evaluation",
                    eval_result.summary, wt_info.branch_name, wt_path,
                )
                _unclaim_active(state, events, repo_root)
                _clear_active(store, state)
                rework_extra = {"evaluation": eval_result.to_dict()}
                rework_extra.update(ctx_extra)
                _record_run(
                    store, state, issue.id, "needs_rework", eval_result.summary,
                    wt_info.branch_name, wt_path, amp_mode=config.amp_mode,
                    extra=rework_extra,
                )
                if fail_fast:
                    click.echo("[SCHEDULER] Fail-fast: stopping after evaluation failure")
                    store.transition(state, OrchestratorMode.idle)
                    events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                    return
                continue

        # Verify and merge
        _update_checkpoint(store, state, RunStage.merge_running)
        click.echo(f"[MERGE] {issue.id} starting verify-and-merge ...")
        events.record(EventType.merge_attempt, {"issue_id": issue.id})
        merge_result = verify_and_merge(
            worktree_info=wt_info,
            repo_root=repo_root,
            base_branch=config.base_branch,
            verification_commands=config.verification_commands,
            auto_push=config.auto_push,
            issue_id=issue.id,
            state_dir=state_dir,
        )

        if merge_result.success:
            if merge_result.conflict_resolved:
                click.echo(f"[MERGE] {issue.id} OK (conflicts auto-resolved)")
            else:
                click.echo(f"[MERGE] {issue.id} OK")
            events.record(EventType.issue_closed, {"issue_id": issue.id})
            state.last_completed_issue = issue.id
            # Clear failure record on success
            state.issue_failures.pop(issue.id, None)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "completed", result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            _check_parent_promotion(issue.id, repo_root, store, state, events)
        else:
            click.echo(f"[MERGE] {issue.id} FAILED at {merge_result.stage}: {merge_result.error}")
            events.record(EventType.error, {
                "issue_id": issue.id,
                "stage": merge_result.stage,
                "error": merge_result.error,
            })
            is_conflict = merge_result.stage in ("rebase", "merge") and merge_result.error and "conflict" in merge_result.error.lower()
            merge_category = FailureCategory.stale_or_conflicted if is_conflict else FailureCategory.fatal_run_error
            preserve = is_conflict
            _record_failure(
                store, state, issue.id, merge_category, f"merge/{merge_result.stage}",
                f"merge failed at {merge_result.stage}", wt_info.branch_name, wt_path,
                preserve_worktree=preserve,
            )
            state.last_error = f"{issue.id}: merge failed at {merge_result.stage}"
            _unclaim_active(state, events, repo_root)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "failed", f"merge failed at {merge_result.stage}", wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            # Preserve worktree for conflict failures
            if not preserve:
                _try_cleanup(worktree_mgr, wt_info)
            if fail_fast:
                click.echo("[SCHEDULER] Fail-fast: stopping after merge failure")
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle", "reason": "fail_fast"})
                return

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
    state: OrchestratorState,
    events: EventLog,
) -> None:
    """After closing *issue_id*, check if its parent should be promoted.

    If *issue_id* has a parent and all siblings are now closed,
    set ``state.promoted_parent`` so the next queue selection prioritizes it.
    """
    parent_id = get_issue_parent(issue_id, cwd=repo_root)
    if not parent_id:
        return

    all_closed = get_children_all_closed(parent_id, cwd=repo_root)
    if all_closed:
        click.echo(f"[PROMOTE] {parent_id} all children closed — promoting parent")
        state.promoted_parent = parent_id
        # Clear any failure record so the parent is not skipped
        state.issue_failures.pop(parent_id, None)
        store.save(state)
        events.record(EventType.parent_promoted, {
            "parent_id": parent_id,
            "triggered_by": issue_id,
        })


def _clear_active(store: StateStore, state: OrchestratorState) -> None:
    """Clear active run checkpoint and save."""
    state.active_run = None
    store.save(state)


def _unclaim_active(
    state: OrchestratorState,
    events: EventLog,
    repo_root: Path,
) -> None:
    """Release the bd claim for the active run if one was acquired."""
    if state.active_run and state.active_run.get("bd_claimed"):
        issue_id = state.active_run["issue_id"]
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
    store.save(state)


def _try_cleanup(worktree_mgr: WorktreeManager, wt_info) -> None:
    """Best-effort worktree cleanup."""
    try:
        worktree_mgr.cleanup_worktree(wt_info)
    except Exception:
        pass


def _finalize_dirty_worktree(worktree_path: Path, issue_id: str) -> None:
    """Commit all uncommitted changes left by amp in the worktree.

    This handles the common case where amp leaves modified/untracked files
    (e.g. regenerated lockfiles) without committing them.
    """
    import subprocess

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore: commit uncommitted changes for {issue_id}"],
            cwd=worktree_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # If commit fails (e.g. nothing to commit after add), leave as-is
        pass
