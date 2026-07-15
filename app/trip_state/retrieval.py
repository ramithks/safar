"""
RetrievalService — the single retrieval layer for the workspace API and copilot.

Merges what were TripContextService (data access) + KnowledgeService (context
façade) into one service. It turns "trip + focus" into a minimal, personalized
shortlist by composing the stores: immutable ``search_results`` (facts) ⋈
``trip_result_views`` (this user's hide/favourite/pin) ⋈ ``trip_recommendations``
(precomputed scores), with ``trip_memory`` rules applied as filters/sort. It
never returns ``raw`` and never ships the trip JSON.

Precedence (fixed): score_override > visible=false > memory rules > reco score >
default sort. ``rank_and_filter`` is PURE and unit-tested without a DB.
"""
from __future__ import annotations

import logging

from app.db.client import run_db

from . import memory as memory_mod
from .batches import SearchBatchStore
from .views import ViewLayer

logger = logging.getLogger(__name__)


# ── pure ranking core ────────────────────────────────────────────────────────
def _hour_from_iso(ts: str | None) -> int | None:
    if not ts or "T" not in str(ts):
        return None
    try:
        return int(str(ts).split("T", 1)[1][:2])
    except (ValueError, IndexError):
        return None


def _passes_filters(row: dict, view: dict | None, rules_filters: dict) -> bool:
    # Explicit hide always wins.
    if view and view.get("visible") is False:
        return False
    if rules_filters.get("refundable_only") and row.get("refundable") is not True:
        return False
    mdh = rules_filters.get("min_depart_hour")
    if mdh is not None:
        hh = _hour_from_iso(row.get("departs_at"))
        if hh is not None and hh < mdh:
            return False
    excl = rules_filters.get("exclude_operators")
    if excl and (row.get("operator") or "").strip().lower() in excl:
        return False
    mx = rules_filters.get("max_price")
    if mx is not None and row.get("price") is not None and row["price"] > mx:
        return False
    tier = rules_filters.get("stay_tier")
    if tier and row.get("item_type") == "hotel":
        want = {"budget": (0, 3), "mid": (3, 4), "luxury": (4, 6)}.get(tier)
        stars = (row.get("summary") or {}).get("star_rating")
        if want and stars is not None and not (want[0] <= stars <= want[1]):
            return False
    return True


def rank_and_filter(results: list[dict], views: dict, scores: dict,
                    rules: dict, *, mode: str | None = None, limit: int = 6) -> list[dict]:
    """PURE: filter by views+memory, sort by precedence, annotate for the LLM/UI.

    `results` are search_results summaries; `views`/`scores` are keyed by result id."""
    rules_filters = (rules or {}).get("filters") or {}
    sort = (rules or {}).get("sort")
    picked = []
    for r in results:
        if mode and r.get("item_type") != mode:
            continue
        view = views.get(r["id"])
        if not _passes_filters(r, view, rules_filters):
            continue
        sc = scores.get(r["id"]) or {}
        picked.append((r, view or {}, sc))

    def key(entry):
        r, view, sc = entry
        # 1) explicit pin wins
        if view.get("score_override") is not None:
            return (0, -float(view["score_override"]))
        # 2) memory/explicit sort axis
        if sort == "score":
            return (1, -float(sc.get("score") or 0.0))
        if sort == "duration_min":
            return (1, r.get("duration_min") if r.get("duration_min") is not None else 1e9)
        # default: cheapest first, then reco score as tiebreak
        price = r.get("price")
        return (1, price if price is not None else 1e12, -float(sc.get("score") or 0.0))

    picked.sort(key=key)

    out = []
    for r, view, sc in picked[:limit]:
        out.append({
            "id": r["id"],
            "item_type": r.get("item_type"),
            "operator": r.get("operator"),
            "price": r.get("price"),
            "currency": r.get("currency"),
            "departs_at": r.get("departs_at"),
            "arrives_at": r.get("arrives_at"),
            "duration_min": r.get("duration_min"),
            "refundable": r.get("refundable"),
            "rating": r.get("rating"),
            "summary": r.get("summary") or {},
            "score": sc.get("score"),
            "why": sc.get("explanation"),
            "favorite": bool(view.get("favorite")),
            "notes": view.get("notes"),
        })
    return out


# ── compact context helpers (for the copilot) ────────────────────────────────
def _trip_line(state: dict) -> str:
    req = (state or {}).get("request") or {}
    bits = []
    route = " → ".join(x for x in [req.get("origin"), req.get("destination")] if x)
    if route:
        bits.append(route)
    if req.get("days"):
        bits.append(f"{req['days']} days")
    if req.get("travelers"):
        bits.append(f"{req['travelers']} pax")
    if req.get("budget_amount"):
        bits.append(f"₹{req['budget_amount']} budget")
    elif req.get("budget_tier"):
        bits.append(f"{req['budget_tier']} budget")
    if req.get("departure_date"):
        bits.append(f"dep {req['departure_date']}")
    return " · ".join(bits)


