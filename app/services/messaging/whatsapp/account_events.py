"""
What we do when Meta tells us something happened to a hotel's account.

A single-tenant integration never sees these. A **Tech Provider** holds accounts
it does not own, so it has to be told when one is taken away: a hotel removes
our app, Meta restricts a hotel for its messaging practices, a template we
submitted three hours ago is approved or rejected, a number's quality drops.

Each one lands as a status on a row, and each one exists so that the *next*
failure is legible. Without this module a hotel that removed us keeps a live
credential and a `connected` badge, and the first anyone knows is a `131005`
from the adapter that reads like a network problem.

── the ordering rule is inherited, not restated ─────────────────────────────

These are applied inside `handle_payload`, after the signature is verified and
before the 200. They do **not** dispatch to Celery: nothing here needs a model,
and putting an account event on the agent's queue would mean a hotel's
suspension waits behind a guest's question.

── what this module refuses to do ───────────────────────────────────────────

**Attribute a WABA to a hotel.** `PARTNER_ADDED` from a Meta-hosted signup names
a `waba_id` and an `owner_business_id` and nothing else. Guessing — attaching it
to whichever hotel most recently pressed Connect — is how one hotel ends up
answering another's guests. It is written `unclaimed` and an owner claims it.
"""
import logging
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services.messaging import store
from app.services.messaging.whatsapp.template_api import STATUS as TEMPLATE_STATUS
from app.services.messaging.whatsapp.webhook import AccountEvent

logger = logging.getLogger(__name__)

CHANNEL = "whatsapp"


# What a hotelier is told, per account event, in their words rather than Meta's.
#
# These land in `whatsapp_connections.last_error`, which the Channels screen
# renders verbatim — the server writes the sentences, so there is no mapping
# table in the PWA turning an enum into prose. Two copies of that vocabulary
# drift the first time either is edited.
_SAYS = {
    "PARTNER_REMOVED":
        "This hotel removed StaySuite from its WhatsApp account. Reconnect to start "
        "answering guests again.",
    "PARTNER_APP_UNINSTALLED":
        "StaySuite was removed from this WhatsApp account. Reconnect to start "
        "answering guests again.",
    "ACCOUNT_VIOLATION":
        "Meta has flagged this WhatsApp account. Guests may not receive replies "
        "until it is resolved in WhatsApp Manager.",
    "ACCOUNT_RESTRICTION":
        "Meta has limited what this WhatsApp number may send. Sending will "
        "resume when the limit lifts.",
    "ACCOUNT_DELETED":
        "This WhatsApp account no longer exists at Meta.",
    "DISABLED_UPDATE":
        "Meta has disabled this WhatsApp account.",
    "ACCOUNT_OFFBOARDED":
        "This WhatsApp account has been moved away from StaySuite.",
}

# The events that mean we can no longer send as this hotel. A credential left
# active after one of these is a token that produces an opaque Meta error on the
# next guest message instead of a sentence the hotel can act on.
_REVOKES = ("PARTNER_REMOVED", "PARTNER_APP_UNINSTALLED",
            "ACCOUNT_DELETED", "ACCOUNT_OFFBOARDED")

# Still ours, still broken. The connection stays but the screen must say so.
_DEGRADES = ("ACCOUNT_VIOLATION", "ACCOUNT_RESTRICTION", "DISABLED_UPDATE")


