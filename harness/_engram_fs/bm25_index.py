"""SQLite-backed BM25 index for keyword recall.

Pure-Python BM25 (Okapi, k1=1.2, b=0.75) over markdown files in the Engram
content root. Stores term frequencies and document lengths in
``.engram/bm25.db`` next to the embedding index. Incremental — files are
re-indexed only when their mtime moves forward, mirroring
``EmbeddingIndex``.

This sits alongside the embedding index (no replacement). The hybrid
recall path in ``EngramMemory`` runs both backends and fuses ranks with
RRF so the agent sees the strongest signal from each — semantic similarity
finds conceptually-related documents that share little surface text;
BM25 finds the file that literally mentions the term you asked about.

Design notes
- File-level granularity. The embedding index is chunk-level; BM25 here is
  file-level so RRF fusion runs over the same primitive (a file path).
- No external dependency. ``rank_bm25`` would shave ~30 lines but adds a
  package install for users who only want keyword recall.
- Tokenization is intentionally simple: ``\\w+`` lowercased, drop length-1
  tokens. Software vocab keeps acronyms like UI, DB, CI, QA. Matches the
  legacy ``_keyword_recall`` tokenizer so behaviour is comparable.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

DB_DIR = ".engram"
DB_NAME = "bm25.db"
DEFAULT_SCOPES = ("memory/knowledge", "memory/skills", "memory/users")

# Okapi BM25 tunables. The defaults k1=1.2, b=0.75 are the canonical values
# from Robertson & Walker — well-behaved across most corpora; we don't have
# the ground-truth data to tune for ours.
K1 = 1.2
B = 0.75

_TOKEN_RE = re.compile(r"\w+")
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _tokenize(text: str) -> list[str]:
    """Lowercase token list, dropping single-character tokens."""
    return [t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 2]


def _strip_frontmatter(content: str) -> str:
    """Drop a leading YAML frontmatter block so its keys don't dominate scores."""
    match = _FRONTMATTER_RE.match(content)
    if match:
        return content[match.end() :]
    return content


def _safe_md_path_for_index(md_file: Path, content_root: Path) -> Path | None:
    """Resolve *md_file* and return it only if it stays under *content_root*.

    Skips symlinks that escape the content tree (e.g. knowledge/leak.md -> /etc/passwd)
    so their contents are not indexed or returned by recall.
    """
    try:
        resolved = md_file.resolve()
        root = content_root.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _scope_match_clause() -> str:
    """SQL fragment and params for path-boundary-safe scope: exact file or subfolder."""
    return " AND (file_path = ? OR file_path LIKE ?)"


def _scope_match_params(scope: str) -> list[str]:
    s = scope.rstrip("/")
    return [s, s + "/%"]


def _path_in_scope(rel_path: str, scope: str) -> bool:
    """True if *rel_path* is the scope root or a file under it (boundary-safe)."""
    s = scope.rstrip("/")
    return rel_path == s or rel_path.startswith(s + "/")


