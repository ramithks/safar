#!/usr/bin/env python3
"""
The real Meta boundary, one controlled number at a time.

    this script → messaging.send → WhatsApp adapter → Meta Cloud API → a phone
    a phone → Meta → webhook → Celery → Gemini → PMS tool → reply → Meta

Everything else in the chain is proved against the real database with a
deterministic fake in Meta's place. This proves the two things a fake cannot:
that Meta accepts our payload and our credentials, and that Meta's own webhooks
drive the pipeline end to end.

**One recipient, and no way to pass a list.** The number comes from
`settings.whatsapp_test_recipient` and nowhere else — not a CLI argument, not a
loop, not a file. A smoke test that could quietly widen into production traffic
would be the worst possible way to discover a mistake.

**Nothing is printed that should not be.** Tokens and secrets are never echoed;
the recipient is masked; only Meta's message ids, which are not secret and are
the whole point of the trace, appear in full.

    cd travbuddy_backend
    # in .env, never on the command line:
    #   WHATSAPP_ACCESS_TOKEN / WHATSAPP_APP_SECRET / WHATSAPP_VERIFY_TOKEN
    #   WHATSAPP_TEST_RECIPIENT / WHATSAPP_TEST_PHONE_NUMBER_ID
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/meta_smoke.py

Two things must be true at Meta before this can pass, and neither is something
this script can do for you:

  1. The recipient is on the test number's allowed list
     (Meta → WhatsApp → API Setup → "To"). Error 131030 says it is not.
  2. The webhook points at a URL Meta can reach, with WHATSAPP_VERIFY_TOKEN.
     Without it the inbound half cannot run and is reported as such.

Run with `--outbound-only` to skip the inbound wait when no webhook is wired.
"""
import asyncio
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.messaging import credentials as creds_module  # noqa: E402
from app.services.messaging import service as messaging  # noqa: E402
from app.services.messaging.channels import MessagingError  # noqa: E402

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

# Through settings, not os.environ. `.env` is read by pydantic-settings and
# never exported to the process environment — reading it directly found an empty
# string and refused to run with the credentials sitting right there.
RECIPIENT = (settings.whatsapp_test_recipient or "").strip()
PHONE_NUMBER_ID = (settings.whatsapp_test_phone_number_id or "").strip()

OUTBOUND_ONLY = "--outbound-only" in sys.argv
INBOUND_WAIT_SECONDS = 120

db = get_db()
PASS = FAIL = 0
BLOCKED: list[str] = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def blocked(label, why):
    BLOCKED.append(f"{label}: {why}")
    print(f"  --   {label}  BLOCKED — {why}")


def mask(number: str) -> str:
    return f"{number[:4]}…{number[-3:]}" if len(number) > 7 else "…"


def run(coro):
    return asyncio.run(coro)


# ── 1. configuration ─────────────────────────────────────────────────────────

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
    # Loudly, and all at once. Half-configured is the state that wastes the most
    # time, because each run reveals one more missing thing.
    print("\n  Not configured. Missing: " + ", ".join(missing))
    print("  Put them in travbuddy_backend/.env — never on the command line,")
    print("  where they end up in shell history and this terminal's scrollback.")
    print("\n  REAL META NOT TESTED")
    sys.exit(2)

print(f"  sending as {PHONE_NUMBER_ID} → {mask(RECIPIENT)}")
check("every credential is present", True)
check("the live ref resolves a token",
      creds_module.resolve("default").access_token is not None)
check("the sandbox ref still resolves to no secret at all",
      creds_module.resolve(settings.whatsapp_sandbox_ref).is_sandbox)


# ── 2. Meta connectivity ─────────────────────────────────────────────────────

print("\n── Meta ──")


async def _get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=30) as c:
        return await c.get(
            f"{settings.whatsapp_api_base}/{path}",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            params=params or {},
        )


probe = run(_get(PHONE_NUMBER_ID, {"fields": "id,display_phone_number,verified_name,quality_rating"}))
check("Meta answers for this phone number id", probe.status_code == 200,
      f"{probe.status_code} {probe.text[:160]}")
if probe.status_code != 200:
    print("\n  Cannot continue without a working number. REAL META NOT TESTED")
    sys.exit(1)

