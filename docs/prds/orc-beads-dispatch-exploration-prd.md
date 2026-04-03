# PRD: Orc Beads Dispatch Semantics Exploration Harness

## Status
Draft for implementation by Orc dev agent.

## Purpose
Build an **exploration and validation harness inside Orc** to experimentally determine how Orc should derive a dispatch plan from Beads structures, especially around:

- `bd ready` semantics
- default Beads ready ordering
- parent-child/container semantics
- non-epic container-like types (for example `integration`)
- fail-closed handling for Beads issue types Orc does not yet support

This is not the full Orc scheduler. It is a **test exploration feature inside Orc** whose job is to create controlled Beads structures, observe actual Beads behavior, calculate a trial Orc dispatch plan, compare observed vs expected behavior, and iterate toward a stable scheduling policy.

---

## Background
Current workflow:

1. PRD ideation happens **outside Beads**.
2. A non-coding planning agent creates an implementation plan **outside Beads**.
3. That plan is lowered into Beads issues for execution.
4. Orc is responsible for dispatch/execution semantics, not upstream planning.

This means Orc does **not** need GasTown’s full planning/template stack right now. We can start with Beads epics and child issues as the execution envelope.

At the same time, Beads behavior is not intuitive enough to safely hard-code assumptions without testing. In particular:

- `bd ready` includes more than plain leaf tasks.
- epics can appear in `bd ready`
- Beads supports parent-child structures and may have non-epic types with children
- the exact practical behavior around child readiness must be observed, not assumed

The exploration harness exists to turn these into measured facts inside a sandbox.

---

## Problem Statement
Orc needs a reliable rule for turning Beads work into a dispatch plan.

The main unresolved questions are:

1. Is **Beads `ready`** the only runnability oracle Orc should trust?
2. Can child tasks under an open container appear directly in `bd ready`?
3. Should Orc ever directly dispatch a container issue?
4. Are there Beads issue types besides `epic` that can act as containers in practice?
5. How should Orc fail when it encounters a Beads type it does not yet understand?
6. Should Orc compute only the next dispatchable unit, or should it compute an inspectable full plan each cycle?

We want to answer these empirically and codify the result in Orc.

---

## Goals

### Primary Goal
Implement an Orc-internal exploration mode that can:

- create Beads test scenarios in an isolated sandbox
- query actual Beads behavior
- compute an Orc dispatch plan using a trial algorithm
- compare that plan against expected behavior
- emit a report that allows the dev agent to refine Orc’s scheduling logic

### Secondary Goals

- verify the starting assumptions below
- build confidence before wiring this logic into the main Orc dispatcher
- create a repeatable regression suite for future Beads upgrades

---

## Non-Goals

- building the full production Orc dispatcher
- adopting GasTown molecules, wisps, or convoy semantics in Orc v1
- re-implementing Beads readiness logic from scratch
- introducing a second issue-tracker-style lifecycle model inside Orc
- solving upstream planning/PRD generation inside Orc

---

## Initial Current Thinking to Start From
This is the **initial Orc workflow hypothesis** the exploration should start from.

### 1. Beads owns readiness
Orc should treat the output of `bd ready` as the Beads-owned runnable set.

Orc should **not** implement its own competing notion of “ready” for ordinary work items.

### 2. Orc owns dispatchability
Orc’s main job is to decide whether a Beads-ready issue is **dispatchable**.

Dispatchable means: Orc is willing to hand this issue to an execution agent now.

### 3. Ready is a Beads term; dispatchable is an Orc term
We should not duplicate Beads states in Orc unless Orc adds real new semantics.

At this stage the only positive Orc-specific state we likely need is:

- `dispatched`: Orc has launched execution for this issue

Everything else should remain Beads-native where possible.

### 4. Use Beads default hybrid ready order for now
Orc should preserve the order returned by Beads `bd ready` for now rather than imposing its own ordering policy.

### 5. Containers are not automatically dispatchable
If a Beads-ready issue is a container-like issue, Orc should not immediately dispatch it as worker work.

Instead Orc should:

- expand its descendants
- identify which descendants are already Beads-ready
- derive a local dispatch frontier from those descendants
- continue classification recursively