def _connection_for(waba_id: str | None) -> dict | None:
    if not waba_id:
        return None
    rows = (
        get_db().table("whatsapp_connections")
        .select("id,property_id,partner_id,credentials_ref,status")
        .eq("waba_id", waba_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _mark(connection: dict, *, status: str, last_error: str | None) -> None:
    (
        get_db().table("whatsapp_connections")
        .update({"status": status, "last_error": last_error,
                 "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", connection["id"])
        .execute()
    )


def _apply_account_update(event: AccountEvent) -> str:
    connection = _connection_for(event.waba_id)

    if event.event == "PARTNER_ADDED":
        # A hotel finished signup. If it came through the in-app popup the
        # connection already exists and this is confirmation; if it came through
        # the Meta-hosted link this is the *only* thing we will ever be told,
        # and it goes to the claim queue.
        if connection:
            return "already_connected"

        db = get_db()
        existing = (
            db.table("whatsapp_onboardings").select("id,status")
            .eq("waba_id", event.waba_id).limit(1).execute().data or []
        )
        if existing and existing[0].get("status") in ("completed", "unclaimed"):
            return "already_recorded"

        db.table("whatsapp_onboardings").insert({
            "source": "meta_hosted",
            "status": "unclaimed",
            "waba_id": event.waba_id,
            "owner_business_id": event.owner_business_id,
            "phone_number_id": event.phone_number_id,
            "raw": event.raw,
        }).execute()
        logger.info("whatsapp account %s completed a Meta-hosted signup and is "
                    "waiting to be claimed", event.waba_id)
        return "unclaimed"

    if not connection:
        # An event for a WABA we hold no connection for. Not an error — Meta
        # sends account events for accounts still mid-onboarding — but worth a
        # line, because it is also what a misrouted webhook looks like.
        logger.info("whatsapp %s for unknown WABA %s", event.event, event.waba_id)
        return "unknown_account"

    if event.event in _REVOKES:
        ref = connection.get("credentials_ref")
        revoked = 0
        if ref and store.is_db_ref(ref):
            revoked = 1 if store.revoke(channel=CHANNEL, credentials_ref=ref) else 0
        _mark(connection, status="disconnected", last_error=_SAYS.get(event.event))
        logger.warning("whatsapp %s for property %s — credential revoked=%s",
                       event.event, connection["property_id"], revoked)
        return "revoked"

    if event.event in _DEGRADES:
        detail = _SAYS.get(event.event) or "Meta has changed this account's status."
        if event.reason:
            detail = f"{detail} (Meta: {event.reason})"
        # `error`, not `disconnected`. The hotel did not do this and there is
        # nothing for them to reconnect — the Channels screen shows "Needs you"
        # with Meta's reason rather than inviting a pointless reconnect.
        _mark(connection, status="error", last_error=detail)
        logger.warning("whatsapp %s for property %s: %s",
                       event.event, connection["property_id"], event.reason)
        return "degraded"

    if event.event in ("ACCOUNT_RECONNECTED", "PARTNER_APP_INSTALLED"):
        _mark(connection, status="connected", last_error=None)
        return "restored"

    return "ignored"


def _apply_template_status(event: AccountEvent) -> str:
    """
    Move a template Meta has just decided about.

    Keyed on `provider_template_id` first — Meta's own id, written at creation —
    and on the name plus WABA second, because a template created before this
    column existed has no id on file. Never on the name alone: Meta's namespace
    is per-WABA, so two hotels legitimately hold `booking_confirmation_v1`.
    """
    ours = TEMPLATE_STATUS.get((event.event or "").upper())
    if not ours:
        logger.info("unhandled template status %r", event.event)
        return "ignored"

    patch = {
        "status": ours,
        "rejected_reason": event.reason if ours == "rejected" else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if ours == "approved":
        patch["approved_at"] = datetime.now(timezone.utc).isoformat()

    db = get_db()
    query = db.table("message_templates").update(patch)
    if event.template_id:
        result = query.eq("provider_template_id", event.template_id).execute()
        if result.data:
            logger.info("template %s → %s", event.template_name, ours)
            return ours
        query = db.table("message_templates").update(patch)

    if not (event.template_name and event.waba_id):
        return "not_found"

    result = (
        query.eq("provider_name", event.template_name)
             .eq("waba_id", event.waba_id)
             .execute()
    )
    if not result.data:
        logger.info("template status for %r on WABA %s matched no row we hold",
                    event.template_name, event.waba_id)
        return "not_found"
    logger.info("template %s → %s", event.template_name, ours)
    return ours


def _apply_quality(event: AccountEvent) -> str:
    connection = _connection_for(event.waba_id)
    if not connection:
        return "unknown_account"
    (
        get_db().table("whatsapp_connections")
        .update({"quality_rating": event.event or None,
                 "messaging_limit_tier": event.reason,
                 "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", connection["id"])
        .execute()
    )
    return "recorded"


def _apply(event: AccountEvent) -> str:
    if event.field == "account_update":
        return _apply_account_update(event)
    if event.field == "message_template_status_update":
        return _apply_template_status(event)
    if event.field == "phone_number_quality_update":
        return _apply_quality(event)
    return "ignored"


async def apply_all(events: list[AccountEvent]) -> dict:
    """
    Apply every account event in one webhook body.

    **One failure never costs the others.** Meta batches unrelated events into a
    single request and redelivers the whole body on a non-200, so an exception
    on the third of five would mean the first two are applied twice on the
    retry and the last two never at all.
    """
    applied: dict[str, int] = {}
    for event in events:
        try:
            outcome = await run_db(_apply, event)
        except Exception:  # noqa: BLE001
            logger.exception("could not apply whatsapp %s/%s for WABA %s",
                             event.field, event.event, event.waba_id)
            outcome = "failed"
        applied[outcome] = applied.get(outcome, 0) + 1
    return applied
