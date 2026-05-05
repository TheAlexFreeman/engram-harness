---
trust: medium
source: agent-generated
created: 2026-03-22
type: skill
tags: [debugging, async, asyncio, python]
---

# Debugging async Python

When an asyncio task hangs:

1. Capture a snapshot of all tasks with `asyncio.all_tasks()` and
   `task.print_stack()` to find which coroutines are blocked.
2. Suspect awaits inside synchronous CPU-bound work — they leak the
   event loop and hide as silent latency rather than timeouts.
3. Run with `PYTHONASYNCIODEBUG=1` and `loop.set_debug(True)` to log
   long-running callbacks (default threshold 100 ms).

A common failure mode is mixing `asyncio` with blocking SDKs without
running them in `loop.run_in_executor`. The traceback is misleading:
you see the coroutine waiting at the await, not the offending blocking
call inside it.
