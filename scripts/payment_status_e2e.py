#!/usr/bin/env python3
"""
The conversation that started this, replayed against a real database.

    Instagram DM ──► quote ──► payment link ──► Razorpay ──► signed webhook
                                                                  │
                          reservation ◄── _fulfil_booking ◄───────┘
                                │
                                ├──► conversations.reservation_id
                                └──► "Payment mil gaya ✅" to the guest

    then:  "Payment hogyi"  ──► get_booking_status ──► CONFIRMED

On 21 August a guest paid a live hotel ₹7,168 at 19:58:52. At 19:59:18 he asked
whether it had gone through. The agent — which had no way to look — told him his
booking would be confirmed once the payment processed; it had been confirmed for
twenty-six seconds, under reference TB-5CECKV. His next message escalated the
thread to a human who was not there, and that was the end of the conversation.

Nothing in the money path was broken. The order settled, the webhook fired, the
reservation was written, the credit was granted. What did not exist was any link
from that reservation back to the thread the guest was sitting in, and any tool
that could have followed one.

So this suite proves four things in order, and the order is the argument:

    1. a payment link raised in a conversation records the conversation
    2. the reservation the webhook creates records it too, and the thread
       learns about the booking
    3. the guest is told, unprompted, in the language they asked for
    4. "payment hogyi" is answered from the ledger — and the guardrail refuses
       the same sentence when the ledger does not back it

Real database, real PMS services, real Razorpay orders, real webhook signatures.
The model is scripted, for the same reason every other suite here scripts it: a
test that needs a live model to prove a payment was recorded is testing the
model.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python -m uvicorn app.main:app --port 8010 &
    ./venv/bin/python scripts/payment_status_e2e.py

⚠ Writes real rows into whatever project .env.hotelos points at. Disposable.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import e2e_sweep  # noqa: E402
os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")

import e2e_fixture  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.ai import context as ai_context  # noqa: E402
from app.services.ai import guardrails, provisioning, registry, runtime  # noqa: E402
from app.services.ai.providers import EchoProvider  # noqa: E402

REF = "rdlozqvuywtgsodunait"
# Which StayOS to test. Defaults to the uvicorn each suite's docstring tells
# you to start; set STAYOS_API_BASE=https://api.hostos.in to run
# the same assertions against the deployed one, which is the only way to find
# out whether what is live behaves like what is on this branch.
API = os.environ.get("STAYOS_API_BASE", "http://127.0.0.1:8010").rstrip("/") + "/api/v1/pms"

# The sales property, for the same reason sales_e2e uses it: its own partner,
# its own billing account, its own wallet. Nothing this suite books or spends
# can be seen by another suite.
PROPERTY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PARTNER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SEA = "10000000-0000-4000-8000-0000000000d1"
SEA_CP = "20000000-0000-4000-8000-0000000000d2"

registry.load_all()
db = get_db()

FIXTURE = e2e_fixture.Fixture(
    label="payment status",
    property_id=PROPERTY,
    partner_id=PARTNER,
    prefixes=("TB-PS",),
)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def run(coro):
    return asyncio.run(coro)


raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()


def sql(query: str):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        method="POST", data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MGMT}",
                 "User-Agent": "SupabaseCLI/2.0.0"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:400]}


def http(url, method="GET", data=None, headers=None, raw_body=None):
    body = raw_body if raw_body is not None else (
        json.dumps(data).encode() if data is not None else None
    )
    req = urllib.request.Request(url, method=method, data=body,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


def webhook_body(order_id: str, payment_id: str) -> tuple[bytes, str]:
    payload = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }).encode()
    signature = hmac.new(
        (settings.razorpay_webhook_secret or "").encode(), payload, hashlib.sha256,
    ).hexdigest()
    return payload, signature


# ── the hotel, the agent, the thread ─────────────────────────────────────────

print("══ payment status e2e ══\n")

status_code, _ = http(f"{API}/public/hotels/__ping__")
if status_code == 0:
    print("  API not running on :8010 — start uvicorn first")
    raise SystemExit(1)
if not (settings.razorpay_key_secret and settings.razorpay_webhook_secret):
    print("  no Razorpay keys in the environment")
    raise SystemExit(1)

FIXTURE.open()

print("\n── setup ──")

db.table("room_types").upsert([
    {"id": SEA, "property_id": PROPERTY, "partner_id": PARTNER,
     "code": "SEA", "name": "Sea View Double", "total_units": 10,
     "max_occupancy": 3, "standard_occupancy": 2, "bed_type": "1 King",
     "sort_order": 1, "is_active": True},
]).execute()
db.table("rate_plans").upsert([
    {"id": SEA_CP, "property_id": PROPERTY, "partner_id": PARTNER,
     "room_type_id": SEA, "code": "SEA-CP", "name": "Room + Breakfast",
     "meal_plan": "CP", "audience": "public", "base_price_paise": 600000,
     "refundable": True, "inclusions": ["Breakfast", "WiFi"],
     "sort_order": 2, "is_active": True},
]).execute()

run(provisioning.provision(property_id=PROPERTY, partner_id=PARTNER, agent_type="sales"))
agent_row = (
    db.table("agents").select("*").eq("property_id", PROPERTY)
    .eq("agent_type", "sales").limit(1).execute().data[0]
)
# **Borrowed, not taken.** `agents.autonomy_level` on the demo property is one
# row and six suites write to it; whichever ran last used to decide what the
# property was. An evaluation then measured a configuration an unrelated suite
# had left behind and reported it as a model regression. `borrow_autonomy`
# records the level it found and puts it back on exit, crash included.
# See scripts/e2e_sweep.py.
e2e_sweep.borrow_autonomy(db, PROPERTY, 3,
                          agent_type=agent_row["agent_type"])
db.table("agents").update({"status": "active"}).eq(
    "id", agent_row["id"]).execute()

granted = {
    t["tool_name"] for t in
    (db.table("agent_tools").select("tool_name").eq("agent_id", agent_row["id"])
     .eq("enabled", True).execute().data or [])
}
check("the selling agent can look up what the guest has actually paid",
      "get_booking_status" in granted, str(sorted(granted))[:200])

TAG = uuid.uuid4().hex[:8]
IGSID = f"igsid_ps_{TAG}"

CONVERSATION = db.table("conversations").insert({
    "property_id": PROPERTY, "partner_id": PARTNER, "channel": "instagram",
    "external_id": f"ig_thread_{TAG}", "guest_handle": IGSID,
    "guest_name": "Sahil", "handler": "ai", "status": "open",
    # A guest who wrote a moment ago, which is what a guest paying a link the
    # hotel just sent them is. Meta's 24-hour service window is real and this
    # suite must not pretend otherwise — see the note at the end of the file
    # about the payment that arrives after it closes.
    "last_inbound_at": datetime.now(timezone.utc).isoformat(),
    # The preference the real guest asked for and never got.
    "guest_profile": {"language": "hinglish"},
}).execute().data[0]

CHECK_IN = date.today() + timedelta(days=20)
CHECK_OUT = CHECK_IN + timedelta(days=4)


def ctx():
    return run(ai_context.load_context(
        property_id=PROPERTY, partner_id=PARTNER, agent_type="sales",
        conversation_id=CONVERSATION["id"], guest_name="Sahil",
        channel="instagram", actor="guest",
    ))


def turn(script: list, message: str):
    provider = EchoProvider(list(script))
    result = run(runtime.run_turn(ctx(), message, provider=provider, trigger="message"))
    rows = (db.table("agent_actions").select("*").eq("run_id", result.run_id)
            .order("seq").execute().data or [])
    for row, outcome in zip(rows, result.actions):
        if outcome.output is not None:
            row["output"] = outcome.output
    return result, rows


def conversation_row() -> dict:
    return (db.table("conversations").select("*")
            .eq("id", CONVERSATION["id"]).limit(1).execute().data or [{}])[0]


# ═════════════════════════════════════════════════════════════════════════════
# 1. before any money — what the agent is allowed to say
# ═════════════════════════════════════════════════════════════════════════════

print("\n── nothing has happened yet ──")

_, acts = turn([[("get_booking_status", {})], "Abhi tak koi booking nahi hai."],
               "meri booking check karo")
check("with no quote and no payment the state is NONE",
      acts[0]["output"]["state"] == "NONE", str(acts[0]["output"]))

CONFIRMATION = "Payment mil gaya, aapki booking confirm ho gayi hai."
verdict = guardrails.check_reply(
    CONFIRMATION, tool_outputs=[], trusted_hosts=set(),
)
check("and the sentence 'booking confirm ho gayi' is refused outright",
      not verdict.ok, verdict.reason)


# ═════════════════════════════════════════════════════════════════════════════
# 2. the quote, and the payment link that names its thread
# ═════════════════════════════════════════════════════════════════════════════

print("\n── the sale ──")

_, acts = turn(
    [[("get_quote", {"room_type_id": SEA, "rate_plan_id": SEA_CP,
                     "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
                     "units": 2, "adults": 4, "children": 0, "discount_pct": 0})],
     "Do rooms ke liye price ready hai."],
    "10 tarikh se 4 din, 4 log, 2 room",
)
check("the quote executed", acts[0]["status"] == "executed",
      acts[0].get("refusal_reason") or "")
QUOTE = acts[0]["output"]

quote_row = sql(f"""select conversation_id from public.quotes
                    where id = '{QUOTE['quoteId']}';""")
check("the quote records the conversation it was made in",
      isinstance(quote_row, list) and quote_row
      and quote_row[0]["conversation_id"] == CONVERSATION["id"], str(quote_row))

_, acts = turn(
    [[("create_booking_payment_link", {"quote_id": QUOTE["quoteId"],
                                       "guest_name": "Sahil"})],
     "Yeh link par pay kar dijiye."],
    "haan book kardo",
)
check("the payment link executed", acts[0]["status"] == "executed",
      acts[0].get("refusal_reason") or "")
LINK = acts[0]["output"]

order = sql(f"""select id, razorpay_order_id, conversation_id, status, amount_paise
                from public.payment_orders
                where quote_id = '{QUOTE['quoteId']}' and purpose = 'booking';""")
check("one order was raised", isinstance(order, list) and len(order) == 1, str(order))
ORDER = order[0]
# The column that did not exist, and the whole reason the guest was told nothing.
check("the order records the conversation it was raised from",
      ORDER["conversation_id"] == CONVERSATION["id"], str(ORDER["conversation_id"]))

_, acts = turn([[("get_booking_status", {})],
                "Abhi tak payment nahi aayi, aate hi bata dunga."],
               "payment hogyi")
check("before the money the state is PAYMENT_PENDING",
      acts[0]["output"]["state"] == "PAYMENT_PENDING", str(acts[0]["output"]))
verdict = guardrails.check_reply(
    CONFIRMATION,
    tool_outputs=[type("O", (), {"tool": "get_booking_status", "ok": True,
                                 "output": acts[0]["output"]})()],
    trusted_hosts=set(),
)
check("and a pending payment still does not license 'booking confirm ho gayi'",
      not verdict.ok, verdict.reason)


# ═════════════════════════════════════════════════════════════════════════════
# 3. the webhook
# ═════════════════════════════════════════════════════════════════════════════

print("\n── the money arrives ──")

payment_id = f"pay_PS{TAG}"
body, signature = webhook_body(ORDER["razorpay_order_id"], payment_id)
st, hook = http(f"{API}/public/razorpay/webhook", "POST", raw_body=body,
                headers={"X-Razorpay-Signature": signature})
check("the signed webhook settles the order", st == 200, f"{st} {hook}")

stay = sql(f"""select id, ref, status, payment_status, conversation_id, guest_id,
                      total_paise, paid_paise
               from public.reservations
               where quote_id = '{QUOTE['quoteId']}';""")
check("a reservation exists", isinstance(stay, list) and len(stay) == 1, str(stay))
STAY = stay[0]
# Handed to the fixture the moment it exists.
#
# This stay was named by `_fulfil_booking`, not by this suite — a random
# `TB-XXXXXX` like any live booking, which is exactly what makes testing the
# real payment path worth doing and exactly why `prefixes` could never find it
# afterwards. Two of ten rooms went every run and never came back; after five
# runs the quote 409'd and it read like a pricing bug.
FIXTURE.adopt(STAY["ref"])
check("it is confirmed", STAY["status"] == "confirmed", str(STAY["status"]))
check("the reservation records the conversation that sold it",
      STAY["conversation_id"] == CONVERSATION["id"], str(STAY["conversation_id"]))

conv = conversation_row()
check("and the thread now points at the booking",
      conv.get("reservation_id") == STAY["id"], str(conv.get("reservation_id")))
check("and at the guest record the payment created",
      conv.get("guest_id") == STAY["guest_id"], str(conv.get("guest_id")))


# ═════════════════════════════════════════════════════════════════════════════
# 4. the guest is told, without asking
# ═════════════════════════════════════════════════════════════════════════════

print("\n── the confirmation nobody had to ask for ──")

outbound = (
    db.table("conversation_messages").select("*")
    .eq("conversation_id", CONVERSATION["id"]).eq("direction", "outbound")
    .order("created_at", desc=True).limit(5).execute().data or []
)
confirmations = [m for m in outbound if (m.get("client_reference") or "")
                 == f"booking:{STAY['id']}"]
check("a confirmation was sent", len(confirmations) == 1, str(len(confirmations)))

if confirmations:
    body_text = confirmations[0].get("body") or ""
    check("it names the booking reference", STAY["ref"] in body_text, body_text[:120])
    check("it is in the language the guest asked for",
          "Aapki booking confirm ho gayi hai" in body_text, body_text[:160])
    check("and not in Devanagari, which is what the live agent kept reverting to",
          not any("ऀ" <= ch <= "ॿ" for ch in body_text), body_text[:120])
    # `/b/{token}`, not `/checkin/{ref}` — this assertion was written in Phase 1
    # and Phase 6.1 moved the confirmation link without updating it, so it has
    # been failing ever since while the plan recorded the suite as green.
    #
    # Asserted as the security property rather than as a path, which is the
    # point of the change: `ref` is six characters from a 32-symbol alphabet
    # built to be read aloud over a phone, and a link that opens a stranger's
    # booking must not be guessable. A confirmation that carried the reference
    # in its URL would be one forwarded email away from being enumerable.
    check("it carries a link to the booking",
          "/b/" in body_text and "/checkin/" not in body_text, body_text[:200])
    check("and the link is a secret rather than the reference",
          STAY["ref"] not in body_text.split("/b/")[-1], body_text[:200])

# The webhook can be delivered again. It must not produce a second message.
st, _ = http(f"{API}/public/razorpay/webhook", "POST", raw_body=body,
             headers={"X-Razorpay-Signature": signature})
again = (
    db.table("conversation_messages").select("id")
    .eq("conversation_id", CONVERSATION["id"])
    .eq("client_reference", f"booking:{STAY['id']}").execute().data or []
)
check("a redelivered webhook does not send a second confirmation",
      len(again) == 1, str(len(again)))


# ═════════════════════════════════════════════════════════════════════════════
# 5. "payment hogyi" — the message that broke the real conversation
# ═════════════════════════════════════════════════════════════════════════════

print("\n── the question, answered from the ledger ──")

reply = f"Payment mil gaya, aapki booking confirm ho gayi hai — {STAY['ref']}"
result, acts = turn([[("get_booking_status", {})], reply], "payment hogyi")

check("the agent looked it up", acts and acts[0]["tool"] == "get_booking_status",
      str([a["tool"] for a in acts]))
check("and found a confirmed booking",
      acts[0]["output"]["state"] == "CONFIRMED", str(acts[0]["output"])[:200])
check("it reports the reference the guest can quote at the desk",
      acts[0]["output"]["ref"] == STAY["ref"], str(acts[0]["output"].get("ref")))
check("and the balance still due at the hotel",
      acts[0]["output"]["balancePaise"]
      == int(STAY["total_paise"]) - int(STAY["paid_paise"]),
      str(acts[0]["output"].get("balancePaise")))

# The assertion this whole suite exists for. On 21 August this exact sentence
# was refused by the guardrail and the conversation went to a human.
check("the guardrail now lets the agent say it, because a tool proved it",
      not result.handoff, result.error or "")
check("the run succeeded rather than escalating",
      result.status == "succeeded", f"{result.status} {result.error}")
check("and the reply reached the guest unchanged", result.reply == reply,
      (result.reply or "")[:120])


# ── sweep ────────────────────────────────────────────────────────────────────

print()
sql(f"""
delete from public.conversation_messages where conversation_id = '{CONVERSATION['id']}';
delete from public.agent_actions where run_id in (
  select id from public.agent_runs where conversation_id = '{CONVERSATION['id']}');
