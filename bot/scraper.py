"""Navigateur Playwright : une page à la fois par site, avec pauses anti-blocage."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from .config import Config
from .parsing import (
    LINK_PATTERNS,
    SELECTORS,
    Item,
    Link,
    Parsed,
    looks_blocked,
    parse_listing_items,
    parse_page,
    store_key,
)
from .scheduling import next_delay

log = logging.getLogger("scraper")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

JS_GRAB = """(sel) => {
  const meta = n => { const e = document.querySelector(
    `meta[property="${n}"],meta[name="${n}"]`); return e ? e.content : null; };
  const text = list => { for (const s of list || []) {
    const e = document.querySelector(s);
    if (e) { const t = (e.textContent || '').trim(); if (t) return t; } } return null; };
  const img = list => { for (const s of list || []) {
    const e = document.querySelector(s);
    if (e) return e.getAttribute('src') || e.getAttribute('data-old-hires'); } return null; };
  return {
    jsonld: [...document.querySelectorAll('script[type="application/ld+json"]')]
              .map(s => s.textContent),
    og_title: meta('og:title'), og_image: meta('og:image'),
    og_price: meta('product:price:amount') || meta('og:price:amount'),
    og_currency: meta('product:price:currency') || meta('og:price:currency'),
    h1: (document.querySelector('h1') || {}).innerText || null,
    title: document.title,
    sel_price: text(sel.price), sel_title: text(sel.title), sel_img: img(sel.img),
    text: (document.body ? document.body.innerText : '').slice(0, 4000),
  };
}"""

# Extrait les cartes produit d'une page liste : on remonte du lien produit jusqu'à
# l'ancêtre le plus haut qui ne contient qu'UN seul produit et qui affiche un prix.
JS_LISTING = """(cfg) => {
  const re = new RegExp(cfg.link);
  const keyOf = h => { const m = h.match(re); return m ? m[0] : null; };
  const hasPrice = t => /[€$£]|EUR|USD|GBP/.test(t || '');
  const OLD = 's, del, strike, [class*="strike"], [class*="a-text-price"], ' +
              '[class*="original"], [class*="list-price"], [class*="old-price"], ' +
              '[class*="oldPrice"], [class*="price--old"]';
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const k = keyOf(a.href);
    if (!k || seen.has(k)) continue;
    let card = a, best = null;
    for (let i = 0; i < 8 && card && card !== document.body; i++, card = card.parentElement) {
      const keys = new Set();
      card.querySelectorAll('a[href]').forEach(x => {
        const kk = keyOf(x.href);
        if (kk) keys.add(kk);
      });
      if (keys.size > 1) break;
      if (hasPrice(card.innerText)) best = card;
    }
    if (!best) continue;
    seen.add(k);
    const img = best.querySelector('img');
    const lines = (best.innerText || '').split('\\n').map(x => x.trim())
      .filter(x => x.length > 12).sort((x, y) => y.length - x.length);
    const title = a.getAttribute('title') || a.getAttribute('aria-label') ||
      (img && img.alt) || (a.innerText || '').trim() || lines[0];
    out.push({
      href: a.href, title: title || '',
      img: img ? (img.currentSrc || img.src || img.getAttribute('data-src')) : null,
      text: (best.innerText || '').slice(0, 600),
      old: [...best.querySelectorAll(OLD)].map(e => (e.textContent || '').trim())
             .filter(Boolean).slice(0, 3),
    });
  }
  return { items: out, title: document.title,
           text: (document.body ? document.body.innerText : '').slice(0, 4000) };
}"""

BLOCK_LIMIT = 3          # blocages consécutifs avant de mettre le site en pause

# Prêt dès que le prix est dans la page (JSON-LD, meta ou sélecteur) : plus besoin d'attendre
# la fin du réseau ni de pauses fixes.
JS_READY = """(sel) => {
  for (const s of document.querySelectorAll('script[type="application/ld+json"]'))
    if (/"(?:low)?[Pp]rice"/.test(s.textContent)) return true;
  if (document.querySelector('meta[property="product:price:amount"],' +
                             'meta[property="og:price:amount"]')) return true;
  for (const q of (sel.price || [])) {
    const e = document.querySelector(q);
    if (e && /[0-9]/.test(e.textContent || '')) return true;
  }
  return false;
}"""

# Nombre de produits distincts présents sur une page liste.
JS_COUNT = """(link) => {
  const re = new RegExp(link), keys = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const m = a.href.match(re);
    if (m) keys.add(m[0]);
  });
  return keys.size;
}"""
# Tous les liens internes d'une page (pour trouver les pages de promos et de catégories).
JS_LINKS = """() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.href); } catch (e) { continue; }
    if (u.hostname !== location.hostname) continue;
    u.hash = '';
    const h = u.toString();
    if (seen.has(h)) continue;
    seen.add(h);
    const t = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim();
    out.push({ href: h, text: t.slice(0, 80) });
    if (out.length >= 2000) break;
  }
  return { links: out, title: document.title,
           text: (document.body ? document.body.innerText : '').slice(0, 4000) };
}"""
JS_HAS_LINKS = JS_COUNT.replace("return keys.size;", "return keys.size > 0;")

# Pubs / analytics : inutiles pour lire un prix. On ne bloque PAS les scripts anti-bot
# (Akamai, DataDome, PerimeterX...) : ils sont nécessaires pour passer les contrôles.
TRACKER_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "adservice.google", "connect.facebook.net", "facebook.com/tr", "hotjar.com", "clarity.ms",
    "criteo.", "taboola.com", "outbrain.com", "amazon-adsystem.com", "adnxs.com",
    "scorecardresearch.com", "quantserve.com", "mathtag.com", "fullstory.com", "mouseflow.com",
    "snapchat.com/tr", "analytics.tiktok.com", "bat.bing.com",
)


def should_block(resource_type: str, url: str, media: bool = True, trackers: bool = True) -> bool:
    if media and resource_type in {"image", "media", "font"}:
        return True
    return trackers and any(h in url for h in TRACKER_HOSTS)


class BlockedError(Exception):
    """Captcha / HTTP 403-429 : le site nous bloque."""


class ScrapeError(Exception):
    """Erreur technique (timeout, page vide...)."""


class Scraper:
    def __init__(self, cfg: Config, on_pause=None):
        self.cfg = cfg
        self.on_pause = on_pause           # async callback(key, minutes)
        self._pw = None
        self._ctxs: dict[bool, object] = {}         # False = direct, True = via proxy
        self._open_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._pause_until: dict[str, float] = {}
        self._delay: dict[str, float] = {}          # délai adaptatif par site
        self._stats: dict[str, dict] = {}
        self._active = 0                            # requêtes en cours
        self._pages = 0                             # pages ouvertes depuis le dernier recyclage
        self._restarting = False
        self._gate = asyncio.Event()                # fermé pendant le recyclage du navigateur
        self._gate.set()

    # --- cycle de vie ---
    async def start(self):
        self._pw = await async_playwright().start()

    def _use_proxy(self, key: str) -> bool:
        """Proxy seulement pour les boutiques de PROXY_SITES (vide = toutes)."""
        if not self.cfg.proxy:
            return False
        return not self.cfg.proxy_sites or key in self.cfg.proxy_sites

    async def _open(self, use_proxy: bool):
        opts = dict(
            user_data_dir=os.path.join(self.cfg.data_dir,
                                       "profile-proxy" if use_proxy else "profile"),
            headless=self.cfg.headless,
            user_agent=USER_AGENT,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1366, "height": 850},
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        if use_proxy:
            opts["proxy"] = self.cfg.proxy
        ctx = await self._pw.chromium.launch_persistent_context(**opts)
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        if self.cfg.block_media or self.cfg.block_trackers:
            await ctx.route("**/*", self._route)
        return ctx

    async def _get_ctx(self, key: str):
        use = self._use_proxy(key)
        async with self._open_lock:                 # évite d'ouvrir 2 fois le même profil
            if use not in self._ctxs:
                self._ctxs[use] = await self._open(use)
            return self._ctxs[use]

    async def _new_page(self, key: str):
        return await (await self._get_ctx(key)).new_page()

    async def _route(self, route):
        req = route.request
        try:
            if should_block(req.resource_type, req.url, self.cfg.block_media,
                            self.cfg.block_trackers):
                await route.abort()
            else:
                await route.continue_()
        except PlaywrightError:
            pass        # page déjà fermée

    async def _close_all(self):
        for ctx in list(self._ctxs.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._ctxs.clear()

    async def _restart(self):
        log.info("recyclage du navigateur (libère la mémoire)")
        await self._close_all()                    # les contextes se rouvrent à la demande

    async def stop(self):
        try:
            await self._close_all()
        finally:
            if self._pw:
                await self._pw.stop()

    # --- état des sites ---
    def paused(self) -> dict[str, int]:
        t = time.monotonic()
        return {k: int((v - t) / 60) + 1 for k, v in self._pause_until.items() if v > t}

    def stats_text(self) -> str:
        lines = []
        for k, st in sorted(self._stats.items()):
            avg = st["secs"] / st["ok"] if st["ok"] else 0
            delay = self._delay.get(k, self.cfg.domain_delay)
            lines.append(f"• {k} : {st['ok']} ok, {st['blocked']} bloqués, "
                         f"{st['error']} erreurs, {avg:.1f} s/page, délai {delay:.0f} s")
        return "\n".join(lines)

    # --- récupération ---
    async def _leave(self):
        """Fin d'une requête : recycle Chromium quand il est inactif et que le quota est atteint."""
        self._active -= 1
        self._pages += 1
        if self._pages >= self.cfg.restart_every and not self._restarting:
            self._restarting = True
            self._gate.clear()          # les nouvelles requêtes attendent
        if self._restarting and self._active == 0:
            try:
                await self._restart()
            finally:
                self._pages = 0
                self._restarting = False
                self._gate.set()

    async def _guarded(self, key: str, job):
        """Exécute `job` : pause du site, 1 requête à la fois, délai adaptatif entre requêtes."""
        if key in self.paused():
            raise BlockedError(f"{key} en pause")
        await self._gate.wait()
        self._active += 1
        try:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                base = self.cfg.domain_delay
                floor, ceiling = self.cfg.domain_delay_min, base * 4
                d = self._delay.get(key, base)
                wait = self._last.get(key, 0) + random.uniform(d, d * 1.5) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                st = self._stats.setdefault(key, {"ok": 0, "blocked": 0, "error": 0, "secs": 0.0})
                t0 = time.monotonic()
                try:
                    result = await job()
                    st["ok"] += 1
                    st["secs"] += time.monotonic() - t0
                    self._fails[key] = 0
                    self._delay[key] = next_delay(d, True, floor, ceiling)
                    return result
                except BlockedError:
                    st["blocked"] += 1
                    self._delay[key] = next_delay(d, False, floor, ceiling)
                    await self._register_block(key)
                    raise
                except ScrapeError:
                    st["error"] += 1
                    raise
                finally:
                    self._last[key] = time.monotonic()
        finally:
            await self._leave()

    async def fetch(self, url: str) -> Parsed:
        key = store_key(url)
        try:
            return await self._guarded(key, lambda: self._fetch_once(url, key))
        except ScrapeError as e:
            if str(e) != "timeout":
                raise
            return await self._guarded(key, lambda: self._fetch_once(url, key))   # 1 nouvel essai

    async def fetch_listing(self, url: str) -> list[Item]:
        key = store_key(url)
        return await self._guarded(key, lambda: self._listing_once(url, key))

    async def fetch_links(self, url: str) -> list[Link]:
        key = store_key(url)
        return await self._guarded(key, lambda: self._links_once(url, key))

    async def _register_block(self, key: str):
        n = self._fails[key] = self._fails.get(key, 0) + 1
        if n >= BLOCK_LIMIT:
            minutes = min(30 * 2 ** (n - BLOCK_LIMIT), 240)
            self._pause_until[key] = time.monotonic() + minutes * 60
            log.warning("%s bloqué %d fois -> pause %d min", key, n, minutes)
            if self.on_pause:
                asyncio.create_task(self.on_pause(key, minutes))

    async def _goto(self, page, url: str):
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if (resp.status if resp else 0) in (403, 429, 503):
            raise BlockedError(f"HTTP {resp.status}")

    async def _fetch_once(self, url: str, key: str) -> Parsed:
        page = await self._new_page(key)
        try:
            await self._goto(page, url)
            sel = SELECTORS.get(key, {})
            try:     # dès que le prix est là, on lit : pas d'attente fixe
                await page.wait_for_function(JS_READY, arg=sel,
                                             timeout=self.cfg.ready_timeout * 1000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(random.randint(200, 600))
            data = await page.evaluate(JS_GRAB, sel)
        except PlaywrightTimeout as e:
            raise ScrapeError("timeout") from e
        except PlaywrightError as e:
            await self._on_pw_error(e)
            raise ScrapeError(str(e)[:200]) from e
        finally:
            await self._close(page)

        parsed = parse_page(data, key)
        if parsed.price is None and looks_blocked(data):
            raise BlockedError("captcha / page de vérification")
        return parsed

    async def _listing_once(self, url: str, key: str) -> list[Item]:
        link = LINK_PATTERNS.get(key)
        if not link:
            raise ScrapeError(f"site non géré : {key}")
        page = await self._new_page(key)
        try:
            await self._goto(page, url)
            try:     # attend l'apparition des premiers produits
                await page.wait_for_function(JS_HAS_LINKS, arg=link,
                                             timeout=self.cfg.ready_timeout * 1000)
            except PlaywrightTimeout:
                pass
            last, stable = -1, 0
            for _ in range(self.cfg.listing_scrolls):      # chargement paresseux
                await page.mouse.wheel(0, random.randint(1000, 1600))
                await page.wait_for_timeout(random.randint(600, 1100))
                n = await page.evaluate(JS_COUNT, link)
                stable = stable + 1 if n == last else 0
                last = n
                if stable >= 2:          # plus de nouveaux produits : on arrête de défiler
                    break
            data = await page.evaluate(JS_LISTING, {"link": link})
        except PlaywrightTimeout as e:
            raise ScrapeError("timeout") from e
        except PlaywrightError as e:
            await self._on_pw_error(e)
            raise ScrapeError(str(e)[:200]) from e
        finally:
            await self._close(page)

        items = parse_listing_items(data.get("items"))
        if not items and looks_blocked(data):
            raise BlockedError("captcha / page de vérification")
        return items[:300]

    async def _links_once(self, url: str, key: str) -> list[Link]:
        page = await self._new_page(key)
        try:
            await self._goto(page, url)
            await page.wait_for_timeout(2500)
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(800)
            data = await page.evaluate(JS_LINKS)
        except PlaywrightTimeout as e:
            raise ScrapeError("timeout") from e
        except PlaywrightError as e:
            await self._on_pw_error(e)
            raise ScrapeError(str(e)[:200]) from e
        finally:
            await self._close(page)
        links = [Link(x["href"], x.get("text", "")) for x in data.get("links") or []]
        if not links and looks_blocked(data):
            raise BlockedError("captcha / page de vérification")
        return links

    async def _on_pw_error(self, e: Exception):
        if "closed" in str(e).lower():
            self._pages = 10**9      # navigateur cassé : recyclage dès qu'il est inactif

    @staticmethod
    async def _close(page):
        try:
            await page.close()
        except Exception:
            pass
