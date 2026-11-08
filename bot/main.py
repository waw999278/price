"""Point d'entrée : python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta

from .autosources import hub_urls, normalize_candidate, score_link
from .config import load
from .db import DB, now
from .detector import evaluate, evaluate_listing
from .notifier import Notifier
from .parsing import STORES, canonical_url, clean_source_url, store_key
from .scheduling import interval_factor, next_interval
from .scraper import BlockedError, ScrapeError, Scraper

log = logging.getLogger("bot")


class App:
    def __init__(self):
        self.cfg = load()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.db = DB(os.path.join(self.cfg.data_dir, "prices.db"))
        self.notifier = Notifier(self.cfg)
        self.scraper = Scraper(self.cfg, on_pause=self._on_pause)
        self.sem = asyncio.Semaphore(self.cfg.concurrency)
        self.running: set[int] = set()
        self._refresh_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()

    # ---------- utilitaires ----------
    async def _on_pause(self, key: str, minutes: int):
        await self.notifier.send_admin(
            f"⚠️ **{key}** nous bloque (captcha/403). Pause {minutes} min. "
            "Pense à un proxy résidentiel (PROXY_URL).")

    def status_text(self) -> str:
        s = self.db.stats()
        paused = self.scraper.paused()
        txt = (f"✅ {s['active']} produits actifs • {s['disabled']} désactivés • "
               f"{s['prices']} relevés • {s['alerts_today']} alertes aujourd'hui\n"
               f"🔎 {s['sources']} pages sources • "
               f"{s['discoveries_today']} découvertes aujourd'hui")
        perf = getattr(self.scraper, "stats_text", lambda: "")()
        if perf:
            txt += "\n⚡ Performance\n" + perf
        if paused:
            txt += "\n⏸️ En pause : " + ", ".join(f"{k} ({m} min)" for k, m in paused.items())
        return txt

    def seed(self):
        urls = (os.environ.get("WATCHLIST", "")).replace(",", " ").split()
        path = os.path.join(self.cfg.data_dir, "watchlist.txt")
        for p in (path, "watchlist.txt"):
            if os.path.exists(p):
                with open(p) as f:
                    urls += [x.strip() for x in f if x.strip() and not x.startswith("#")]
        for u in urls:
            try:
                u = canonical_url(u)
            except ValueError:
                continue
            if store_key(u) in STORES:
                self.db.add_product(u, store_key(u))

    def seed_sources(self):
        urls = os.environ.get("SOURCES", "").split()
        for p in (os.path.join(self.cfg.data_dir, "sources.txt"), "sources.txt"):
            if os.path.exists(p):
                with open(p) as f:
                    urls += [x.strip() for x in f if x.strip() and not x.startswith("#")]
        for u in urls:
            try:
                u = clean_source_url(u)
            except ValueError:
                continue
            if store_key(u) in STORES:
                self.db.add_source(u, store_key(u))

    def _cooldown_ok(self, pid: int, price: float) -> bool:
        last = self.db.last_alert(pid)
        if not last:
            return True
        last_price, ts = last
        if (now() - ts).total_seconds() >= self.cfg.alert_cooldown:
            return True
        return price < last_price * 0.8   # nouvelle baisse nette -> on ré-alerte

    # ---------- traitement d'un produit ----------
    async def process(self, p):
        cfg = self.cfg
        async with self.sem:
            try:
                parsed = await self.scraper.fetch(p.url)
            except BlockedError as e:
                log.warning("bloqué %s : %s", p.url[:60], e)
                self.db.touch_failure(p.id, now() + timedelta(seconds=cfg.interval), count=False)
                return
            except ScrapeError as e:
                return await self._failure(p, str(e))

            if parsed.price is None or not parsed.in_stock:
                return await self._failure(p, "prix introuvable ou rupture",
                                           soft=not parsed.in_stock)

            hist = self.db.reference_prices(p.id)
            verdict = evaluate(parsed.price, hist, p.threshold or cfg.drop_threshold,
                               p.max_price, cfg.min_history)
            anomaly = verdict.alert
            notify = verdict.alert and await self._confirm(p, parsed.price)

            self.db.record_price(p.id, parsed.price, parsed.currency, anomaly)
            if anomaly and self.db.anomaly_streak(p.id) >= cfg.baseline_after:
                log.info("nouveau prix normal accepté : %s", p.url[:60])
                self.db.accept_new_baseline(p.id)

            if notify and self._cooldown_ok(p.id, parsed.price):
                sent = await self.notifier.send_alert(
                    url=p.url, name=parsed.name or p.name, price=parsed.price,
                    currency=parsed.currency or p.currency, ref=verdict.ref,
                    drop=verdict.drop, image=parsed.image, reason=verdict.reason)
                if sent:
                    self.db.record_alert(p.id, parsed.price)
                log.info("ALERTE %s -> %s (réf %s)", parsed.name[:50], parsed.price, verdict.ref)
            else:
                log.info("ok %s -> %s", (parsed.name or p.url)[:50], parsed.price)

            watch = anomaly or self.db.anomaly_streak(p.id) > 0
            factor = interval_factor(self.db.reference_prices(p.id, 12), watch, cfg.adaptive)
            delay = next_interval(cfg.interval, factor, cfg.min_interval)
            self.db.touch_success(p.id, parsed.name, parsed.price, parsed.currency,
                                  now() + timedelta(seconds=delay))

    async def _confirm(self, p, first_price: float) -> bool:
        """Re-vérifie l'anomalie pour éliminer les erreurs d'extraction."""
        self.sem.release()              # on libère le créneau pendant l'attente
        try:
            await asyncio.sleep(self.cfg.confirm_delay)
        finally:
            await self.sem.acquire()
        try:
            second = await self.scraper.fetch(p.url)
        except (BlockedError, ScrapeError):
            return False
        return second.price is not None and second.price <= first_price * 1.05

    async def _failure(self, p, reason: str, soft: bool = False):
        cfg = self.cfg
        delay = cfg.interval * min(2 ** p.fail_count, 8)
        fails = self.db.touch_failure(p.id, now() + timedelta(seconds=delay), count=not soft)
        log.warning("échec %s : %s (%d)", p.url[:60], reason, fails)
        if fails >= cfg.max_fails:
            self.db.deactivate(p.id)
            await self.notifier.send_admin(
                f"⚫ Produit désactivé après {fails} échecs : {p.url}\n"
                "(sélecteurs à ajuster ? lien mort ?)")

    # ---------- découverte : scan des pages liste ----------
    async def scan_source(self, s):
        cfg = self.cfg
        async with self.sem:
            try:
                items = await self.scraper.fetch_listing(s.url)
            except BlockedError as e:
                log.warning("source bloquée %s : %s", s.url[:60], e)
                self.db.source_done(s.id, now() + timedelta(seconds=cfg.discover_interval))
                return
            except ScrapeError as e:
                return await self._source_failure(s, str(e))
            if not items:
                return await self._source_failure(s, "aucun produit détecté sur la page")

            cands = []
            for it in items:
                try:
                    url = canonical_url(it.url)
                except ValueError:
                    continue
                v = evaluate_listing(it.price, it.old_price, self.db.listing_history(url),
                                     cfg.drop_threshold, cfg.discover_min_discount,
                                     cfg.discover_min_price, cfg.discover_use_old)
                self.db.record_listing(url, it.price, it.old_price, v.alert)
                if v.alert and self.db.listing_streak(url) >= cfg.baseline_after:
                    self.db.listing_accept(url)          # baisse durable = nouveau prix normal
                elif v.alert and not self.db.discovery_recent(url, cfg.alert_cooldown):
                    cands.append((v.drop or 0, url, it, v))
            self.db.commit()

            cands.sort(key=lambda c: c[0], reverse=True)
            found = 0
            for _, url, it, v in cands[: cfg.discover_max_verify]:
                found += await self._verify_discovery(url, it, v)
            log.info("source %s : %d produits, %d candidats, %d alertes",
                     s.url[:60], len(items), len(cands), found)
            if found:
                self.db.source_mark_found(s.id)
            hot = found or (s.last_found and (now() - s.last_found).total_seconds() < 86400)
            delay = next_interval(cfg.discover_interval, 0.5 if hot and cfg.adaptive else 1.0,
                                  cfg.min_interval)
            self.db.source_done(s.id, now() + timedelta(seconds=delay), items=len(items))

    async def _verify_discovery(self, url, it, v) -> bool:
        """Ouvre la fiche produit : le prix de la liste doit être confirmé."""
        try:
            parsed = await self.scraper.fetch(url)
        except (BlockedError, ScrapeError):
            return False
        if parsed.price is None or not parsed.in_stock or parsed.price > it.price * 1.10:
            return False
        drop = 1 - parsed.price / v.ref if v.ref else None
        sent = await self.notifier.send_alert(
            url=url, name=parsed.name or it.name, price=parsed.price,
            currency=parsed.currency or it.currency, ref=v.ref, drop=drop,
            image=parsed.image or it.image, reason=v.reason, discovered=True)
        if sent:
            self.db.record_discovery(url, parsed.price)
        log.info("DÉCOUVERTE %s -> %s (réf %s)", (parsed.name or url)[:50], parsed.price, v.ref)
        return sent

    async def _source_failure(self, s, reason: str):
        cfg = self.cfg
        delay = cfg.discover_interval * min(2 ** s.fail_count, 8)
        fails = self.db.source_done(s.id, now() + timedelta(seconds=delay), fail=True)
        log.warning("échec source %s : %s (%d)", s.url[:60], reason, fails)
        if fails >= cfg.max_fails:
            self.db.deactivate_source(s.id)
            await self.notifier.send_admin(
                f"⚫ Source désactivée après {fails} échecs ({reason}) : {s.url}")

    async def _source_worker(self, s):
        try:
            await self.scan_source(s)
        except Exception:
            log.exception("erreur inattendue sur la source %s", s.url)
        finally:
            self.running.discard(-s.id)

    async def source_scheduler(self):
        while True:
            for s in self.db.due_sources():
                if -s.id not in self.running:
                    self.running.add(-s.id)
                    asyncio.create_task(self._source_worker(s))
            await asyncio.sleep(20)

    # ---------- découverte AUTOMATIQUE des pages sources ----------
    async def refresh_sources(self) -> dict[str, int]:
        """Explore les sites (SITES), repère les pages de promos/catégories, les vérifie
        (>= AUTO_MIN_ITEMS produits) et les enregistre comme sources automatiques."""
        async with self._refresh_lock:
            cfg = self.cfg
            added: dict[str, int] = {}
            for host in cfg.sites:
                key = store_key(host)
                if key not in STORES:
                    log.warning("SITES : %s n'est pas géré", host)
                    continue
                scored: dict[str, int] = {}
                for hub in hub_urls(host):
                    try:
                        links = await self.scraper.fetch_links(hub)
                    except (BlockedError, ScrapeError) as e:
                        log.warning("exploration %s : %s", hub, e)
                        continue
                    for link in links:
                        href = normalize_candidate(link.href)
                        score = score_link(href, link.text, host)
                        if score > 0:
                            scored[href] = max(scored.get(href, 0), score)

                ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
                have = self.db.count_auto_sources(key)
                for url, score in ranked[: cfg.auto_candidates]:
                    if have >= cfg.auto_max_sources:
                        break
                    if self.db.has_source(url) or self.db.candidate_recent(url):
                        continue
                    try:
                        items = await self.scraper.fetch_listing(url)
                    except (BlockedError, ScrapeError):
                        continue                      # on réessaiera à la prochaine exploration
                    if len(items) >= cfg.auto_min_items:
                        self.db.add_source(url, key, auto=True)
                        self.db.candidate_set(url, key, "accepted", len(items))
                        have += 1
                        added[key] = added.get(key, 0) + 1
                        log.info("source auto : %s (%d produits, score %d)", url[:70],
                                 len(items), score)
                    else:
                        self.db.candidate_set(url, key, "rejected", len(items))

            removed = self.db.deactivate_weak_auto(5)
            if added or removed:
                parts = [f"+{n} {k}" for k, n in added.items()]
                await self.notifier.send_admin(
                    "🤖 Pages sources auto : " + (", ".join(parts) or "aucune nouvelle")
                    + (f" • {removed} retirée(s) (trop peu de produits)" if removed else ""))
            return added

    def trigger_refresh(self) -> bool:
        """Lance une exploration en arrière-plan (commande /prix auto)."""
        if self._refresh_lock.locked() or not self.cfg.sites:
            return False
        task = asyncio.create_task(self.refresh_sources())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        task.add_done_callback(self._log_task)
        return True

    async def autodiscover_loop(self):
        await asyncio.sleep(30)           # laisse le navigateur et les contrôles démarrer
        while True:
            try:
                await self.refresh_sources()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("erreur pendant l'exploration automatique")
            await asyncio.sleep(self.cfg.auto_refresh)

    async def _worker(self, p):
        try:
            await self.process(p)
        except Exception:
            log.exception("erreur inattendue sur %s", p.url)
        finally:
            self.running.discard(p.id)

    # ---------- tâches de fond ----------
    async def scheduler(self):
        while True:
            for p in self.db.due():
                if p.id not in self.running:
                    self.running.add(p.id)
                    asyncio.create_task(self._worker(p))
            await asyncio.sleep(10)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(24 * 3600)
            self.db.prune()
            await self.notifier.send_admin("📊 Rapport quotidien\n" + self.status_text())

    @staticmethod
    def _log_task(t: asyncio.Task):
        if not t.cancelled() and t.exception():
            log.error("tâche arrêtée : %r", t.exception())

    async def run(self):
        self.seed()
        self.seed_sources()
        await self.scraper.start()
        tasks = [asyncio.create_task(self.scheduler()),
                 asyncio.create_task(self.source_scheduler()),
                 asyncio.create_task(self.heartbeat())]
        if self.cfg.sites and self.cfg.auto_enabled:
            tasks.append(asyncio.create_task(self.autodiscover_loop()))
        if self.cfg.discord_token:
            from .commands import build_client
            client = build_client(self)
            tasks.append(asyncio.create_task(client.start(self.cfg.discord_token)))
        for t in tasks:
            t.add_done_callback(self._log_task)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await self.notifier.send_admin("🟢 Bot démarré. " + self.status_text())
        await stop.wait()

        log.info("arrêt...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.scraper.stop()
        await self.notifier.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(App().run())


if __name__ == "__main__":
    main()
