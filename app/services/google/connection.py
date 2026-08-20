"""
A hotel's Google connection, in our terms rather than Google's.

Two jobs. The first is the `google_connections` row: where consent is recorded,
which listing the owner picked, and what we last saw on it. The second is the
drafting — a reply to a review, or a post — which is a plain model call with no
tools and no side effects, because **nothing here publishes anything**.

That separation is the point. `draft_review_reply` returns words. Publishing
them is a second, explicit call that an owner has to make, and the two live at
different endpoints so no refactor can quietly join them. An AI that answers a
hotel's public reviews unprompted is a reputational accident waiting for its
first bad draft.
"""
import logging
from typing import Any

from app.db.client import get_db, run_db
from app.services.ai import model_router, providers
from app.services.google import client as gclient
from app.services.google import oauth
from app.services.messaging import store

logger = logging.getLogger(__name__)

TABLE = "google_connections"
CHANNEL = "google"


# ── the row ─────────────────────────────────────────────────────────────────


def get_connection(property_id: str) -> dict | None:
    db = get_db()
    row = (
        db.table(TABLE).select("*").eq("property_id", property_id).limit(1).execute()
    )
    return (row.data or [None])[0]


def upsert_pending(*, property_id: str, partner_id: str, credentials_ref: str) -> dict:
    """
    Consent granted, listing not chosen yet.

    Written before the owner picks a location because the token exists from this
    moment and has to be findable. A row that appeared only after the choice
    would leave a live Google credential with nothing pointing at it if the
    owner closed the tab on the picker.
    """
    payload = {
        "property_id": property_id,
        "partner_id": partner_id,
        "credentials_ref": credentials_ref,
        "status": "pending",
        "last_error": None,
        "connected_at": "now()",
    }
    db = get_db()
    result = db.table(TABLE).upsert(payload, on_conflict="property_id").execute()
    return (result.data or [{}])[0]


def attach_location(
    *,
    property_id: str,
    account: gclient.Account,
    location: gclient.Location,
) -> dict:
    patch: dict[str, Any] = {
        "account_name": account.name,
        "account_display_name": account.display_name,
        "location_name": location.name,
        "location_title": location.title,
        "address": location.address,
        "primary_phone": location.phone,
        "website_uri": location.website,
        "primary_category": location.category,
        "is_verified": location.is_verified,
        "maps_uri": location.maps_uri,
        "review_uri": location.review_uri,
        "status": "connected",
        "last_error": None,
        "last_synced_at": "now()",
    }
    db = get_db()
    result = db.table(TABLE).update(patch).eq("property_id", property_id).execute()
    return (result.data or [{}])[0]


def record_error(property_id: str, message: str) -> None:
    db = get_db()
    (
        db.table(TABLE)
        .update({"status": "error", "last_error": message})
        .eq("property_id", property_id)
        .execute()
    )


def record_rating(property_id: str, *, average: float | None, count: int | None) -> None:
    """
    Cache what the reviews call already told us.

    Not a separate fetch: `list_reviews` returns the location's average and total
    alongside the page, so the dashboard can show a rating without a second
    round trip to Google on every load.
    """
    patch: dict[str, Any] = {"last_synced_at": "now()"}
    if average is not None:
        patch["average_rating"] = round(float(average), 1)
    if count is not None:
        patch["review_count"] = int(count)

    db = get_db()
    db.table(TABLE).update(patch).eq("property_id", property_id).execute()


def disconnect(property_id: str) -> dict | None:
    db = get_db()
    result = (
        db.table(TABLE)
        .update({"status": "disconnected", "last_error": None})
        .eq("property_id", property_id)
        .execute()
    )
    return (result.data or [None])[0]


async def token_for(connection: dict) -> str:
    """A live access token for a connection row."""
    return await oauth.access_token(credentials_ref=connection["credentials_ref"])