number = probe.json()
print(f"  {number.get('display_phone_number')}  “{number.get('verified_name')}”  "
      f"quality={number.get('quality_rating')}")
check("the token and the number belong together", number.get("id") == PHONE_NUMBER_ID)


# ── 3. the connection row ────────────────────────────────────────────────────

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
    # The flag that decides fake or real. Set explicitly, restored at the end.
    "is_sandbox": False,
}, on_conflict="property_id").execute()

connection = (db.table("whatsapp_connections").select("*")
              .eq("property_id", PROPERTY).limit(1).execute().data[0])
check("the connection is live, not sandbox", connection["is_sandbox"] is False)
check("and it builds the real adapter",
      type(creds_module.adapter_for(connection)).__name__ == "WhatsAppAdapter")

resolved = run(messaging.resolve_connection(PHONE_NUMBER_ID))
check("the phone number id resolves to exactly one property",
      resolved["property_id"] == PROPERTY, str(resolved.get("property_id")))


def restore_connection():
    if restore:
        db.table("whatsapp_connections").update({
            "phone_number_id": restore["phone_number_id"],
            "credentials_ref": restore["credentials_ref"],
            "is_sandbox": restore["is_sandbox"],
            "status": restore["status"],
        }).eq("id", restore["id"]).execute()
    else:
        db.table("whatsapp_connections").delete().eq("property_id", PROPERTY).execute()


# ── 4. the conversation ──────────────────────────────────────────────────────

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

window_open = messaging.window_open(conversation)
print(f"\n── outbound ── (service window {'open' if window_open else 'closed'})")


# ── 5. outbound ──────────────────────────────────────────────────────────────

tag = uuid.uuid4().hex[:6]
sent = None
failure = None

if window_open:
    # Free text, which is what the agent sends on a live conversation.
    try:
        sent = run(messaging.send(
            conversation, body=f"TravBuddy Partner smoke test {tag}. No reply needed.",
            author="staff", author_name="Smoke test",
            client_reference=f"smoke:{tag}",
        ))
    except MessagingError as exc:
        failure = exc
else:
    # Outside the window our own service refuses free text before the request
    # leaves — which is the rule working, not a failure. Prove that, then reach
    # Meta with the one thing that is permitted out here: an approved template.
    refused = None
    try:
        run(messaging.send(conversation, body="this must not go out",
                           client_reference=f"blocked:{tag}"))
    except MessagingError as exc:
        refused = exc
    check("free text outside the window is refused by our own service",
          refused is not None and refused.code == "MESSAGE_WINDOW_CLOSED",
          str(refused and refused.code))

    # `hello_world` is the template every Meta test app ships with. Registered
    # here as a real (non-test) template so the send goes through the same
    # `message_templates` path a hotel's own template would.
    db.table("message_templates").upsert({
        "property_id": PROPERTY, "partner_id": PARTNER, "channel": "whatsapp",
        "purpose": "reengagement", "provider_name": "hello_world",
        "language": "en_US", "category": "utility", "status": "approved",
        "parameters": [], "body_preview": "Hello World",
        "is_test": False, "approved_at": "now()",
    }, on_conflict="property_id,channel,provider_name,language").execute()

    try:
        sent = run(messaging.send(conversation, purpose="reengagement",
                                  template_values={}, language="en_US",
                                  author="staff", author_name="Smoke test",
                                  client_reference=f"smoke:{tag}"))
    except MessagingError as exc:
        failure = exc

if failure is not None:
    # A refusal from Meta itself. The adapter turns Meta's numeric codes into
    # something actionable — see _META_ERRORS — so print it and stop honestly.
    check("Meta accepted the message", False, f"{failure.code}: {failure.detail}")
    print(f"\n  Meta refused: {failure.code}")
    print(f"  {failure.detail}")
    stored = (db.table("conversation_messages").select("delivery_status,error_code")
              .eq("property_id", PROPERTY).eq("client_reference", f"smoke:{tag}")
              .limit(1).execute().data or [])
    check("and the intent survives as a failed row a human can see",
          stored and stored[0]["delivery_status"] == "failed", str(stored))
    blocked("outbound", f"{failure.code} — {failure.detail[:120]}")
    restore_connection()
    print(f"\n{PASS} passed, {FAIL} failed")
    print("\nREAL META PARTIALLY TESTED — connectivity yes, delivery no")
    for b in BLOCKED:
        print(f"  BLOCKED  {b}")
    sys.exit(1)

