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


async def handle_payload(payload: dict, *, provider=None) -> dict:
    """
    The part after the signature.

    Separated so the E2E suite can drive the same code path it would drive over
    HTTP without having to forge a signature for every case — the signature
    itself gets its own tests, positive and negative. `provider` rides along for
    the same reason it does in the runtime: the suite scripts the model and
    asserts on the database.
    """
    messages, statuses = wa.parse(payload)
    accepted = duplicates = 0
    touched: set[str] = set()

    for inbound in messages:
        try:
            connection = await messaging.resolve_connection(inbound.account_id)
        except MessagingError as exc:
            # A number nobody has connected. Logged and acknowledged: we cannot
            # say whose guest this is, and a redelivery will not change that.
            logger.warning("inbound whatsapp for unknown number: %s", exc.detail)
            continue

        recorded = await messaging.record_inbound(inbound, connection)
        if recorded.duplicate:
            # The whole reason persistence comes first. No second conversation,
            # no second run, no second ticket, no second reply.
            duplicates += 1
            continue

        accepted += 1
        touched.add(recorded.conversation["id"])

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
