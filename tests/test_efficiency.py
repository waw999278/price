import asyncio
import os
import sqlite3
import tempfile
from datetime import timedelta

import pytest

from bot.db import DB, now
from bot.scheduling import interval_factor, next_delay, next_interval, volatility
from bot.scraper import BlockedError, ScrapeError, Scraper, should_block


# ---------- planification ----------
def test_volatility():
    assert volatility([100, 100]) is None                  # pas assez de relevés
    assert volatility([100, 100, 100, 100]) == 0
    assert volatility([100, 50, 100, 50]) > 0.3


def test_interval_factor():
    stable = [100.0] * 12
    moving = [100, 90, 110, 95, 105, 80]
    assert interval_factor(stable) == 1.5
    assert interval_factor([100.0] * 6) == 1.0             # stable mais historique court
    assert interval_factor(moving) == 0.5
    assert interval_factor(stable, watch_closely=True) == 0.25
    assert interval_factor(moving, adaptive=False) == 1.0
    assert interval_factor([100.0]) == 1.0


def test_next_interval_bounds():
    for _ in range(50):
        v = next_interval(1800, 0.25, 300)
        assert 300 <= v <= 5400
    assert next_interval(1800, 10, 300) <= 5400            # plafond 3 x base


def test_next_delay_aimd():
    d = 6.0
    for _ in range(100):
        d = next_delay(d, True, 4, 24)
    assert d == 4                                          # plancher
    assert next_delay(6, False, 4, 24) == 12               # double si blocage
    assert next_delay(20, False, 4, 24) == 24              # plafond


# ---------- blocage des ressources ----------
def test_should_block():
    assert should_block("image", "https://x/a.jpg")
    assert should_block("font", "https://x/a.woff2")
    assert not should_block("image", "https://x/a.jpg", media=False, trackers=False)
    assert should_block("script", "https://www.googletagmanager.com/gtm.js")
    assert not should_block("script", "https://www.googletagmanager.com/gtm.js", trackers=False)
    assert not should_block("script", "https://www.amazon.fr/app.js")
    assert not should_block("script", "https://client.px-cloud.net/main.min.js")   # anti-bot


# ---------- DB : priorité et migration ----------
def test_due_most_overdue_first_new_first():
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "t.db"))
        for i in range(3):
            db.add_product(f"https://www.cdiscount.com/p{i}.html", "cdiscount")
        ids = {p.url: p.id for p in db.list_products()}
        db.touch_success(ids["https://www.cdiscount.com/p0.html"], "a", 1, "EUR",
                         now() - timedelta(minutes=5))
        db.touch_success(ids["https://www.cdiscount.com/p1.html"], "b", 1, "EUR",
                         now() - timedelta(hours=3))
        db.add_product("https://www.cdiscount.com/new.html", "cdiscount")
        order = [p.url.rsplit("/", 1)[1] for p in db.due()]
        assert order[0] == "new.html" or order[0] == "p2.html"      # jamais contrôlés d'abord
        assert order.index("p1.html") < order.index("p0.html")      # puis le plus en retard


def test_migration_adds_last_found():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.db")
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE sources(id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL,
            store TEXT, active INTEGER DEFAULT 1, added_at TEXT, next_check TEXT,
            last_items INTEGER, fail_count INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO sources(url, store) VALUES('https://www.amazon.fr/deals','amazon')")
        con.commit()
        con.close()
        db = DB(path)                                   # ne doit pas planter
        s = db.list_sources()[0]
        assert s.last_found is None
        db.source_mark_found(s.id)
        assert db.list_sources()[0].last_found is not None


# ---------- scraper : délai adaptatif et recyclage (sans navigateur) ----------
def _scraper(**env):
    os.environ.update(DISCORD_WEBHOOK="http://x", DATA_DIR=tempfile.mkdtemp(),
                      DOMAIN_DELAY_SEC="0.01", DOMAIN_DELAY_MIN_SEC="0.001", **env)
    from bot.config import load
    return Scraper(load())


def test_adaptive_delay_and_stats():
    async def go():
        sc = _scraper()
        sc._restart = lambda: asyncio.sleep(0)

        async def ok():
            return 1

        async def blocked():
            raise BlockedError("captcha")

        async def broken():
            raise ScrapeError("boom")

        assert await sc._guarded("amazon", ok) == 1
        d_ok = sc._delay["amazon"]
        with pytest.raises(BlockedError):
            await sc._guarded("amazon", blocked)
        assert sc._delay["amazon"] > d_ok                 # le blocage double le délai
        with pytest.raises(ScrapeError):
            await sc._guarded("amazon", broken)
        st = sc._stats["amazon"]
        assert (st["ok"], st["blocked"], st["error"]) == (1, 1, 1)
        assert "amazon" in sc.stats_text()
    asyncio.run(go())


def test_browser_recycled_after_quota():
    async def go():
        sc = _scraper(RESTART_EVERY_PAGES="3")
        restarts = []

        async def fake_restart():
            restarts.append(1)
        sc._restart = fake_restart

        async def ok():
            return 1

        for _ in range(7):
            await sc._guarded("cdiscount", ok)
        assert len(restarts) == 2 and sc._gate.is_set() and sc._active == 0
    asyncio.run(go())


def test_recycle_waits_for_inflight_requests():
    async def go():
        sc = _scraper(RESTART_EVERY_PAGES="2")
        events = []

        async def fake_restart():
            events.append("restart")
        sc._restart = fake_restart

        async def slow():
            await asyncio.sleep(0.15)
            events.append("slow done")

        async def quick():
            events.append("quick done")

        # 'slow' (autre site) est en cours quand le quota est atteint
        t = asyncio.create_task(sc._guarded("alibaba", slow))
        await asyncio.sleep(0.02)
        await sc._guarded("amazon", quick)
        await sc._guarded("amazon", quick)
        await t
        assert events.index("slow done") < events.index("restart")   # pas de coupure en vol
    asyncio.run(go())
