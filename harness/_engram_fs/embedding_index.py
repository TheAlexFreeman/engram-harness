"""SQLite-backed embedding index used by ``EngramMemory`` for semantic recall.

Lifted from ``engram_mcp.agent_memory_mcp.tools.semantic.search_tools`` so the
harness doesn't reach into the MCP package for its optional semantic-search
path. The implementation is unchanged; only the surrounding BM25 / hybrid
ranking / helpfulness loader was left behind (the harness only needs vector
search).

Optional dependency: ``sentence-transformers`` + ``numpy``. When those aren't
installed, constructing the index still works (the model is lazy) but any
call that accesses ``self.model`` raises ``ImportError`` — callers should
fall back to keyword search.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "all-MiniLM-L6-v2"
DB_DIR = ".engram"
DB_NAME = "search.db"
CHUNK_TARGET_TOKENS = 512
CHUNK_MAX_TOKENS = 768
DEFAULT_SCOPES = ("memory/knowledge", "memory/skills", "memory/users")

_HEADING_RE = re.compile(r"^(#{1,3}\s+.+)$", re.MULTILINE)


_EMBEDDING_AVAILABLE: bool | None = None


def _check_embedding_deps() -> bool:
    global _EMBEDDING_AVAILABLE
    if _EMBEDDING_AVAILABLE is None:
        try:
            import numpy  # noqa: F401
            from sentence_transformers import SentenceTransformer  # noqa: F401

            _EMBEDDING_AVAILABLE = True
        except ImportError:
            _EMBEDDING_AVAILABLE = False
    return _EMBEDDING_AVAILABLE


class EmbeddingIndex:
    """Manages a local SQLite-backed embedding index for memory files."""

    def __init__(self, repo_root: Path, content_root: Path, model_name: str = DEFAULT_MODEL):
        self.repo_root = repo_root
        self.content_root = content_root
        self.db_path = repo_root / DB_DIR / DB_NAME
        self.model_name = model_name
        self._model: Any = None
        self._ensure_db()

    # -- database setup ----------------------------------------------------

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    chunk_idx INTEGER NOT NULL,
                    heading TEXT,
                    content TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    UNIQUE(file_path, chunk_idx)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_meta (
                    file_path TEXT PRIMARY KEY,
                    mtime REAL NOT NULL,
                    model_name TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path)")

    # -- model access (lazy) -----------------------------------------------

    @property
    def model(self) -> Any:
        if self._model is None:
            if not _check_embedding_deps():
                raise ImportError(
                    "sentence-transformers is required for semantic search. "
                    "Install with: pip install engram-harness[search]"
                )
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    # -- incremental indexing ----------------------------------------------

    def _needs_reindex(self, file_path: str, mtime: float) -> bool:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT mtime, model_name FROM file_meta WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            if row is None:
                return True
            return row[0] < mtime or row[1] != self.model_name

    @staticmethod
    def _chunk_file(content: str) -> list[tuple[str | None, str]]:
        """Split file content into chunks at ## headings.

        Returns list of ``(heading, chunk_text)`` tuples.
        """
        body = content
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                body = content[end + 3 :].strip()

        parts = _HEADING_RE.split(body)
        chunks: list[tuple[str | None, str]] = []
        current_heading: str | None = None
        current_text = ""
        for part in parts:
            if _HEADING_RE.match(part):
                if current_text.strip():
                    chunks.append((current_heading, current_text.strip()))
                current_heading = part.strip()
                current_text = ""
            else:
                current_text += part
        if current_text.strip():
            chunks.append((current_heading, current_text.strip()))
        if not chunks and body.strip():
            chunks = [(None, body.strip())]

        # Sub-split oversized chunks
        result: list[tuple[str | None, str]] = []
        for heading, text in chunks:
            words = text.split()
            if len(words) > CHUNK_MAX_TOKENS:
                for i in range(0, len(words), CHUNK_TARGET_TOKENS):
                    result.append((heading, " ".join(words[i : i + CHUNK_TARGET_TOKENS])))
            else:
                result.append((heading, text))
        return result

    def index_file(self, rel_path: str, content: str, mtime: float) -> int:
        """Index a single file. Returns number of chunks created."""
        chunks = self._chunk_file(content)
        if not chunks:
            return 0

        texts = [f"{h}: {t}" if h else t for h, t in chunks]
        embeddings = self.model.encode(texts, normalize_embeddings=True)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
            for idx, ((heading, text), emb) in enumerate(zip(chunks, embeddings)):
                conn.execute(
                    "INSERT INTO chunks (file_path, chunk_idx, heading, content, embedding) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rel_path, idx, heading, text, emb.tobytes()),
                )
            conn.execute(
                "INSERT OR REPLACE INTO file_meta (file_path, mtime, model_name) VALUES (?, ?, ?)",
                (rel_path, mtime, self.model_name),
            )
        return len(chunks)

    def remove_file(self, rel_path: str) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
            conn.execute("DELETE FROM file_meta WHERE file_path = ?", (rel_path,))

    def build_index(
        self,
        scopes: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """Build or update the embedding index.

        Args:
            scopes: Folder paths relative to content root to index.
            force: Re-index all files regardless of mtime.

        Returns:
            Stats dict with indexed/skipped/removed/errors counts.
        """
        if scopes is None:
            scopes = list(DEFAULT_SCOPES)

        stats = {"indexed": 0, "skipped": 0, "removed": 0, "errors": 0}
        seen_files: set[str] = set()

        for scope in scopes:
            scope_path = self.content_root / scope
            if not scope_path.is_dir():
                continue
            for md_file in scope_path.rglob("*.md"):
                rel_path = str(md_file.relative_to(self.content_root)).replace("\\", "/")
                seen_files.add(rel_path)
                mtime = md_file.stat().st_mtime

                if not force and not self._needs_reindex(rel_path, mtime):
                    stats["skipped"] += 1
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8")
                    self.index_file(rel_path, content, mtime)
                    stats["indexed"] += 1
                except Exception:
                    stats["errors"] += 1

        # Prune deleted files — only within the requested scopes so
        # that a partial rebuild does not remove chunks from untouched scopes.
        scope_prefixes = tuple(s.rstrip("/") + "/" for s in scopes)
        with sqlite3.connect(str(self.db_path)) as conn:
            indexed_files = {
                row[0] for row in conn.execute("SELECT file_path FROM file_meta").fetchall()
            }
        for old_file in indexed_files - seen_files:
            if any(old_file.startswith(prefix) for prefix in scope_prefixes):
                self.remove_file(old_file)
                stats["removed"] += 1

        return stats

    def search_vectors(
        self,
        query: str,
        limit: int | None = 30,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return chunks ranked by cosine similarity to *query*.

        When *limit* is ``None`` all matching chunks are returned (useful
        when post-retrieval filtering such as trust filtering is planned).
        """
        import numpy as np

        query_emb = self.model.encode([query], normalize_embeddings=True)[0]

        with sqlite3.connect(str(self.db_path)) as conn:
            sql = "SELECT file_path, chunk_idx, heading, content, embedding FROM chunks"
            params: list[str] = []
            if scope:
                sql += " WHERE file_path LIKE ?"
                params.append(f"{scope}%")
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return []

        results: list[dict[str, Any]] = []
        for file_path, chunk_idx, heading, content, emb_bytes in rows:
            emb = np.frombuffer(emb_bytes, dtype=np.float32)
            similarity = float(np.dot(query_emb, emb))
            results.append(
                {
                    "file_path": file_path,
                    "chunk_idx": chunk_idx,
                    "heading": heading,
                    "content": content,
                    "similarity": similarity,
                }
            )
        results.sort(key=lambda x: x["similarity"], reverse=True)
        if limit is not None:
            return results[:limit]
        return results

    def chunk_count(self) -> int:
        """Return total number of indexed chunks."""
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return row[0] if row else 0


__all__ = ["EmbeddingIndex", "DEFAULT_MODEL", "DEFAULT_SCOPES"]
