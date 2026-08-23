"""
What is missing, per room and per section, computed rather than claimed.

Two things the brief is emphatic about and which pull in opposite directions:

  §2  no core field may be optional — a room must not be sellable without the
      information the AI, the booking engine and the website all need
  §36 nothing may break — twenty-one room types and ten properties already exist
      and every one of them predates most of those fields

A `NOT NULL` constraint cannot serve both. So the database records what is
*known* and this module decides what may be *sold*, which also gives the answer
a shape a hotelier can act on: not "invalid", but "your AI can't answer what's
in the Deluxe, and here are the three things it needs".

**Every percentage on the dashboard comes from a count of these checks.** §40:
no fake 100%. A section is `4 of 6` because six checks ran and four passed —
there is no weighting, no partial credit, and nothing that can be true without a
row somewhere being true. That is also why the checks are declared as data: the
list is the specification, and a screen, a test and an API projection all read
the same list rather than three interpretations of it.

**"Not applicable" counts as answered.** `extra_bed = 'none'` passes; null does
not. A hotel that has thought about extra beds and has none is finished with
that question, and telling it otherwise is how a checklist becomes something
nobody completes.
"""
import logging
from dataclasses import dataclass, field
from typing import Callable

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    """
    One deterministic question about one thing.

    `blocks` is what stops working while this is unanswered — the same vocabulary
    the existing gap list uses, so the two can be shown side by side: 'agent',
    'website', 'booking'.
    """

    key: str
    label: str
    blocks: tuple[str, ...]
    passes: Callable[[dict], bool]
    # Shown when it fails. Written for a hotelier, in the second person, saying
    # what cannot happen rather than which column is null.
    detail: str = ""


@dataclass
class SectionResult:
    key: str
    label: str
    passed: int
    total: int
    missing: list[dict] = field(default_factory=list)

    @property
    def pct(self) -> int:
        """
        Whole percent, and never a rounded-up 100.

        `round(5/6 * 100)` is 83, which is fine. `round(199/200 * 100)` is 100,
        which is a lie of exactly the kind §40 forbids — a hotelier reading 100%
        with something still missing stops looking. So anything short of every
        check is capped at 99.
        """
        if self.total == 0:
            return 100
        if self.passed == self.total:
            return 100
        return min(99, round(self.passed * 100 / self.total))

    @property
    def complete(self) -> bool:
        return self.passed == self.total

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "total": self.total,
            "pct": self.pct,
            "complete": self.complete,
            "missing": self.missing,
        }


def _has_text(value, minimum: int = 1) -> bool:
    return len(str(value or "").strip()) >= minimum


def _answered(value) -> bool:
    """
    Whether a closed-vocabulary field has been answered at all.

    `'none'` and `'not_allowed'` are answers. Only null and empty string are not.
    This single line is the §2 "explicitly select Not applicable" rule, and every
    check that needs it goes through here rather than reimplementing the
    distinction.
    """
    return value is not None and str(value).strip() != ""


# ── one room ─────────────────────────────────────────────────────────────────
#
# The context each check receives is a flat dict:
#   room:  the room_types row
#   plans: its active rate plans
#   property: the properties row

