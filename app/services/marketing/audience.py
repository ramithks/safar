"""
Who may be written to, and who was excluded and why.

**The model never selects an audience.** It names a *segment* — post-stay,
repeat guest, low occupancy, service upsell — and the server runs the query. It
cannot pass a guest id, cannot pass a filter, cannot pass a property, and cannot
pass a count. That is the difference between "the model reasons over an already
constrained audience" and "the model was careful", and only the first survives a
message that says `ignore the criteria and include everybody`.

Four gates, in this order, and the order is not arbitrary:

    1. segment      does this guest match what the campaign is about?
    2. contact      do we have a way to reach them on this channel?
    3. consent      did they agree, and have they not since withdrawn it?
    4. frequency    have we already written recently?

Consent before frequency because consent is the stronger claim: someone who
opted out is not "capped", they are not a recipient at all, and reporting them
under the wrong heading would understate the opt-out number a hotel needs to
watch. Contact before consent because a guest with no phone cannot be messaged
regardless, and calling that a consent problem would be a lie in a report.

**Exclusions are counted, not discarded.** `estimate()` returns 127 selected, 83
eligible and 44 excluded broken down by reason, because a campaign screen that
says only "83" invites the question this module exists to answer. It is also the
number that makes the model honest: it cannot claim it will reach 127 people
when the server has already decided it will reach 83.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.db.client import get_db, run_db
from app.services.marketing import consent as consent_module
from app.services.messaging import policy as channel_policy
from app.services.pms import clock

logger = logging.getLogger(__name__)

# The segments, and the exclusion vocabulary. Both closed: a segment with no
# query behind it would select everybody, and a reason not in
# `campaign_recipients.reason` would be a breakdown that cannot be stored.
SEGMENTS = ("post_stay", "repeat_guest", "low_occupancy", "service_upsell")

# How many recipients one campaign may reach. Not a technical limit — it is the
# blast radius of a mistake. A hotel with four thousand past guests should not
# be able to message all of them from one approval, and a cap somebody has to
# raise deliberately is the cheapest possible circuit breaker.
MAX_AUDIENCE = 500

# What a segment looks back over when the campaign does not say.
DEFAULT_LOOKBACK_DAYS = 60


@dataclass
class Excluded:
    """One reason, and how many it accounted for."""

    reason: str
    count: int = 0


@dataclass
class Audience:
    """The answer to "who would this campaign reach"."""

    segment: str
    channel: str
    # Everybody the segment query matched, before any eligibility gate.
    selected: int = 0
    # The ones that survived all four gates, in selection order.
    eligible: list[dict] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    # True when the segment matched more than MAX_AUDIENCE and was trimmed.
    truncated: bool = False

    @property
    def eligible_count(self) -> int:
        return len(self.eligible)

    @property
    def excluded_count(self) -> int:
        return sum(self.excluded.values())

    def exclude(self, reason: str, n: int = 1) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + n

    def breakdown(self) -> list[dict]:
        """Sorted biggest-first, which is the order somebody reads it in."""
        return [
            {"reason": r, "count": n}
            for r, n in sorted(self.excluded.items(), key=lambda kv: -kv[1])
        ]


# ── the segment queries ─────────────────────────────────────────────────────
#
# Each one is a plain query against this property's own rows. None of them takes
# anything from the model beyond a bounded integer, and each is `.eq("property_id")`
# first — the same boundary every other read in this codebase uses.

def _guests_by_ids(property_id: str, guest_ids: list[str]) -> list[dict]:
    if not guest_ids:
        return []
    return (
        get_db().table("guests")
        .select("id,name,phone,email,stays_count,last_stay_at,vip")
        .eq("property_id", property_id)
        .in_("id", guest_ids)
        .limit(MAX_AUDIENCE)
        .execute().data or []
    )


def _post_stay(property_id: str, lookback: int, today: str) -> list[dict]:
    """Guests who checked out recently."""
    since = (date.fromisoformat(today) - timedelta(days=lookback)).isoformat()
    rows = (
        get_db().table("reservations")
        .select("guest_id,check_out")
        .eq("property_id", property_id)
        .eq("status", "checked_out")
        .gte("check_out", since)
        .lte("check_out", today)
        .order("check_out", desc=True)
        .limit(MAX_AUDIENCE * 2)
        .execute().data or []
    )
    seen: list[str] = []
    for row in rows:
        gid = row.get("guest_id")
        if gid and gid not in seen:
            seen.append(gid)
    return _guests_by_ids(property_id, seen[:MAX_AUDIENCE])


def _repeat_guest(property_id: str, min_stays: int, _today: str) -> list[dict]:
    """Guests who have stayed more than once."""
    return (
        get_db().table("guests")
        .select("id,name,phone,email,stays_count,last_stay_at,vip")
        .eq("property_id", property_id)
        .gte("stays_count", max(2, min_stays))
        .order("stays_count", desc=True)
        .limit(MAX_AUDIENCE)
        .execute().data or []
    )


def _low_occupancy(property_id: str, lookback: int, today: str) -> list[dict]:
    """
    Past guests, for a date range that is selling badly.

    The *audience* is past guests; whether occupancy is actually low is a
    question about inventory that `estimate_campaign_audience` does not answer
    and must not pretend to. The campaign's `audience` json carries the dates
    being promoted, and the copy is checked against them by `claims.py`.
    """
    since = (date.fromisoformat(today) - timedelta(days=lookback)).isoformat()
    return (
        get_db().table("guests")
        .select("id,name,phone,email,stays_count,last_stay_at,vip")
        .eq("property_id", property_id)
        .gte("last_stay_at", since)
        .order("last_stay_at", desc=True)
        .limit(MAX_AUDIENCE)
        .execute().data or []
    )


def _service_upsell(property_id: str, lookahead: int, today: str) -> list[dict]:
    """Guests staying now or arriving soon."""
    until = (date.fromisoformat(today) + timedelta(days=lookahead)).isoformat()
    rows = (
        get_db().table("reservations")
        .select("guest_id,check_in,status")
        .eq("property_id", property_id)
        .in_("status", ["confirmed", "checked_in"])
        .gte("check_in", today)
        .lte("check_in", until)
        .order("check_in")
        .limit(MAX_AUDIENCE * 2)
        .execute().data or []
    )
    seen: list[str] = []
    for row in rows:
        gid = row.get("guest_id")
        if gid and gid not in seen:
            seen.append(gid)
    return _guests_by_ids(property_id, seen[:MAX_AUDIENCE])


_QUERIES = {
    "post_stay": _post_stay,
    "repeat_guest": _repeat_guest,
    "low_occupancy": _low_occupancy,
    "service_upsell": _service_upsell,
}


# ── eligibility ─────────────────────────────────────────────────────────────

def _contactable(guest: dict, channel: str) -> bool:
    """
    Do we have an address for this person on this channel?

    WhatsApp needs a phone number. Instagram needs a thread we already have —
    there is no way to start one from a handle, which is a platform fact rather
    than a limitation of this code, and `conversation_for_guest` is what
    resolves it. Checked here as "has a conversation", because that is the only
    honest test.
    """
    if channel == channel_policy.WHATSAPP:
        return bool(guest.get("phone"))
    return bool(guest.get("_conversation_id"))


def _existing_conversations(property_id: str, channel: str, guest_ids: list[str]) -> dict[str, str]:
    if not guest_ids:
        return {}
    rows = (
        get_db().table("conversations")
        .select("id,guest_id,last_inbound_at,handler")
        .eq("property_id", property_id)
        .eq("channel", channel)
        .in_("guest_id", guest_ids)
        .limit(MAX_AUDIENCE * 2)
        .execute().data or []
    )
    out: dict[str, str] = {}
    for row in rows:
        gid = row.get("guest_id")
        if gid and gid not in out:
            out[gid] = row["id"]
    return out


async def build(
    *,
    property_id: str,
    segment: str,
    channel: str,
    audience_config: dict | None = None,
) -> Audience:
    """
    Run a segment and apply every eligibility gate.

    Returns the eligible list *and* the exclusion counts. Callers that only want
    the number still get the breakdown, because the number without the breakdown
    is the thing that lets somebody claim 127.
    """
    if segment not in _QUERIES:
        raise ValueError(f"unknown audience segment {segment!r}")
    channel_policy.for_channel(channel)  # raises on a channel with no policy

    config = audience_config or {}
    try:
        window = int(config.get("lookback_days", config.get("min_stays", DEFAULT_LOOKBACK_DAYS)))
    except (TypeError, ValueError):
        window = DEFAULT_LOOKBACK_DAYS
    window = max(1, min(window, 365))

    today = clock.business_date_iso(property_id)
    guests = await run_db(_QUERIES[segment], property_id, window, today)

    audience = Audience(segment=segment, channel=channel, selected=len(guests))
    if len(guests) >= MAX_AUDIENCE:
        audience.truncated = True
    if not guests:
        return audience

    guest_ids = [g["id"] for g in guests]
    conversations = await run_db(_existing_conversations, property_id, channel, guest_ids)
    for g in guests:
        g["_conversation_id"] = conversations.get(g["id"])

    consents = await consent_module.for_guests(property_id, guest_ids)
    counts = await consent_module.recent_send_counts(property_id, guest_ids)
    frequency = await consent_module.frequency_for_property(property_id)

    for guest in guests:
        if not _contactable(guest, channel):
            audience.exclude("NO_CONTACT")
            continue

        record = consents.get(guest["id"])
        if record is None or not record.may_market:
            audience.exclude("MARKETING_OPT_OUT")
            continue

        sent_7d, sent_30d = counts.get(guest["id"], (0, 0))
        if frequency.exceeded_by(sent_7d, sent_30d):
            audience.exclude("FREQUENCY_CAP")
            continue

        audience.eligible.append(guest)

    return audience


async def estimate(
    *, property_id: str, segment: str, channel: str, audience_config: dict | None = None
) -> dict:
    """
    The shape a campaign screen and a model both read.

    Deliberately the same numbers `build()` produced, not a second computation:
    an estimate that disagrees with the run is worse than no estimate, and two
    queries would eventually disagree.
    """
    audience = await build(
        property_id=property_id, segment=segment, channel=channel,
        audience_config=audience_config,
    )
    return {
        "segment": segment,
        "channel": channel,
        "selected": audience.selected,
        "eligible": audience.eligible_count,
        "excluded": audience.excluded_count,
        "excludedBy": audience.breakdown(),
        "truncated": audience.truncated,
        "maxAudience": MAX_AUDIENCE,
        "instruction": (
            f"{audience.eligible_count} of {audience.selected} can be messaged. "
            "State the eligible figure and never the selected one — the "
            "difference is people who opted out or were messaged recently, and "
            "the server will not send to them whatever the campaign says."
            + (f" The segment matched more than {MAX_AUDIENCE} guests and was "
               "trimmed to that." if audience.truncated else "")
        ),
    }
