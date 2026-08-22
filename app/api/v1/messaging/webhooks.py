"""
The webhook Meta calls.

Two routes and a strict order of operations, because the order *is* the safety
property:

    verify signature  ──►  resolve property  ──►  persist  ──►  acknowledge
                                                                    │
                                                                    └─► process

Meta retries anything it does not get a prompt 200 for, and it retries anything
that looks like a failure. So the acknowledgement has to come after the durable
write and before the model call: persist-then-ack means a retry finds the
message already recorded and does nothing, while ack-then-persist would lose
messages and persist-ack-after-the-model would guarantee retries on every slow
turn — each of which becomes a second reply to a guest.

**Unauthenticated by definition.** These routes have no JWT and cannot have one;
Meta does not hold one. Everything that would normally come from a session comes
instead from the signature and from `whatsapp_connections`. In particular the
property is resolved from the phone number id the message arrived on — never
from the body, which is written by whoever sent the request.

The handler returns 200 for almost everything, including messages it could not
process. A non-200 tells Meta to redeliver, and redelivering a message we have
already stored helps nobody. The exception is a bad signature, which is a 403:
that request was not from Meta and there is nothing to redeliver.
"""
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.config import settings
from app.core.limiter import limiter
from app.services.messaging import credentials as creds_module
from app.services.messaging import pipeline
from app.services.messaging import service as messaging
from app.services.messaging.channels import MessagingError
from app.services import trace
from app.services.ai import usage as usage_mod
from app.services.messaging import policy
from app.services.messaging.instagram import webhook as ig
from app.services.messaging.whatsapp import webhook as wa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messaging", tags=["messaging"])


