"""Core scheduler loop for amp-orchestrator."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import click

from amp_orchestrator.amp_runner import AmpRunner, IssueContext, ResultType
from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.evaluator import IssueEvaluator
from amp_orchestrator.events import EventLog, EventType
from amp_orchestrator.merge import verify_and_merge
from amp_orchestrator.queue import claim_issue, get_ready_issues, select_next_issue
from amp_orchestrator.state import (
    FailureAction,
    FailureCategory,
    IssueFailure,
    OrchestratorMode,
    OrchestratorState,
    StateStore,
)
from amp_orchestrator.worktree import WorktreeManager


_ISSUE_DIVIDER = "-" * 60
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


def run_loop(
    repo_root: Path,
    state_dir: Path,
    config: OrchestratorConfig,
    runner: AmpRunner,
    evaluator: IssueEvaluator | None = None,
) -> None:
    """Run the main scheduler loop until the queue is empty or stopped."""
    store = StateStore(state_dir)
    events = EventLog(state_dir)
    worktree_mgr = WorktreeManager(repo_root, config.base_branch)
    state = store.load()
    issue_num = 0

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

        issue = select_next_issue(queue_result.issues, skip_ids=failed_ids)

        if issue is None:
            click.echo("[SCHEDULER] No ready issues -- queue exhausted.")
            store.transition(state, OrchestratorMode.idle)
            events.record(EventType.state_changed, {"to": "idle", "reason": "queue_empty"})
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
            continue

        click.echo(f"[WORKTREE] {issue.id} branch={wt_info.branch_name}")
        click.echo(f"[WORKTREE] {issue.id} path={wt_info.worktree_path}")

        # Update state with active issue
        state.active_issue_id = issue.id
        state.active_issue_title = issue.title
        state.active_branch = wt_info.branch_name
        state.active_worktree_path = str(wt_info.worktree_path)
        store.save(state)

        # Claim the issue in bd so it shows as in-progress
        if not claim_issue(issue.id, cwd=repo_root):
            click.echo(f"[CLAIM] {issue.id} WARNING: bd update --claim failed (continuing)")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "claim", "error": "bd update --claim failed"})

        # Invoke Amp
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

        try:
            result = runner.run(ctx)
        except Exception as exc:
            click.echo(f"[AMP] {issue.id} FAILED: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "amp", "error": str(exc)})
            _record_failure(
                store, state, issue.id, FailureCategory.issue_needs_rework, "amp",
                str(exc), wt_info.branch_name, str(wt_info.worktree_path),
            )
            _clear_active(store, state)
            _record_run(store, state, issue.id, "failed", str(exc), worktree_path=str(wt_info.worktree_path), amp_mode=config.amp_mode)
            _try_cleanup(worktree_mgr, wt_info)
            continue

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
            from amp_orchestrator.amp_runner import RealAmpRunner

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

        # Build extra dict with context usage and thread ID if available
        ctx_extra: dict = {}
        if result.context_window_usage_pct is not None:
            ctx_extra["context_window_usage_pct"] = result.context_window_usage_pct
        if result.thread_id:
            ctx_extra["thread_id"] = result.thread_id

        # Handle non-merge outcomes
        if result.result == ResultType.decomposed:
            click.echo(f"[AMP] {issue.id} decomposed -- skipping merge")
            _record_failure(
                store, state, issue.id, FailureCategory.blocked_by_dependency, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _clear_active(store, state)
            _record_run(store, state, issue.id, "decomposed", result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            continue

        if result.result == ResultType.blocked:
            click.echo(f"[AMP] {issue.id} {result.result.value} -- moving on")
            _record_failure(
                store, state, issue.id, FailureCategory.blocked_by_dependency, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _clear_active(store, state)
            _record_run(store, state, issue.id, result.result.value, result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            continue

        if result.result in (ResultType.failed, ResultType.needs_human):
            click.echo(f"[AMP] {issue.id} {result.result.value} -- moving on")
            _record_failure(
                store, state, issue.id, FailureCategory.issue_needs_rework, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _clear_active(store, state)
            _record_run(store, state, issue.id, result.result.value, result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            _try_cleanup(worktree_mgr, wt_info)
            continue

        if not result.merge_ready:
            click.echo(f"[AMP] {issue.id} completed but not merge-ready -- skipping merge")
            _record_failure(
                store, state, issue.id, FailureCategory.issue_needs_rework, "amp",
                result.summary, wt_info.branch_name, wt_path,
            )
            _clear_active(store, state)
            _record_run(store, state, issue.id, "completed_no_merge", result.summary, wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            continue

        # Independent evaluation
        if evaluator is not None:
            events.record(EventType.evaluation_started, {"issue_id": issue.id})
            click.echo(f"[EVAL] {issue.id} running ...")

            try:
                eval_result = evaluator.evaluate(
                    context=ctx,
                    base_branch=config.base_branch,
                    verification_commands=config.verification_commands,
                )
            except Exception as exc:
                from amp_orchestrator.evaluator import EvaluationResult
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
                _record_failure(
                    store, state, issue.id, FailureCategory.issue_needs_rework, "evaluation",
                    eval_result.summary, wt_info.branch_name, wt_path,
                )
                _clear_active(store, state)
                rework_extra = {"evaluation": eval_result.to_dict()}
                rework_extra.update(ctx_extra)
                _record_run(
                    store, state, issue.id, "needs_rework", eval_result.summary,
                    wt_info.branch_name, wt_path, amp_mode=config.amp_mode,
                    extra=rework_extra,
                )
                continue

        # Verify and merge
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
        else:
            click.echo(f"[MERGE] {issue.id} FAILED at {merge_result.stage}: {merge_result.error}")
            events.record(EventType.error, {
                "issue_id": issue.id,
                "stage": merge_result.stage,
                "error": merge_result.error,
            })
            is_conflict = merge_result.stage in ("rebase", "merge") and merge_result.error and "conflict" in merge_result.error.lower()
            merge_category = FailureCategory.stale_or_conflicted if is_conflict else FailureCategory.stale_or_conflicted
            preserve = is_conflict
            _record_failure(
                store, state, issue.id, merge_category, f"merge/{merge_result.stage}",
                f"merge failed at {merge_result.stage}", wt_info.branch_name, wt_path,
                preserve_worktree=preserve,
            )
            state.last_error = f"{issue.id}: merge failed at {merge_result.stage}"
            _clear_active(store, state)
            _record_run(store, state, issue.id, "failed", f"merge failed at {merge_result.stage}", wt_info.branch_name, wt_path, amp_mode=config.amp_mode, extra=ctx_extra or None)
            # Preserve worktree for conflict failures
            if not preserve:
                _try_cleanup(worktree_mgr, wt_info)


def _clear_active(store: StateStore, state: OrchestratorState) -> None:
    """Clear active issue fields and save."""
    state.active_issue_id = None
    state.active_issue_title = None
    state.active_branch = None
    state.active_worktree_path = None
    store.save(state)


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
