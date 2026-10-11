"""Atomic ingestion: cursor, parser state and delivery records commit together."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .models import Event, canonical


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    def _open(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    async def run(self, fn):
        async with self.lock:
            # Shield the worker; cancellation must not close the lock while a transaction runs.
            task = asyncio.create_task(asyncio.to_thread(self._run_sync, fn))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _run_sync(self, fn):
        # sqlite3's transaction context commits/rolls back but does not close
        # the connection. Close explicitly rather than retaining Windows file
        # handles and descriptors until a later garbage collection.
        with closing(self._open()) as db, db:
            return fn(db)

    async def initialize(self):
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)

        def create(db):
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(
                    event_id TEXT, target TEXT, payload TEXT NOT NULL, level TEXT NOT NULL,
                    due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, PRIMARY KEY(event_id,target));
                CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(due);
                CREATE TABLE IF NOT EXISTS cooldowns(
                    key TEXT PRIMARY KEY, until_at REAL NOT NULL);
            """)

        await self.run(create)

    async def get(self, key, default=None):
        def read(db):
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

        return await self.run(read)

    async def put(self, key, value):
        await self.ingest({key: value}, [], [], None)

    async def ingest(self, state, events, targets, settings):
        now = time.time()

        def write(db):
            for key, value in state.items():
                db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, canonical(value)))
            for event in events:
                inserted = db.execute("INSERT OR IGNORE INTO events VALUES(?,?)", (event.id, now))
                if not inserted.rowcount or settings is None:
                    continue
                policy = settings.policy(event.kind)
                if not policy["enabled"]:
                    continue
                level = policy["level"]
                due = now if level == "high" else now + settings["notifications"]["merge_seconds"]
                for target in targets:
                    db.execute(
                        "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,0,?)",
                        (event.id, target, canonical(event.to_dict()), level, due, now),
                    )
            limit = settings["notifications"]["queue_limit"] if settings else None
            # Never advance a log cursor while silently dropping undelivered facts.
            if limit and db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] > limit:
                raise RuntimeError("notification_queue_full")

        await self.run(write)

    async def due(self, now, limit=100, level=None):
        return await self.run(
            lambda db: [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM deliveries WHERE due<=? AND (? IS NULL OR level=?) "
                    "ORDER BY CASE level WHEN 'high' THEN 0 ELSE 1 END, due LIMIT ?",
                    (now, level, level, limit),
                )
            ]
        )

    async def complete(self, rows, cooldown_key=None, until_at=0):
        def write(db):
            db.executemany(
                "DELETE FROM deliveries WHERE event_id=? AND target=?",
                [(x["event_id"], x["target"]) for x in rows],
            )
            if cooldown_key:
                db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (cooldown_key, until_at))

        await self.run(write)

    async def postpone(self, rows, due, failed=False):
        await self.run(
            lambda db: db.executemany(
                "UPDATE deliveries SET due=?, attempts=attempts+? WHERE event_id=? AND target=?",
                [(due, int(failed), x["event_id"], x["target"]) for x in rows],
            )
        )

    async def cooldown(self, key):
        def read(db):
            row = db.execute("SELECT until_at FROM cooldowns WHERE key=?", (key,)).fetchone()
            return row[0] if row else 0

        return await self.run(read)

    async def pending_count(self):
        return await self.run(
            lambda db: db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        )

    async def prune(self, retention_days):
        cutoff = time.time() - retention_days * 86400

        def clean(db):
            db.execute(
                "DELETE FROM events WHERE created<? AND id NOT IN "
                "(SELECT event_id FROM deliveries)",
                (cutoff,),
            )
            db.execute("DELETE FROM cooldowns WHERE until_at<?", (time.time(),))

        await self.run(clean)

    @staticmethod
    def decode(row):
        return Event.from_dict(json.loads(row["payload"]))