ROOM_CHECKS: tuple[Check, ...] = (
    Check("name", "A name guests recognise", ("agent", "website", "booking"),
          lambda c: _has_text(c["room"].get("name"), 2),
          "Guests and your AI both refer to this room by name."),
    Check("units", "How many you have", ("booking",),
          lambda c: int(c["room"].get("total_units") or 0) >= 1,
          "Without this the booking engine has nothing to sell."),
    Check("occupancy", "Adults and children it sleeps", ("agent", "booking"),
          lambda c: _answered(c["room"].get("max_adults")) and _answered(c["room"].get("max_children")),
          "“Do 2 adults and a child fit?” is the question before the price."),
    Check("children", "Whether children are allowed", ("agent",),
          lambda c: _answered(c["room"].get("children_allowed")),
          "Say yes, no, or above an age — your AI must not guess this one."),
    Check("beds", "The bed, and how many", ("agent", "website"),
          lambda c: _has_text(c["room"].get("bed_type")) and _answered(c["room"].get("bed_count")),
          "Guests ask what the bed is before they ask the price."),
    Check("extra_bed", "Whether an extra bed is possible", ("agent",),
          lambda c: _answered(c["room"].get("extra_bed")),
          "“Can you add a mattress?” — available, on request, or not at all."),
    Check("description", "What the room is like", ("agent", "website"),
          lambda c: _has_text(c["room"].get("description"), 20),
          "Your AI reads this out to guests. Without it, it can sell the room "
          "and not describe it."),
    Check("amenities", "What's in the room", ("agent", "website"),
          lambda c: len(c["room"].get("amenities") or []) >= 3,
          "AC, wifi, hot water — at least three of the things a guest checks for."),
    Check("photos", "Photographs", ("agent", "website"),
          lambda c: len(c["room"].get("photos") or []) >= 1,
          "Your AI can send these on Instagram, and your page cannot be listed "
          "on Google without them."),
    Check("rate", "A price per night", ("agent", "booking", "website"),
          lambda c: any(int(p.get("base_price_paise") or 0) > 0 for p in c["plans"]),
          "A room with no rate plan is a room the booking engine cannot price."),
    Check("meal_plan", "What the rate includes", ("agent", "booking"),
          lambda c: any(_has_text(p.get("meal_plan")) for p in c["plans"]),
          "Room only, breakfast, half board or full board."),
    Check("cancellation", "Cancellation terms", ("agent", "booking", "website"),
          lambda c: any(
              (p.get("cancellation_policy") or {}).get("tiers")
              or p.get("cancellation_cutoff_hours") is not None
              or p.get("refundable") is False
              for p in c["plans"]
          ),
          "Until this is set your AI hands every “can I cancel?” to a person."),
)


# What a room cannot be sold *at all* without: a name, some stock, and a price.
#
# This set is small on purpose, and finding out why is the most useful thing
# running this module against production did. The first version treated every
# check marked `blocks: booking` as a sales gate — occupancy, meal plan,
# cancellation terms included — and reported **0 of 4 sellable** for Casa
# Mandala and 0 of 2 for harry. Every room type in production predates those
# columns, so a gate built that way would have taken the entire live inventory
# off sale on deploy. §36 says nothing may break, and that would have broken
# everything at once.
#
# So there are two bars rather than one, and the difference between them is the
# whole answer to §2:
#
#     sellable      the booking engine can price and hold this room today.
#                   Existing rooms pass; nothing goes dark.
#     readyToSell   every check passes — the §2 bar. What the room wizard
#                   requires before it will publish a *new* room, and what the
#                   dashboard badges.
#
# A room between the two is live and incomplete, which is exactly what the
# twenty-one rooms in production are, and telling their owners so is the point.
MUST_PRICE: frozenset[str] = frozenset({"name", "units", "rate"})


def room_result(room: dict, plans: list[dict], prop: dict) -> dict:
    """
    Whether one room type may be sold, whether it is finished, and what it is
    short of. See `MUST_PRICE` for why those are two different questions.
    """
    context = {"room": room, "plans": plans, "property": prop}
    missing = []
    unpriceable = False
    for check in ROOM_CHECKS:
        try:
            ok = check.passes(context)
        except Exception:  # noqa: BLE001
            # A check that raises is a bug in the check, not a failing room. It
            # counts as unmet — the conservative direction — and says so loudly
            # rather than taking a room off sale silently.
            logger.warning("room readiness check %s raised on room %s",
                           check.key, room.get("id"), exc_info=True)
            ok = False
        if ok:
            continue
        missing.append({
            "key": check.key,
            "label": check.label,
            "detail": check.detail,
            "blocks": list(check.blocks),
        })
        if check.key in MUST_PRICE:
            unpriceable = True

    passed = len(ROOM_CHECKS) - len(missing)
    return {
        "roomTypeId": str(room.get("id")),
        "name": room.get("name"),
        "sellable": not unpriceable,
        "readyToSell": not missing,
        "complete": not missing,
        "passed": passed,
        "total": len(ROOM_CHECKS),
        "pct": 100 if not missing else min(99, round(passed * 100 / len(ROOM_CHECKS))),
        "missing": missing,
    }


# ── the hotel, by section ────────────────────────────────────────────────────

