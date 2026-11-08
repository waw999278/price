"""Découverte automatique de pages à scanner (promos, meilleures ventes, catégories)."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .parsing import LINK_PATTERNS, store_key

# Pages qui contiennent souvent des erreurs de prix : promos, déstockage, reconditionné...
_KW = (r"deals?|offres?|promos?|promotions?|soldes?|sales?|flash|super-?deals?|best-?sellers?|"
       r"meilleures?[ -]ventes|clearance|liquidation|d[ée]stockage|outlet|bonnes?[ -]affaires|"
       r"bons?[ -]plans?|reconditionn\w*|occasions?|warehouse|seconde[ -]main|black[ -]friday|"
       r"cyber[ -]?monday|big-?save|discounts?|r[ée]ductions?|price[ -]drops?|movers|"
       r"hot[ -]?deals?|limited[ -]?time")
KEYWORDS = re.compile(rf"(?<![a-z])(?:{_KW})(?![a-z])", re.I)

# Pages sans intérêt (compte, panier, aide, légal...). Comparaison par mot entier.
EXCLUDE_TOKENS = {
    "login", "signin", "signup", "register", "logout", "account", "profile", "cart", "basket",
    "checkout", "wishlist", "help", "aide", "contact", "privacy", "terms", "cgv", "cgu",
    "careers", "about", "returns", "track", "orders", "cookie", "cookies", "sitemap", "seller",
    "sellers", "sell", "vendre", "legal", "mentions", "newsletter", "download", "apps", "faq",
    "support", "feedback", "advertising", "press", "investor", "investors", "affiliate",
    "affiliates", "customer", "service", "services",
}

# Forme des pages catégorie / liste, par site (le motif est testé sur chemin + query).
CATEGORY_URL = {
    "amazon": re.compile(
        r"/(?:b|s|zgbs|gp/(?:bestsellers|movers-and-shakers|new-releases))(?:[/?]|$)"
        r"|/Best-Sellers", re.I),
    "cdiscount": re.compile(r"/(?:l|c)-[\w-]+\.html", re.I),
    "aliexpress": re.compile(r"/category/\d+|/w/wholesale-", re.I),
    "jumia": re.compile(r"^/[a-z0-9-]+/?$", re.I),
    "alibaba": re.compile(r"/(?:Products|catalog|category|trade/search)", re.I),
}

# Points d'entrée explorés pour trouver des liens (les inconnus sont simplement ignorés).
HUB_PATHS = {
    "amazon": ["/", "/deals", "/gp/bestsellers", "/gp/movers-and-shakers"],
    "jumia": ["/", "/flash-sales/"],
}

SUBDOMAINS = {"www", "fr", "m", "es", "de", "it", "pt", "ar", "en", "uk"}
_TRACKING = re.compile(r"^(?:utm_|pf_rd_|ref|spm|scm|qid|sr$|tag$|linkcode|linkid|psc$|th$|"
                       r"crid|sprefix|gps-id|pvid|algo_|aem_|browser_id|content-id|_encoding)",
                       re.I)


def normalize_host(host: str) -> str:
    """'amazon.fr' -> 'www.amazon.fr' ; 'fr.aliexpress.com' reste tel quel."""
    h = host.strip().lower()
    for prefix in ("https://", "http://"):
        h = h.removeprefix(prefix)
    h = h.strip("/")
    return h if h.split(".")[0] in SUBDOMAINS else f"www.{h}"


def hub_urls(host: str) -> list[str]:
    paths = HUB_PATHS.get(store_key(host), ["/"])
    return [f"https://{host}{p}" for p in paths]


def normalize_candidate(url: str) -> str:
    """Retire paramètres de suivi et segment /ref=... pour éviter les doublons."""
    u = urlparse(url)
    path = re.sub(r"/ref=[^/?#]*", "", u.path)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(u.query) if not _TRACKING.match(k)))
    return urlunparse((u.scheme, u.netloc, path, "", query, ""))


def score_link(url: str, text: str, host: str) -> int:
    """0 = à ignorer ; plus c'est haut, plus la page ressemble à une page de bonnes affaires."""
    try:
        u = urlparse(url)
    except ValueError:
        return 0
    path = u.path or "/"
    if (u.hostname or "") != host or path == "/" or len(url) > 300 or len(u.query) > 160:
        return 0
    if re.search(r"\.(?:jpe?g|png|gif|webp|svg|pdf|zip|css|js|xml|txt)$", path, re.I):
        return 0
    key = store_key(host)
    product = LINK_PATTERNS.get(key)
    if product and re.search(product, path):
        return 0                                   # fiche produit, pas une page liste
    if set(re.split(r"[^a-z0-9]+", path.lower())) & EXCLUDE_TOKENS:
        return 0

    score = 0
    if KEYWORDS.search(text or ""):
        score += 3
    if KEYWORDS.search(path):
        score += 3
    cat = CATEGORY_URL.get(key)
    if cat and cat.search(path + (f"?{u.query}" if u.query else "")):
        score += 2
    if path.strip("/").count("/") >= 5:
        score -= 1
    return max(score, 0)
