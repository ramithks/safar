"""
May we write to this person, and did they ask us to stop.

Two rules, and the second is the one that gets systems into trouble.

**Consent is a record, never an inference.** Not a phone number, not a past
stay, not "they messaged us last week", not "they are in the CRM". Those are all
reasons a hotel *has* someone's number and none of them is permission to
market to them. `guests.marketing_opt_in` is the only authority and it is false
by default; the three columns beside it say when and by what route it became
true, so a hotel asked to prove consent has something to show.

**Opting out is server-side and immediate.** A guest who writes "stop" is opted
out by `apply_inbound()` before the agent runtime is ever reached — before a
model reads the message, before a run is opened, before anything can decide to
be helpful about it. That ordering is the entire safety property: if the model
were the thing that noticed, then a model having a bad day would be a compliance
incident.

The matching is deliberately blunt. A short message that is mostly a stop word
is a stop; a long message that happens to contain "stop" is not, because
"we had to stop at the airport" is not a withdrawal of consent and treating it
as one silently loses a guest the hotel is allowed to talk to. When it is
ambiguous, nothing happens here and the agent handles it as a normal message —
which is the safe direction, because the *next* campaign will still ask.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# How a hotel came to believe it may message someone. Mirrors
# `guests_consent_source_check`; a value not in this tuple fails the insert,
# which is the correct direction for a consent record.
SOURCES = (
    "booking_form", "check_in", "whatsapp_reply", "import", "staff", "unsubscribe",
)

# ── stopping ────────────────────────────────────────────────────────────────
#
# Meta's own reserved keywords plus the phrases people actually type. English
# and Hindi, because this hotel's guests write in both and an opt-out the system
# does not recognise is worse than one it over-recognises.
_STOP_PHRASES = (
    "stop", "unsubscribe", "unsub", "opt out", "optout", "opt-out",
    "remove me", "delete me", "cancel subscription", "no more messages",
    "don't message me", "dont message me", "do not message me",
    "don't text me", "dont text me", "stop messaging", "stop messages",
    "leave me alone", "band karo", "message mat bhejo", "mat bhejo",
    "hata do", "band kar do",
)

# A message this long is prose, not a command. "STOP" is four characters;
# "please stop sending me these offers" is thirty-five. Past that, somebody is
# telling a story that happens to contain the word.
_MAX_STOP_CHARS = 60

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


def _normalise(body: str) -> str:
    return _PUNCT.sub(" ", (body or "").lower()).strip()


def is_stop_request(body: str | None) -> bool:
    """
    Whether this message is a withdrawal of marketing consent.

    Exact match first — "STOP", "unsubscribe" — then a containment pass bounded
    by length. The bound is what stops "we had to stop at the airport" from
    unsubscribing a guest who was making conversation, and the exact pass is
    what guarantees the single word always works however the containment rule
    changes.
    """
    if not body:
        return False
    text = _normalise(body)
    if not text:
        return False

    if text in _STOP_PHRASES:
        return True
    if len(text) > _MAX_STOP_CHARS:
        return False
    return any(phrase in text for phrase in _STOP_PHRASES)


# ── the record ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Consent:
    """One guest's marketing consent at this property."""

    guest_id: str
    opted_in: bool
    opted_in_at: str | None = None
    opted_out_at: str | None = None
    source: str | None = None

    @property
    def may_market(self) -> bool:
        # Both conditions, not just the boolean. A row with `opt_in = true` and
        # an opt-out date is a data error — the writer below cannot produce one
        # — and reading it as consent would be the worst possible way to resolve
        # a contradiction.
        return bool(self.opted_in) and self.opted_out_at is None


def _read(property_id: str, guest_ids: list[str]) -> list[dict]:
    return (
        get_db().table("guests")
        .select("id,marketing_opt_in,marketing_opted_in_at,marketing_opted_out_at,"
                "marketing_consent_source")
        .eq("property_id", property_id)
        .in_("id", guest_ids)
        .execute().data or []
    )


async def for_guests(property_id: str, guest_ids: list[str]) -> dict[str, Consent]:
    """
    Consent for several guests at once, keyed by id.

    Property-scoped in the query, not filtered afterwards. A guest id from
    another hotel returns nothing rather than that hotel's consent record, which
    is the difference between a lookup that fails and a cross-property leak.
    """
    if not guest_ids:
        return {}
    rows = await run_db(_read, property_id, list(guest_ids))
    return {
        r["id"]: Consent(
            guest_id=r["id"],
            opted_in=bool(r.get("marketing_opt_in")),
            opted_in_at=r.get("marketing_opted_in_at"),
            opted_out_at=r.get("marketing_opted_out_at"),
            source=r.get("marketing_consent_source"),
        )
        for r in rows
    }


async def for_guest(property_id: str, guest_id: str) -> Consent | None:
    return (await for_guests(property_id, [guest_id])).get(guest_id)


def _write(property_id: str, guest_id: str, patch: dict) -> dict | None:
    rows = (
        get_db().table("guests").update(patch)
        .eq("id", guest_id).eq("property_id", property_id)
        .execute().data
    )
    return rows[0] if rows else None