PROFILE_CHECKS: tuple[Check, ...] = (
    Check("name", "Hotel name", ("agent", "website"),
          lambda c: _has_text(c["property"].get("name"), 2), ""),
    Check("address", "Street address", ("agent", "website"),
          lambda c: _has_text(c["property"].get("address"), 8),
          "Guests arriving by taxi ask for this."),
    Check("city", "City and state", ("website",),
          lambda c: _has_text(c["property"].get("city")) and _has_text(c["property"].get("state")), ""),
    Check("location", "Your place on the map", ("agent", "website"),
          lambda c: c["property"].get("lat") is not None and c["property"].get("lon") is not None,
          "Without it your AI cannot tell a guest how far you are from anything."),
    Check("about", "A description of the hotel", ("agent", "website"),
          lambda c: len(str(c["site"].get("about") or "").strip()) >= 120,
          "This is what Google shows under your name."),
    Check("facilities", "What you offer", ("agent", "website"),
          lambda c: len(c["site"].get("amenities") or []) >= 3,
          "Parking, WiFi, a restaurant — the things a guest checks for."),
    Check("times", "Check-in and check-out times", ("agent",),
          lambda c: bool(c["property"].get("check_in_time") and c["property"].get("check_out_time")), ""),
)

POLICY_CHECKS: tuple[Check, ...] = (
    Check("cancellation", "Cancellation policy", ("agent", "website"),
          lambda c: _has_text((c["property"].get("policies") or {}).get("cancellation")),
          "Your AI will hand a guest to you rather than answer this, every time."),
    Check("children", "Children policy", ("agent",),
          lambda c: _has_text((c["property"].get("policies") or {}).get("children")), ""),
    Check("pets", "Pets policy", ("agent",),
          lambda c: _has_text((c["property"].get("policies") or {}).get("pets")), ""),
    Check("smoking", "Smoking policy", ("agent",),
          lambda c: _has_text((c["property"].get("policies") or {}).get("smoking")), ""),
    Check("id_required", "What ID you need at check-in", ("agent",),
          lambda c: _has_text((c["property"].get("policies") or {}).get("id_required")), ""),
    Check("early_late", "Early check-in and late checkout", ("agent",),
          lambda c: bool(c["time_rules"]),
          "“Main 10 baje aa sakta hoon?” arrives constantly. Set what it costs "
          "and your AI can answer it."),
)

MEAL_CHECKS: tuple[Check, ...] = (
    Check("breakfast", "Breakfast", ("agent", "website"),
          lambda c: "breakfast" in c["meals"],
          "The second most asked question on Instagram."),
    Check("lunch", "Lunch", ("agent",), lambda c: "lunch" in c["meals"], ""),
    Check("dinner", "Dinner", ("agent",), lambda c: "dinner" in c["meals"], ""),
)

PRICING_CHECKS: tuple[Check, ...] = (
    Check("public", "A public rate on every room", ("booking", "website"),
          lambda c: c["rooms_with_rates"] == c["room_count"] and c["room_count"] > 0,
          "A room with no rate is a room the booking engine cannot price."),
    Check("deposit", "How much a guest pays to confirm", ("booking",),
          lambda c: (c["property"].get("settings") or {}).get("token_pct") is not None,
          "Full payment, a deposit, or pay at the hotel."),
    Check("negotiation", "How far your price may move", ("agent",),
          lambda c: c["has_negotiation_rules"],
          "Set a floor and your AI can negotiate safely. Without one it can only "
          "give the fixed percentage in your settings."),
    Check("group", "Group pricing", ("agent",),
          lambda c: c["group_tier_count"] > 0,
          "“We are twenty people” — without a tier your AI quotes twenty times "
          "the single-room price."),
    Check("b2b", "Travel-agent rates", ("agent",),
          lambda c: c["b2b_plan_count"] > 0,
          "Optional, but agencies cannot be quoted without one."),
)

TEAM_CHECKS: tuple[Check, ...] = (
    Check("directory", "Somebody to reach", ("agent",),
          lambda c: c["team_count"] > 0,
          "Your AI has nobody to escalate a discount or a group request to."),
    Check("whatsapp", "A WhatsApp number for escalations", ("agent",),
          lambda c: c["team_with_whatsapp"] > 0,
          "Escalations go out on WhatsApp. Without a number they go nowhere."),
    Check("routing", "Who hears about what", ("agent",),
          lambda c: c["notification_rule_count"] > 0,
          "Which of your team is told about a group enquiry, a payment, a "
          "cancellation."),
)

SERVICE_CHECKS: tuple[Check, ...] = (
    Check("services", "Extras you sell", ("agent", "website"),
          lambda c: c["service_count"] > 0,
          "Airport pickup, laundry, a cab — the things guests ask for after the room."),
    Check("units", "How each extra is priced", ("agent",),
          lambda c: c["services_without_unit"] == 0,
          "Per person or per vehicle is the difference between ₹1,500 and ₹9,000."),
)

