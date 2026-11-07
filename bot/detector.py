"""Logique de détection d'erreur de prix (pure, testable)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass
class Verdict:
    alert: bool
    reason: str = ""
    ref: float | None = None
    drop: float | None = None   # 0.6 = -60 %


def evaluate(price: float | None, history: list[float], threshold: float,
             max_price: float | None = None, min_history: int = 3) -> Verdict:
    if price is None or price <= 0:
        return Verdict(False, "prix invalide")

    ref = statistics.median(history) if len(history) >= min_history else None
    drop = (1 - price / ref) if ref else None

    if max_price is not None and price <= max_price:
        return Verdict(True, "sous le prix max", ref, drop)
    if ref and price <= ref * (1 - threshold):
        return Verdict(True, "chute de prix", ref, drop)
    return Verdict(False, "normal", ref, drop)


def evaluate_listing(price: float | None, old_price: float | None, history: list[float],
                     threshold: float, min_discount: float, min_ref_price: float,
                     use_old: bool = True, min_history: int = 2) -> Verdict:
    """Détection sur une page liste : prix barré annoncé OU chute vs scans précédents."""
    if price is None or price <= 0:
        return Verdict(False, "prix invalide")

    if len(history) >= min_history:
        import statistics
        ref = statistics.median(history)
        if ref >= min_ref_price and price <= ref * (1 - threshold):
            return Verdict(True, "chute vs scans précédents", ref, 1 - price / ref)

    if use_old and old_price and old_price >= min_ref_price \
            and price <= old_price * (1 - min_discount):
        return Verdict(True, "remise annoncée (prix barré)", old_price, 1 - price / old_price)
    return Verdict(False, "normal")
