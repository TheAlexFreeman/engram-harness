"""Legacy module kept for import-compat. The canonical implementation lives
in ``harness.tools.memory_tools``.

The original tool class was ``RecallMemory`` (tool name ``recall_memory``);
it is now ``MemoryRecall`` (tool name ``memory_recall``). Existing imports
of ``RecallMemory`` continue to work via the alias below.
"""

from __future__ import annotations

from harness.tools.memory_tools import MemoryRecall

# Historical name preserved for callers that still import it directly.
RecallMemory = MemoryRecall

__all__ = ["MemoryRecall", "RecallMemory"]