class BM25Index:
    """Persistent BM25 index over markdown files in the Engram content root."""

    def __init__(self, repo_root: Path, content_root: Path):
        self.repo_root = Path(repo_root)
        self.content_root = Path(content_root)
        self.db_path = self.repo_root / DB_DIR / DB_NAME
        self._ensure_db()

    # -- database setup ----------------------------------------------------

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bm25_terms (
                    term TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    tf INTEGER NOT NULL,
                    PRIMARY KEY (term, file_path)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bm25_docs (
                    file_path TEXT PRIMARY KEY,
                    doc_len INTEGER NOT NULL,
                    mtime REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bm25_meta (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bm25_term ON bm25_terms(term)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bm25_file ON bm25_terms(file_path)")

    # -- incremental indexing ----------------------------------------------

    def _needs_reindex(self, conn: sqlite3.Connection, file_path: str, mtime: float) -> bool:
        row = conn.execute(
            "SELECT mtime FROM bm25_docs WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return True
        return row[0] < mtime

    def index_file(self, rel_path: str, content: str, mtime: float) -> int:
        """Index a single file. Returns the document length (token count)."""
        body = _strip_frontmatter(content)
        tokens = _tokenize(body)
        term_counts = Counter(tokens)
        doc_len = sum(term_counts.values())

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM bm25_terms WHERE file_path = ?", (rel_path,))
            if term_counts:
                conn.executemany(
                    "INSERT INTO bm25_terms (term, file_path, tf) VALUES (?, ?, ?)",
                    [(term, rel_path, tf) for term, tf in term_counts.items()],
                )
            conn.execute(
                "INSERT OR REPLACE INTO bm25_docs (file_path, doc_len, mtime) VALUES (?, ?, ?)",
                (rel_path, doc_len, mtime),
            )
            self._refresh_corpus_meta(conn)
        return doc_len

    def remove_file(self, rel_path: str) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM bm25_terms WHERE file_path = ?", (rel_path,))
            conn.execute("DELETE FROM bm25_docs WHERE file_path = ?", (rel_path,))
            self._refresh_corpus_meta(conn)

    def _refresh_corpus_meta(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*), COALESCE(AVG(doc_len), 0.0) FROM bm25_docs").fetchone()
        total_docs, avg_doc_len = row[0], float(row[1] or 0.0)
        conn.execute(
            "INSERT OR REPLACE INTO bm25_meta (key, value) VALUES (?, ?)",
            ("total_docs", float(total_docs)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bm25_meta (key, value) VALUES (?, ?)",
            ("avg_doc_len", avg_doc_len),
        )

    def build_index(
        self,
        scopes: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """Build or update the BM25 index over the requested scopes.

        Args:
            scopes: Folder paths relative to ``content_root`` to scan. Defaults
                to ``DEFAULT_SCOPES``.
            force: Re-index every file regardless of mtime.

        Returns:
            ``{"indexed", "skipped", "removed", "errors"}`` counts.
        """
        if scopes is None:
            scopes = list(DEFAULT_SCOPES)

        stats = {"indexed": 0, "skipped": 0, "removed": 0, "errors": 0}
        seen: set[str] = set()

        with sqlite3.connect(str(self.db_path)) as conn:
            for scope in scopes:
                scope_dir = self.content_root / scope
                if not scope_dir.is_dir():
                    continue
                for md_file in scope_dir.rglob("*.md"):
                    safe = _safe_md_path_for_index(md_file, self.content_root)
                    if safe is None:
                        stats["errors"] += 1
                        continue
                    rel_path = str(safe.relative_to(self.content_root)).replace("\\", "/")
                    seen.add(rel_path)
                    try:
                        mtime = safe.stat().st_mtime
                    except OSError:
                        stats["errors"] += 1
                        continue
                    if not force and not self._needs_reindex(conn, rel_path, mtime):
                        stats["skipped"] += 1
                        continue
                    try:
                        content = safe.read_text(encoding="utf-8")
                    except OSError:
                        stats["errors"] += 1
                        continue
                    # index_file opens its own connection; close+reopen is cheap
                    # and keeps the COMMIT cycle on per-file boundaries.
                    self.index_file(rel_path, content, mtime)
                    stats["indexed"] += 1

            indexed_files = {
                row[0] for row in conn.execute("SELECT file_path FROM bm25_docs").fetchall()
            }

        for old_file in indexed_files - seen:
            if any(_path_in_scope(old_file, s) for s in scopes):
                self.remove_file(old_file)
                stats["removed"] += 1

        return stats

    # -- query -------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int | None = 30,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return files ranked by Okapi BM25 score against *query*.

        Args:
            query: User query — tokenized the same way as documents.
            limit: Max results. ``None`` returns all matches (use sparingly).
            scope: Path prefix filter (e.g. ``"memory/knowledge"``); ``None``
                searches every indexed file.
        """
        terms = _tokenize(query)
        if not terms:
            return []

        with sqlite3.connect(str(self.db_path)) as conn:
            meta_rows = conn.execute("SELECT key, value FROM bm25_meta").fetchall()
            meta = {k: v for k, v in meta_rows}
            total_docs = int(meta.get("total_docs", 0) or 0)
            avg_doc_len = float(meta.get("avg_doc_len", 0.0) or 0.0)
            if total_docs == 0 or avg_doc_len <= 0:
                return []

            scope_clause = ""
            scope_params: list[str] = []
            if scope:
                scope_clause = _scope_match_clause()
                scope_params = _scope_match_params(scope)

            # df per term, plus per (term, file_path) tf so we score every
            # candidate document in one pass over the postings.
            postings: dict[str, list[tuple[str, int]]] = {}
            df: dict[str, int] = {}
            for term in set(terms):
                rows = conn.execute(
                    "SELECT file_path, tf FROM bm25_terms WHERE term = ?" + scope_clause,
                    [term, *scope_params],
                ).fetchall()
                postings[term] = rows
                df[term] = len(rows)

            # Document lengths for files mentioned in any posting.
            relevant_files: set[str] = set()
            for rows in postings.values():
                relevant_files.update(fp for fp, _ in rows)
            if not relevant_files:
                return []

            placeholders = ",".join("?" for _ in relevant_files)
            doc_lens: dict[str, int] = dict(
                conn.execute(
                    f"SELECT file_path, doc_len FROM bm25_docs WHERE file_path IN ({placeholders})",
                    list(relevant_files),
                ).fetchall()
            )

        scores: dict[str, float] = {fp: 0.0 for fp in relevant_files}
        for term in set(terms):
            n_qi = df.get(term, 0)
            if n_qi == 0:
                continue
            idf = math.log(((total_docs - n_qi + 0.5) / (n_qi + 0.5)) + 1.0)
            for fp, tf in postings[term]:
                doc_len = doc_lens.get(fp, 0)
                if doc_len <= 0:
                    continue
                norm = 1.0 - B + B * (doc_len / avg_doc_len)
                tf_component = (tf * (K1 + 1.0)) / (tf + K1 * norm)
                scores[fp] += idf * tf_component

        ranked = [
            {"file_path": fp, "score": score}
            for fp, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
            if score > 0.0
        ]
        if limit is not None:
            return ranked[:limit]
        return ranked

    # -- introspection -----------------------------------------------------

    def doc_count(self) -> int:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM bm25_docs").fetchone()
            return int(row[0]) if row else 0


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    *,
    rrf_k: int = 60,
    key: str = "file_path",
) -> list[dict[str, Any]]:
    """Combine ranked-result lists via RRF.

    For each candidate appearing in any list, sum ``1 / (rrf_k + rank)``
    across the lists where it appears (1-based rank). Returns a single
    list sorted by the combined RRF score, descending. Ties broken by the
    first list's ordering (stable Python sort).

    The ``rrf_k`` constant is the standard 60 from Cormack/Clarke/Buettcher
    — large enough to dampen the head of each list so a single dominant
    backend doesn't drown out the other.

    Each candidate dict is preserved from the *first* list in which it
    appeared, with one extra key added: ``rrf_score``.
    """
    rrf_scores: dict[Any, float] = {}
    canonical: dict[Any, dict[str, Any]] = {}

    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            ident = item.get(key)
            if ident is None:
                continue
            rrf_scores[ident] = rrf_scores.get(ident, 0.0) + 1.0 / (rrf_k + rank)
            if ident not in canonical:
                canonical[ident] = dict(item)

    fused: list[dict[str, Any]] = []
    for ident, score in sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True):
        merged = canonical[ident]
        merged["rrf_score"] = score
        fused.append(merged)
    return fused


__all__ = [
    "BM25Index",
    "DEFAULT_SCOPES",
    "K1",
    "B",
    "_tokenize",
    "_strip_frontmatter",
    "reciprocal_rank_fusion",
]
