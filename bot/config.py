"""Configuration centralisée (variables d'environnement)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from .autosources import normalize_host
from .parsing import store_key


def load_dotenv(path: str = ".env") -> None:
    """Lit un fichier .env (NOM=valeur) sans dépendance. Les vraies variables d'environnement
    (ex. Railway) restent prioritaires."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:       # utf-8-sig : tolère le Bloc-notes Windows
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _proxy(url: str | None) -> dict | None:
    if not url:
        return None
    u = urlparse(url)
    p = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        p["username"] = unquote(u.username)
        p["password"] = unquote(u.password or "")
    return p


@dataclass(frozen=True)
class Config:
    webhook: str
    admin_webhook: str | None
    discord_token: str | None
    guild_id: int | None
    admin_ids: frozenset
    data_dir: str
    headless: bool
    proxy: dict | None
    brand: str
    drop_threshold: float      # 0.50 = alerte à -50 %
    min_history: int           # relevés nécessaires avant détection
    interval: int              # secondes entre deux contrôles d'un produit
    concurrency: int
    domain_delay: float        # secondes mini entre 2 requêtes sur un même site
    alert_cooldown: int        # secondes avant de ré-alerter le même produit
    confirm_delay: int         # secondes avant la re-vérification d'une anomalie
    block_media: bool
    max_fails: int
    baseline_after: int        # anomalies consécutives -> nouveau prix normal
    discover_interval: int     # secondes entre deux scans d'une page source
    discover_min_discount: float   # remise 'prix barré' minimale (0.70 = -70 %)
    discover_min_price: float  # ignore les produits dont le prix de réf. est inférieur
    discover_max_verify: int   # nb max de fiches vérifiées par scan
    discover_use_old: bool     # utiliser le prix barré comme référence
    listing_scrolls: int       # défilements de la page (chargement paresseux)
    min_interval: int          # plancher des intervalles adaptatifs (secondes)
    adaptive: bool             # intervalles adaptatifs selon la volatilité
    domain_delay_min: float    # plancher du délai adaptatif entre requêtes d'un site
    ready_timeout: float       # attente max (s) que le prix apparaisse dans la page
    block_trackers: bool       # bloquer pubs / analytics
    restart_every: int         # recycler Chromium toutes les N pages
    sites: tuple               # sites explorés pour trouver des pages sources
    auto_enabled: bool
    auto_candidates: int       # pages candidates vérifiées par site et par exploration
    auto_max_sources: int      # sources automatiques max par boutique
    auto_min_items: int        # produits minimum pour qu'une page soit retenue
    auto_refresh: int          # secondes entre deux explorations
    proxy_sites: frozenset     # boutiques qui passent par le proxy (vide = toutes)


def load() -> Config:
    load_dotenv()
    webhook = _env("DISCORD_WEBHOOK")
    if not webhook:
        raise SystemExit("DISCORD_WEBHOOK manquant")
    raw_ids = (_env("ADMIN_IDS", "") or "").replace(" ", "").split(",")
    ids = {int(x) for x in raw_ids if x}
    guild = _env("DISCORD_GUILD_ID")
    return Config(
        webhook=webhook,
        admin_webhook=_env("ADMIN_WEBHOOK"),
        discord_token=_env("DISCORD_TOKEN"),
        guild_id=int(guild) if guild else None,
        admin_ids=frozenset(ids),
        data_dir=_env("DATA_DIR", "./data"),
        headless=_env("HEADLESS", "1") != "0",
        proxy=_proxy(_env("PROXY_URL")),
        brand=_env("BRAND", "PRICE BOT"),
        drop_threshold=float(_env("DROP_THRESHOLD_PERCENT", "50")) / 100,
        min_history=int(_env("MIN_HISTORY", "3")),
        interval=int(_env("CHECK_INTERVAL_MIN", "30")) * 60,
        concurrency=int(_env("CONCURRENCY", "2")),
        domain_delay=float(_env("DOMAIN_DELAY_SEC", "6")),
        alert_cooldown=int(_env("ALERT_COOLDOWN_HOURS", "12")) * 3600,
        confirm_delay=int(_env("CONFIRM_DELAY_SEC", "20")),
        block_media=_env("BLOCK_MEDIA", "1") != "0",
        max_fails=int(_env("MAX_FAILS", "15")),
        baseline_after=int(_env("BASELINE_AFTER", "6")),
        discover_interval=int(_env("DISCOVER_INTERVAL_MIN", "20")) * 60,
        discover_min_discount=float(_env("DISCOVER_MIN_DISCOUNT_PERCENT", "70")) / 100,
        discover_min_price=float(_env("DISCOVER_MIN_PRICE", "20")),
        discover_max_verify=int(_env("DISCOVER_MAX_VERIFY", "5")),
        discover_use_old=_env("DISCOVER_USE_OLD_PRICE", "1") != "0",
        listing_scrolls=int(_env("LISTING_SCROLLS", "8")),
        min_interval=int(_env("MIN_INTERVAL_MIN", "5")) * 60,
        adaptive=_env("ADAPTIVE_INTERVAL", "1") != "0",
        domain_delay_min=float(_env("DOMAIN_DELAY_MIN_SEC", "4")),
        ready_timeout=float(_env("READY_TIMEOUT_SEC", "8")),
        block_trackers=_env("BLOCK_TRACKERS", "1") != "0",
        restart_every=int(_env("RESTART_EVERY_PAGES", "300")),
        sites=tuple(normalize_host(h) for h in (_env("SITES", "") or "").replace(",", " ").split()),
        auto_enabled=_env("AUTO_SOURCES", "1") != "0",
        auto_candidates=int(_env("AUTO_CANDIDATES", "12")),
        auto_max_sources=int(_env("AUTO_MAX_SOURCES", "8")),
        auto_min_items=int(_env("AUTO_MIN_ITEMS", "15")),
        auto_refresh=int(float(_env("AUTO_REFRESH_HOURS", "12")) * 3600),
        proxy_sites=frozenset(
            store_key(t) for t in (_env("PROXY_SITES", "") or "").replace(",", " ").split()),
    )