### 6. Do not build a second readiness engine
When Orc reasons about a container subtree, it should use **membership in the global `bd ready` output** as the runnability signal.

That means “global ready set” in this PRD refers to:

- the actual list returned by **Beads `bd ready`**
- not an Orc-computed ready set

### 7. Unknown Beads types must fail closed
Orc must either:

- explicitly support a Beads issue type, with documented semantics, or
- fail with a clear unsupported-type error/report

We should not silently guess.

### 8. Compute a full ephemeral plan for inspection
Each exploration run should compute the whole current Orc dispatch plan, not just the first dispatchable item.

This plan is **ephemeral** and recomputed from live Beads state each cycle. It is for inspection and validation, not long-lived schedule storage.

---

## Key Design Principles

### Fail-Closed Type Handling
Orc must have an explicit type policy. It should not assume only `epic` can be a container.

A screenshot from current usage already shows at least one additional type with children: `integration`.

Therefore Orc must:

- enumerate issue types actually encountered in the sandbox and in real repos
- explicitly classify each known type
- fail when it encounters a type that is not in the classification map

### Structure vs Scheduling
Parent-child gives structure and containment.

Execution ordering should come from:

- Beads `ready`
- explicit dependency semantics
- Orc dispatchability policy

The exploration harness should test this rather than assume it.

### Observe Before Generalizing
If Beads behavior is surprising, the experiment should record the fact and drive Orc policy from the observation.

---

## Proposed Orc Taxonomy for the Experiment
This taxonomy is only for the experiment and the first Orc scheduling model.

### A. Dispatchable worker issue
Examples we expect may belong here:

- `task`
- `bug`
- `feature`
- `chore`

These are candidate worker-executable issues.

### B. Container/control issue
Examples we expect may belong here:

- `epic`
- possibly `integration`
- possibly other types discovered during testing

These are not directly dispatched until explicitly proven safe to do so.

### C. Unsupported issue type
Any type not explicitly classified above.

Encountering one should cause Orc to:

- stop plan generation for that scenario, or mark the plan invalid
- emit a structured unsupported-type report
- require explicit semantic classification before proceeding

---

## Required Deliverable
Implement an Orc exploration feature with the following capabilities.

### 1. Sandbox Creator
Create an isolated temporary repo/workspace for Beads experiments.

Requirements:

- initialize Beads cleanly
- run in an isolated temp directory
- clean up after itself unless a `--keep-sandbox` style option is requested
- produce reproducible scenario creation

### 2. Scenario Builder
The harness must be able to create Beads issues and dependencies for a named scenario.

Scenarios should be declarative enough that the dev agent can add new ones easily.

The scenario builder must support:

- issue type
- title
- priority
- status
- parent-child relations
- blocker relations
- defer/block conditions where needed
- nested structures

### 3. Beads Observer
The harness must query Beads directly and capture at minimum:

- `bd ready` output in default order
- issue list/tree output for context
- issue type, status, priority, and parent-child structure
- dependencies relevant to each scenario

### 4. Orc Trial Planner
Given the Beads-observed state, Orc must calculate a trial dispatch plan using the initial workflow hypothesis in this PRD.

### 5. Comparison + Report
The harness must emit a human-readable report containing:

- scenario definition
- raw Beads observations
- Orc trial plan
- expected behavior for the scenario
- mismatches
- recommended next refinement

### 6. Regression Mode
Once expectations are updated, the scenario should be runnable as a regression test so future Beads changes or Orc changes can be detected.

---

## Trial Orc Planning Algorithm (V0)
The first algorithm to test should be:

1. Call `bd ready` with default settings.
2. Preserve the returned order exactly.
3. Treat that ordered result as the **global Beads-ready set**.
4. Walk the ordered set from top to bottom.
5. For each issue:
   - classify its type using Orc’s explicit type map
   - if type is unsupported, fail closed
   - if type is dispatchable worker type, add it to the Orc dispatch plan
   - if type is container/control type:
     - collect its descendants
     - intersect those descendants with the global Beads-ready set
     - derive a local frontier in global ready order
     - recursively classify that local frontier
     - add any resulting dispatchable items to the Orc dispatch plan
     - do not add the container itself as dispatchable unless a later experiment proves this is correct for a specific type
