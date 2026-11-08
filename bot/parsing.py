"""Extraction pure (sans réseau) : testable sans navigateur."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

STORES = {
    "amazon": "Amazon",
    "cdiscount": "Cdiscount",
    "aliexpress": "AliExpress",
    "alibaba": "Alibaba",
    "jumia": "Jumia",
}
TLD_FLAGS = {"fr": "🇫🇷", "com": "🇺🇸", "de": "🇩🇪", "es": "🇪🇸", "it": "🇮🇹", "uk": "🇬🇧",
             "nl": "🇳🇱", "be": "🇧🇪",
             "ng": "🇳🇬", "eg": "🇪🇬", "ke": "🇰🇪", "ma": "🇲🇦", "gh": "🇬🇭", "sn": "🇸🇳",
             "ci": "🇨🇮", "tn": "🇹🇳", "dz": "🇩🇿", "ug": "🇺🇬"}

# Sélecteurs de secours (JSON-LD et meta tags passent en premier). À ajuster si le site change.
SELECTORS = {
    "amazon": {
        "price": ["#corePrice_feature_div span.a-price:not(.a-text-price) span.a-offscreen",
                  "span.a-price:not(.a-text-price) span.a-offscreen",
                  "#priceblock_dealprice", "#priceblock_ourprice"],
        "title": ["#productTitle"],
        "img": ["#landingImage"],
    },
    "aliexpress": {
        "price": ["[class*='price-default--current']", "[class*='product-price-current']",
                  "[class*='price--currentPriceText']"],
        "title": ["h1"],
        "img": [],
    },
    "alibaba": {
        "price": ["[data-testid='price']", "[class*='price-item'] [class*='price']",
                  "[class*='price'] span"],
        "title": ["h1"],
        "img": [],
    },
    "jumia": {
        "price": ["span.-b.-fs24", "[class*='-fs24']", "[class*='-b'][class*='-ubpt']"],
        "title": ["h1"],
        "img": [],
    },
    "cdiscount": {
        "price": ["[itemprop='price']", ".fpPrice", ".price"],
        "title": ["h1"],
        "img": [],
    },
}

# Motif d'URL d'une fiche produit, par site (sert à repérer les produits sur une page liste)
LINK_PATTERNS = {
    "amazon": r"/(?:dp|gp/product)/[A-Z0-9]{10}",
    "cdiscount": r"/f-\d+-[^/?#]+\.html",
    "aliexpress": r"/item/\d+\.html",
    "alibaba": r"/product-detail/[^/?#]+\.html",
    "jumia": r"/[^/?#]+-\d{5,}\.html",
}

BLOCK_HINTS = (
    "captcha", "saisissez les caractères", "enter the characters you see", "robot check",
    "automated access", "punish", "slide to verify", "verify you are human",
    "unusual traffic", "access denied", "accès refusé",
)


@dataclass
class Parsed:
    name: str
    price: float | None
    currency: str | None = None
    image: str | None = None
    in_stock: bool = True


def store_key(url: str) -> str:
    """Clé de boutique pour une URL ou un simple nom d'hôte (ex. 'www.amazon.fr')."""
    host = (urlparse(url).netloc or urlparse("//" + url).netloc).lower()
    for k in STORES:
        if k in host:
            return k
    return host


def store_label(url: str) -> tuple[str, str]:
    key = store_key(url)
    name = STORES.get(key, key)
    host = urlparse(url).netloc.lower()
    if key in ("aliexpress", "alibaba"):
        return name, "🇨🇳"
    if key == "cdiscount":
        return name, "🇫🇷"
    flag = TLD_FLAGS.get(host.rsplit(".", 1)[-1], "")
    return name, flag


