"""Règles de planification adaptatives (pures, testables)."""
from __future__ import annotations

import random
import statistics


def volatility(prices: list[float], min_samples: int = 4) -> float | None:
    """Coefficient de variation (écart-type / moyenne) des derniers prix normaux."""
    if len(prices) < min_samples:
        return None
    mean = statistics.fmean(prices)
    return statistics.pstdev(prices) / mean if mean > 0 else None


def interval_factor(prices: list[float], watch_closely: bool = False,
                    adaptive: bool = True) -> float:
    """Multiplicateur de l'intervalle de contrôle d'un produit.

    - anomalie en cours  -> x0.25 (on suit l'évolution de près)
    - prix qui bougent   -> x0.5  (promos, enchères : c'est là que les erreurs surviennent)
    - prix très stable   -> x1.5  (on économise des requêtes)
    """
    if not adaptive:
        return 1.0
    if watch_closely:
        return 0.25
    cv = volatility(prices)
    if cv is None:
        return 1.0
    if cv >= 0.03:
        return 0.5
    if cv <= 0.002 and len(prices) >= 10:
        return 1.5
    return 1.0


def next_interval(base: float, factor: float, min_interval: float,
                  jitter: tuple[float, float] = (0.8, 1.2)) -> float:
    """Délai avant le prochain contrôle (secondes), borné entre min_interval et 3 x base."""
    value = base * factor * random.uniform(*jitter)
    return max(min_interval, min(base * 3, value))


def next_delay(current: float, ok: bool, floor: float, ceiling: float) -> float:
    """Délai entre 2 requêtes sur un site : AIMD (baisse douce si OK, double si blocage)."""
    return max(floor, current * 0.95) if ok else min(ceiling, current * 2)
