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
import random
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

# Bumped whenever a schema change makes an older build unable to read this file
# correctly. A world someone has played twenty runs into must be able to *say*
# it was written by a different build; a stack trace against a changed table is
# the failure mode this exists to prevent. M7 adds the first forward migration.
SCHEMA_VERSION = 1


# `edges` is WITHOUT ROWID, which makes every primary-key column implicitly
# NOT NULL -- so `run_id=None` on a link has never actually been insertable,
# despite the signature offering it since M0. World-scoped edges (a wall between
# two rooms, which is true of the world rather than of one run) use this
# sentinel instead, and dedupe across runs because it is a constant.
WORLD_SCOPE = 0


class WorldError(RuntimeError):
    """Something about this world file stops us opening it. Names the way out."""


class WorldVersionError(WorldError):
    """This database was written by a different build."""


def archive_world(path: Path | str) -> Path | None:
    """Move a world file aside, siblings and all. Returns where it went.

    Archive, never delete. The whole premise of a persistent world is that
    losing it costs something, and disk is not the constrained resource on this
    box -- a destructive default on a file someone has spent twenty runs filling
    is the wrong default. Returns None when there was nothing to move.
    """
    path = Path(path)
    if not path.exists():
        return None

    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    n = 2
    while dest.exists():  # twice in one second is rare, not impossible
        dest = path.with_name(f"{path.stem}-{stamp}-{n}{path.suffix}")
        n += 1

    path.rename(dest)
    # WAL mode means the real contents may still be sitting in these.
    for suffix in ("-wal", "-shm"):
        side = Path(f"{path}{suffix}")
        if side.exists():
            side.rename(Path(f"{dest}{suffix}"))
    return dest


