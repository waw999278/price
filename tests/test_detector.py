import os
import tempfile

from bot.db import DB
from bot.detector import evaluate

HIST = [100.0, 102.0, 99.0, 101.0]


def test_drop_detected():
    v = evaluate(40.0, HIST, 0.5)
    assert v.alert and v.reason == "chute de prix" and round(v.drop, 2) == 0.6


def test_small_drop_ignored():
    assert not evaluate(80.0, HIST, 0.5).alert


def test_exact_threshold():
    assert evaluate(50.0, [100.0] * 4, 0.5).alert


def test_not_enough_history():
    assert not evaluate(1.0, [100.0, 100.0], 0.5, min_history=3).alert


def test_max_price_without_history():
    v = evaluate(20.0, [], 0.5, max_price=25.0)
    assert v.alert and v.reason == "sous le prix max"


def test_invalid_price():
    assert not evaluate(0, HIST, 0.5).alert
    assert not evaluate(None, HIST, 0.5).alert


def test_median_robust_to_outlier():
    assert not evaluate(70.0, [100, 100, 100, 5, 100], 0.5).alert


def test_db_reference_excludes_anomalies():
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "t.db"))
        db.add_product("https://www.amazon.fr/dp/B0ABCDEF12", "amazon")
        pid = db.list_products()[0].id
        for price, anomaly in [(100, False), (101, False), (5, True), (5, True)]:
            db.record_price(pid, price, "EUR", anomaly)
        assert sorted(db.reference_prices(pid)) == [100, 101]
        assert db.anomaly_streak(pid) == 2
        db.accept_new_baseline(pid)
        assert db.anomaly_streak(pid) == 0


def test_db_add_update_remove():
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "t.db"))
        u = "https://www.cdiscount.com/x.html"
        assert db.add_product(u, "cdiscount") is True
        assert db.add_product(u, "cdiscount", threshold=0.3) is False
        assert db.list_products()[0].threshold == 0.3
        assert len(db.due()) == 1
        assert db.remove_product(u) and not db.list_products()
