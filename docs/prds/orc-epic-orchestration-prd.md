# PRD: Orc Dispatch Policy Derived From Beads Exploration

**Status:** Adopted for the exploration harness and promoted into `orc start`  
**Date:** 2026-04-04  
**Audience:** Orc engineering  
**Scope:** How Orc should turn Beads `bd ready` output into a worker dispatch frontier with minimal surprises

## Summary

The exploration harness replaced earlier guesswork with live Beads evidence. The
settled Orc policy is:

1. Beads owns readiness and ordering.
2. Orc owns only dispatch-safety filtering.
3. Orc preserves the exact order returned by `bd ready`.
4. Orc never dispatches containers such as `epic` or `integration`.
5. Orc excludes `in_progress` work from a new dispatch frontier.
6. Orc fails closed on unsupported types and suppresses their descendant subtrees.

In short: Orc should treat its dispatch frontier as the ordered subsequence of
`bd ready` that passes Orc's explicit safety rules.

## Why This Supersedes The Earlier Proposal

An earlier draft suggested that Orc should compute its own container-local
frontier rather than trusting raw `bd ready`. Live exploration runs showed that
this would add avoidable complexity and create surprising differences from what
operators see in Beads:

- open parents and ready children can appear together in `bd ready`
- the order is often child-first rather than parent-first
- blocked and deferred parents already suppress descendants in Beads
- custom parent types such as `integration` can behave like real containers

Because Beads is already doing the hard readiness work, Orc's safest role is to
preserve that ordering and add only conservative dispatch filters.

## Observed Beads Behavior

These observations came from live harness runs using `orc explore dispatch` on
2026-04-04:

| Scenario | Observed `bd ready` order | What it means for Orc |
|---|---|---|
| Open epic with children | `E.2`, `E.1`, `E` | Orc must not assume parent-before-child ordering. |
| Epic with ordered children | `E.1`, `E` | A container can be ready alongside its first runnable child. |
| Blocked parent | `Blocker` only | Beads already suppresses descendants of blocked parents. |
| Deferred parent | `(none)` | Beads already suppresses descendants of deferred parents. |
| Nested integration container | `P.2`, `P.1`, `I.3`, `I.2`, `I.1`, `I`, `P` | `integration` can act as a real container, and ready order is still child-first. |
| In-progress sibling | `Open` only | Current Beads behavior excludes `in_progress` work from `bd ready`. |

These results make two product decisions clear:

- Orc should not build a second competing readiness engine.
- Orc should not silently guess the semantics of unsupported container-like types.

## Dispatch Rules

Orc's dispatch frontier is defined as the ordered subsequence of `bd ready`
where every item satisfies all of the following:

1. It is a supported worker type.
2. It does not currently have children.
3. It is not already `in_progress`.
4. It is not itself a container/control issue.
5. It is not inside an unsupported container subtree.

### Supported Worker Types

- `task`
- `bug`
- `feature`
- `chore`

### Supported Container / Control Types

- `epic`
- `integration`

### Unsupported Types

Any Beads type not explicitly listed above is unsupported until classified.

Rules for unsupported types:

- an unsupported leaf is skipped and surfaced as invalid
- an unsupported node with children suppresses its entire descendant subtree
- unrelated ready work outside that subtree may still proceed

### Structural Safety Rule

Even if a type is normally worker-dispatchable, any issue that currently has
children is treated as a control/container node and is not dispatched directly.

This keeps Orc from accidentally dispatching an issue that is being used as a
container in practice.

## Mapping To Beads Concepts

| Concept | Beads owns | Orc owns |
|---|---|---|
| Is this issue ready? | Yes, via `bd ready` | No |
| What order should ready items be considered in? | Yes | No |
| Should this ready item be handed to a worker right now? | No | Yes |
| Is this type safe and supported for direct dispatch? | No | Yes |
| Should descendants under an unsupported type be suppressed? | No | Yes |

This is the core division of responsibility:

- Beads answers "ready"
- Orc answers "dispatchable"

## Examples

### Example 1: Open Epic With Ready Children

Beads says:

```text
bd ready -> E.2, E.1, E
```

Orc dispatches:

```text
E.2, E.1
```

Reason: the child tasks are ready worker issues, and the epic is a ready
container that Orc does not dispatch directly.

### Example 2: Nested Integration Container

Beads says:

```text
bd ready -> P.2, P.1, I.3, I.2, I.1, I, P
```

Orc dispatches:

```text
P.2, P.1, I.3, I.2, I.1
```

Reason: `integration` is now treated as an explicit container/control type, so
Orc preserves the child-first Beads order while filtering out the container
nodes `I` and `P`.

### Example 3: Unsupported Custom Container

Beads says:

```text
bd ready -> X.1, X, P
```

Orc dispatches:

```text
(nothing from the X subtree)
```

Reason: Orc sees that `X` has unsupported container semantics and suppresses the
subtree until that type is classified. This is intentionally fail-closed.

### Example 4: In-Progress Work

Beads says:

```text
bd ready -> Open
```

Orc dispatches:

```text
Open
```

Reason: current Beads builds already exclude `in_progress` items from the ready
set. Orc keeps the same policy even if a future Beads version ever surfaces
them.

## CLI Mapping

### `orc explore dispatch`

This command is the canonical way to validate the policy above.

It:

1. creates isolated Beads sandboxes
2. builds declarative scenarios
3. captures raw `bd ready` behavior and tree output
4. computes Orc's trial dispatch frontier
5. writes markdown and JSON reports

Use it whenever Beads changes, new issue types appear, or the Orc dispatch rules
need to be refined.

### `orc start`

The production runner now follows this policy in its main queue path:

1. fetch raw `bd ready --json --limit 0`
2. preserve Beads ordering exactly
3. fetch full issue metadata for structural safety checks
4. filter out containers/control nodes, unsupported subtrees, and `in_progress`
5. surface policy skip reasons in operator-facing scheduler/status output

## Promotion Outcome

The promotion work is complete when all of the following are true:

1. the main queue path preserves raw Beads ordering instead of re-sorting it
2. container types are filtered explicitly rather than only excluding `epic`
3. unsupported container subtrees are suppressed with actionable diagnostics
4. `orc status` / logs can explain why a Beads-ready item was skipped
5. the exploration scenario suite remains green against the active Beads version

## Final Recommendation

Build Orc around this rule:

> Trust Beads for what is ready and in what order; trust Orc only for what is
> safe to dispatch.

That keeps the workflow legible to operators, matches the live evidence from the
exploration harness, and avoids creating a second scheduler before it is needed.
