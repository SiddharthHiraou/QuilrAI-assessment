"""Token-aware sliding window rate limiter on SQLite.

Reserve, then settle. A request reserves its worst case (prompt estimate plus
max_tokens) before it goes upstream, inside one BEGIN IMMEDIATE transaction
that both reads the window and writes the reservation. When the provider
reports real usage the row is corrected; if every provider failed the row is
deleted, since an outage shouldn't also eat the tenant's minute.

Counting after the call instead lets N concurrent requests all read the same
"under the limit" total and all go through. Agent traffic hits that constantly
(one turn fanning out ten parallel calls), so it isn't an edge case.

The window is a true sliding window over individual rows, not fixed buckets.
Fixed one-minute buckets let a tenant spend the limit twice across a boundary.
"""

import asyncio
import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id      INTEGER PRIMARY KEY,
    tenant  TEXT    NOT NULL,
    ts      REAL    NOT NULL,
    tokens  INTEGER NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS token_usage_tenant_ts ON token_usage (tenant, ts);
CREATE INDEX IF NOT EXISTS token_usage_ts ON token_usage (ts);
"""


def tenant_id(api_key):
    # The window is keyed by a hash so the database never holds a usable key.
    return hashlib.sha256(api_key.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Reservation:
    row_id: int
    tenant: str
    tokens: int
    remaining: int


class RateLimited(Exception):
    def __init__(self, retry_after, limit, used):
        super().__init__(f"token budget exhausted, retry in {retry_after:.1f}s")
        self.retry_after = retry_after
        self.limit = limit
        self.used = used


class TooLargeForBudget(Exception):
    """One request that could never fit, however long the caller waits."""

    def __init__(self, tokens, limit):
        super().__init__(f"request needs {tokens} tokens, the per-minute budget is {limit}")
        self.tokens = tokens
        self.limit = limit


class TokenLimiter:
    def __init__(self, path, limit=50_000, window=60.0, clock=time.time):
        self.path = path
        self.limit = limit
        self.window = window
        self.clock = clock
        # One thread owns the connection: sqlite3 objects aren't safe to share
        # across threads, and the event loop must never block on disk.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="limiter")
        self._db = None
        self._run_sync(self._open)

    def _open(self):
        db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # WAL so readers don't block the writer; busy_timeout so a second
        # gateway process on the same file waits for the lock instead of
        # failing with "database is locked".
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(SCHEMA)
        self._db = db

    def _run_sync(self, fn, *args):
        return self._pool.submit(fn, *args).result()

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # --- the three phases ----------------------------------------------------

    async def reserve(self, tenant, tokens):
        if tokens > self.limit:
            raise TooLargeForBudget(tokens, self.limit)
        return await self._run(self._reserve, tenant, tokens)

    async def settle(self, reservation, actual_tokens):
        await self._run(self._settle, reservation.row_id, max(0, int(actual_tokens)))

    async def release(self, reservation):
        await self._run(self._release, reservation.row_id)

    async def usage(self, tenant):
        return await self._run(self._used, tenant, self.clock())

    def close(self):
        if self._db is not None:
            self._run_sync(self._db.close)
            self._db = None
        self._pool.shutdown(wait=True)

    # --- sync bodies, run on the limiter thread ------------------------------

    def _used(self, tenant, now):
        (used,) = self._db.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ? AND ts > ?",
            (tenant, now - self.window),
        ).fetchone()
        return used

    def _reserve(self, tenant, tokens):
        now = self.clock()
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            # Eviction rides along with every reserve, so the table never holds
            # much more than one window of rows and there's no sweeper to run.
            db.execute("DELETE FROM token_usage WHERE ts <= ?", (now - self.window,))
            used = self._used(tenant, now)
            if used + tokens > self.limit:
                retry_after = self._retry_after(tenant, now, used, tokens)
                db.execute("COMMIT")
                raise RateLimited(retry_after, self.limit, used)
            cur = db.execute(
                "INSERT INTO token_usage (tenant, ts, tokens) VALUES (?, ?, ?)", (tenant, now, tokens)
            )
            db.execute("COMMIT")
        except RateLimited:
            raise
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return Reservation(cur.lastrowid, tenant, tokens, self.limit - used - tokens)

    def _retry_after(self, tenant, now, used, tokens):
        # When will enough rows have aged out for this request to fit? Walking
        # the rows gives the real answer. A flat "Retry-After: 60" makes every
        # client wait a full window and then all retry in the same second.
        need = used + tokens - self.limit
        freed = 0
        rows = self._db.execute(
            "SELECT ts, tokens FROM token_usage WHERE tenant = ? AND ts > ? ORDER BY ts",
            (tenant, now - self.window),
        )
        for ts, row_tokens in rows:
            freed += row_tokens
            if freed >= need:
                return max(0.0, ts + self.window - now)
        return self.window

    def _settle(self, row_id, tokens):
        self._db.execute("UPDATE token_usage SET tokens = ?, settled = 1 WHERE id = ?", (tokens, row_id))

    def _release(self, row_id):
        self._db.execute("DELETE FROM token_usage WHERE id = ?", (row_id,))
