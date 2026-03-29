"""Core scheduler loop for amp-orchestrator."""

from __future__ import annotations

from pathlib import Path

import click

from amp_orchestrator.amp_runner import AmpRunner, IssueContext, ResultType
from amp_orchestrator.config import OrchestratorConfig
from amp_orchestrator.events import EventLog, EventType
from amp_orchestrator.merge import verify_and_merge
from amp_orchestrator.queue import get_ready_issues, select_next_issue
from amp_orchestrator.state import OrchestratorMode, OrchestratorState, StateStore
from amp_orchestrator.worktree import WorktreeManager


def run_loop(
    repo_root: Path,
    state_dir: Path,
    config: OrchestratorConfig,
    runner: AmpRunner,
) -> None:
    """Run the main scheduler loop until the queue is empty or stopped."""
    store = StateStore(state_dir)
    events = EventLog(state_dir)
    worktree_mgr = WorktreeManager(repo_root, config.base_branch)
    failed_ids: set[str] = set()

    while True:
        state = store.load()

        if state.mode in (
            OrchestratorMode.pause_requested,
            OrchestratorMode.stopping,
        ):
            if state.mode == OrchestratorMode.pause_requested:
                store.transition(state, OrchestratorMode.paused)
                events.record(EventType.state_changed, {"to": "paused"})
                click.echo("Paused.")
            else:
                store.transition(state, OrchestratorMode.idle)
                events.record(EventType.state_changed, {"to": "idle"})
                click.echo("Stopped.")
            return

        if state.mode != OrchestratorMode.running:
            return

        # Select next issue
        ready = get_ready_issues(repo_root)
        issue = select_next_issue(ready, skip_ids=failed_ids)

        if issue is None:
            click.echo("No ready issues — queue exhausted.")
            store.transition(state, OrchestratorMode.idle)
            events.record(EventType.state_changed, {"to": "idle", "reason": "queue_empty"})
            return

        events.record(EventType.issue_selected, {"issue_id": issue.id, "title": issue.title})
        click.echo(f"Selected issue: {issue.id} — {issue.title}")

        # Create worktree
        try:
            wt_info = worktree_mgr.create_worktree(issue.id, issue.title)
        except Exception as exc:
            click.echo(f"Failed to create worktree for {issue.id}: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "worktree", "error": str(exc)})
            failed_ids.add(issue.id)
            _record_run(store, state, issue.id, "failed", str(exc))
            continue

        # Update state with active issue
        state.active_issue_id = issue.id
        state.active_branch = wt_info.branch_name
        state.active_worktree_path = str(wt_info.worktree_path)
        store.save(state)

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

        try:
            result = runner.run(ctx)
        except Exception as exc:
            click.echo(f"Amp failed for {issue.id}: {exc}")
            events.record(EventType.error, {"issue_id": issue.id, "stage": "amp", "error": str(exc)})
            failed_ids.add(issue.id)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "failed", str(exc))
            _try_cleanup(worktree_mgr, wt_info)
            continue

        events.record(EventType.amp_finished, {
            "issue_id": issue.id,
            "result": result.result.value,
            "summary": result.summary,
        })
        click.echo(f"Amp result: {result.result.value} — {result.summary}")

        # Handle non-merge outcomes
        if result.result == ResultType.decomposed:
            click.echo(f"Issue {issue.id} was decomposed — skipping merge.")
            _clear_active(store, state)
            _record_run(store, state, issue.id, "decomposed", result.summary, wt_info.branch_name)
            _try_cleanup(worktree_mgr, wt_info)
            continue

        if result.result in (ResultType.failed, ResultType.blocked, ResultType.needs_human):
            click.echo(f"Issue {issue.id}: {result.result.value} — moving on.")
            failed_ids.add(issue.id)
            _clear_active(store, state)
            _record_run(store, state, issue.id, result.result.value, result.summary, wt_info.branch_name)
            _try_cleanup(worktree_mgr, wt_info)
            continue

        if not result.merge_ready:
            click.echo(f"Issue {issue.id}: completed but not merge-ready — skipping merge.")
            failed_ids.add(issue.id)
            _clear_active(store, state)
            _record_run(store, state, issue.id, "completed_no_merge", result.summary, wt_info.branch_name)
            continue

        # Verify and merge
        events.record(EventType.merge_attempt, {"issue_id": issue.id})
        merge_result = verify_and_merge(
            worktree_info=wt_info,
            repo_root=repo_root,
            base_branch=config.base_branch,
            verification_commands=config.verification_commands,
            auto_push=config.auto_push,
            issue_id=issue.id,
        )

        if merge_result.success:
            click.echo(f"Issue {issue.id} merged and closed successfully.")
            events.record(EventType.issue_closed, {"issue_id": issue.id})
            state.last_completed_issue = issue.id
            _clear_active(store, state)
            _record_run(store, state, issue.id, "completed", result.summary, wt_info.branch_name)
            _try_cleanup(worktree_mgr, wt_info)
        else:
            click.echo(f"Merge failed for {issue.id} at stage {merge_result.stage}: {merge_result.error}")
            events.record(EventType.error, {
                "issue_id": issue.id,
                "stage": merge_result.stage,
                "error": merge_result.error,
            })
            failed_ids.add(issue.id)
            state.last_error = f"{issue.id}: merge failed at {merge_result.stage}"
            _clear_active(store, state)
            _record_run(store, state, issue.id, "failed", f"merge failed at {merge_result.stage}", wt_info.branch_name)


def _clear_active(store: StateStore, state: OrchestratorState) -> None:
    """Clear active issue fields and save."""
    state.active_issue_id = None
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
) -> None:
    """Append a run record to history and save."""
    from datetime import datetime, timezone

    entry: dict = {
        "issue_id": issue_id,
        "result": result,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if branch:
        entry["branch"] = branch
    state.run_history.append(entry)
    store.save(state)


def _try_cleanup(worktree_mgr: WorktreeManager, wt_info) -> None:
    """Best-effort worktree cleanup."""
    try:
        worktree_mgr.cleanup_worktree(wt_info)
    except Exception:
        pass