def _selected(state: dict) -> dict:
    out = {}
    stay = ((state or {}).get("stay") or {}).get("selected") or {}
    if stay.get("name"):
        out["stay"] = stay["name"]
    costs = (state or {}).get("costs") or {}
    if costs.get("total"):
        out["total"] = costs["total"]
    return out


def _weather_fragment(state: dict) -> dict | None:
    w = (state or {}).get("weather") or {}
    if not (w.get("daily") or []):
        return None
    return {"weather": {"location": w.get("location"), "days": len(w["daily"]),
                        "summary": w.get("summary")}}


def _visa_fragment(state: dict) -> dict | None:
    docs = (state or {}).get("documents") or {}
    if not docs.get("is_international"):
        return None
    return {"visa": {"required": docs.get("visa_required"), "info": docs.get("visa_info")}}


# intent → extra fragments to fold in (empty intent stays cheap).
_INTENT_FRAGMENTS = {
    "weather": [_weather_fragment],
    "visa": [_visa_fragment],
    "documents": [_visa_fragment],
    "packing": [_weather_fragment, _visa_fragment],
}


class RetrievalService:
    # ── data access ──
    @staticmethod
    def load_state(trip_id: str) -> dict | None:
        from .store import get_trip_state
        row = get_trip_state(trip_id)
        return (row or {}).get("state") if row else None

    @staticmethod
    def find_batch(trip_id: str, kind: str, scope_id: str = "") -> dict | None:
        """Newest ready batch for an EXACT scope, or None.

        `scope_id` is the typed reference to the thing being searched:
          - graph trips: the leg id (transport) or stop/node id (hotel)
          - classic trips: 'going' / 'return' (transport) or the destination (hotel)
        There is deliberately NO cross-scope fallback — a miss returns None so we
        never serve one leg's options under another leg's scope."""
        from app.db.client import get_db
        res = (get_db().table("search_batches")
               .select("id, kind, scope_id, mode, origin, destination, depart_date, result_count")
               .eq("trip_id", trip_id).eq("kind", kind).eq("scope_id", scope_id)
               .eq("status", "ready").order("created_at", desc=True).limit(1).execute())
        return res.data[0] if res.data else None

    @staticmethod
    def resolve_options(trip_id: str, *, scope_id: str = "", kind: str = "transport",
                        mode: str | None = None, rules: dict | None = None,
                        limit: int = 6) -> list[dict]:
        """Retrieval workhorse (sync; run via run_db). Loads a scope's batch,
        overlays views + reco scores, applies memory rules, returns top-N. No raw."""
        from app.reco.index import RecommendationIndex
        batch = RetrievalService.find_batch(trip_id, kind, scope_id)
        if not batch:
            return []
        results = SearchBatchStore.load_summaries(batch["id"], item_type=mode, limit=200)
        views = ViewLayer.load(trip_id)
        scores = RecommendationIndex.load_scores(trip_id, batch.get("scope_id"))
        return rank_and_filter(results, views, scores, rules or {}, mode=mode, limit=limit)

    @staticmethod
    def set_view(trip_id: str, result_id: str, **fields) -> None:
        ViewLayer.set_view(trip_id, result_id, **fields)

    @staticmethod
    def hydrate_raw(result_ids: list[str]) -> dict:
        return SearchBatchStore.hydrate_raw(result_ids)

    # ── compact copilot context ──
    @staticmethod
    async def build_context(trip_id: str, *, focus: dict | None = None,
                            intent: dict | None = None, user_id: str | None = None) -> dict:
        """Assemble the minimal copilot context (~5KB): trip line, selected picks,
        active memory rules, and only the focused scope's personalized shortlist."""
        intent = intent or {}
        focus = focus or {}
        state = await run_db(RetrievalService.load_state, trip_id) or {}
        rules = await memory_mod.get_rules(trip_id, scope=focus.get("scope"), user_id=user_id)

        ctx: dict = {
            "trip": _trip_line(state),
            "selected": _selected(state),
            "memory": rules.get("described") or [],
        }
        if focus.get("kind"):
            options = await run_db(lambda: RetrievalService.resolve_options(
                trip_id, scope_id=focus.get("scope", ""), kind=focus["kind"],
                mode=focus.get("mode"), rules=rules, limit=focus.get("limit", 6)))
            ctx["focus"] = {"scope": focus.get("scope", ""), "kind": focus["kind"],
                            "mode": focus.get("mode"), "options": options}

        intent_name = intent.get("name") or intent.get("intent")
        for frag_fn in _INTENT_FRAGMENTS.get(intent_name, []):
            try:
                frag = frag_fn(state)
                if frag:
                    ctx.update(frag)
            except Exception as exc:  # noqa: BLE001 — one fragment must not break context
                logger.debug("[retrieval] fragment %s failed: %s", frag_fn.__name__, exc)
        return ctx
