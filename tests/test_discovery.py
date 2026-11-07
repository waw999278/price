import asyncio
import os
import tempfile

import pytest

from bot.db import DB
from bot.detector import evaluate_listing
from bot.parsing import Item, clean_source_url, extract_prices, parse_listing_items


# ---------- extraction des prix ----------
def test_extract_prices_basic():
    assert extract_prices("Switch 24 ports\n199,90 €\n249,00 €") == [(199.9, "EUR"), (249.0, "EUR")]
    assert extract_prices("US $12.50 and €1.299,00") == [(12.5, "USD"), (1299.0, "EUR")]
    assert extract_prices("1 299,99 €") == [(1299.99, "EUR")]


def test_extract_prices_ignores_noise():
    txt = "Casque 89,00 €\nou 3x 29,67 €\n4,99 € de livraison\n12,50 €/kg\n9,99 €/mois"
    assert extract_prices(txt) == [(89.0, "EUR")]


def test_extract_prices_none():
    assert extract_prices("Aucun prix ici") == []
    assert extract_prices(None) == []


# ---------- cartes produit ----------
def _card(href, text, old=None, title="Produit test"):
    return {"href": href, "title": title, "img": "http://i/x.jpg", "text": text,
            "old": old or []}


def test_parse_listing_with_old_price():
    items = parse_listing_items([_card("https://a/dp/B0AAAAAAAA", "49,99 €\n199,00 €",
                                       old=["199,00 €"])])
    assert len(items) == 1
    it = items[0]
    assert (it.price, it.old_price, it.currency) == (49.99, 199.0, "EUR")


def test_parse_listing_old_price_not_taken_as_current():
    # le prix barré apparaît en premier dans le texte
    it = parse_listing_items([_card("https://a/1", "199,00 €\n49,99 €", old=["199,00 €"])])[0]
    assert it.price == 49.99


def test_parse_listing_skips_no_price_and_duplicates():
    cards = [_card("https://a/1", "pas de prix"), _card("https://a/2", "10,00 €"),
             _card("https://a/2", "10,00 €")]
    assert [i.url for i in parse_listing_items(cards)] == ["https://a/2"]


def test_old_price_lower_than_current_ignored():
    it = parse_listing_items([_card("https://a/1", "50,00 €", old=["20,00 €"])])[0]
    assert it.old_price is None


def test_clean_source_url_keeps_query():
    u = "https://www.amazon.fr/s?k=ssd&rh=p_8%3A50-#frag"
    assert clean_source_url(u) == "https://www.amazon.fr/s?k=ssd&rh=p_8%3A50-"
    with pytest.raises(ValueError):
        clean_source_url("nope")


# ---------- détecteur ----------
KW = dict(threshold=0.5, min_discount=0.7, min_ref_price=20.0)


def test_listing_old_price_discount():
    v = evaluate_listing(50.0, 400.0, [], **KW)
    assert v.alert and v.reason.startswith("remise") and round(v.drop, 3) == 0.875


def test_listing_discount_too_small():
    assert not evaluate_listing(150.0, 400.0, [], **KW).alert   # -62 % < 70 %


def test_listing_old_price_disabled():
    assert not evaluate_listing(50.0, 400.0, [], use_old=False, **KW).alert


def test_listing_history_drop():
    v = evaluate_listing(40.0, None, [100.0, 99.0, 101.0], **KW)
    assert v.alert and v.reason.startswith("chute")


def test_listing_history_needs_two_points():
    assert not evaluate_listing(10.0, None, [100.0], **KW).alert


def test_listing_ignores_cheap_items():
    assert not evaluate_listing(1.0, 15.0, [], **KW).alert
    assert not evaluate_listing(2.0, None, [15.0, 15.0, 15.0], **KW).alert


# ---------- DB ----------
def test_db_sources_and_listing():
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "t.db"))
        u = "https://www.amazon.fr/deals"
        assert db.add_source(u, "amazon") is True
        assert db.add_source(u, "amazon") is False
        assert len(db.due_sources()) == 1
        from datetime import timedelta

        from bot.db import now
        assert db.source_done(1, now() + timedelta(hours=1), items=42) == 0
        assert db.due_sources() == []
        assert db.source_done(1, now(), fail=True) == 1
        assert db.source_done(1, now(), items=10) == 0       # succès -> remise à zéro

        p = "https://www.amazon.fr/dp/B0AAAAAAAA"
        for price, anomaly in [(100, False), (101, False), (30, True), (30, True)]:
            db.record_listing(p, price, None, anomaly)
        assert sorted(db.listing_history(p)) == [100, 101]
        assert db.listing_streak(p) == 2
        db.listing_accept(p)
        assert db.listing_streak(p) == 0

        assert not db.discovery_recent(p, 3600)
        db.record_discovery(p, 30.0)
        assert db.discovery_recent(p, 3600)
        assert db.remove_source(u) and not db.list_sources()


# ---------- scan complet avec faux scraper ----------
def _app(tmp):
    os.environ.update(DISCORD_WEBHOOK="http://x", DATA_DIR=tmp, DOMAIN_DELAY_SEC="0")
    from bot.main import App
    return App()


def test_scan_source_end_to_end():
    from bot.parsing import Parsed
    with tempfile.TemporaryDirectory() as tmp:
        app = _app(tmp)
        sent = []

        async def fake_send(**kw):
            sent.append(kw)
            return True
        app.notifier.send_alert = fake_send

        listing = [
            Item("https://www.amazon.fr/dp/B0ERROR000", "SSD 2To", 39.0, 399.0, "EUR", None),
            Item("https://www.amazon.fr/dp/B0NORMAL00", "Câble", 25.0, 30.0, "EUR", None),
            Item("https://www.amazon.fr/dp/B0CHEAP000", "Clip", 1.0, 15.0, "EUR", None),
        ]

        class FS:
            async def fetch_listing(self, url):
                return listing

            async def fetch(self, url):          # la fiche confirme le prix
                return Parsed("SSD 2To", 39.0, "EUR", "http://img", True)

        app.scraper = FS()
        app.db.add_source("https://www.amazon.fr/deals", "amazon")
        src = app.db.list_sources()[0]
        asyncio.run(app.scan_source(src))

        assert len(sent) == 1
        assert sent[0]["price"] == 39.0 and sent[0]["ref"] == 399.0
        assert sent[0]["discovered"] is True
        # re-scan immédiat : cooldown -> pas de doublon
        asyncio.run(app.scan_source(src))
        assert len(sent) == 1


def test_scan_source_rejects_unconfirmed_price():
    from bot.parsing import Parsed
    with tempfile.TemporaryDirectory() as tmp:
        app = _app(tmp)
        sent = []

        async def fake_send(**kw):
            sent.append(kw)
            return True
        app.notifier.send_alert = fake_send

        class FS:
            async def fetch_listing(self, url):
                return [Item("https://www.amazon.fr/dp/B0FAKE0000", "X", 39.0, 399.0, "EUR")]

            async def fetch(self, url):          # la fiche affiche le vrai prix : pas d'erreur
                return Parsed("X", 399.0, "EUR", None, True)

        app.scraper = FS()
        app.db.add_source("https://www.amazon.fr/deals", "amazon")
        asyncio.run(app.scan_source(app.db.list_sources()[0]))
        assert sent == []