check("Meta accepted the message", sent is not None)
check("stored as sent", sent.message["delivery_status"] == "sent",
      sent.message["delivery_status"])
check("with Meta's own message id, not the fake's",
      (sent.external_id or "").startswith("wamid.") and "FAKE" not in (sent.external_id or ""),
      str(sent.external_id))
check("and it was metered",
      bool(db.table("usage_events").select("id")
           .eq("message_id", sent.message["id"]).execute().data))

# Idempotency, against the real provider.
again = run(messaging.send(
    conversation,
    body=f"TravBuddy Partner smoke test {tag}. No reply needed." if window_open else None,
    purpose=None if window_open else "reengagement",
    template_values=None if window_open else {}, language="en_US",
    author="staff", client_reference=f"smoke:{tag}",
))
check("a retry with the same client_reference does not send twice",
      again.duplicate is True and again.message["id"] == sent.message["id"],
      f"duplicate={again.duplicate}")

print(f"\n  outbound Meta message id: {sent.external_id}")
print(f"  check the handset for {mask(RECIPIENT)}")


# ── 6. delivery status ───────────────────────────────────────────────────────

print("\n── delivery status ──")
print("  waiting up to 45s for Meta's callbacks…")

deadline = time.monotonic() + 45
final = None
while time.monotonic() < deadline:
    final = (db.table("conversation_messages").select("*")
             .eq("id", sent.message["id"]).limit(1).execute().data[0])
    if final["delivery_status"] in ("delivered", "read", "failed"):
        break
    time.sleep(3)

history = ((final or {}).get("metadata") or {}).get("statuses") or []
billing = ((final or {}).get("metadata") or {}).get("providerBilling") or {}

if final and final["delivery_status"] != "sent":
    check("Meta reported a delivery status", True)
    print(f"  status: {final['delivery_status']}  history: "
          f"{[h.get('status') for h in history]}")
    if billing:
        print(f"  billing: {billing}")
        check("and Meta's billing dimensions were captured", bool(billing))
else:
    blocked("delivery status",
            "no callback arrived — the webhook is not pointed at this deployment")


# ── 7. inbound ───────────────────────────────────────────────────────────────

print("\n── inbound ──")

if OUTBOUND_ONLY:
    blocked("inbound", "--outbound-only was passed")