def canonical_url(url: str) -> str:
    """Normalise l'URL (retire tracking/paramètres) pour éviter les doublons."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("URL invalide")
    host = u.netloc.lower()
    if "amazon" in host:
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", u.path, re.I)
        if m:
            return f"https://{host}/dp/{m.group(1).upper()}"
    return urlunparse(("https", host, u.path.rstrip("/") or "/", "", "", ""))


_NUM = re.compile(r"\d[\d\s.,\u00a0\u202f]*\d|\d")


def to_float(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = _NUM.search(str(x))
    if not m:
        return None
    s = re.sub(r"[\s\u00a0\u202f]", "", m.group())
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if dec == "," else ","
        s = s.replace(thousands, "").replace(dec, ".")
    elif dots + commas > 1:
        s = s.replace(".", "").replace(",", "")
    elif dots + commas == 1:
        sep = "." if dots else ","
        head, tail = s.split(sep)
        if len(tail) == 3 and head not in ("", "0") and len(head) <= 3:
            s = head + tail
        else:
            s = f"{head}.{tail}"
    try:
        return float(s)
    except ValueError:
        return None


def guess_currency(text: str | None) -> str | None:
    if not text:
        return None
    for sym, cur in (("€", "EUR"), ("EUR", "EUR"), ("£", "GBP"), ("GBP", "GBP"),
                     ("₦", "NGN"), ("NGN", "NGN"), ("KSh", "KES"), ("Ksh", "KES"),
                     ("KES", "KES"), ("EGP", "EGP"), ("E£", "EGP"), ("MAD", "MAD"),
                     ("Dhs", "MAD"), ("DH", "MAD"), ("GH₵", "GHS"), ("GHS", "GHS"),
                     ("FCFA", "XOF"), ("XOF", "XOF"),
                     ("US", "USD"), ("$", "USD"), ("USD", "USD")):
        if sym in text:
            return cur
    return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def parse_page(data: dict, key: str = "") -> Parsed:
    """data = dictionnaire renvoyé par le JS d'extraction (voir scraper.JS_GRAB)."""
    name = data.get("og_title") or data.get("h1") or data.get("title") or ""
    img = data.get("og_image")
    price = currency = None
    in_stock = True

    for raw in data.get("jsonld") or []:
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        for n in _walk(parsed):
            t = n.get("@type")
            types = [str(x).lower() for x in (t if isinstance(t, list) else [t])]
            if "product" not in types:
                continue
            offers = n.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                continue
            raw_price = offers.get("price")
            if raw_price is None:
                raw_price = offers.get("lowPrice")
            p = to_float(raw_price)
            if p is None:
                continue
            price = p
            currency = offers.get("priceCurrency")
            name = n.get("name") or name
            im = n.get("image")
            img = (im[0] if isinstance(im, list) and im else im) or img
            if "outofstock" in str(offers.get("availability", "")).lower():
                in_stock = False
            break
        if price is not None:
            break

    if price is None:
        price = to_float(data.get("og_price"))
        currency = currency or data.get("og_currency")

    if price is None:
        price = to_float(data.get("sel_price"))
        currency = currency or guess_currency(data.get("sel_price"))

    if data.get("sel_title"):
        name = data["sel_title"]
    if data.get("sel_img"):
        img = data["sel_img"]
    if not isinstance(img, str):
        img = None

    return Parsed(name=" ".join(str(name).split())[:250], price=price,
                  currency=currency, image=img, in_stock=in_stock)


def looks_blocked(data: dict) -> bool:
    blob = f"{data.get('title', '')} {data.get('text', '')}".lower()
    return any(h in blob for h in BLOCK_HINTS)


# ---------------- pages liste (découverte) ----------------
@dataclass
class Item:
    url: str
    name: str
    price: float
    old_price: float | None = None
    currency: str | None = None
    image: str | None = None


_AMT = r"(\d[\d \u00a0\u202f.,]*\d|\d)"
_CUR = (r"(?:€|£|US ?\$|\$|EUR|USD|GBP|₦|NGN|KSh|Ksh|KES|EGP|E£|MAD|Dhs|DH|GH₵|GHS|"
        r"FCFA|XOF)")
_SP = r"[ \u00a0\u202f]?"          # espace sur la MÊME ligne (jamais un saut de ligne)
_PRICE_RE = re.compile(rf"(?:({_CUR}){_SP}{_AMT})|(?:{_AMT}{_SP}({_CUR}))")
_NOISE_AFTER = re.compile(
    r"^\s*(?:/|par |de |d')?\s*(?:mois|mo\b|month|livr|ship|frais|port|kg|l\b|100|unit)", re.I)
_NOISE_BEFORE = re.compile(r"\d\s?[x×]\s*$", re.I)


def extract_prices(text: str | None) -> list[tuple[float, str | None]]:
    """Montants avec devise trouvés dans un texte, dans l'ordre. Ignore 3x, /mois, livraison."""
    out = []
    for m in _PRICE_RE.finditer(text or ""):
        cur_a, amt_a, amt_b, cur_b = m.groups()
        amount, cur = (amt_a, cur_a) if amt_a else (amt_b, cur_b)
        if _NOISE_BEFORE.search((text or "")[max(0, m.start() - 6):m.start()]):
            continue
        if _NOISE_AFTER.match((text or "")[m.end():m.end() + 14]):
            continue
        v = to_float(amount)
        if v is not None and v > 0:
            out.append((v, guess_currency(cur)))
    return out


def parse_listing_items(raw_items: list[dict]) -> list[Item]:
    """raw_items = cartes produit renvoyées par le JS (href, title, img, text, old[])."""
    items, seen = [], set()
    for r in raw_items or []:
        href = r.get("href")
        if not href or href in seen:
            continue
        old_vals = []
        for t in r.get("old") or []:
            amounts = extract_prices(t) or [(to_float(t), None)]
            old_vals += [v for v, _ in amounts if v]
        current = [(v, c) for v, c in extract_prices(r.get("text")) if v not in old_vals]
        if not current:
            continue
        price, cur = current[0]
        old = max(old_vals) if old_vals and max(old_vals) > price else None
        seen.add(href)
        items.append(Item(url=href, name=" ".join(str(r.get("title") or "").split())[:250],
                          price=price, old_price=old, currency=cur, image=r.get("img") or None))
    return items


@dataclass
class Link:
    href: str
    text: str = ""


def clean_source_url(url: str) -> str:
    """URL de page liste : on GARDE la query (recherche, filtres), on retire le fragment."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("URL invalide")
    return urlunparse((u.scheme, u.netloc.lower(), u.path, u.params, u.query, ""))
