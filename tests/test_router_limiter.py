"""Task 4: the SQLite token limiter."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from tollgate.router.limiter import RateLimited, TokenLimiter, TooLargeForBudget, tenant_id

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "limits.sqlite3")


@pytest.fixture
def limiter(db, clock):
    lim = TokenLimiter(db, limit=50_000, window=60.0, clock=clock)
    yield lim
    lim.close()


async def test_allows_up_to_the_limit(limiter):
    await limiter.reserve("acme", 30_000)
    r = await limiter.reserve("acme", 20_000)
    assert r.remaining == 0
    with pytest.raises(RateLimited):
        await limiter.reserve("acme", 1)


async def test_window_slides_instead_of_resetting(limiter, clock):
    # Fixed buckets would let 50k at :59 and another 50k at :01 through.
    clock.t = 1_059.0
    await limiter.reserve("acme", 50_000)
    clock.t = 1_061.0
    with pytest.raises(RateLimited):
        await limiter.reserve("acme", 1)
    clock.t = 1_118.999
    with pytest.raises(RateLimited):
        await limiter.reserve("acme", 1)
    clock.t = 1_119.0                            # exactly one window later
    await limiter.reserve("acme", 50_000)


async def test_partial_expiry(limiter, clock):
    await limiter.reserve("acme", 20_000)        # t=1000
    clock.t = 1_030.0
    await limiter.reserve("acme", 30_000)        # t=1030
    clock.t = 1_060.5                            # first row has aged out
    await limiter.reserve("acme", 20_000)
    with pytest.raises(RateLimited):
        await limiter.reserve("acme", 1)


async def test_retry_after_is_when_capacity_actually_frees(limiter, clock):
    await limiter.reserve("acme", 10_000)        # t=1000, frees at 1060
    clock.t = 1_020.0
    await limiter.reserve("acme", 40_000)        # t=1020, frees at 1080
    clock.t = 1_030.0
    with pytest.raises(RateLimited) as info:
        await limiter.reserve("acme", 5_000)     # fits once the first row goes
    assert info.value.retry_after == pytest.approx(30.0)
    with pytest.raises(RateLimited) as info:
        await limiter.reserve("acme", 15_000)    # needs both rows gone
    assert info.value.retry_after == pytest.approx(50.0)


async def test_expired_rows_are_evicted(limiter, clock, db):
    for _ in range(5):
        await limiter.reserve("acme", 100)
    clock.t += 61
    await limiter.reserve("acme", 100)
    (rows,) = sqlite3.connect(db).execute("SELECT COUNT(*) FROM token_usage").fetchone()
    assert rows == 1


async def test_settle_corrects_the_estimate(limiter):
    r = await limiter.reserve("acme", 40_000)
    await limiter.settle(r, 1_200)
    assert await limiter.usage("acme") == 1_200
    await limiter.reserve("acme", 48_800)


async def test_release_gives_the_budget_back(limiter):
    r = await limiter.reserve("acme", 50_000)
    await limiter.release(r)
    assert await limiter.usage("acme") == 0
    await limiter.reserve("acme", 50_000)


async def test_tenants_are_isolated(limiter):
    await limiter.reserve("acme", 50_000)
    await limiter.reserve("globex", 50_000)
    with pytest.raises(RateLimited):
        await limiter.reserve("acme", 1)


async def test_request_bigger_than_the_whole_budget(limiter):
    with pytest.raises(TooLargeForBudget):
        await limiter.reserve("acme", 50_001)


async def test_concurrent_reserves_cannot_oversubscribe(limiter):
    async def attempt():
        try:
            await limiter.reserve("acme", 1_000)
            return True
        except RateLimited:
            return False
    results = await asyncio.gather(*[attempt() for _ in range(200)])
    assert sum(results) == 50


def test_two_processes_sharing_one_file(db, clock):
    # Two limiter instances on one file stand in for two gateway workers.
    # BEGIN IMMEDIATE serialises them; a read-then-write without it oversells.
    a = TokenLimiter(db, limit=10_000, clock=clock)
    b = TokenLimiter(db, limit=10_000, clock=clock)
    wins = []

    def worker(lim):
        for _ in range(40):
            try:
                asyncio.run(lim.reserve("acme", 500))
                wins.append(1)
            except RateLimited:
                pass

    threads = [threading.Thread(target=worker, args=(lim,)) for lim in (a, b, a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a.close()
    b.close()
    assert len(wins) == 20


async def test_survives_a_restart(db, clock):
    first = TokenLimiter(db, clock=clock)
    await first.reserve("acme", 45_000)
    first.close()
    second = TokenLimiter(db, clock=clock)
    with pytest.raises(RateLimited):
        await second.reserve("acme", 10_000)
    second.close()


async def test_raw_api_keys_never_hit_the_disk(limiter, db):
    key = "tg-live-super-secret-key"
    await limiter.reserve(tenant_id(key), 10)
    raw = Path(db).read_bytes() + Path(db + "-wal").read_bytes()
    assert key.encode() not in raw