else:
    before = len(db.table("conversation_messages").select("id")
                 .eq("conversation_id", conversation["id"])
                 .eq("direction", "inbound").execute().data or [])
    print(f"  Reply from {mask(RECIPIENT)} now — try: "
          "\"do you have a room tomorrow for two?\"")
    print(f"  waiting up to {INBOUND_WAIT_SECONDS}s…")

    deadline = time.monotonic() + INBOUND_WAIT_SECONDS
    arrived = None
    while time.monotonic() < deadline:
        rows = (db.table("conversation_messages").select("*")
                .eq("conversation_id", conversation["id"]).eq("direction", "inbound")
                .order("sent_at", desc=True).limit(1).execute().data or [])
        if rows and len(rows) and (len(db.table("conversation_messages").select("id")
                                   .eq("conversation_id", conversation["id"])
                                   .eq("direction", "inbound").execute().data or [])) > before:
            arrived = rows[0]
            break
        time.sleep(3)

    if arrived is None:
        blocked("inbound",
                "no webhook arrived — Meta cannot reach this deployment. "
                "Point the callback URL at a public address and subscribe to "
                "the `messages` field")
    else:
        check("a real inbound message was persisted", True)
        check("with Meta's own message id",
              (arrived["external_id"] or "").startswith("wamid."), str(arrived["external_id"]))
        check("attributed to this property, from the connection",
              arrived["property_id"] == PROPERTY)
        print(f"  inbound Meta message id: {arrived['external_id']}")

        # The agent should now answer it, through Celery.
        print("  waiting up to 90s for the worker and Gemini…")
        deadline = time.monotonic() + 90
        run_row = None
        while time.monotonic() < deadline:
            rows = (db.table("agent_runs").select("*")
                    .eq("conversation_id", conversation["id"])
                    .order("started_at", desc=True).limit(1).execute().data or [])
            if rows and rows[0].get("finished_at"):
                run_row = rows[0]
                break
            time.sleep(3)

        if run_row is None:
            blocked("agent run", "no run finished — is the Celery worker running?")
        else:
            check("a real agent run processed it", True)
            check("on Vertex", run_row["provider"] == "vertex_ai", str(run_row["provider"]))
            check("with a real model", bool(run_row["model"]), str(run_row["model"]))
            check("and real token counts", (run_row["tokens_in"] or 0) > 0)
            actions = (db.table("agent_actions").select("*")
                       .eq("run_id", run_row["id"]).order("seq").execute().data or [])
            print(f"  model: {run_row['model']}  tools: {[a['tool'] for a in actions]}")
            check("it called at least one PMS tool", bool(actions), "no actions")

            reply = (db.table("conversation_messages").select("*")
                     .eq("conversation_id", conversation["id"]).eq("direction", "outbound")
                     .order("sent_at", desc=True).limit(1).execute().data or [])
            if reply and reply[0]["external_id"] and reply[0]["id"] != sent.message["id"]:
                check("and the answer went back out through real Meta", True)
                print(f"  reply Meta message id: {reply[0]['external_id']}")
            else:
                blocked("outbound reply", "the agent's reply did not reach Meta")

            # Duplicate webhook, with a real payload.
            print("\n── duplicate webhook ──")
            raw = (arrived.get("metadata") or {})
            from app.api.v1.messaging import webhooks

            replay = {
                "object": "whatsapp_business_account",
                "entry": [{"id": "0", "changes": [{"field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                    "messages": [{"from": RECIPIENT, "id": arrived["external_id"],
                                  "timestamp": str(int(time.time())), "type": "text",
                                  "text": {"body": arrived.get("body") or ""}}],
                }}]}],
            }
            runs_before = len(db.table("agent_runs").select("id")
                              .eq("conversation_id", conversation["id"]).execute().data or [])
            result = run(webhooks.handle_payload(replay))
            check("Meta's own message id is recognised as a duplicate",
                  result["duplicates"] == 1 and result["accepted"] == 0, str(result))
            time.sleep(4)
            runs_after = len(db.table("agent_runs").select("id")
                             .eq("conversation_id", conversation["id"]).execute().data or [])
            check("and no second agent run was created", runs_before == runs_after,
                  f"{runs_before} → {runs_after}")


# ── 8. the trace ─────────────────────────────────────────────────────────────

print("\n── trace ──")
print(f"  conversation_id: {conversation['id']}")
runs = (db.table("agent_runs").select("id,model,provider")
        .eq("conversation_id", conversation["id"])
        .order("started_at", desc=True).limit(1).execute().data or [])
if runs:
    print(f"  agent_run_id:    {runs[0]['id']}  ({runs[0]['provider']} / {runs[0]['model']})")
    acts = (db.table("agent_actions").select("id,tool")
            .eq("run_id", runs[0]["id"]).order("seq").execute().data or [])
    for a in acts:
        print(f"  agent_action:    {a['id']}  {a['tool']}")
    ue = (db.table("usage_events").select("id,usage_type")
          .eq("run_id", runs[0]["id"]).execute().data or [])
    for u in ue:
        print(f"  usage_event:     {u['id']}  {u['usage_type']}")
if sent:
    print(f"  outbound wamid:  {sent.external_id}")


# ── restore ──────────────────────────────────────────────────────────────────

print("\n── restoring ──")
restore_connection()
back = (db.table("whatsapp_connections").select("is_sandbox")
        .eq("property_id", PROPERTY).limit(1).execute().data or [])
check("the connection is back to how it was",
      (not restore and not back) or (restore and back and back[0]["is_sandbox"] == restore["is_sandbox"]),
      str(back))

print(f"\n{PASS} passed, {FAIL} failed")
for b in BLOCKED:
    print(f"  BLOCKED  {b}")
print("\nREAL META TESTED" if (FAIL == 0 and not BLOCKED)
      else "\nREAL META PARTIALLY TESTED")
sys.exit(1 if FAIL else 0)