SCHEMA = """
-- The world's own identity. Exactly one row, ever.
--
-- `world_seed` is what makes floors persist: layout at every depth is a pure
-- function of it, so a floor is *recomputed* rather than stored. Splitting it
-- from the per-run dice seed is what stops a combat roll changing the shape of
-- the next floor (see engine/state.py).
CREATE TABLE IF NOT EXISTS world (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    world_seed     INTEGER NOT NULL,
    theme          TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at     REAL NOT NULL
);

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
    def __init__(
        self,
        path: Path | str,
        *,
        embed_dim: int = EMBED_DIM,
        theme: str = "",
        world_seed: int | None = None,
    ):
        """Open (or create) a world.

        `theme` and `world_seed` are used **only** when this file has no world
        row yet -- opening an existing world never rewrites its identity. They
        default to empty/random so that a bare `Store(tmp_path)` in a test still
        gets a valid world without knowing this table exists.
        """
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
        # Before the schema is created, while "has no world table" still
        # distinguishes a pre-M6 file from a brand new one.
        self._refuse_legacy()
        self.db.executescript(SCHEMA)
        self.db.executescript(VEC_SCHEMA)
        self.db.commit()
        self._ensure_world(theme, world_seed)

    # -- world -------------------------------------------------------------

    def _has_table(self, name: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _refuse_legacy(self) -> None:
        """A world from before floors persisted is not opened, and not migrated.

        An empty file and a POC-era file both lack the `world` table; recorded
        runs are what tells them apart. Migrating is possible and pointless --
        those runs walked floors generated from a throwaway per-run seed, so
        their room ids refer to rooms that no longer exist and never will.
        """
        if self._has_table("world") or not self._has_table("runs"):
            return
        played = self.db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        if played:
            raise WorldVersionError(
                f"{self.path} predates the persistent world "
                f"({played} runs recorded, no world table). Archive it with: "
                f"simulacra --new-world"
            )

    def _ensure_world(self, theme: str, world_seed: int | None) -> None:
        with self._lock:
            row = self.db.execute("SELECT * FROM world WHERE id = 1").fetchone()
            if row is None:
                self.db.execute(
                    """INSERT INTO world (id, world_seed, theme, schema_version, created_at)
                       VALUES (1, ?, ?, ?, ?)""",
                    (
                        random.randrange(1 << 30) if world_seed is None else int(world_seed),
                        theme,
                        SCHEMA_VERSION,
                        time.time(),
                    ),
                )
                self.db.commit()
                return

            if row["schema_version"] != SCHEMA_VERSION:
                raise WorldVersionError(
                    f"{self.path} is schema version {row['schema_version']}; this build "
                    f"speaks {SCHEMA_VERSION}. Archive it with: simulacra --new-world"
                )

            # A world created by a bare Store() has no theme yet. Claiming it on
            # the first real open beats a second table or a nullable column.
            if theme and not row["theme"]:
                self.db.execute("UPDATE world SET theme = ? WHERE id = 1", (theme,))
                self.db.commit()

    def world(self) -> sqlite3.Row:
        with self._lock:
            return self.db.execute("SELECT * FROM world WHERE id = 1").fetchone()

    def run_count(self) -> int:
        with self._lock:
            return int(self.db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])

    def claim_world_seed(self, seed: int | None) -> int:
        """Set the world seed, but only while the world is still untouched.

        This is what makes `--seed` context-dependent rather than ambiguous. On
        a world nobody has played it means "give me this dungeon", the sense it
        has always had. On a world with runs behind it, changing the layout
        under floors the player has already walked is not something a flag
        should silently do -- there it is the run seed, and the caller keeps it
        for the dice.
        """
        current = int(self.world()["world_seed"])
        if seed is None or self.run_count():
            return current
        with self._lock:
            self.db.execute("UPDATE world SET world_seed = ? WHERE id = 1", (int(seed),))
            self.db.commit()
        return int(seed)

    # -- lifecycle ---------------------------------------------------------

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
        """Add or reinforce an edge.

        `run_id=None` means the edge is a fact about the world rather than about
        one run -- stored as `WORLD_SCOPE` so that re-persisting the same floor
        on every run reinforces one row instead of adding an identical one.
        """
        run_id = WORLD_SCOPE if run_id is None else run_id
        with self._lock:
            self.db.execute(
                """INSERT INTO edges (src, rel, dst, run_id, weight, data) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(src, rel, dst, run_id) DO UPDATE SET weight = edges.weight + ?""",
                (src, rel, dst, run_id, weight, json.dumps(data or {}), weight),
            )

    def node(self, node_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()

    def floor_names(self, *, below_depth: int, limit: int = 3) -> list[str]:
        """Names of the world's already-identified floors, nearest first.

        Feeds the director's do-not-repeat list. Reading it from the world
        rather than from this run's traversal matters once floors persist: run
        2's first descent has walked nothing, and would otherwise tell the
        director to avoid nothing while the world already uses those names.
        """
        with self._lock:
            rows = self.db.execute(
                """SELECT name, data FROM nodes
                   WHERE kind = 'floor' AND json_extract(data, '$.depth') < ?
                   ORDER BY json_extract(data, '$.depth') DESC LIMIT ?""",
                (below_depth, limit),
            ).fetchall()
        out = []
        for r in rows:
            name = (json.loads(r["data"] or "{}").get("theme_name") or "").strip()
            if name:
                out.append(name)
        return out

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

    def forget(self, node_id: str) -> int:
        """Erase what the world remembers *about* one node, keeping the node.

        The escape hatch a persistent world needs: a generated persona that goes
        bad is permanent unless something can retire it, and retiring one NPC is
        a far better answer than discarding a world.

        Subject links go first and memories only follow when nothing else claims
        them -- a death is subject to both the NPC who witnessed it and the room
        it happened in, and forgetting the NPC must not quietly erase the room's
        history too. Returns the number of memories actually removed.
        """
        with self._lock:
            self.db.execute("DELETE FROM memory_subjects WHERE node_id = ?", (node_id,))
            orphans = [
                r["id"] for r in self.db.execute(
                    """SELECT id FROM memories
                       WHERE id NOT IN (SELECT memory_id FROM memory_subjects)"""
                )
            ]
            for mid in orphans:
                self.db.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (mid,))
                self.db.execute("DELETE FROM memories WHERE id = ?", (mid,))
            self.db.commit()
            return len(orphans)

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
