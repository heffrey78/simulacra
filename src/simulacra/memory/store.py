"""World memory: one SQLite file, no daemons.

notes.md asked for ChromaDB + Neo4j. This does both jobs in a single file:

* **vectors** -- `sqlite-vec` gives real KNN over `float[768]` (nomic-embed-text),
  verified working on this box's CPython 3.14.
* **graph** -- a typed `edges` table. The queries this game needs are one and two
  hops ("what does this NPC remember", "who died on floor 4"), which is
  comfortably inside what SQL joins do well. Cypher would buy expressiveness we
  have no use for, at the cost of a JVM sharing 10GB with CPU inference.

The `Store` surface below is deliberately narrow so that if the graph queries
ever *do* outgrow SQL, a Neo4j-backed implementation slots in behind it without
the engine noticing.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlite_vec

EMBED_DIM = 768  # nomic-embed-text

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    depth       INTEGER DEFAULT 1,
    turns       INTEGER DEFAULT 0,
    cause       TEXT,
    epitaph     TEXT,
    theme       TEXT,
    -- Recorded so any run can be replayed exactly. A floor you cannot
    -- reproduce is a bug you cannot reproduce.
    seed        INTEGER
);

-- Anything the world can remember: rooms, npcs, items, floors, runs.
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    first_run   INTEGER,
    last_run    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);

-- The graph. Typed, directed, optionally scoped to the run it was formed in.
CREATE TABLE IF NOT EXISTS edges (
    src     TEXT NOT NULL,
    rel     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    run_id  INTEGER,
    weight  REAL DEFAULT 1.0,
    data    TEXT DEFAULT '{}',
    PRIMARY KEY (src, rel, dst, run_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, rel);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, rel);

-- Episodic memory. One row per thing worth recalling later.
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    turn        INTEGER DEFAULT 0,
    depth       INTEGER DEFAULT 0,
    tags        TEXT DEFAULT '[]',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_run ON memories(run_id);

-- Which memories concern which nodes. Drives "what does *this* NPC recall".
CREATE TABLE IF NOT EXISTS memory_subjects (
    memory_id   INTEGER NOT NULL,
    node_id     TEXT NOT NULL,
    PRIMARY KEY (memory_id, node_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ms_node ON memory_subjects(node_id);

-- Generated prose, cached. Regenerating a described room is pure waste at
-- 15 tok/s, and cache hits are what make backtracking feel instant.
CREATE TABLE IF NOT EXISTS prose_cache (
    key         TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
) WITHOUT ROWID;
"""

VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding float[{EMBED_DIM}]
);
"""


def _pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@dataclass(frozen=True)
class Recollection:
    """One recalled memory, with why it surfaced."""

    text: str
    kind: str
    run_id: int
    depth: int
    distance: float
    via: str  # "semantic" | "graph" | "both"

    @property
    def age_runs(self) -> int:
        return self.run_id


class Store:
    def __init__(self, path: Path | str, *, embed_dim: int = EMBED_DIM):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embed_dim = embed_dim
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # One connection, several threads: the turn loop writes memory rows
        # while the embedding worker attaches vectors to them. sqlite3
        # serializes individual statements but not transactions, so without
        # this two interleaved commits silently drop each other's work --
        # observed as 5 vectors for 6 memories.
        self._lock = threading.RLock()
        self.db.executescript(SCHEMA)
        self.db.executescript(VEC_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- runs --------------------------------------------------------------

    def start_run(self, theme: str, seed: int | None = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO runs (started_at, theme, seed) VALUES (?, ?, ?)",
                (time.time(), theme, seed),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def end_run(self, run_id: int, *, cause: str, depth: int, turns: int, epitaph: str = "") -> None:
        with self._lock:
            self.db.execute(
                "UPDATE runs SET ended_at=?, cause=?, depth=?, turns=?, epitaph=? WHERE id=?",
                (time.time(), cause, depth, turns, epitaph, run_id),
            )
            self.db.commit()

    def previous_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM runs WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # -- graph -------------------------------------------------------------

    def upsert_node(
        self, node_id: str, kind: str, name: str, data: dict | None = None, run_id: int | None = None
    ) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO nodes (id, kind, name, data, first_run, last_run)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, data=excluded.data, last_run=excluded.last_run""",
                (node_id, kind, name, json.dumps(data or {}), run_id, run_id),
            )

    def link(
        self, src: str, rel: str, dst: str, *, run_id: int | None = None,
        weight: float = 1.0, data: dict | None = None,
    ) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO edges (src, rel, dst, run_id, weight, data) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(src, rel, dst, run_id) DO UPDATE SET weight = edges.weight + ?""",
                (src, rel, dst, run_id, weight, json.dumps(data or {}), weight),
            )

    def neighbors(self, node_id: str, rel: str | None = None, *, incoming: bool = False) -> list[sqlite3.Row]:
        with self._lock:
            col, other = ("dst", "src") if incoming else ("src", "dst")
            q = f"""SELECT n.*, e.rel, e.weight, e.run_id FROM edges e
                    JOIN nodes n ON n.id = e.{other}
                    WHERE e.{col} = ?"""
            args: list[Any] = [node_id]
            if rel:
                q += " AND e.rel = ?"
                args.append(rel)
            return self.db.execute(q, args).fetchall()

    # -- memories ----------------------------------------------------------

    def remember(
        self,
        run_id: int,
        text: str,
        *,
        kind: str = "event",
        embedding: Sequence[float] | None = None,
        subjects: Iterable[str] = (),
        tags: Iterable[str] = (),
        turn: int = 0,
        depth: int = 0,
    ) -> int:
        with self._lock:
            cur = self.db.execute(
                """INSERT INTO memories (run_id, kind, text, turn, depth, tags, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, kind, text, turn, depth, json.dumps(list(tags)), time.time()),
            )
            mem_id = int(cur.lastrowid)
            for node_id in subjects:
                self.db.execute(
                    "INSERT OR IGNORE INTO memory_subjects (memory_id, node_id) VALUES (?, ?)",
                    (mem_id, node_id),
                )
            if embedding is not None:
                if len(embedding) != self.embed_dim:
                    raise ValueError(f"embedding dim {len(embedding)} != {self.embed_dim}")
                self.db.execute(
                    "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                    (mem_id, _pack(embedding)),
                )
            self.db.commit()
            return mem_id

    def attach_embedding(self, memory_id: int, embedding: Sequence[float]) -> None:
        """Add a vector to a memory written earlier.

        Writing the row and embedding it are deliberately separate: embedding is
        a model call and must not happen on the turn loop, but a memory that
        never gets a vector is still reachable by graph anchor. A lost memory is
        lost; a vectorless one is merely harder to find.
        """
        with self._lock:
            if len(embedding) != self.embed_dim:
                raise ValueError(f"embedding dim {len(embedding)} != {self.embed_dim}")
            self.db.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            self.db.execute(
                "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                (memory_id, _pack(embedding)),
            )
            self.db.commit()

    def recall(
        self,
        embedding: Sequence[float] | None = None,
        *,
        about: str | None = None,
        exclude_run: int | None = None,
        limit: int = 3,
    ) -> list[Recollection]:
        """Hybrid recall: semantic KNN, optionally anchored to a graph node.

        `about` is the whole point of pairing the two. Pure vector search over a
        shared world returns whatever is *topically* closest, which is often
        someone else's business. Anchoring to the node first means an NPC recalls
        only what it was actually present for -- then ranks those by relevance.
        """
        with self._lock:
            rows: list[sqlite3.Row] = []
            via = "semantic"

            if about is not None:
                # Graph-first: restrict the candidate set, then rank.
                base = """SELECT m.*, 0.0 AS distance FROM memories m
                          JOIN memory_subjects s ON s.memory_id = m.id
                          WHERE s.node_id = ?"""
                args: list[Any] = [about]
                if exclude_run is not None:
                    base += " AND m.run_id != ?"
                    args.append(exclude_run)

                if embedding is None:
                    rows = self.db.execute(
                        base + " ORDER BY m.id DESC LIMIT ?", (*args, limit)
                    ).fetchall()
                    via = "graph"
                else:
                    candidates = self.db.execute(base, args).fetchall()
                    if not candidates:
                        return []
                    ids = [r["id"] for r in candidates]
                    placeholders = ",".join("?" * len(ids))
                    rows = self.db.execute(
                        f"""SELECT m.*, v.distance FROM memory_vectors v
                            JOIN memories m ON m.id = v.memory_id
                            WHERE v.embedding MATCH ? AND k = ? AND v.memory_id IN ({placeholders})
                            ORDER BY v.distance""",
                        (_pack(embedding), limit, *ids),
                    ).fetchall()
                    via = "both"
            else:
                if embedding is None:
                    raise ValueError("recall needs an embedding, a subject, or both")
                q = """SELECT m.*, v.distance FROM memory_vectors v
                       JOIN memories m ON m.id = v.memory_id
                       WHERE v.embedding MATCH ? AND k = ?"""
                args = [_pack(embedding), limit if exclude_run is None else limit * 3]
                rows = self.db.execute(q + " ORDER BY v.distance", args).fetchall()
                if exclude_run is not None:
                    rows = [r for r in rows if r["run_id"] != exclude_run][:limit]

            return [
                Recollection(
                    text=r["text"], kind=r["kind"], run_id=r["run_id"],
                    depth=r["depth"], distance=float(r["distance"] or 0.0), via=via,
                )
                for r in rows
            ]

    # -- prose cache -------------------------------------------------------

    def cached_prose(self, key: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT text FROM prose_cache WHERE key=?", (key,)).fetchone()
            return row["text"] if row else None

    def cache_prose(self, key: str, text: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO prose_cache (key, text, created_at) VALUES (?, ?, ?)",
                (key, text, time.time()),
            )
            self.db.commit()

    def commit(self) -> None:
        with self._lock:
            self.db.commit()
