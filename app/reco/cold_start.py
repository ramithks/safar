"""
Cold start (Part 10) — good recommendations before we know the traveller.

Three levers, cheapest first:
  1. The intake already gives a usable intent (who/vibe/budget) — cold start is
     mostly solved by asking.
  2. Destination priors: what trips to THIS place usually look like (modal vibes /
     personas), aggregated offline into ``destination_priors``. A first-timer to
     Udaipur gets seeded with {romantic, heritage, lake_view}.
  3. Diversity: the first screen is MMR-diversified (rank.mmr_diversify) so a new
     user sees range (budget / mid / splurge / central / scenic), not five clones.

All best-effort — a missing priors table just means we lean on intake + diversity.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _read_prior_sync(destination: str) -> dict:
    from app.db.client import get_db
    res = (
        get_db().table("destination_priors")
        .select("top_personas, top_vibes")
        .eq("destination", destination).limit(1).execute()
    )
    return (res.data[0] if res.data else {}) or {}


async def destination_prior(destination: str | None) -> dict:
    """Modal vibes/personas for a destination, or {} if unknown / table absent."""
    if not destination:
        return {}
    try:
        from app.db.client import run_db
        return await run_db(_read_prior_sync, destination)
    except Exception as exc:  # noqa: BLE001 — priors are advisory
        logger.debug("[reco.cold_start] no prior for %s: %s", destination, exc)
        return {}


def is_cold(memory: dict | None) -> bool:
    """A traveller we know little about."""
    m = memory or {}
    return int(m.get("trips_planned") or 0) < 1 and not m.get("notes")


async def seed_context(ctx: dict, memory: dict | None, destination: str | None) -> dict:
    """When the traveller is cold, fold destination priors into their vibes so the
    ranking has *something* personal to lean on. Explicit vibes still lead."""
    if not is_cold(memory):
        return ctx
    prior = await destination_prior(destination)
    prior_vibes = [str(v).lower() for v in (prior.get("top_vibes") or [])]
    if prior_vibes:
        vibes = list(ctx.get("vibes") or [])
        for v in prior_vibes:
            if v not in vibes:
                vibes.append(v)
        ctx["vibes"] = vibes[:12]
        ctx["_cold_start_seed"] = prior_vibes
    return ctx


def first_screen(hotels: list[dict], k: int = 12) -> list[dict]:
    """Diversify the top of the list so a new user sees range, not clones."""
    from app.reco.rank import mmr_diversify
    return mmr_diversify(hotels, k)