6. Emit the full ordered dispatch plan.
7. Emit diagnostics explaining how each item entered or failed to enter the plan.

### Important Constraint
This algorithm must **not** invent readiness for children outside Beads.

If a child is not in the Beads-ready result, Orc V0 must treat it as not runnable even if Orc believes it “should” be runnable.

---

## Scenarios to Implement
These are the minimum scenarios.

### Scenario 1: Simple independent worker issues
Structure:

- task A
- task B
- task C

No parents, no blockers.

Questions:

- What is the Beads ready order?
- Does Orc produce the same dispatch order?

### Scenario 2: Simple blocker chain
Structure:

- task A blocks task B
- task B blocks task C

Questions:

- Does only A appear in `bd ready` initially?
- Does Orc only dispatch A?

### Scenario 3: Open epic with child tasks and no explicit blockers
Structure:

- epic E
  - task E.1
  - task E.2

Questions:

- Does the epic appear in `bd ready`?
- Do child tasks also appear in `bd ready`?
- Does Orc dispatch the epic, the children, both, or neither under V0?
- Is the descendant-intersection rule sufficient?

### Scenario 4: Open epic with ordered child tasks via blockers
Structure:

- epic E
  - task E.1
  - task E.2 (blocked by E.1)
  - task E.3 (blocked by E.2)

Questions:

- Does only E.1 appear as Beads-ready among the children?
- Does Orc produce only E.1 as dispatchable?

### Scenario 5: Blocked parent suppresses child readiness
Structure:

- epic E (blocked or otherwise non-runnable by Beads rules)
  - task E.1
  - task E.2

Questions:

- Are children suppressed from `bd ready` when the parent is blocked?
- Is this consistent across statuses/dependency styles?

### Scenario 6: Deferred parent suppresses children
Structure:

- epic E deferred into the future
  - child task(s)

Questions:

- Are children excluded from `bd ready` while parent is deferred?
- Does Orc correctly produce no local frontier?

### Scenario 7: Nested container type observed in practice
Structure inspired by the current VedaLang screenshot:

- epic P
  - integration I
    - task I.1
    - task I.2
    - task I.3
  - task P.1
  - task P.2

Questions:

- Can `integration` act as a parent/container in practice?
- Does it itself appear in `bd ready`?
- Should Orc treat `integration` as unsupported initially, or as a container candidate?
- What behavior emerges if Orc fails closed here?

### Scenario 8: Unknown custom type with children
Structure:

- epic P
  - custom type X
    - task X.1

Questions:

- Does the harness fail closed with a useful error?
- Is the error actionable enough for the developer to classify the type?

### Scenario 9: Mixed priorities under default hybrid ordering
Structure:

- multiple recent issues and older issues
- varying priorities
- some under containers, some standalone

Questions:

- Does observed order match current Beads hybrid behavior?
- Is Orc preserving that order correctly?

### Scenario 10: in-progress issues in ready set
Structure:

- issue already marked `in_progress`
- sibling issue `open`

Questions:

- Does Beads include `in_progress` in `bd ready` by default in the current version?
- How should Orc treat such issues for dispatch planning?
- Should Orc exclude already-dispatched work from the plan even if Beads includes it?

This last question may become the first real Orc-specific filter beyond type classification.

---

## Expected Behaviors / Hypotheses to Test
The experiment should begin with these hypotheses, but treat them as provisional.

### H1
Beads `bd ready` is the authoritative runnable set Orc should build from.

### H2
Epics may appear in `bd ready`.

### H3
Child tasks under an open parent may also appear in `bd ready` if they are not otherwise blocked.

### H4
Children of blocked or deferred parents are suppressed from `bd ready`.

### H5
Not all parent-child containers are necessarily `epic` type.

### H6
Unsupported Beads types must fail closed in Orc.

### H7
Orc should compute a full ephemeral dispatch plan each cycle for visibility and validation.

### H8
The first production-safe Orc behavior is likely:

