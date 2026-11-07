"""Commandes slash Discord pour gérer la watchlist sans redéployer."""
from __future__ import annotations

import discord
from discord import app_commands

from .parsing import STORES, canonical_url, clean_source_url, store_key, store_label


def build_client(app) -> discord.Client:
    cfg, db = app.cfg, app.db

    class Tree(app_commands.CommandTree):
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if cfg.admin_ids and interaction.user.id not in cfg.admin_ids:
                await interaction.response.send_message("⛔ Non autorisé.", ephemeral=True)
                return False
            return True

    class Client(discord.Client):
        def __init__(self):
            super().__init__(intents=discord.Intents.none())
            self.tree = Tree(self)

        async def setup_hook(self):
            register(self.tree)
            if cfg.guild_id:
                g = discord.Object(id=cfg.guild_id)
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            else:
                await self.tree.sync()

    def register(tree: app_commands.CommandTree):
        perms = discord.Permissions(administrator=True)
        group = app_commands.Group(name="prix", description="Gérer le bot d'erreurs de prix",
                                   default_permissions=perms, guild_only=True)

        @group.command(name="add", description="Surveiller un produit")
        @app_commands.describe(url="Lien du produit", seuil="Chute en % pour alerter (def. global)",
                               prix_max="Alerter si le prix passe sous cette valeur")
        async def add(i: discord.Interaction, url: str,
                      seuil: app_commands.Range[int, 5, 95] | None = None,
                      prix_max: float | None = None):
            try:
                url = canonical_url(url)
            except ValueError:
                return await i.response.send_message("❌ URL invalide.", ephemeral=True)
            if store_key(url) not in STORES:
                return await i.response.send_message(
                    f"❌ Site non géré. Sites : {', '.join(STORES.values())}. "
                    "Utilise le lien complet (pas de lien raccourci).", ephemeral=True)
            new = db.add_product(url, store_key(url), seuil / 100 if seuil else None, prix_max)
            await i.response.send_message(
                f"{'✅ Ajouté' if new else '🔁 Mis à jour'} : {url}", ephemeral=True)

        @group.command(name="remove", description="Arrêter de surveiller un produit")
        async def remove(i: discord.Interaction, url: str):
            try:
                url = canonical_url(url)
            except ValueError:
                return await i.response.send_message("❌ URL invalide.", ephemeral=True)
            ok = db.remove_product(url)
            await i.response.send_message("🗑️ Supprimé." if ok else "Introuvable.",
                                          ephemeral=True)

        @group.command(name="list", description="Lister les produits surveillés")
        async def list_(i: discord.Interaction):
            rows = db.list_products()
            if not rows:
                return await i.response.send_message("Watchlist vide.", ephemeral=True)
            lines = []
            for p in rows[:40]:
                flag = "🟢" if p.active else "⚫"
                price = f"{p.last_price:.2f}" if p.last_price else "—"
                label = (p.name or p.url)[:55]
                lines.append(f"{flag} {store_label(p.url)[0]} • {price} • {label}")
            extra = f"\n… +{len(rows) - 40}" if len(rows) > 40 else ""
            await i.response.send_message("\n".join(lines) + extra, ephemeral=True)

        @group.command(name="source_add",
                       description="Scanner une page de promos/catégorie/recherche")
        async def source_add(i: discord.Interaction, url: str):
            try:
                url = clean_source_url(url)
            except ValueError:
                return await i.response.send_message("❌ URL invalide.", ephemeral=True)
            if store_key(url) not in STORES:
                return await i.response.send_message(
                    f"❌ Site non géré. Sites : {', '.join(STORES.values())}.", ephemeral=True)
            new = db.add_source(url, store_key(url))
            await i.response.send_message(
                f"{'🔎 Source ajoutée' if new else '🔁 Source réactivée'} : {url}", ephemeral=True)

        @group.command(name="source_remove", description="Arrêter de scanner une page")
        async def source_remove(i: discord.Interaction, url: str):
            try:
                url = clean_source_url(url)
            except ValueError:
                return await i.response.send_message("❌ URL invalide.", ephemeral=True)
            ok = db.remove_source(url)
            await i.response.send_message("🗑️ Supprimée." if ok else "Introuvable.",
                                          ephemeral=True)

        @group.command(name="source_list", description="Lister les pages scannées")
        async def source_list(i: discord.Interaction):
            rows = db.list_sources()
            if not rows:
                return await i.response.send_message("Aucune source.", ephemeral=True)
            lines = [f"{'🟢' if s.active else '⚫'} {store_label(s.url)[0]} • "
                     f"{s.last_items if s.last_items is not None else '?'} produits • {s.url[:70]}"
                     for s in rows[:30]]
            await i.response.send_message("\n".join(lines), ephemeral=True)

        @group.command(name="status", description="État du bot")
        async def status(i: discord.Interaction):
            await i.response.send_message(app.status_text(), ephemeral=True)

        tree.add_command(group)

    return Client()