delete from public.agent_runs where conversation_id = '{CONVERSATION['id']}';
update public.reservations set conversation_id = null
  where id = '{STAY['id']}';
update public.conversations set reservation_id = null where id = '{CONVERSATION['id']}';
delete from public.conversations where id = '{CONVERSATION['id']}';
""")
# The room is released by `FIXTURE.close()` below, which goes through
# `sweep_reservations` and therefore through `release_booking`. This sweep
# deliberately no longer sets `status = 'cancelled'` itself: that UPDATE
# bypasses the inventory RPCs, and a row already marked dead is one the fixture
# would have released and then deleted anyway. See scripts/e2e_sweep.py.
print(f"  · swept the {TAG} thread; the fixture releases its room")

FIXTURE.close()

print(f"\n╔═ {PASS} passed, {FAIL} failed")
print("╚═ REAL DATABASE · REAL PMS · REAL RAZORPAY ORDER · SIGNED WEBHOOK · SCRIPTED MODEL")

# ── the case this suite deliberately does not assert ─────────────────────────
#
# A payment that settles *after* Meta's 24-hour service window has closed
# cannot be announced on Instagram at all: there is no template mechanism on
# the channel and free text outside the window is refused by Meta, not by us.
# `announce_booking` logs MESSAGE_WINDOW_CLOSED and gives up, which is the only
# honest behaviour available — and the guest is not stranded, because
# `get_booking_status` answers the moment they write again. WhatsApp has the
# approved-template road and takes it.
sys.exit(1 if FAIL else 0)
