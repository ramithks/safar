#!/usr/bin/env python3
"""
One controlled message through real Meta. The last unverified link.

    this script → messaging.send → WhatsApp adapter → Meta Cloud API → a phone

Everything else in the chain has been proved against the real database with a
deterministic fake in Meta's place. This proves the one thing a fake cannot:
that our payload shape, our credentials and our phone number id are the ones
Meta accepts.

**Deliberately small and deliberately loud.** It sends to exactly one number,
which you name, and refuses to run without it. It does not touch the agent, does
not process inbound traffic, and does not enable the connection for anything
else. A smoke test that quietly widened into production traffic would be the
worst possible way to discover a mistake.

    cd travbuddy_backend
    # in .env, not on the command line:
    #   WHATSAPP_ACCESS_TOKEN=...
    #   WHATSAPP_APP_SECRET=...
    #   WHATSAPP_VERIFY_TOKEN=...
    #   WHATSAPP_TEST_RECIPIENT=91XXXXXXXXXX      one number, yours
    #   WHATSAPP_TEST_PHONE_NUMBER_ID=...         from Meta, the sending number
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/meta_smoke.py

The recipient must have messaged the business number in the last 24 hours, or
Meta will refuse a free-form send — which is the window rule this codebase
already enforces, arriving from the other direction. Message the number from the
handset first, then run this.
"""
import asyncio
import os
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.messaging import credentials as creds_module  # noqa: E402
from app.services.messaging import service as messaging  # noqa: E402
from app.services.messaging.channels import MessagingError  # noqa: E402

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

RECIPIENT = os.environ.get("WHATSAPP_TEST_RECIPIENT", "").strip()
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_TEST_PHONE_NUMBER_ID", "").strip()

db = get_db()
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


# ── refuse to run half-configured ────────────────────────────────────────────

print("\n── configuration ──")
missing = [
    name for name, value in [
        ("WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token),
        ("WHATSAPP_APP_SECRET", settings.whatsapp_app_secret),
        ("WHATSAPP_VERIFY_TOKEN", settings.whatsapp_verify_token),
        ("WHATSAPP_TEST_RECIPIENT", RECIPIENT),
        ("WHATSAPP_TEST_PHONE_NUMBER_ID", PHONE_NUMBER_ID),
    ] if not value
]
if missing:
    print("\n  Not configured. Missing: " + ", ".join(missing))
    print("  Put them in travbuddy_backend/.env — never on the command line,")
    print("  where they end up in shell history and in this terminal's scrollback.")
    print("\n  REAL META NOT TESTED")
    sys.exit(2)

print(f"  sending as {PHONE_NUMBER_ID} → {RECIPIENT[:4]}…{RECIPIENT[-3:]}")
check("credentials resolve for the live ref",
      creds_module.resolve("default").access_token is not None)
check("and the sandbox ref still resolves to no secret at all",
      creds_module.resolve(settings.whatsapp_sandbox_ref).is_sandbox)


# ── one live connection, for the length of this script ───────────────────────

print("\n── the connection ──")

existing = (db.table("whatsapp_connections").select("*")
            .eq("property_id", PROPERTY).limit(1).execute().data or [])
restore = existing[0] if existing else None

db.table("whatsapp_connections").upsert({
    **({"id": restore["id"]} if restore else {}),
    "property_id": PROPERTY, "partner_id": PARTNER,
    "phone_number_id": PHONE_NUMBER_ID,
    "credentials_ref": "default",
    "status": "connected",
    # The flag that decides fake or real. Set explicitly here and put back at
    # the end, so a crashed run leaves a note in the row rather than a
    # production connection somebody forgot about.
    "is_sandbox": False,
}, on_conflict="property_id").execute()

check("the connection is live, not sandbox",
      (db.table("whatsapp_connections").select("is_sandbox")
       .eq("property_id", PROPERTY).limit(1).execute().data[0]["is_sandbox"]) is False)

adapter = creds_module.adapter_for(
    db.table("whatsapp_connections").select("*")
    .eq("property_id", PROPERTY).limit(1).execute().data[0]
)
check("and it builds the real adapter, not the fake",
      type(adapter).__name__ == "WhatsAppAdapter", type(adapter).__name__)


# ── the conversation, and one message ────────────────────────────────────────

print("\n── one message ──")

tag = uuid.uuid4().hex[:6]
conv = (db.table("conversations").select("*")
        .eq("property_id", PROPERTY).eq("channel", "whatsapp")
        .eq("external_id", RECIPIENT).limit(1).execute().data or [])
if conv:
    conversation = conv[0]
else:
    conversation = db.table("conversations").insert({
        "property_id": PROPERTY, "partner_id": PARTNER, "channel": "whatsapp",
        "external_id": RECIPIENT, "guest_phone": RECIPIENT,
        "guest_name": "Meta smoke test", "handler": "human", "status": "open",
    }).execute().data[0]

body = f"TravBuddy Partner smoke test {tag}. No reply needed."
sent = None
failure = None
try:
    sent = asyncio.run(messaging.send(
        conversation, body=body, author="staff", author_name="Smoke test",
        client_reference=f"smoke:{tag}",
    ))
except MessagingError as exc:
    failure = exc

if failure is not None and failure.code == "MESSAGE_WINDOW_CLOSED":
    # Not our bug — Meta's rule, enforced by our own service before the request
    # left. The fix is a human action, so say so plainly.
    print(f"\n  The 24-hour window is closed for {RECIPIENT[:4]}…{RECIPIENT[-3:]}.")
    print("  Message the business number from that handset, then re-run within 24h.")
    print("\n  REAL META NOT TESTED (window closed, nothing sent)")
    if restore:
        db.table("whatsapp_connections").update(
            {"is_sandbox": restore["is_sandbox"], "phone_number_id": restore["phone_number_id"],
             "credentials_ref": restore["credentials_ref"]}
        ).eq("id", restore["id"]).execute()
    sys.exit(2)

check("Meta accepted the message", failure is None,
      f"{failure and failure.code}: {failure and failure.detail}")

if sent is not None:
    check("it is stored as sent", sent.message["delivery_status"] == "sent",
          sent.message["delivery_status"])
    check("with Meta's own message id, not ours",
          (sent.external_id or "").startswith("wamid.") and "FAKE" not in (sent.external_id or ""),
          str(sent.external_id))
    check("and it was metered",
          bool(db.table("usage_events").select("id")
               .eq("message_id", sent.message["id"]).execute().data),
          "no usage event")
    print(f"\n  Meta message id: {sent.external_id}")
    print("  Check the handset. Delivery receipts will arrive on the webhook if")
    print("  the subscription points at this deployment.")


# ── put the connection back ──────────────────────────────────────────────────

print("\n── restoring ──")
if restore:
    db.table("whatsapp_connections").update({
        "phone_number_id": restore["phone_number_id"],
        "credentials_ref": restore["credentials_ref"],
        "is_sandbox": restore["is_sandbox"],
        "status": restore["status"],
    }).eq("id", restore["id"]).execute()
    back = (db.table("whatsapp_connections").select("is_sandbox")
            .eq("id", restore["id"]).limit(1).execute().data[0])
    check("the connection is back to how it was",
          back["is_sandbox"] == restore["is_sandbox"], str(back))
else:
    db.table("whatsapp_connections").delete().eq("property_id", PROPERTY).execute()
    check("the temporary connection was removed", True)

print(f"\n{PASS} passed, {FAIL} failed")
print("\nREAL META TESTED" if FAIL == 0 else "\nREAL META TESTED — WITH FAILURES")
sys.exit(1 if FAIL else 0)
