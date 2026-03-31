# Repository Hygiene Audit — 2026-03

## Summary

Audit of the `orc` codebase found 9 actionable findings across code quality,
dead code, branch hygiene, and build/config areas. Tests pass (451/451) and the
lock file is up to date. The biggest items are 42 ruff lint violations (31
auto-fixable), a stale `src/amp_orchestrator/` directory left from the rename,
and 4 local branches pointing at closed issues.

## Findings by Category

### 1. Dead Code & Unused Modules

| # | Files | Finding | Action | Priority |
|---|-------|---------|--------|----------|
| 1 | `src/amp_orchestrator/` | Stale directory with only `__pycache__` dirs — no `.py` files, no git-tracked content. Remnant from the `amp-orchestrator → orc` rename. | Delete entire directory. | P2 |

### 2. Code Quality (Ruff)

| # | Files | Finding | Action | Priority |
|---|-------|---------|--------|----------|
| 2 | Multiple src/ and tests/ files | 42 ruff violations: 22 unused imports (F401), 9 f-strings without placeholders (F541), 8 unused variables (F841), 1 ambiguous variable name (E741), 1 import-not-at-top (E402), 1 undefined name (F821). 31 are auto-fixable. | Run `ruff check --fix`, manually fix remaining 11. | P2 |
| 3 | `src/orc/tui/widgets.py:559` | `F821 Undefined name 'OrchestratorState'` — missing import, potential runtime crash. | Add `from orc.state import OrchestratorState` import. | P1 |
| 4 | `src/orc/control.py`, `scheduler.py`, `doctor.py` | 7 bare `except Exception:` blocks that silently swallow errors. | Audit each and add logging or narrow exception type. | P3 |

### 3. Branch Hygiene

| # | Files | Finding | Action | Priority |
|---|-------|---------|--------|----------|
| 5 | Local branches | 4 local branches for closed issues: `amp-orchestrator-1ss.1`, `amp/amp-orchestrator-1ss-2-*`, `amp/amp-orchestrator-ea9-*`, `amp/amp-orchestrator-eb8-*`. 1 remote branch (`origin/amp-orchestrator-1ss.1`) also for a closed issue. | Delete stale local and remote branches. | P3 |
| 6 | `.worktrees/` | 5 stale worktree directories from previous runs (3ab, 49c, cp3, ea9, eb8). Not git-ignored but untracked. | Clean up with `git worktree prune` and remove dirs. | P3 |

### 4. Configuration & Build

| # | Files | Finding | Action | Priority |
|---|-------|---------|--------|----------|
| 7 | `pyproject.toml` | `pytest-asyncio` is a dev dependency but no `asyncio_mode` config is set. Tests collect with warnings about this. | Add `asyncio_mode = "auto"` to `[tool.pytest.ini_options]`. | P3 |

### 5. CI / Pipeline Health

| # | Finding | Action | Priority |
|---|---------|--------|----------|
| 8 | No `.github/workflows/` directory — zero CI configured. | Create basic CI workflow (lint + test). | P3 |

### 6. Issue Tracker Hygiene

| # | Finding | Action | Priority |
|---|---------|--------|----------|
| 9 | In-progress issue `amp-orchestrator-tam` (rename project) is partially complete — src was renamed but issue is still open. The rename left `src/amp_orchestrator/` as a stale remnant (finding #1). | Complete or close the rename issue. | P2 |

## Recommendation

1. **Quick wins (P1-P2):** Fix the F821 undefined name bug, run `ruff --fix`, delete `src/amp_orchestrator/`, address the rename issue.
2. **Housekeeping (P3):** Prune branches/worktrees, add CI, configure pytest-asyncio.
3. **Low priority:** The bare `except Exception:` blocks are worth auditing but not urgent.