@router.get("/whatsapp/webhook")
@limiter.limit("30/minute")
async def verify_whatsapp_webhook(
    request: Request,
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """
    Meta's one-time handshake.

    The challenge must come back as `text/plain` and nothing else — Meta
    compares the body byte for byte, and a JSON-quoted string fails the
    subscription with an error that says nothing about quoting.
    """
    try:
        challenge = wa.verify_challenge(
            mode=hub_mode,
            token=hub_verify_token,
            challenge=hub_challenge,
            expected_token=settings.whatsapp_verify_token,
        )
    except ValueError as exc:
        logger.warning("whatsapp webhook verification refused: %s", exc)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed") from exc

    return Response(content=challenge, media_type="text/plain")


@router.post("/whatsapp/webhook")
@limiter.limit("600/minute")
async def receive_whatsapp_webhook(request: Request):
    """
    Inbound messages and delivery receipts.

    Reads the raw body first: the signature is over the bytes as sent, and
    anything that re-serialises the JSON before checking is checking a different
    document from the one Meta signed.
    """
    body = await request.body()

    if not wa.verify_signature(
        body=body,
        header=request.headers.get("X-Hub-Signature-256"),
        app_secret=creds_module.resolve("default").app_secret,
    ):
        # 403, not 200. This request was not from Meta, so there is nothing for
        # Meta to redeliver and nothing to record.
        logger.warning("whatsapp webhook with a bad or missing signature")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signature")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        logger.warning("whatsapp webhook body was not JSON")
        return {"received": True, "processed": 0}

    return await handle_payload(payload)


async def handle_payload(payload: dict, *, provider=None, channel: str = "whatsapp") -> dict:
    """
    The part after the signature.

    Separated so the E2E suite can drive the same code path it would drive over
    HTTP without having to forge a signature for every case — the signature
    itself gets its own tests, positive and negative. `provider` rides along for
    the same reason it does in the runtime: the suite scripts the model and
    asserts on the database.

    One function for both channels. The *parsers* differ and the *policies*
    differ; the order of operations — resolve, persist, acknowledge, dispatch —
    is the safety property and must not differ, because a second copy of it is a
    second place for the order to be got wrong.
    """
    parser = ig if channel == "instagram" else wa
    messages, statuses = parser.parse(payload)
    accepted = duplicates = 0
    touched: set[str] = set()

    for inbound in messages:
        try:
            connection = await messaging.resolve_connection(inbound.account_id, channel)
        except MessagingError as exc:
            # An account nobody has connected. Logged and acknowledged: we
            # cannot say whose guest this is, and a redelivery will not change
            # that.
            logger.warning("inbound %s for unknown account: %s", channel, exc.detail)
            continue

        recorded = await messaging.record_inbound(inbound, connection)
        if recorded.duplicate:
            # The whole reason persistence comes first. No second conversation,
            # no second run, no second ticket, no second reply.
            duplicates += 1
            continue

        accepted += 1
        touched.add(recorded.conversation["id"])
        # Bound as soon as we know it, so every line from here down — including
        # the ones libraries emit — carries the thread this message belongs to.
        trace.bind(conversation_id=recorded.conversation["id"],
                   hotel_id=recorded.conversation["property_id"],
                   guest_id=recorded.conversation.get("guest_id"))
        logger.info("inbound %s message accepted on conversation %s",
                    channel, recorded.conversation["id"])

        # A channel Meta does not bill per conversation gets its conversation
        # charge opened here, on the message that actually opened the window.
        # WhatsApp's arrives on the delivery callback instead, where Meta names
        # the category and the price — see messaging.apply_delivery_status.
        #
        # Keyed on the opening message, so a redelivery (handled above) and a
        # second message inside the same window both resolve to one charge.
        pol = policy.for_channel(channel)
        if recorded.opened_window and not pol.bills_per_conversation \
                and pol.conversation_usage_type:
            try:
                await usage_mod.record_conversation(
                    property_id=recorded.conversation["property_id"],
                    channel=channel,
                    category="conversation",
                    external_conversation_id=inbound.external_id,
                    conversation_id=recorded.conversation["id"],
                    message_id=recorded.message["id"],
                    usage_type=pol.conversation_usage_type,
                )
            except Exception:  # noqa: BLE001
                logger.warning("could not meter the %s conversation window", channel,
                               exc_info=True)

    for update in statuses:
        await messaging.apply_delivery_status(update)

    # Enqueued after everything is durable. A worker that starts late, twice or
    # never leaves a guest visible in the inbox rather than lost.
    for conversation_id in touched:
        await pipeline.dispatch(conversation_id, provider=provider)

    return {
        "received": True,
        "accepted": accepted,
        "duplicates": duplicates,
        "statuses": len(statuses),
    }


# ── Instagram ───────────────────────────────────────────────────────────────
#
# Two routes, the same order of operations, a different parser and a different
# app secret. Deliberately not folded into the WhatsApp routes with a query
# parameter: the subscriptions are separate in Meta, the secrets are separate,
# and a single endpoint verifying against whichever secret happened to work
# would be an endpoint that verifies nothing.

@router.get("/instagram/webhook")
@limiter.limit("30/minute")
async def verify_instagram_webhook(
    request: Request,
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta's one-time handshake, echoed as text/plain exactly as sent."""
    try:
        challenge = ig.verify_challenge(
            mode=hub_mode,
            token=hub_verify_token,
            challenge=hub_challenge,
            expected_token=settings.instagram_verify_token,
        )
    except ValueError as exc:
        logger.warning("instagram webhook verification refused: %s", exc)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed") from exc

    return Response(content=challenge, media_type="text/plain")


@router.post("/instagram/webhook")
@limiter.limit("600/minute")
async def receive_instagram_webhook(request: Request):
    """
    Inbound Instagram messages, echoes and read receipts.

    Signature over the raw bytes, against the *Instagram* app secret. Verified
    before parsing, for the same reason as WhatsApp: anything that
    re-serialises the JSON first is checking a different document from the one
    Meta signed.
    """
    body = await request.body()

    if not ig.verify_signature(
        body=body,
        header=request.headers.get("X-Hub-Signature-256"),
        app_secret=creds_module.resolve("default", "instagram").app_secret,
    ):
        logger.warning("instagram webhook with a bad or missing signature")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signature")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        logger.warning("instagram webhook body was not JSON")
        return {"received": True, "processed": 0}

    return await handle_payload(payload, channel="instagram")