async def revoke_everything(connection: dict) -> None:
    """
    Tell Google to forget us, then forget Google.

    Google first and best-effort: if the revoke fails we still take our copy out
    of service, because leaving an owner unable to disconnect because Google was
    briefly unreachable is the worse failure.
    """
    ref = connection["credentials_ref"]
    try:
        refresh = await run_db(store.load_refresh_token, ref, CHANNEL)
        if refresh:
            await oauth.revoke(refresh)
    except Exception:  # noqa: BLE001
        logger.warning("could not revoke the Google token for %s", ref, exc_info=True)

    await run_db(store.revoke, channel=CHANNEL, credentials_ref=ref)
    await run_db(disconnect, connection["property_id"])


# ── drafting ────────────────────────────────────────────────────────────────

_REPLY_SYSTEM = """You write replies to Google reviews on behalf of a hotel.

You are writing as the hotel, in the first person plural ("we", "our team").

Rules:
- 2 to 4 sentences. Never longer. A wall of text under a review reads as PR.
- Thank the guest by first name if one is given, and never invent one.
- Name something specific they mentioned. Generic gratitude is worse than none.
- For a complaint: acknowledge it plainly, say what has been done or will be,
  and invite them to contact the hotel directly. Never argue, never blame the
  guest, never make an excuse, and never promise a refund or any compensation.
- No emoji, no hashtags, no marketing slogans, no "book direct" pitch.
- Never state a fact about the hotel you were not given below.

Return only the reply text. No preamble, no quotation marks, no options."""

_POST_SYSTEM = """You write short posts for a hotel's Google Business Profile.

Rules:
- 1 to 3 sentences, under 250 characters. Google truncates longer posts.
- Concrete and specific: a real thing a traveller can picture and act on.
- No emoji, no hashtags, no ALL CAPS, no exclamation marks stacked up.
- Never invent a price, a date, a discount or an amenity you were not given.

Return only the post text. No preamble, no quotation marks, no options."""


async def _one_shot(*, system: str, user: str, task: str, max_tokens: int) -> str:
    """
    One model call, no tools, no persistence.

    Deliberately not routed through `ai/runtime.py`. That runtime exists to run
    an *agent*: it opens a run, resolves permissions, exposes tools, meters
    usage and writes agent_actions. None of that applies to composing a
    paragraph nobody has agreed to publish, and borrowing it would put a draft
    that may be discarded into the hotel's audit trail as an action.
    """
    choice = model_router.select(agent_type="marketing", task=task)
    llm = providers.for_name(choice.provider)

    step = await llm.step(
        model=choice.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=[],
        params={
            "temperature": choice.temperature,
            "max_output_tokens": max_tokens,
            "thinking_budget": choice.thinking_budget,
        },
    )
    text = (step.reply or "").strip()
    if not text:
        raise gclient.GoogleApiError("draft_failed", "the model returned nothing")

    # Models like to wrap a single requested string in quotes. Strip a matched
    # pair only — a reply that legitimately opens and closes on a quotation is
    # rare enough, and mangling it would be worse than leaving the quotes on.
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


async def draft_review_reply(*, property_name: str, review: gclient.Review) -> str:
    stars = review.star_rating or 0
    tone = "unhappy" if stars and stars <= 2 else "mixed" if stars == 3 else "happy"

    user = "\n".join(
        (
            f"Hotel: {property_name}",
            f"Guest: {review.reviewer_name}",
            f"Rating: {stars} out of 5 ({tone})",
            f"What they wrote: {review.comment or '(they left a rating with no words)'}",
        )
    )
    return await _one_shot(
        system=_REPLY_SYSTEM,
        user=user,
        # A one-star review is a dispute in public. That is the reasoning tier's
        # job by the same rule the rest of the system uses for money and
        # conflict, and it is the cheapest place in the product to spend more.
        task="cancellation_dispute" if stars and stars <= 2 else "marketing_planning",
        max_tokens=400,
    )


async def draft_post(*, property_name: str, city: str | None, brief: str) -> str:
    user = "\n".join(
        (
            f"Hotel: {property_name}" + (f", {city}" if city else ""),
            f"What the owner wants to say: {brief}",
        )
    )
    return await _one_shot(
        system=_POST_SYSTEM, user=user, task="marketing_planning", max_tokens=300
    )
