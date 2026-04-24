"""Harness-owned filesystem primitives for Engram-format memory repos.

These modules are direct copies of the format-layer utilities that used to
live inside ``engram_mcp.agent_memory_mcp``. The harness now owns them
directly so it doesn't reach into the MCP package for basic file / git
operations — and so PR D can drop the ``engram_mcp`` package without
breaking the harness.

Intentionally underscore-prefixed. External consumers should depend on the
Engram format layer in the standalone Engram repo, not on this module.
"""

from harness._engram_fs.errors import (
    AgentMemoryError,
    AlreadyDoneError,
    ConflictError,
    DuplicateContentError,
    MemoryPermissionError,
    NotFoundError,
    StagingError,
    ValidationError,
)
from harness._engram_fs.frontmatter_utils import (
    read_with_frontmatter,
    render_with_frontmatter,
    write_with_frontmatter,
)
from harness._engram_fs.git_repo import GitPublicationResult, GitRepo

__all__ = [
    "AgentMemoryError",
    "AlreadyDoneError",
    "ConflictError",
    "DuplicateContentError",
    "GitPublicationResult",
    "GitRepo",
    "MemoryPermissionError",
    "NotFoundError",
    "StagingError",
    "ValidationError",
    "read_with_frontmatter",
    "render_with_frontmatter",
    "write_with_frontmatter",
]