- use Beads `ready`
- preserve Beads order
- filter by explicit type policy
- treat containers as planner/control nodes
- dispatch only dispatchable descendants already present in the Beads-ready set

---

## Open Questions the Harness Must Help Answer

1. Is `integration` a true Beads type with intended semantics, or a custom/local convention?
2. Are there other real Beads types that should be treated as containers?
3. Should Orc’s dispatch plan include items already `in_progress`, or should those be considered already dispatched and therefore omitted from the next dispatch frontier?
4. Is descendant intersection with the Beads-ready set enough, or do we also need a local wave view for explanation/debugging?
5. Should some non-epic container types eventually be directly dispatchable?
6. Should Orc fail the whole plan on unsupported type, or produce a partial plan plus explicit failure markers?

---

## Suggested Outputs
Each exploration run should emit:

### Human-readable markdown report
Containing:

- scenario name
- scenario structure
- raw Beads observations
- Orc plan
- mismatches
- conclusion

### Machine-readable JSON artifact
Containing:

- issues created
- type map used
- raw `bd ready` output
- descendant maps
- Orc classified plan
- mismatch list

### Optional graph/debug views
Helpful but not required initially:

- tree view
- dependency view
- dispatch frontier view

---

## Acceptance Criteria

### Functional
- Orc can create isolated Beads test scenarios automatically.
- Orc can run `bd ready` and preserve returned order.
- Orc can compute a trial dispatch plan from that output.
- Orc explicitly classifies Beads issue types.
- Orc fails closed on unsupported issue types.
- Orc emits a report comparing observed vs expected behavior.

### Investigative
- The harness answers whether child tasks under open epics appear in `bd ready`.
- The harness answers how blocked/deferred parents affect children.
- The harness answers how a non-epic parent type such as `integration` behaves.
- The harness produces enough information for the Orc developer to refine the scheduler without manually reproducing all cases.

### Reusability
- At least the minimum scenario suite can be rerun after Beads version upgrades.
- New scenarios can be added without rewriting the harness.

---

## Implementation Guidance for the Orc Dev Agent

### Recommended Sequence
1. Build the sandbox creator.
2. Build a small declarative scenario format.
3. Implement Scenarios 1–4 first.
4. Add the raw Beads observer.
5. Implement the Orc V0 planner exactly as described above.
6. Emit markdown + JSON reports.
7. Add Scenario 7 (`integration`) and Scenario 8 (unknown type fail-closed).
8. Refine the Orc type policy only after observing actual behavior.

### Important Restraints
- Do not silently treat unknown types as worker-dispatchable.
- Do not reimplement Beads-ready logic.
- Do not persist long-lived schedules yet.
- Do not import GasTown molecules/wisps/planner concepts unless the experiment proves they are needed.

---

## Recommended Initial Default Type Policy
Start strict.

### Worker-dispatchable (provisional)
- task
- bug
- feature
- chore

### Container/control (provisional)
- epic

### Unsupported until proven/classified
- integration
- molecule
- gate
- message
- agent
- role
- rig
- merge-request
- any other type encountered

Note: if Beads excludes some of these from `bd ready` already, that is useful context but does not remove the need for Orc to own an explicit policy.

---

## Success Definition
This exploration is successful when Orc can answer, with evidence rather than guesswork:

- what `bd ready` really returns in representative structures
- which items Orc should consider dispatchable
- how container issues should be handled
- how unsupported types are surfaced safely

The output should leave us with a concrete, test-backed Orc scheduling policy that can then be promoted into the production dispatcher.

---

## Appendix: Developer Notes

### Clarification of “global ready set”
In this PRD, “global ready set” means:

- the exact ordered output of **Beads `bd ready`** for the current workspace

It does **not** mean an Orc-maintained ready cache.

### Clarification of “full plan”
“Full plan” means the full **current** dispatch plan for the observed graph at the time of the run. It is recomputed each time. It is not a durable long-range schedule.

### Clarification of current intent
At this stage, the likely target architecture is:

- upstream planning outside Beads
- Beads as execution graph / work memory
- Orc as dispatch planner + dispatcher
- no GasTown-style molecule/template system yet

