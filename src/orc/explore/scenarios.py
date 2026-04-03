"""Declarative scenario registry for the Beads dispatch exploration harness."""

from __future__ import annotations

from orc.explore.models import IssueSpec, ScenarioDefinition, ScenarioExpectation


def _scenario_1() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="simple-independent-workers",
        description="Three standalone worker issues with no parents or blockers.",
        issues=(
            IssueSpec(key="A", title="Task A"),
            IssueSpec(key="B", title="Task B"),
            IssueSpec(key="C", title="Task C"),
        ),
        hypotheses=(
            "Beads should return all three worker issues in its default ready order.",
            "Orc should dispatch the same worker set without reordering it.",
        ),
        expectations=ScenarioExpectation(
            ready_contains=("A", "B", "C"),
            dispatch_contains=("A", "B", "C"),
            dispatch_preserves_ready_order=True,
        ),
    )


def _scenario_2() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="simple-blocker-chain",
        description="A linear task chain where only the head should be ready.",
        issues=(
            IssueSpec(key="A", title="Task A"),
            IssueSpec(key="B", title="Task B", blockers=("A",)),
            IssueSpec(key="C", title="Task C", blockers=("B",)),
        ),
        hypotheses=(
            "Only the unblocked head of the chain should appear in bd ready.",
            "Orc should dispatch only the head task under V0.",
        ),
        expectations=ScenarioExpectation(
            ready_contains=("A",),
            ready_excludes=("B", "C"),
            dispatch_contains=("A",),
            dispatch_excludes=("B", "C"),
            dispatch_preserves_ready_order=True,
        ),
    )


def _scenario_3() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="open-epic-with-children",
        description="An open epic with child tasks and no explicit blockers.",
        issues=(
            IssueSpec(key="E", title="Epic E", issue_type="epic"),
            IssueSpec(key="E.1", title="Task E.1", parent="E"),
            IssueSpec(key="E.2", title="Task E.2", parent="E"),
        ),
        hypotheses=(
            "The epic may appear in bd ready alongside its child tasks.",
            "Orc should treat the epic as control-only and dispatch only worker descendants.",
        ),
        expectations=ScenarioExpectation(
            ready_contains=("E", "E.1", "E.2"),
            dispatch_contains=("E.1", "E.2"),
            dispatch_excludes=("E",),
            dispatch_preserves_ready_order=True,
        ),
    )


def _scenario_4() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="epic-with-ordered-children",
        description="An epic whose child tasks form a blocker chain.",
        issues=(
            IssueSpec(key="E", title="Epic E", issue_type="epic"),
            IssueSpec(key="E.1", title="Task E.1", parent="E"),
            IssueSpec(key="E.2", title="Task E.2", parent="E", blockers=("E.1",)),
            IssueSpec(key="E.3", title="Task E.3", parent="E", blockers=("E.2",)),
        ),
        hypotheses=(
            "Only the head child should be ready among the blocked siblings.",
            "Orc should dispatch only the head child while leaving the epic nondispatchable.",
        ),
        expectations=ScenarioExpectation(
            ready_contains=("E", "E.1"),
            ready_excludes=("E.2", "E.3"),
            dispatch_contains=("E.1",),
            dispatch_excludes=("E", "E.2", "E.3"),
            dispatch_preserves_ready_order=True,
        ),
    )


def _scenario_5() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="blocked-parent-suppresses-children",
        description="An epic blocked by another epic, with child tasks under the blocked parent.",
        issues=(
            IssueSpec(key="Blocker", title="Blocking epic", issue_type="epic"),
            IssueSpec(key="E", title="Blocked epic", issue_type="epic", blockers=("Blocker",)),
            IssueSpec(key="E.1", title="Task E.1", parent="E"),
            IssueSpec(key="E.2", title="Task E.2", parent="E"),
        ),
        hypotheses=(
            "Children of a blocked parent should be suppressed from bd ready.",
            "Orc should not derive any dispatchable frontier from the blocked subtree.",
        ),
        expectations=ScenarioExpectation(
            ready_excludes=("E", "E.1", "E.2"),
            dispatch_excludes=("E", "E.1", "E.2"),
        ),
    )


def _scenario_6() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="deferred-parent-suppresses-children",
        description="An epic deferred into the future with child tasks under it.",
        issues=(
            IssueSpec(key="E", title="Deferred epic", issue_type="epic", defer_until="+1d"),
            IssueSpec(key="E.1", title="Task E.1", parent="E"),
            IssueSpec(key="E.2", title="Task E.2", parent="E"),
        ),
        hypotheses=(
            "Children of a deferred parent should be excluded from bd ready.",
            "Orc should produce no local frontier for a deferred container subtree.",
        ),
        expectations=ScenarioExpectation(
            ready_excludes=("E", "E.1", "E.2"),
            dispatch_excludes=("E", "E.1", "E.2"),
        ),
    )


