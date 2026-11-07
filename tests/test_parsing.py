import json

import pytest

from bot.parsing import canonical_url, looks_blocked, parse_page, store_label, to_float


@pytest.mark.parametrize("raw,expected", [
    ("12.50", 12.5), ("12,50", 12.5), ("1 299,99 €", 1299.99), ("1\u202f299,99 €", 1299.99),
    ("1,299.99", 1299.99), ("1.299,99", 1299.99), ("US $12.50", 12.5), ("0,99", 0.99),
    ("€ 1.299", 1299.0), ("1,234,567", 1234567.0), (19, 19.0), (None, None), ("N/A", None),
])
def test_to_float(raw, expected):
    assert to_float(raw) == expected


def test_canonical_amazon():
    u = "https://www.amazon.fr/Un-Produit-Super/dp/b0abcdef12/ref=sr_1_1?crid=XYZ&tag=foo"
    assert canonical_url(u) == "https://www.amazon.fr/dp/B0ABCDEF12"


def test_canonical_strips_query():
    u = "https://fr.aliexpress.com/item/1005001234567890.html?spm=a2g0o&gps-id=x"
    assert canonical_url(u) == "https://fr.aliexpress.com/item/1005001234567890.html"


def test_canonical_invalid():
    with pytest.raises(ValueError):
        canonical_url("pas une url")


def test_store_label():
    assert store_label("https://www.cdiscount.com/x.html") == ("Cdiscount", "🇫🇷")
    assert store_label("https://fr.aliexpress.com/item/1.html")[1] == "🇨🇳"


def _ld(offers, **kw):
    return json.dumps({"@type": "Product", "name": "Switch", "image": ["http://i/x.jpg"],
                       "offers": offers, **kw})


def test_parse_jsonld():
    d = {"jsonld": [_ld({"@type": "Offer", "price": "199,90", "priceCurrency": "EUR"})]}
    p = parse_page(d)
    assert (p.price, p.currency, p.name, p.image) == (199.9, "EUR", "Switch", "http://i/x.jpg")


def test_parse_jsonld_list_and_graph():
    graph = json.dumps({"@graph": [{"@type": "WebPage"}, {
        "@type": "Product", "name": "X", "offers": [{"price": 5, "priceCurrency": "USD"}]}]})
    assert parse_page({"jsonld": [graph]}).price == 5.0


def test_parse_out_of_stock():
    d = {"jsonld": [_ld({"price": 10, "availability": "https://schema.org/OutOfStock"})]}
    assert parse_page(d).in_stock is False


def test_parse_fallbacks():
    assert parse_page({"og_price": "49.90", "og_currency": "EUR"}).price == 49.9
    p = parse_page({"sel_price": "1 299,00 €", "sel_title": " Mon   produit "})
    assert (p.price, p.currency, p.name) == (1299.0, "EUR", "Mon produit")


def test_parse_nothing():
    assert parse_page({"jsonld": ["{pas du json"]}).price is None


def test_blocked():
    assert looks_blocked({"title": "Robot Check", "text": ""})
    assert not looks_blocked({"title": "Switch Netgear", "text": "prix 199 €"})
