# Research: Fast/Cheap Thread Summary Extraction

**Issue:** orc-9n4  
**Date:** 2026-03-29  
**Status:** Investigation complete

## Background

The orchestrator launches `amp -x` threads to work on issues, then parses structured
JSON from stdout to get results. We want a way to also extract a brief one-line
human-readable summary of what a thread did, cheaply and quickly, for TUI display and
logging.

## Available Amp Modes

Amp has 5 modes (via `--mode` / `-m`):

| Mode | Model | Speed | Cost | Use Case |
|------|-------|-------|------|----------|
| `smart` | Claude Opus 4.6 | Medium | High | Full autonomy, complex tasks |
| `rush` | Faster/cheaper model | Fast | Low | Small, well-defined tasks |
| `deep` | GPT-5.4 (extended thinking) | Slow | Highest | Complex reasoning |
| `large` | Hidden/undocumented | Unknown | Unknown | Large context? |
| `free` | Unknown | Unknown | Free tier? | Unknown |

**`rush` mode is the clear winner for summary extraction** — it's explicitly designed
for "faster, cheaper, suited for small, well-defined tasks."

## Approaches Evaluated

### Approach 1: Rush-mode Amp thread with `read_thread` tool (RECOMMENDED)

**How it works:**
```bash
amp -x "Read the thread @T-{thread_id} and produce a one-line summary of what was accomplished." \
    --mode rush \
    --dangerously-allow-all \
    --no-notifications \
    --no-color \
    --archive
```

Amp's `read_thread` tool (available to all agents) can read any thread by ID and
extract relevant information. A rush-mode agent given a simple extraction task like
this would:
1. Call `read_thread` with the target thread ID
2. Produce a one-line summary
3. Exit quickly

**Pros:**
- Uses the cheapest/fastest mode
- `read_thread` is a built-in tool — no extra setup
- The `--archive` flag auto-cleans up the summary thread
- One-line summary extraction is exactly the kind of task rush mode excels at

**Cons:**
- Still spawns a full amp process (process overhead)
- Requires knowing the thread ID of the worker thread (need `--stream-json` to capture it)
- Costs credits (albeit minimal in rush mode)

**Estimated cost:** Very low — single tool call + minimal output tokens in rush mode.

### Approach 2: Parse summary from existing structured output (ZERO COST)

**How it works:**
The orchestrator already requires workers to output structured JSON with a `summary`
field. The `AmpResult.summary` field already contains a description of what the thread
did.

```python
# Already exists in amp_runner.py
@dataclass
class AmpResult:
    summary: str  # "brief description of what you did"
```

**Pros:**
- Zero additional cost — already captured
- Zero additional latency — already parsed
- No extra process spawning

**Cons:**
- Summary quality depends on the worker agent's compliance
- The summary is self-reported (not independently verified)
- Only available after the thread completes (not while running)

### Approach 3: `amp threads markdown` + local parsing

**How it works:**
```bash
amp threads markdown T-{thread_id} | head -50
```

Renders the full thread as markdown. Could pipe through a local summarizer or extract
the last assistant message.

**Pros:**
- No LLM credits needed (just API call for thread data)
- Full thread content available

**Cons:**
- Produces the full thread (could be very large)
- Requires additional parsing/extraction logic
- No built-in summarization

### Approach 4: Thread handoff with summary extraction

**How it works:**
```bash
amp threads handoff T-{thread_id} --goal "Summarize what was done" --print
```

Creates a handoff thread which includes context from the original.

**Pros:**
- Built-in context transfer from the source thread

**Cons:**
- Creates a new interactive thread (not execute mode)
- Heavier than needed — handoff is designed for continuing work, not summarizing
- More expensive than rush-mode direct extraction

### Approach 5: Stream JSON parsing during execution

**How it works:**
Use `--stream-json` with the worker thread to capture real-time output, then extract
the thread ID and final message without a second amp invocation.

```bash
amp -x "..." --stream-json --dangerously-allow-all --mode smart 2>/dev/null
```

The stream JSON output includes structured events with the thread ID and all messages.
The final assistant message can serve as a summary source.

**Pros:**
- Zero additional cost
- Thread ID captured automatically for other approaches
- Real-time visibility into what the agent is doing

**Cons:**
- Requires parsing streaming JSON (more complex than current stdout capture)
- Final message may not be a clean summary
- Significant refactor of `RealAmpRunner` needed

## Recommendations

### Short-term (zero cost, zero effort)

**Use the existing `AmpResult.summary` field.** The orchestrator already extracts this
from the worker's structured JSON output. Surface it in the TUI and logs. This is
already implemented — the only work needed is displaying it.

### Medium-term (low cost, moderate effort)

**Add `--stream-json` support to `RealAmpRunner`.** This enables:
1. Capturing the thread ID during execution
2. Real-time progress indication in the TUI
3. The final assistant message as an alternative summary source

### Long-term (minimal per-use cost, moderate effort)

**Implement rush-mode summary extraction.** After a worker thread completes, optionally
spawn a rush-mode thread that reads the worker thread via `read_thread` and produces
a polished one-line summary. This gives:
1. Independent, high-quality summaries (not self-reported)
2. Consistent format across all threads
3. Minimal cost in rush mode

### Configuration addition

```yaml
# .orc/config.yaml
summary_mode: "self-report"  # "self-report" | "rush-extract" | "stream-json"
summary_amp_mode: "rush"     # mode for rush-extract approach
```

## Key Findings

1. **`rush` mode** is the right choice for cheap/fast summary extraction — explicitly
   designed for small, well-defined tasks.
2. **The `read_thread` tool** enables any amp thread to read another thread's content,
   making cross-thread summary extraction possible.
3. **The existing `AmpResult.summary` is already available** — the cheapest option is
   simply surfacing what we already have.
4. **`--stream-json`** enables real-time monitoring and thread ID capture, which is a
   prerequisite for the rush-extract approach.
5. **`--archive` flag** can auto-cleanup ephemeral summary threads.

## Implementation Priority

1. Surface existing `AmpResult.summary` in TUI/logs (free, already captured)
2. Add `--stream-json` support for real-time progress + thread ID capture
3. Optional rush-mode `read_thread` summary extraction for independent verification