MARKETING_CHECKS: tuple[Check, ...] = (
    Check("profile", "Who your hotel is for", ("marketing",),
          lambda c: _has_text(c["marketing"].get("usp")) and bool(c["marketing"].get("best_for")),
          "Ad copy is generated from this. Without it there is nothing true to say."),
    Check("audience", "A saved audience", ("marketing",),
          lambda c: c["audience_count"] > 0, ""),
    Check("instagram", "Instagram connected", ("marketing",),
          lambda c: c["instagram_connected"], ""),
)

SECTIONS: tuple[tuple[str, str, tuple[Check, ...]], ...] = (
    ("profile", "Hotel profile", PROFILE_CHECKS),
    ("policies", "Policies", POLICY_CHECKS),
    ("meals", "Meals", MEAL_CHECKS),
    ("pricing", "Pricing", PRICING_CHECKS),
    ("services", "Services", SERVICE_CHECKS),
    ("team", "Team", TEAM_CHECKS),
    ("marketing", "Marketing", MARKETING_CHECKS),
)


def evaluate_sections(context: dict) -> list[SectionResult]:
    """
    Run every section's checks over one already-loaded context.

    Pure, and takes a dict rather than a property id, so the same evaluation can
    be exercised in a unit test without a database and called from the endpoint
    with one read.
    """
    results = []
    for key, label, checks in SECTIONS:
        missing = []
        for check in checks:
            try:
                ok = check.passes(context)
            except Exception:  # noqa: BLE001
                logger.warning("readiness check %s.%s raised", key, check.key, exc_info=True)
                ok = False
            if not ok:
                missing.append({
                    "key": check.key,
                    "label": check.label,
                    "detail": check.detail,
                    "blocks": list(check.blocks),
                })
        results.append(
            SectionResult(
                key=key, label=label,
                passed=len(checks) - len(missing), total=len(checks), missing=missing,
            )
        )
    return results


# ── loading ──────────────────────────────────────────────────────────────────


