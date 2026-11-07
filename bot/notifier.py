"""Envoi des alertes Discord via webhook (avec gestion du rate-limit)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import aiohttp

from .config import Config
from .parsing import store_label

log = logging.getLogger("notifier")
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def money(value: float, currency: str | None) -> str:
    sym = SYMBOLS.get((currency or "EUR").upper(), currency or "")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + f" {sym}".rstrip()


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def _post(self, url: str, payload: dict) -> bool:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        for _ in range(3):
            try:
                async with self._session.post(url, json=payload) as r:
                    if r.status == 429:
                        retry = (await r.json()).get("retry_after", 2)
                        await asyncio.sleep(float(retry))
                        continue
                    if r.status >= 400:
                        log.error("Discord HTTP %s: %s", r.status, (await r.text())[:200])
                        return False
                    return True
            except aiohttp.ClientError as e:
                log.error("Discord erreur réseau: %s", e)
                await asyncio.sleep(2)
        return False

    async def send_alert(self, *, url, name, price, currency, ref, drop, image, reason,
                         discovered=False) -> bool:
        store, flag = store_label(url)
        price_txt = f"**{money(price, currency)}**"
        if ref:
            price_txt += f"\n~~{money(ref, currency)}~~  •  **-{round((drop or 0) * 100)} %**"
        embed = {
            "author": {"name": f"{self.cfg.brand} • ERREUR DE PRIX POSSIBLE"
                       + (" • DÉCOUVERTE" if discovered else "")},
            "title": (name or url)[:250],
            "url": url,
            "color": 0xE74C3C,
            "fields": [
                {"name": "Store", "value": f"{store} {flag}".strip(), "inline": True},
                {"name": "Vendor", "value": store, "inline": True},
                {"name": "Price", "value": price_txt, "inline": False},
            ],
            "footer": {"text": f"Powered by {self.cfg.brand} | {datetime.now():%d/%m/%Y %H:%M}"},
        }
        if discovered and reason:
            embed["fields"].append({"name": "Détection", "value": reason, "inline": False})
        if image and image.startswith("http"):
            embed["thumbnail"] = {"url": image}
        return await self._post(self.cfg.webhook, {
            "content": "🚨 **ALERTE & DÉTECTION PAR NOTRE BOT**", "embeds": [embed]})

    async def send_admin(self, text: str):
        url = self.cfg.admin_webhook
        if url:
            await self._post(url, {"content": text[:1900]})
        else:
            log.info("[admin] %s", text)

    async def close(self):
        if self._session:
            await self._session.close()
