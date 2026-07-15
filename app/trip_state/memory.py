"""
MemoryService — per-trip conversational memory (docs/persistence_architecture.md §6B).

Durable rules the traveller states *in this trip's* chat ("only refundable from
now on", "avoid early flights", "rank for comfort"). Distinct from the per-user
``user_travel_memory`` (cross-trip priors, app.services.user_memory): user memory
warm-starts a trip; trip memory captures what's said during it. MemoryService
merges both — user memory contributes low-confidence defaults, trip memory wins.

The heart is ``as_query_rules`` — a PURE translation of stored rules into the
filters/sort that ``RetrievalService.resolve_options`` applies, so every future
search (this leg and re-searches) is personalized without re-deriving intent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class MemoryService:
    # ── trip_memory CRUD (sync; run via run_db) ──
    @staticmethod
    def get_trip_rows(trip_id: str, scope: str | None = None) -> list[dict]:
        from app.db.client import get_db
        q = (get_db().table("trip_memory")
             .select("key, scope, value, confidence, source")
             .eq("trip_id", trip_id))
        if scope is not None:
            # rules with '' scope are trip-wide and always apply; scope-specific too.
            q = q.in_("scope", ["", scope])
        return q.execute().data or []

    @staticmethod
    def upsert(trip_id: str, key: str, value, *, scope: str = "",
               confidence: float = 0.6, source: str = "copilot") -> None:
        from app.db.client import get_db
        (get_db().table("trip_memory").upsert({
            "trip_id": trip_id, "key": key, "scope": scope, "value": value,
            "confidence": confidence, "source": source,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="trip_id,key,scope").execute())

    # ── pure translation: stored rules → query filters/sort ──
    @staticmethod
    def as_query_rules(rows: list[dict]) -> dict:
        """Translate memory rows into {filters, sort}. Pure — no DB, unit-tested.

        Known keys:
          only_refundable      -> filters.refundable_only
          avoid_early_flights  -> filters.min_depart_hour (value {"before":"HH:MM"})
          hide_operators       -> filters.exclude_operators (lowercased list)
          rank_axis            -> sort ("price" | "comfort"/"score" | "time"/"duration")
          stay_tier            -> filters.stay_tier
          max_price            -> filters.max_price
        Unknown keys are ignored (forward-compatible).
        """
        filters: dict = {}
        sort: str | None = None
        for r in rows:
            key, val = r.get("key"), r.get("value")
            if key == "only_refundable" and val:
                filters["refundable_only"] = True
            elif key == "avoid_early_flights":
                hh = _hour_of(val)
                if hh is not None:
                    filters["min_depart_hour"] = hh
            elif key == "hide_operators" and isinstance(val, list):
                ops = [str(x).strip().lower() for x in val if str(x).strip()]
                if ops:
                    filters.setdefault("exclude_operators", [])
                    filters["exclude_operators"] = sorted(set(filters["exclude_operators"]) | set(ops))
            elif key == "rank_axis" and isinstance(val, str):
                sort = _axis_to_sort(val)
            elif key == "stay_tier" and val:
                filters["stay_tier"] = str(val)
            elif key == "max_price":
                try:
                    filters["max_price"] = float(val)
                except (TypeError, ValueError):
                    pass
        return {"filters": filters, "sort": sort}

    @staticmethod
    def describe(rows: list[dict]) -> list[str]:
        """Human phrases for the active rules — shown to the LLM so it can explain
        why a shortlist looks the way it does. Pure."""
        out: list[str] = []
        for r in rows:
            key, val = r.get("key"), r.get("value")
            if key == "only_refundable" and val:
                out.append("only refundable options")
            elif key == "avoid_early_flights":
                hh = _hour_of(val)
                if hh is not None:
                    out.append(f"avoid departures before {hh:02d}:00")
            elif key == "hide_operators" and isinstance(val, list) and val:
                out.append("hide " + ", ".join(str(x) for x in val))
            elif key == "rank_axis" and isinstance(val, str):
                out.append(f"rank by {val}")
            elif key == "stay_tier" and val:
                out.append(f"{val} stays")
            elif key == "max_price":
                out.append(f"under {val}")
        return out

    @staticmethod
    def from_user_memory(user_mem: dict) -> list[dict]:
        """Low-confidence default rules derived from cross-trip user memory, so a
        new trip starts warm. Trip-level rules (higher confidence) override these."""
        rows: list[dict] = []
        if not user_mem:
            return rows
        pref = user_mem.get("transport_pref")
        if pref in ("comfort", "price", "time", "duration"):
            rows.append({"key": "rank_axis", "value": pref, "confidence": 0.4})
        tier = user_mem.get("budget_tier")
        if tier in ("low", "mid", "high"):
            rows.append({"key": "stay_tier",
                         "value": {"low": "budget", "mid": "mid", "high": "luxury"}[tier],
                         "confidence": 0.4})
        return rows


def _hour_of(val) -> int | None:
    """{'before':'09:00'} | '09:00' | 9 -> 9."""
    if isinstance(val, dict):
        val = val.get("before") or val.get("after")
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str) and ":" in val:
        try:
            return int(val.split(":", 1)[0])
        except ValueError:
            return None
    return None


def _axis_to_sort(axis: str) -> str:
    a = axis.strip().lower()
    if a in ("comfort", "score", "best"):
        return "score"
    if a in ("time", "duration", "fastest"):
        return "duration_min"
    return "price"


async def get_rules(trip_id: str, *, scope: str | None = None, user_id: str | None = None) -> dict:
    """Active {filters, sort} for a trip — trip memory over user-memory defaults.
    Best-effort: a memory failure degrades to no rules, never breaks retrieval."""
    from app.db.client import run_db
    try:
        rows = await run_db(MemoryService.get_trip_rows, trip_id, scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory] get_trip_rows failed for %s: %s", trip_id, exc)
        rows = []
    defaults: list[dict] = []
    if user_id:
        try:
            from app.services.user_memory import get_memory
            defaults = MemoryService.from_user_memory(await get_memory(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] user memory unavailable: %s", exc)
    # trip rows last so they win in as_query_rules' sequential apply
    merged = defaults + rows
    out = MemoryService.as_query_rules(merged)
    out["described"] = MemoryService.describe(rows)  # describe trip rules (not silent defaults)
    return out