def _scenario_7() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="nested-integration-container",
        description="A nested custom integration node under an epic with its own children.",
        issues=(
            IssueSpec(key="P", title="Parent epic", issue_type="epic"),
            IssueSpec(key="I", title="Integration node", issue_type="integration", parent="P"),
            IssueSpec(key="I.1", title="Task I.1", parent="I"),
            IssueSpec(key="I.2", title="Task I.2", parent="I"),
            IssueSpec(key="I.3", title="Task I.3", parent="I"),
            IssueSpec(key="P.1", title="Task P.1", parent="P"),
            IssueSpec(key="P.2", title="Task P.2", parent="P"),
        ),
        hypotheses=(
            "A custom integration type can act as a parent/container in Beads.",
            "Once Orc classifies integration as a container, it should leave the node nondispatchable and keep its ready worker descendants in Beads order.",
        ),
        expectations=ScenarioExpectation(
            dispatch_contains=("I.1", "I.2", "I.3", "P.1", "P.2"),
            dispatch_excludes=("P", "I"),
            dispatch_preserves_ready_order=True,
        ),
    )


def _scenario_8() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="unknown-custom-type-with-children",
        description="A custom unsupported type with a child task under an epic.",
        issues=(
            IssueSpec(key="P", title="Parent epic", issue_type="epic"),
            IssueSpec(key="X", title="Unknown custom node", issue_type="mystery", parent="P"),
            IssueSpec(key="X.1", title="Task X.1", parent="X"),
        ),
        hypotheses=(
            "The harness should surface an actionable unsupported-type error.",
            "Orc should suppress the entire subtree until that type is classified, even if ready descendants appear earlier than the unsupported node in bd ready.",
        ),
        expectations=ScenarioExpectation(
            invalid_due_to_types=("mystery",),
            dispatch_excludes=("X", "X.1"),
        ),
    )


def _scenario_9() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="mixed-priority-hybrid-ordering",
        description="A mix of priorities, creation order, standalone work, and epic-contained work.",
        issues=(
            IssueSpec(key="Old", title="Old low-priority task", priority=4, create_delay_seconds=1.0),
            IssueSpec(key="Urgent", title="Urgent task", priority=1, create_delay_seconds=1.0),
            IssueSpec(key="Epic", title="Priority epic", issue_type="epic", priority=2),
            IssueSpec(key="Epic.1", title="Epic child", parent="Epic", priority=3),
            IssueSpec(key="Feature", title="Standalone feature", issue_type="feature", priority=2),
        ),
        hypotheses=(
            "Beads default ready ordering should remain stable and inspectable under mixed priorities.",
            "Orc should preserve the observed Beads ordering while filtering out nondispatchable containers.",
        ),
        expectations=ScenarioExpectation(
            ready_contains=("Old", "Urgent", "Epic", "Epic.1", "Feature"),
            dispatch_contains=("Old", "Urgent", "Epic.1", "Feature"),
            dispatch_excludes=("Epic",),
            dispatch_preserves_ready_order=True,
        ),
    )


def _scenario_10() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="in-progress-issue-in-ready-check",
        description="One task is already in progress while a sibling task remains open.",
        issues=(
            IssueSpec(key="Doing", title="Already in progress", status="in_progress"),
            IssueSpec(key="Open", title="Still open"),
        ),
        hypotheses=(
            "Current Beads versions should exclude in_progress items from bd ready.",
            "If Beads ever includes them, Orc should observe them but not re-dispatch them by default.",
        ),
        expectations=ScenarioExpectation(
            ready_contains=("Open",),
            ready_excludes=("Doing",),
            dispatch_contains=("Open",),
            dispatch_excludes=("Doing",),
            dispatch_preserves_ready_order=True,
        ),
    )


def get_scenarios() -> dict[str, ScenarioDefinition]:
    """Return the full named scenario registry."""

    scenarios = [
        _scenario_1(),
        _scenario_2(),
        _scenario_3(),
        _scenario_4(),
        _scenario_5(),
        _scenario_6(),
        _scenario_7(),
        _scenario_8(),
        _scenario_9(),
        _scenario_10(),
    ]
    return {scenario.name: scenario for scenario in scenarios}