def _load(property_id: str) -> dict:
    """
    Everything the checks read, in one pass.

    Ten queries rather than one join, because these are ten unrelated tables and
    PostgREST is not going to join them for us — but ten *scalar* reads on
    indexed columns, most of which return under twenty rows. The screen this
    feeds is opened once when a hotelier sits down to finish their setup, not on
    every page load.

    Counts rather than rows where only a count is used: `select("id")` on
    services returns fifteen ids where `count` would do, and the difference is
    invisible at this size — kept as ids anyway so a check that later needs the
    row does not have to change the loader.
    """
    from fastapi import HTTPException, status

    db = get_db()

    prop_rows = (
        db.table("properties").select("*").eq("id", property_id).limit(1).execute().data or []
    )
    if not prop_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    prop = prop_rows[0]

    rooms = (
        db.table("room_types").select("*")
        .eq("property_id", property_id).eq("is_active", True)
        .order("sort_order").limit(200).execute().data or []
    )
    plans = (
        db.table("rate_plans").select("*")
        .eq("property_id", property_id).eq("is_active", True)
        .limit(400).execute().data or []
    )
    site_rows = (
        db.table("hotel_websites").select("status,published,draft")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    site_row = site_rows[0] if site_rows else {}
    site = site_row.get("published") or site_row.get("draft") or {}

    meals = (
        db.table("meal_options").select("meal,availability")
        .eq("property_id", property_id).limit(10).execute().data or []
    )
    time_rules = (
        db.table("stay_time_rules").select("id")
        .eq("property_id", property_id).eq("is_active", True).limit(20).execute().data or []
    )
    negotiation_rules = (
        db.table("negotiation_rules").select("id")
        .eq("property_id", property_id).limit(20).execute().data or []
    )
    tiers = (
        db.table("group_rate_tiers").select("id")
        .eq("property_id", property_id).eq("is_active", True).limit(50).execute().data or []
    )
    team = (
        db.table("hotel_team_members").select("id,whatsapp")
        .eq("property_id", property_id).eq("is_active", True).limit(100).execute().data or []
    )
    rules = (
        db.table("team_notification_rules").select("id")
        .eq("property_id", property_id).eq("is_active", True).limit(100).execute().data or []
    )
    services = (
        db.table("services").select("id,price_unit,price_paise")
        .eq("property_id", property_id).eq("is_active", True).limit(100).execute().data or []
    )
    marketing_rows = (
        db.table("marketing_profiles").select("*")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    audiences = (
        db.table("ad_audiences").select("id")
        .eq("property_id", property_id).limit(50).execute().data or []
    )
    instagram = (
        db.table("instagram_connections").select("status")
        .eq("property_id", property_id).limit(1).execute().data or []
    )

    plans_by_room: dict[str, list[dict]] = {}
    for plan in plans:
        plans_by_room.setdefault(str(plan.get("room_type_id")), []).append(plan)

    return {
        "property": prop,
        "rooms": rooms,
        "plans_by_room": plans_by_room,
        "site": site,
        "site_status": site_row.get("status"),
        # A meal counts as described only if the hotel actually said something
        # about it. A row that exists purely because a form was opened and saved
        # empty is not an answer, so 'unknown' availability does not count.
        "meals": {
            str(m["meal"]) for m in meals
            if str(m.get("availability") or "") in {"available", "on_request", "not_available"}
        },
        "time_rules": time_rules,
        "has_negotiation_rules": bool(negotiation_rules),
        "group_tier_count": len(tiers),
        "b2b_plan_count": sum(1 for p in plans if p.get("audience") == "b2b"),
        "room_count": len(rooms),
        "rooms_with_rates": sum(
            1 for r in rooms
            if any(int(p.get("base_price_paise") or 0) > 0
                   for p in plans_by_room.get(str(r["id"]), []))
        ),
        "team_count": len(team),
        "team_with_whatsapp": sum(1 for t in team if t.get("whatsapp")),
        "notification_rule_count": len(rules),
        "service_count": len(services),
        # A service priced at zero is a free extra, which is a legitimate thing
        # to offer and does not need a unit. Only priced ones are checked.
        "services_without_unit": sum(
            1 for s in services
            if int(s.get("price_paise") or 0) > 0 and not s.get("price_unit")
        ),
        "marketing": marketing_rows[0] if marketing_rows else {},
        "audience_count": len(audiences),
        "instagram_connected": bool(instagram and instagram[0].get("status") == "connected"),
    }


async def report(property_id: str) -> dict:
    """
    The whole readiness picture: sections, rooms, and the named gaps.

    The existing `onboarding.readiness` gap list is *not* replaced. It answers a
    different question — "what is stopping your website being listed on Google"
    — and the panel on Today already reads it. This adds the sectioned view §40
    asks for beside it, from the same load, so the two can never disagree about
    the same hotel.
    """
    context = await run_db(_load, property_id)

    sections = [s.as_dict() for s in evaluate_sections(context)]

    rooms = [
        room_result(room, context["plans_by_room"].get(str(room["id"]), []), context["property"])
        for room in context["rooms"]
    ]
    # Rooms are a section too, and its arithmetic is over rooms rather than
    # checks: "4 of 6 rooms ready" is what a hotelier with six rooms wants to
    # read, not "51 of 78 checks".
    sections.insert(
        1,
        SectionResult(
            key="rooms",
            label="Rooms",
            passed=sum(1 for r in rooms if r["complete"]),
            total=max(len(rooms), 1),
            missing=(
                [] if rooms and all(r["complete"] for r in rooms)
                else [
                    {
                        "key": r["roomTypeId"],
                        "label": r["name"],
                        "detail": (
                            "Cannot be sold: " + r["missing"][0]["label"].lower()
                            if not r["sellable"] and r["missing"]
                            else f"{len(r['missing'])} thing"
                            f"{'' if len(r['missing']) == 1 else 's'} missing"
                        ),
                        "blocks": sorted({b for m in r["missing"] for b in m["blocks"]}),
                    }
                    for r in rooms if not r["complete"]
                ]
                # A hotel with no rooms at all has one thing missing, and it is
                # not any particular room.
                or [{"key": "none", "label": "No rooms yet",
                     "detail": "Add the kinds of room you sell.",
                     "blocks": ["agent", "website", "booking"]}]
            ),
        ).as_dict(),
    )

    remaining = sum(len(s["missing"]) for s in sections)
    return {
        "sections": sections,
        "rooms": rooms,
        "sellableRooms": sum(1 for r in rooms if r["sellable"]),
        "readyRooms": sum(1 for r in rooms if r["readyToSell"]),
        "roomCount": len(rooms),
        # The headline. Named things remaining, not a score — §40 again, and the
        # score is on each section for anyone who wants one.
        "remaining": remaining,
        "complete": remaining == 0,
    }
