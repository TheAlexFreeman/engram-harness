---
title: "B2+: Agent-Directed Context Compaction"
status: draft
audience: project maintainers + contributors
parent: improvement-plans-2026.md (Theme B — Agent loop)
last_updated: 2026-04-28
---

# B2+: Agent-Directed Context Compaction

An extension of the shipped B2 tiered compaction system. Today compaction is
purely reactive — the harness fires Layer 2 or Layer 3 when input tokens
cross configured thresholds. This plan adds two complementary capabilities:

1. **Agent-triggered compaction.** The agent can request a compaction pass at
   a natural stopping point rather than waiting for the threshold.
2. **Pinned context.** The agent can mark specific earlier turns as
   "preserve verbatim" so compaction summarizes *around* them instead of
   flattening everything uniformly.

Together these shift compaction from a blunt resource-pressure valve to a
tool the agent uses strategically: compact when you've just finished a phase
of investigation and know the details won't matter; pin the turns that
*will* matter for the remainder of the session.

---

## Motivation

B2's threshold-based compaction works, but it has two structural
limitations:

**Timing is wrong.** The threshold fires mid-turn when input tokens happen
to cross a line. The agent may be mid-investigation — it just read a file,
hasn't acted on the result yet, and compaction rewrites the tool_result it
was about to reason over. The agent has much better information about when
compaction is safe: after finishing a subtask, after committing a file,
after completing an exploratory phase and deciding on an approach. Letting
the agent choose the moment produces better summaries (the agent can
describe what to keep in the request) and avoids mid-thought disruption.

**Preservation is uniform.** Both Layer 2 and Layer 3 treat all older tool
pairs the same: everything outside the most-recent N pairs is fair game.
But sessions routinely produce a few high-value turns that the agent needs
verbatim for the rest of the session — a DB schema it's migrating against,
the exact error message it's debugging, a config file it's cross-
referencing across multiple edits. Today the agent's only option is to
re-read the file, which costs a tool call and re-fetches content that was
already in context. Pinning lets the agent declare "keep this one" so the
summarizer works around it.

These two features compose naturally: the agent calls `compact_context`
with a set of pins when it reaches a stopping point, getting a clean
context with the right details preserved.

---

## Design

### Single tool surface: `compact_context`

One tool handles both triggering and pinning. Adding two separate tools
(`compact_context` + `pin_context`) would require the agent to reason about
ordering and would double the tool-call cost for the common case where you
pin and compact together.

```
compact_context(
    reason?: string,
    pins?: list[{
        description: string,
        reason: string
    }]
)
```

Called with no arguments, it triggers Layer 2 compaction unconditionally
(bypasses the token threshold). Called with `pins`, it first resolves and
marks the target turns, then triggers compaction. `reason` is logged to the
trace for post-session analysis of when/why the agent chose to compact.

### Pin resolution: description-based, not index-based

The agent should not need to know message indices. Pins use a `description`
field that the harness resolves by scanning backward through the
conversation for matching tool names, file paths, or content snippets. For
example:

```json
{
    "description": "the read_file call that returned schema.sql",
    "reason": "need the exact column definitions for the migration"
}
```

Resolution strategy (in priority order):

1. Exact tool name match in a tool_use block (e.g. "read_file" matches a
   `tool_use` block with `name: "read_file"`).
2. Substring match against tool_use input args or tool_result content
   (e.g. "schema.sql" matches if it appears in the `input.path` or in the
   result text).
3. Substring match against assistant text in the same turn (e.g. "the
   migration plan" matches if the assistant's reasoning text contains that
   phrase).

The resolver scans backward from the most recent message, skipping
already-compacted pairs and the most-recent `keep_recent_pairs` (which are
already preserved). First match wins. If no match is found for a pin, the
tool result reports which pins couldn't be resolved so the agent can
adjust.

### Pin storage: message-level metadata

Pins are stored directly on the assistant message in the matched pair as a
`_harness_pins` key. The underscore prefix marks it as harness metadata;
it gets stripped before sending messages to the model API (same pattern
as any internal bookkeeping the harness might add to message dicts).

```python
messages[target_assistant_idx]["_harness_pins"] = {
    "description": "the read_file call that returned schema.sql",
    "reason": "need the exact column definitions for the migration",
    "pinned_at_turn": current_turn,
}
```

Storing pins on the messages (rather than in a side list) means they
survive message-list mutations — compaction already mutates in place, so
pins travel with their messages naturally.

### Pin budget and expiry

Uncapped pinning defeats the purpose of compaction. Constraints:

