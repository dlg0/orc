# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
```

## Beads-First Architecture

The orchestrator (`orc`) follows a **beads-first architecture**. Beads (`bd`) is designed to be wrapped by agent management tooling — it already handles issue tracking, state management, claiming, dependencies, and workflow out of the box.

**Before building any orchestrator feature, ask: "Does beads already do this?"**

- **Always discover beads capabilities first.** Run `bd help`, `bd prime`, and explore subcommands before assuming you need custom logic. Beads likely already supports what you need.
- **Never build workarounds for things beads handles natively.** If beads has a mechanism for something (dependencies, priorities, state transitions, metadata, memory), use it directly rather than reimplementing or wrapping it with custom code.
- **Follow the directions beads encourages.** Beads' design choices are intentional — its data model, CLI patterns, and workflow conventions should guide how `orc` structures its own logic. Work *with* beads, not around it.
- **Treat beads as the source of truth** for all issue/task state. The orchestrator coordinates agents and delegates work, but beads owns the task graph.
- **When in doubt, check beads first.** If you're about to write code that manages task state, tracks progress, stores metadata, or coordinates work items, verify that `bd` doesn't already provide that functionality before writing a single line.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## Draft Issues (Deferred-as-Draft Workflow)

When brainstorming or discussing issues with AI (Oracle/Amp), **create issues as deferred** so the orchestrator doesn't pick them up prematurely via `bd ready`.

### Rules

- **During brainstorming/exploration**: Always create issues with `--defer` unless the user explicitly says the issue is ready for work.
  ```bash
  bd create "Refactor auth module" --defer=+1y -d "Needs design discussion"
  ```
- **When an issue is fully spec'd and ready**: Create it normally (no `--defer`) or promote a deferred issue:
  ```bash
  bd update <id> --defer=""
  ```

### How it works

- `bd ready` automatically excludes deferred issues — **no orchestrator code changes needed**
- Deferred issues remain visible in `bd list` for tracking and review
- Use `--defer=+1y` (or any far-future date) for pure drafts with no target date
- Use `--defer=<date>` for time-gated issues that should become ready at a specific time

### Status meanings

| Status | Meaning |
|--------|---------|
| **Deferred** | Draft / not ready for work / time-gated |
| **Open** | Promoted / fully spec'd / ready for orchestrator pickup |
| **Closed** | Completed |

## Testing in a Target Repository

When testing `orc` against a real project (e.g., a testing repo), use `uvx --reinstall --from` to install and run the CLI from the local source tree. **Do NOT** activate or run from the `.venv` directly.

```bash
# Run orc from local source in the target repo
uvx --reinstall --from /path/to/orc orc [args...]
```

This ensures the tool is installed as a proper package (with correct entry points and dependencies) rather than relying on the development virtualenv, which may have path or isolation differences.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