async def opt_in(property_id: str, guest_id: str, *, source: str) -> Consent | None:
    """Record consent, with the route it arrived by."""
    if source not in SOURCES:
        raise ValueError(f"unknown consent source {source!r}")
    await run_db(_write, property_id, guest_id, {
        "marketing_opt_in": True,
        "marketing_opted_in_at": datetime.now(timezone.utc).isoformat(),
        "marketing_opted_out_at": None,
        "marketing_consent_source": source,
    })
    return await for_guest(property_id, guest_id)


async def opt_out(property_id: str, guest_id: str, *, source: str = "unsubscribe") -> Consent | None:
    """
    Withdraw it.

    `marketing_opted_in_at` is deliberately left alone. The question a regulator
    asks is "when did they consent and when did they withdraw it", and clearing
    the first to record the second destroys half the answer.
    """
    await run_db(_write, property_id, guest_id, {
        "marketing_opt_in": False,
        "marketing_opted_out_at": datetime.now(timezone.utc).isoformat(),
        "marketing_consent_source": source,
    })
    return await for_guest(property_id, guest_id)


async def apply_inbound(conversation: dict, body: str | None) -> bool:
    """
    Act on a stop request in an inbound message. Returns whether one was found.

    **Called from the webhook path, before the pipeline.** Not from a tool, not
    from the agent, not from a prompt — the model is never asked whether this
    was an opt-out, because a model that answers that question wrong is a
    compliance incident and a model that answers it right is still an
    unnecessary dependency.
    """
    if not is_stop_request(body):
        return False

    guest_id = conversation.get("guest_id")
    if not guest_id:
        # An enquiry from somebody with no guest record. There is nothing to
        # switch off — they were never marketable, because consent lives on a
        # guest row and they do not have one — so this is correctly a no-op.
        logger.info("stop request on conversation %s with no guest record",
                    conversation.get("id"))
        return True

    await opt_out(conversation["property_id"], guest_id)
    logger.info("guest %s opted out of marketing at property %s",
                guest_id, conversation["property_id"])
    return True


# ── how often ───────────────────────────────────────────────────────────────
#
# The frequency cap. Configuration on the property, never a number in a prompt:
# a limit the model knows is a limit the model can be argued out of, and a limit
# in `properties.settings` is one a hotelier can lower on a Tuesday because they
# got a complaint.

DEFAULT_MAX_PER_7D = 1
DEFAULT_MAX_PER_30D = 3


@dataclass(frozen=True)
class FrequencyPolicy:
    max_per_7d: int
    max_per_30d: int

    def exceeded_by(self, sent_7d: int, sent_30d: int) -> bool:
        return sent_7d >= self.max_per_7d or sent_30d >= self.max_per_30d


def resolve_frequency(settings: dict | None) -> FrequencyPolicy:
    """
    Read the cap off `properties.settings`, forgivingly and conservatively.

    A malformed value falls back to the default rather than raising — the same
    rule `resolve_property_limits` follows — and the fallback is the *tighter*
    number in every case. A property whose JSON somebody hand-edited should
    under-message, not over-message.
    """
    settings = settings or {}

    def _read_int(key: str, default: int) -> int:
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError):
            return default
        # A negative cap means "never", which is a legitimate setting. A cap of
        # a thousand is a typo, and honouring it is how a hotel gets blocked.
        return max(0, min(value, 30))

    return FrequencyPolicy(
        max_per_7d=_read_int("marketing_max_per_7d", DEFAULT_MAX_PER_7D),
        max_per_30d=_read_int("marketing_max_per_30d", DEFAULT_MAX_PER_30D),
    )


def _settings(property_id: str) -> dict | None:
    rows = (
        get_db().table("properties").select("settings")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return rows[0].get("settings") if rows else None


async def frequency_for_property(property_id: str) -> FrequencyPolicy:
    try:
        return resolve_frequency(await run_db(_settings, property_id))
    except Exception:  # noqa: BLE001
        logger.warning("could not read frequency policy for %s", property_id, exc_info=True)
        return FrequencyPolicy(DEFAULT_MAX_PER_7D, DEFAULT_MAX_PER_30D)


def _recent_sends(property_id: str, guest_ids: list[str], since: str) -> list[dict]:
    return (
        get_db().table("campaign_recipients")
        .select("guest_id,attempted_at")
        .eq("property_id", property_id)
        .in_("guest_id", guest_ids)
        .eq("status", "sent")
        .gte("attempted_at", since)
        .limit(5000)
        .execute().data or []
    )


async def recent_send_counts(
    property_id: str, guest_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """
    How many marketing messages each guest has had in the last 7 and 30 days.

    Counted from `campaign_recipients`, not from `conversation_messages`. That
    is the distinction the cap is about: a guest who exchanged forty messages
    with the front desk yesterday has had zero marketing, and counting the
    thread would silence a hotel's marketing because its service was good.
    """
    if not guest_ids:
        return {}

    now = datetime.now(timezone.utc)
    since_30 = (now - timedelta(days=30)).isoformat()
    rows = await run_db(_recent_sends, property_id, list(guest_ids), since_30)

    cutoff_7 = now - timedelta(days=7)
    counts: dict[str, list[int]] = {}
    for row in rows:
        slot = counts.setdefault(row["guest_id"], [0, 0])
        slot[1] += 1
        attempted = row.get("attempted_at")
        if attempted:
            try:
                when = datetime.fromisoformat(str(attempted).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when >= cutoff_7:
                slot[0] += 1

    return {gid: (c[0], c[1]) for gid, c in counts.items()}