- **Max active pins: 4.** The tool returns a clear error if the agent
  tries to exceed this. The cap is a module-level constant
  (`MAX_ACTIVE_PINS`) so it's easy to tune. Rationale: 4 pinned pairs
  plus the default 4 `keep_recent_pairs` means at most 8 pairs are
  immune from Layer 2 — a meaningful fraction of a typical session but
  not enough to prevent compaction from freeing space.
- **Expiry after compaction.** Pins survive one compaction pass. After
  the pass that skipped them, they're cleared. If the agent still needs
  the content, it can re-pin. This prevents stale pins from accumulating
  across a long session. Implementation: after `maybe_compact` completes,
  sweep messages for `_harness_pins` and remove them.
- **Layer 3 override.** When Layer 3 fires (the session is genuinely at
  the context budget), pins are *respected but capped*: at most 2 pinned
  pairs survive Layer 3. If more than 2 are pinned, the 2 most recently
  pinned survive and the rest are included in the summary region. This
  prevents pins from blocking the emergency compaction that Layer 3
  exists to provide.
- **Unpin.** The agent can clear a pin by calling `compact_context` with
  an empty `pins` list and `reason: "clearing pins"`. More precisely,
  any `compact_context` call replaces the active pin set — pins from a
  previous call that aren't in the new `pins` list are cleared before
  the compaction pass runs. This keeps the mental model simple: the
  most recent `compact_context` call is the canonical pin state.

### Compaction handle: the PauseHandle pattern

Agent-triggered compaction follows the same deferred-flag pattern as
`pause_for_user` (B4). A `CompactionHandle` dataclass is owned by the
loop and passed into the `CompactContext` tool at construction time.

```python
@dataclass
class CompactionHandle:
    requested: bool = False
    reason: str | None = None
    pins: list[PinRequest] | None = None

    def request_compaction(self, *, reason: str | None, pins: list | None) -> None:
        self.requested = True
        self.reason = reason
        self.pins = pins

    def reset(self) -> None:
        self.requested = False
        self.reason = None
        self.pins = None
```

The tool's `run()` method validates inputs, calls
`handle.request_compaction(...)`, and returns a confirmation string. The
loop checks `compaction_handle.requested` after the tool batch completes
— right before the existing threshold-based compaction site — and
force-triggers `maybe_compact` with `threshold_tokens=1`. After the
compaction pass, the loop calls `handle.reset()`.

Because the handle is checked *after* the full tool batch, a
`compact_context` call in the same batch as other tools works correctly:
the other tools' results get appended to messages first, then compaction
runs. This is the same ordering guarantee `pause_for_user` relies on.

### Interaction with threshold-based compaction

Agent-triggered compaction and threshold-based compaction coexist. The
loop processes them in order:

1. Check `compaction_handle.requested` → if set, force-trigger Layer 2
   (with pin resolution applied first).
2. Run the existing threshold-based Layer 2 check (respects pins that
   are still marked on messages).
3. Run the existing threshold-based Layer 3 check (respects the Layer 3
   pin cap).

If the agent triggers compaction and the threshold also fires on the same
turn, the agent-triggered pass runs first and the threshold pass sees
"all_already_compacted" and no-ops. This is the right behavior: the
agent's request already freed the space.

Agent-triggered compaction never fires Layer 3. The agent can request
focused cleanup; the nuclear option remains exclusively threshold-driven
as a safety net.

### Compaction respecting pins: changes to `maybe_compact`

The existing `maybe_compact` function already filters candidate pairs by
checking `_is_already_compacted`. Pin support adds one more filter:

```python
fresh_pairs = [
    (a, u) for a, u in candidate_pairs
    if not _is_already_compacted(messages[u])
    and "_harness_pins" not in messages[a]
]
```

This is the only change to `maybe_compact`'s core logic. The pin
metadata is already on the messages by the time `maybe_compact` runs
(the loop resolves and applies pins from the handle before calling
`maybe_compact`).

The summarizer prompt also gains a brief note when pins exist:

```
Note: some turns in this session are pinned and preserved verbatim.
They are NOT included in the region below. Your summary should not
repeat their content — the model will see both your summary and the
pinned turns.
```

This prevents the summary from redundantly restating pinned content,
which would waste the space that pinning was meant to save.

### Changes to `maybe_full_compact` for pins

Layer 3 is more invasive because it does a slice replacement. When pins
exist in the compacted region:

1. Identify pinned messages in the region (up to the Layer 3 pin cap
   of 2, selected by most-recent `pinned_at_turn`).
2. Remove pinned pairs from the region before building the summarizer
   prompt (so the summary doesn't cover their content).
3. After the slice replacement, re-insert the pinned pairs immediately
   after the summary message.

```python
# Before slicing:
pinned_in_region = [
    (i, messages[i], messages[i + 1])
    for i in range(region_start, region_end - 1)
    if messages[i].get("_harness_pins")
    and _is_assistant_with_tool_use(messages[i])
    and _is_user_with_tool_result(messages[i + 1])
]
# Apply Layer 3 pin cap:
pinned_in_region = pinned_in_region[-FULL_KEEP_PINNED:]

# After slicing:
insert_msgs = [summary_msg]
for _, asst, user in pinned_in_region:
    insert_msgs.extend([asst, user])
messages[region_start:region_end] = insert_msgs
```

The tool_use → tool_result adjacency invariant is preserved because
pinned pairs are re-inserted as complete (assistant, user) pairs.

---

## Implementation plan

### PR 1: `CompactionHandle` + `compact_context` tool (trigger only, no pins)

The minimal useful feature: the agent can trigger compaction on demand.

**Files touched:**

- New `harness/tools/compact.py` — `CompactionHandle` dataclass and
  `CompactContext` tool class. Follows the `pause.py` pattern exactly.
- `harness/tools/__init__.py` — add `CAP_COMPACT = "compact"` capability.
- `harness/tool_registry.py` — instantiate `CompactionHandle`, wire it
  into `CompactContext`, register the tool when compaction thresholds are
  configured (or unconditionally — the tool is useful even without
  thresholds since it forces compaction regardless).
- `harness/loop.py` — accept `compaction_handle` parameter in
  `run_until_idle`, check `compaction_handle.requested` before the
  existing threshold-based compaction site, reset after.
- `harness/cli.py` — thread the handle through `run_session`.
- `harness/prompt_templates/memory.md` — add `compact_context` to the
  tool documentation the agent sees in its system prompt.
- `harness/compaction.py` — no changes needed for this PR.

**Tests:**

- `test_compact_tool.py` — tool validates args, stamps the handle,
  returns confirmation string. Mirrors `test_pause_tool.py`.
- `test_compaction.py` — new test: agent-triggered compaction
  force-fires Layer 2 regardless of token count. Verify it does NOT
  fire Layer 3.
- Integration test: a scripted session where the agent calls
  `compact_context`, verify messages are compacted and the summary is
  inserted.

**Complexity:** low. ~200 lines of new code, mostly boilerplate
following the pause.py pattern.

### PR 2: Pin resolution + `maybe_compact` integration

**Files touched:**

- `harness/tools/compact.py` — extend `CompactContext.run()` to accept
  `pins` parameter, extend `CompactionHandle` with `pins` field.
- New `harness/compaction.py::resolve_pins()` — backward scan through
  messages to resolve pin descriptions to message indices. Returns a
  list of `(index, pin)` tuples and a list of unresolved descriptions.
- `harness/compaction.py::apply_pins()` — stamp `_harness_pins` onto
  resolved messages, clear pins from previous calls that aren't in the
  new set.
- `harness/compaction.py::maybe_compact()` — add the `_harness_pins`
  filter to the `fresh_pairs` comprehension. Add pin-awareness note to
  the summarizer prompt when pins are present.
- `harness/compaction.py::clear_expired_pins()` — post-compaction sweep
  that removes `_harness_pins` from all messages (pins survive one pass).
- `harness/loop.py` — call `resolve_pins` + `apply_pins` before
  `maybe_compact` when the handle has pins. Call `clear_expired_pins`
  after compaction completes.

**Tests:**

- Pin resolution: exact tool name match, substring match in args,
  substring match in result content, substring match in assistant text.
  Priority ordering. No-match returns unresolved list.
- Pin filtering: `maybe_compact` skips pinned pairs. Pinned +
  already-compacted pairs are both filtered. Pins don't prevent
  compaction of unpinned pairs.
- Pin expiry: after `maybe_compact` runs, pins are cleared.
- Pin budget: attempting to pin more than `MAX_ACTIVE_PINS` returns an
  error from the tool.
- Pin replacement: a second `compact_context` call with different pins
  clears the old set and applies the new one.

**Complexity:** medium. ~300 lines. The pin resolver is the main new
logic; everything else is plumbing.

### PR 3: Layer 3 pin support

**Files touched:**

- `harness/compaction.py::maybe_full_compact()` — detect pinned pairs
  in the compaction region, enforce the Layer 3 pin cap
  (`FULL_KEEP_PINNED = 2`), exclude pinned content from the summarizer
  prompt, re-insert pinned pairs after the summary message.
- `harness/compaction.py` — add `FULL_KEEP_PINNED` constant.

**Tests:**

- Layer 3 with pins: pinned pairs survive the slice, appear after the
  summary message, tool_use/tool_result adjacency is preserved.
- Layer 3 pin cap: when >2 pairs are pinned, only the 2 most recently
  pinned survive.
- Layer 3 with no pins: behavior is unchanged from current (regression
  test).

**Complexity:** medium. ~150 lines. The tricky part is getting the
slice arithmetic right when re-inserting pinned pairs, but the test
surface is small and well-defined.

### PR 4 (optional): Prompt tuning + system prompt guidance

After the mechanical parts ship, tune the agent's usage:

- System prompt guidance on *when* to compact: after finishing a subtask,
  after committing, after an exploratory phase. Discourage compacting
  after every tool call.
- System prompt guidance on *what* to pin: exact error messages being
  debugged, schemas/configs being cross-referenced, user requirements
  stated early in the session. Discourage pinning large file dumps.
- Trace analysis: add `compact_context` calls to the trace bridge's
  event vocabulary so post-session analysis can measure how often the
  agent compacts, how many pins it uses, and whether pinned content
  actually gets referenced in subsequent turns.

**Complexity:** low. Prompt iteration + trace schema additions.

---

## Risks and mitigations

**Agent over-compacts.** If the agent calls `compact_context` every few
turns, the session devolves into a cycle of work → summarize → work →
summarize, spending tokens on summarization instead of the task. Mitigation:
the system prompt discourages frequent compaction, and the tool's
description emphasizes "use at natural stopping points." A future
refinement could add a cooldown (minimum turns between compactions), but
start without one and see if prompt guidance suffices.

**Agent over-pins.** If the agent pins 4 pairs and they're all large file
reads, compaction can't free meaningful space. Mitigation: the 4-pin cap
limits exposure, and pin expiry after one compaction pass prevents
accumulation. The system prompt should guide the agent to pin specific
high-value content, not bulk file reads.

**Pin resolution is ambiguous.** Multiple tool pairs might match a
description like "the read_file call." Mitigation: backward scan with
first-match-wins is deterministic and biased toward recency, which matches
the likely intent. The tool result reports which description matched which
turn, so the agent gets feedback. If resolution quality is a problem in
practice, a future version could let the agent specify a tool name +
argument substring for precise targeting.

**Pinned pairs break Layer 3 slice arithmetic.** Re-inserting pinned pairs
after the summary changes the indices of everything after them. Mitigation:
the slice replacement is atomic (`messages[start:end] = new_list`), so
there's no stale-index window. Tests cover the adjacency invariant
explicitly.

**Cost of the compaction model call.** Agent-triggered compaction adds a
model call the threshold might not have triggered. Mitigation: the call
uses `reflect` (a lightweight, no-tool model call) and the cost is folded
into the session total. The agent is making a deliberate trade-off: spend
a small amount now to free context for more productive turns later. The
trace records the cost so users can audit.

---

## Success criteria

- An agent running a 50+ turn session can call `compact_context` at a
  natural stopping point and continue with a cleaner context window.
- Pinned turns survive Layer 2 compaction verbatim and appear in the
  post-compaction conversation exactly as they were.
- The agent does not need to re-read files it pinned — the content is
  still in context after compaction.
- Layer 3 respects the pin cap and does not break the tool_use/tool_result
  adjacency invariant.
- No behavioral change for sessions that don't use `compact_context` — the
  existing threshold-based system is unaffected.
- Compaction cost (model calls from agent-triggered compaction) is visible
  in the session's usage accounting and trace events.

---

## Non-goals

- **Automatic pin inference.** The harness could try to guess which turns
  are important (e.g. based on how often the agent references a file
  path). Interesting but out of scope — start with explicit agent control
  and revisit if usage data suggests the agent is bad at choosing what to
  pin.
- **Cross-session pin persistence.** Pins are session-scoped. If the
  session pauses and resumes (B4), pins on the messages survive in the
  checkpoint. But there's no mechanism to carry pins across separate
  sessions — that's what Engram memory is for.
- **Pin content editing.** The agent can't modify pinned content, only
  preserve or unpin it. If the agent needs a modified version of a pinned
  turn, it should unpin, compact, and re-read.
- **Layer 3 agent trigger.** The agent can only trigger Layer 2. Layer 3
  remains exclusively threshold-driven as the emergency backstop.
