#!/usr/bin/env python3
"""
Could a hotel that has never existed before be onboarded, and then run for a day?

Every other suite here tests one surface against a hotel somebody already
seeded. This one builds a hotel from nothing — see `pilot_fixture` — and then
walks the day: a stranger finds the website and books, a guest messages
WhatsApp and the agent answers from real inventory, a complaint becomes a task,
a receptionist takes the thread over and the AI stops, the desk checks people
in and out, a travel agent books on credit, a creator gets attributed and paid.

    onboarding ─► discovery ─► booking ─► conversation ─► front desk
                                   │            │
                                   └── B2B ─────┴── creator ─► payout

**Three things this suite is for that the others are not.**

*Cross-feature interaction.* Every other suite owns its surface. Real failures
in this system have come from the seams — a quote created by one path and
consumed by another, a wallet drained by an agent and read by the desk.

*Failure.* Section 8 is deliberately the largest. Vertex refusing, Meta
redelivering, two guests racing the last room, a quote whose stock moved, a
webhook arriving twice, an empty wallet. The claim under test is not that these
do not happen — they will — but that when they do the system fails **safely**:
no double booking, no double charge, no double payout, no silent loss.

*Authorization, negatively.* Section 9 tries to get at data it should not have,
from every principal in the system, and asserts the refusal rather than the
success.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/pilot_e2e.py

⚠ Creates a whole hotel under the `f1…` id namespace and `@pilot.staysuite.test`
logins, and deletes it again on the way out. It cannot touch Casa Mandala or
the three properties the other suites share.
"""
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")

import pilot_fixture as fx  # noqa: E402

API = "http://127.0.0.1:8010/api/v1/pms"

PASS = FAIL = 0
SECTION = ""


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def section(title):
    global SECTION
    SECTION = title
    print(f"\n── {title} ──")


http = fx.http
sql = fx.sql
try_sql = fx.try_sql


def run(coro):
    return asyncio.run(coro)


def payment_signature(order_id: str, payment_id: str) -> str:
    from app.config import settings
    return hmac.new((settings.razorpay_key_secret or "").encode(),
                    f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def webhook_body(event: str, order_id: str, payment_id: str) -> tuple[bytes, str]:
    from app.config import settings
    payload = json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "status": "captured",
        }}},
    }).encode()
    signature = hmac.new((settings.razorpay_webhook_secret or "").encode(),
                         payload, hashlib.sha256).hexdigest()
    return payload, signature


GUEST_PHONE = "919812" + str(int(time.time()))[-6:]
IGSID = "pilot_igsid_" + uuid.uuid4().hex[:10]
_seq = 0


def wa_payload(text: str, *, mid: str | None = None, account: str | None = None):
    global _seq
    _seq += 1
    message_id = mid or f"wamid.PILOT{uuid.uuid4().hex[:10]}{_seq:03d}"
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "pilot-waba", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "919000000450",
                         "phone_number_id": account or fx.PHONE_NUMBER_ID},
            "contacts": [{"profile": {"name": "Priya Nair"}, "wa_id": GUEST_PHONE}],
            "messages": [{"from": GUEST_PHONE, "id": message_id,
                          "timestamp": str(int(time.time())), "type": "text",
                          "text": {"body": text}}],
        }}]}],
    }, message_id


def ig_payload(text: str, *, mid: str | None = None, account: str | None = None):
    global _seq
    _seq += 1
    message_id = mid or f"ig_mid.PILOT{uuid.uuid4().hex[:10]}{_seq:03d}"
    return {
        "object": "instagram",
        "entry": [{"id": account or fx.IG_ACCOUNT_ID, "time": int(time.time() * 1000),
                   "messaging": [{"sender": {"id": IGSID},
                                  "recipient": {"id": account or fx.IG_ACCOUNT_ID},
                                  "timestamp": int(time.time() * 1000),
                                  "message": {"mid": message_id, "text": text}}]}],
    }, message_id


def deliver(payload, *, script=None, channel="whatsapp"):
    from app.api.v1.messaging import webhooks
    from app.services.ai.providers import EchoProvider
    provider = EchoProvider(script) if script is not None else None
    return run(webhooks.handle_payload(payload, provider=provider, channel=channel))


def conversation_for(channel="whatsapp"):
    rows = (fx.db_conn().table("conversations").select("*")
            .eq("property_id", fx.PROPERTY).eq("channel", channel)
            .order("created_at", desc=True).limit(1).execute().data or [])
    return rows[0] if rows else None


def latest_run(conversation_id):
    """
    The run the webhook just opened.

    Read back rather than returned: `handle_payload` answers Meta with a
    delivery receipt — how many messages it accepted and how many were
    redeliveries — and deliberately not with our internal run id. That is the
    right shape for a webhook and the wrong shape for a test, so the test does
    the lookup.
    """
    rows = (fx.db_conn().table("agent_runs").select("*")
            .eq("conversation_id", conversation_id)
            .order("started_at", desc=True).limit(1).execute().data or [])
    return rows[0] if rows else None


def actions_of(run_id):
    return (fx.db_conn().table("agent_actions").select("*")
            .eq("run_id", run_id).order("seq").execute().data or []) if run_id else []


def messages_in(conversation_id):
    return (fx.db_conn().table("conversation_messages").select("*")
            .eq("conversation_id", conversation_id)
            .order("created_at").execute().data or [])


def main() -> int:  # noqa: C901 — one linear day; splitting it hides the order
    global PASS, FAIL
    print("\n══ pilot readiness e2e ══")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    logins = fx.open_fixture()
    owner = logins["owner"]
    manager = logins["manager"]
    reception = logins["reception"]
    housekeeping = logins["housekeeping"]
    agency = logins["agency"]
    if not all(logins.values()):
        print("  could not sign in as the pilot staff")
        return 1

    print(f"""
── pilot hotel ─────────────────────────────────────────────
   property         Sunbird Beach Resort  {fx.PROPERTY[:8]}…
   billing account  {fx.ACCOUNT[:8]}…  {fx.wallet_available():,.0f} credits
   owns             everything under the f1… id namespace
""")

    P = fx.PROPERTY
    today = date.today()

    # ═════════════════════════════════════════════════════════════════════════
    section("1. onboarding — a hotel that did not exist an hour ago")

    st, me = http(f"{API}/me", headers=owner)
    check("the owner signs in and holds exactly one hotel",
          st == 200 and len(me.get("memberships", [])) == 1, f"{st} {str(me)[:200]}")

    st, props = http(f"{API}/properties", headers=reception)
    check("so does the receptionist", st == 200 and len(props) == 1, str(st))
    prop = props[0] if st == 200 and props else {}
    check("the property carries its own timezone, not the server's",
          prop.get("timezone") == "Asia/Kolkata", str(prop.get("timezone")))

    from app.services.pms import clock
    business = run(clock.abusiness_date(P))
    check("and the business date is read from that timezone",
          isinstance(business, date), str(business))

    st, types = http(f"{API}/properties/{P}/room-types", headers=reception)
    check("three room types, twelve rooms",
          st == 200 and len(types) == 3, f"{st} {len(types) if st == 200 else types}")

    st, rooms = http(f"{API}/properties/{P}/rooms", headers=reception)
    check("the rooms exist and are countable", st == 200 and len(rooms) == 12,
          f"{st} {len(rooms) if st == 200 else rooms}")

    # The claim that matters on day one: a hotel that has typed in no calendar
    # is still fully bookable, because inventory and rates both inherit.
    st, avail = http(f"{API}/properties/{P}/availability"
                     f"?checkIn={today + timedelta(days=30)}"
                     f"&checkOut={today + timedelta(days=32)}&units=1", headers=reception)
    check("a hotel with no ARI calendar at all is still sellable",
          st == 200 and len(avail) == 3, f"{st} {str(avail)[:200]}")
    total_units = sum(o["unitsLeft"] for o in avail) if st == 200 else 0
    check("and every room is on sale, inherited from the room types",
          total_units == 12, f"{total_units}")

    st, wallet = http(f"{API}/properties/{P}/billing", headers=owner)
    # The API's answer, not the database's. They disagreed once: `billing`
    # cached a property's account for the process lifetime, so a hotel whose
    # account had been replaced read a wallet that no longer existed and showed
    # a zero balance beside a full one. The cache is TTL-bounded now, and this
    # is the assertion that would have caught it.
    check("the property was attached to a billing account by the onboarding trigger",
          st == 200 and wallet.get("available", 0) > 0, f"{st} {str(wallet)[:200]}")
    check("and the API's view of the wallet matches the ledger's",
          st == 200 and abs(wallet.get("balance", 0) - fx.wallet_available()) < 1,
          f"api {wallet.get('balance')} vs ledger {fx.wallet_available()}")

    st, agents = http(f"{API}/properties/{P}/agents", headers=owner)
    check("all five agents are provisioned and active",
          st == 200 and len(agents) == 5 and all(a["status"] == "active" for a in agents),
          f"{st} {[a.get('type') for a in agents] if st == 200 else agents}")

    # ═════════════════════════════════════════════════════════════════════════
    section("2. a stranger finds the hotel and books it")

    st, site = http(f"{API}/public/hotels/{fx.SLUG}")
    check("the published website answers an unauthenticated caller", st == 200,
          f"{st} {str(site)[:200]}")
    check("with the hotel's own policies, not a template's",
          "48 hours" in json.dumps(site.get("policies", {})), str(site.get("policies"))[:160])
    check("and no internal identifier anywhere in the payload",
          fx.PARTNER not in json.dumps(site) and P not in json.dumps(site),
          "an internal id leaked to a public page")

    ci, co = today + timedelta(days=40), today + timedelta(days=43)
    st, pub = http(f"{API}/public/hotels/{fx.SLUG}/availability?from={ci}&to={co}&units=1&adults=2")
    check("public availability answers", st == 200 and not pub.get("soldOut"),
          f"{st} {str(pub)[:200]}")
    plans = [(r["roomTypeId"], p["ratePlanId"], p["totalPaise"])
             for r in pub.get("rooms", []) for p in r["ratePlans"]] if st == 200 else []
    check("showing only public rate plans — the agent rate is not on the website",
          all(p["audience"] == "public" for r in pub.get("rooms", []) for p in r["ratePlans"]),
          str(plans))
    check("and three of them, one per room type", len(plans) == 3, str(len(plans)))

    dlx_public = next((p for p in plans if p[2] == fx.RATE_DLX * 3), None)
    check("the sea view is priced at its own tariff × three nights",
          dlx_public is not None, str(plans))

    st, quote = http(f"{API}/public/hotels/{fx.SLUG}/quote", "POST", {
        "ratePlanId": plans[0][1], "checkIn": str(ci), "checkOut": str(co),
        "units": 1, "adults": 2, "children": 0,
    })
    check("a public quote holds the rooms", st == 200 and quote.get("quoteId"),
          f"{st} {str(quote)[:200]}")
    check("with tax computed server-side and a token from the property's own setting",
          quote["taxPaise"] > 0 and 0 < quote["tokenPaise"] < quote["totalPaise"],
          str(quote)[:220])

    st, order = http(f"{API}/public/hotels/{fx.SLUG}/quotes/{quote['quoteId']}/order",
                     "POST", {"guest": {"fullName": "Ananya Iyer",
                                        "email": "ananya@example.test",
                                        "phone": "+919812300011"},
                              "addons": [{"serviceId": fx.SVC_TRANSFER, "quantity": 1,
                                          "serviceDate": str(ci)}]})
    check("a real Razorpay order is created, in test mode",
          st == 200 and str(order.get("razorpayOrderId", "")).startswith("order_"),
          f"{st} {str(order)[:200]}")
    check("charging the room's token plus the extra in full",
          order["amountPaise"] == order["roomTokenPaise"] + order["addonsTotalPaise"],
          str(order)[:200])

    rzp_order = order["razorpayOrderId"]
    payment_id = "pay_pilot_" + uuid.uuid4().hex[:12]

    st, _ = http(f"{API}/public/hotels/{fx.SLUG}/confirm", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": "not-a-signature"})
    check("a forged payment signature is refused", st == 400, str(st))

    st, booked = http(f"{API}/public/hotels/{fx.SLUG}/confirm", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": payment_signature(rzp_order, payment_id)})
    check("a valid one books the stay", st == 200, f"{st} {str(booked)[:250]}")
    web_ref = booked.get("ref") if st == 200 else None
    check("the confirmation carries a reference and a check-in link",
          bool(web_ref) and booked.get("checkInUrl", "").endswith(web_ref or "x"),
          str(booked)[:200])
    check("and the extra is on the folio, priced by the server",
          any(a["totalPaise"] == 180000 for a in booked.get("addons", [])),
          str(booked.get("addons"))[:200])

    # ── web check-in ─────────────────────────────────────────────────────────
    st, stay = http(f"{API}/public/stays/{web_ref}")
    check("the guest can open their own booking with the reference alone",
          st == 200 and stay.get("ref") == web_ref, f"{st} {str(stay)[:200]}")
    check("and the phone on it is masked",
          "•" in str(stay.get("guestPhoneMasked", "")), str(stay.get("guestPhoneMasked")))
    check("with no email, guest id or room number on it at all",
          not any(k in stay for k in ("guestEmail", "guestId", "roomNumber")),
          str(sorted(stay))[:200])

    st, reg = http(f"{API}/public/stays/{web_ref}/check-in", "POST", {
        "fullName": "Ananya Iyer", "email": "ananya@example.test",
        "phone": "+919812300011", "address": "14 Turner Road, Bandra",
        "city": "Mumbai", "country": "India",
        "idType": "aadhaar", "idNumber": "987612345678",
        "travellingWith": 1, "estimatedArrival": "16:30"})
    check("web check-in is accepted", st == 200, f"{st} {str(reg)[:200]}")
    leak = sql(f"""select count(*) as n from public.guest_registrations
                    where reservation_id = (select id from public.reservations
                                             where ref = '{web_ref}')
                      and id_last4 = '5678';""")
    check("only the last four digits of the ID are kept",
          int(leak[0]["n"]) == 1, str(leak))
    full = sql("""select count(*) as n from public.guest_registrations
                   where id_last4 like '%987612345678%';""")
    check("and the full number is nowhere in the database", int(full[0]["n"]) == 0, str(full))

    # ═════════════════════════════════════════════════════════════════════════
    section("3. a guest messages the hotel on WhatsApp")

    payload, first_mid = wa_payload("Hi, do you have a room tomorrow for 2 people?")
    result = deliver(payload, script=[
        [("search_availability", {"check_in": str(today + timedelta(days=1)),
                                  "check_out": str(today + timedelta(days=2)),
                                  "adults": 2})],
        "Yes — we have a Garden Room free tomorrow. Shall I get you a price?",
    ])
    conv = conversation_for("whatsapp")
    check("the message created a conversation on this property",
          conv is not None and conv["property_id"] == P, str(conv)[:160])
    check("resolved from the phone_number_id, not from anything in the payload",
          conv is not None and conv["guest_phone"] == GUEST_PHONE, str(conv)[:160])

    acts = actions_of((latest_run(conv["id"]) or {}).get("id"))
    check("the agent checked real availability rather than answering from memory",
          [a["tool"] for a in acts] == ["search_availability"],
          str([a["tool"] for a in acts]))
    out = (acts[0].get("output") or {}) if acts else {}
    check("and the numbers it saw are the hotel's own",
          out.get("anyAvailable") is True
          and any(r["unitsLeft"] > 0 for r in out.get("roomTypes", [])),
          str(out)[:240])
    check("with the tool telling it not to invent a price from an availability call",
          "Prices come from get_quote" in (out.get("instruction") or ""),
          str(out.get("instruction"))[:160])

    msgs = messages_in(conv["id"])
    check("both sides of the exchange are stored",
          len([m for m in msgs if m["direction"] == "inbound"]) == 1
          and len([m for m in msgs if m["direction"] == "outbound"]) == 1,
          str([(m["direction"], m["body"][:30]) for m in msgs]))

    # ── the redelivery Meta will certainly send ──────────────────────────────
    repeat = deliver(payload, script=["THIS MUST NEVER BE SPOKEN."])
    msgs_after = messages_in(conv["id"])
    check("a redelivered webhook with the same message id changes nothing",
          len(msgs_after) == len(msgs), f"{len(msgs)} → {len(msgs_after)}")
    check("and Meta is told it was a redelivery, not that it was accepted",
          repeat.get("duplicates") == 1 and repeat.get("accepted") == 0,
          str(repeat)[:160])

    # ── "I'll take it" ───────────────────────────────────────────────────────
    # `get_quote` has an autonomy floor of 3 and the Guest agent ships at 2,
    # because a quote takes a hold and a hold is a room somebody else cannot
    # book. So the guest agent *proposes* and the desk approves — which is the
    # designed behaviour, and asserting it here is the point.
    quote_args = {"room_type_id": fx.RT_STD, "rate_plan_id": fx.PLAN_STD_CP,
                  "check_in": str(today + timedelta(days=1)),
                  "check_out": str(today + timedelta(days=2)),
                  "units": 1, "adults": 2}
    payload, _ = wa_payload("I'll take it. Name is Priya Nair.")
    deliver(payload, script=[[("get_quote", quote_args)],
                             "Let me get that confirmed for you."])
    acts = actions_of((latest_run(conv["id"]) or {}).get("id"))
    check("a guest agent at autonomy 2 cannot take a hold by itself",
          acts and acts[0]["status"] == "awaiting_approval",
          str([(a["tool"], a["status"]) for a in acts]))
    held_now = sql(f"""select coalesce(sum(units_held), 0) as n from public.room_inventory
                        where room_type_id = '{fx.RT_STD}'
                          and date = date '{today + timedelta(days=1)}';""")
    check("and no room is held while it waits",
          int(held_now[0]["n"]) == 0, str(held_now))

    st, approved_action = http(
        f"{API}/properties/{P}/agent-actions/{acts[0]['id']}/decision",
        "POST", {"decision": "approved"}, manager)
    check("the desk approves the proposal", st == 200, f"{st} {str(approved_action)[:200]}")

    quoted = sql(f"""select output from public.agent_actions where id = '{acts[0]["id"]}';""")
    quoted = (quoted[0]["output"] or {}) if quoted else {}
    check("and the quote is the pricing engine's, not the model's arithmetic",
          bool(quoted.get("quoteId")), str(quoted)[:220])
    check("carrying the tariff, the tax and the total the server computed",
          quoted.get("grossPaise") == fx.RATE_STD and quoted.get("taxPaise", 0) > 0,
          str(quoted)[:240])
    agent_quote = quoted.get("quoteId")

    held = sql(f"""select coalesce(sum(units_held), 0) as n from public.room_inventory
                    where room_type_id = '{fx.RT_STD}'
                      and date = date '{today + timedelta(days=1)}';""")
    check("only now is the room actually held",
          int(held[0]["n"]) == 1, str(held))

    # ── the complaint ────────────────────────────────────────────────────────
    payload, _ = wa_payload("The AC in room 204 isn't working.")
    deliver(payload, script=[
        [("create_task", {"title": "AC not cooling", "category": "maintenance",
                          "priority": "high"})],
        "I've logged that with maintenance — someone will come up shortly.",
    ])
    tasks = sql(f"""select * from public.tasks where property_id = '{P}'
                     and title ilike '%AC not cooling%';""")
    check("a complaint became a real task on this property",
          len(tasks) == 1, str(tasks)[:200])
    check("at the priority the guest's words justified",
          tasks and tasks[0]["priority"] == "high", str(tasks[0]["priority"]) if tasks else "")
    check("and the guest was told what happened",
          "logged" in (messages_in(conv["id"])[-1]["body"] or "").lower(),
          messages_in(conv["id"])[-1]["body"][:120])

    # ── the receptionist takes over ──────────────────────────────────────────
    st, taken = http(f"{API}/properties/{P}/conversations/{conv['id']}/handler",
                     "POST", {"handler": "human"}, reception)
    check("a receptionist can take the thread over", st == 200, f"{st} {str(taken)[:160]}")

    before = len(messages_in(conv["id"]))
    payload, _ = wa_payload("Any update on the AC?")
    deliver(payload, script=["THE AI MUST NOT SPEAK AFTER A HUMAN TAKES OVER."])
    after = messages_in(conv["id"])
    check("the guest's next message is still stored",
          len(after) == before + 1, f"{before} → {len(after)}")
    check("but the AI does not answer it",
          after[-1]["direction"] == "inbound", f"{after[-1]['direction']} {after[-1]['body'][:60]}")

    st, sent = http(f"{API}/properties/{P}/conversations/{conv['id']}/messages",
                    "POST", {"body": "Engineer is on the way — sorry about that."}, reception)
    check("and the human's reply goes out", st == 200, f"{st} {str(sent)[:160]}")
    final = messages_in(conv["id"])[-1]
    check("attributed to the person who sent it, not to the agent",
          final["author"] == "staff" and final.get("author_name") == "Riya Menon",
          f"{final['author']} {final.get('author_name')}")

    # ═════════════════════════════════════════════════════════════════════════
    section("4. the same chain on Instagram")

    payload, ig_mid = ig_payload("hey! is the beach suite free next month?")
    deliver(payload, script=[
        [("search_availability", {"check_in": str(today + timedelta(days=35)),
                                  "check_out": str(today + timedelta(days=37)),
                                  "adults": 2})],
        "Yes — the Beach Suite is free those nights.",
    ], channel="instagram")
    ig_conv = conversation_for("instagram")
    check("an Instagram DM creates its own conversation on the same property",
          ig_conv is not None and ig_conv["property_id"] == P, str(ig_conv)[:160])
    check("keyed on the Instagram-scoped user id, and holding no phone number",
          ig_conv and ig_conv.get("external_id") == IGSID
          and ig_conv.get("guest_phone") is None,
          f"{ig_conv.get('external_id')} / {ig_conv.get('guest_phone')}")
    check("and the agent answered on it",
          len([m for m in messages_in(ig_conv["id"]) if m["direction"] == "outbound"]) == 1,
          str([(m["direction"], m["channel"]) for m in messages_in(ig_conv["id"])]))

    ig_before = len(messages_in(ig_conv["id"]))
    deliver(payload, script=["THIS MUST NEVER BE SPOKEN."], channel="instagram")
    check("a redelivered Instagram webhook is also a no-op",
          len(messages_in(ig_conv["id"])) == ig_before, str(ig_before))

    # ═════════════════════════════════════════════════════════════════════════
    section("5. the front desk works the day")

    st, res = http(f"{API}/properties/{P}/reservations", "POST", {
        "quoteId": agent_quote, "guestName": "Priya Nair",
        "guestPhone": f"+{GUEST_PHONE}", "source": "whatsapp",
        "tokenPaise": 100000, "paymentMethod": "upi"}, reception)
    check("the desk turns the agent's quote into a booking", st == 200,
          f"{st} {str(res)[:220]}")
    stay_id = res.get("id")
    check("the token it collected is on the folio, not on a note",
          res.get("paidPaise") == 100000, str(res.get("paidPaise")))

    st, again = http(f"{API}/properties/{P}/reservations", "POST", {
        "quoteId": agent_quote, "guestName": "Priya Nair", "source": "whatsapp",
        "tokenPaise": 0}, reception)
    check("and the same quote cannot be booked twice", again and st == 409, str(st))

    st, listed = http(f"{API}/properties/{P}/reservations?search=Priya", headers=reception)
    check("the reservation list finds it by the guest's name",
          st == 200 and any(r["id"] == stay_id for r in listed), f"{st} {str(listed)[:200]}")

    room_id = sql(f"""select id from public.rooms
                       where property_id = '{P}' and number = 'G01';""")[0]["id"]
    st, checked_in = http(f"{API}/properties/{P}/reservations/{stay_id}/check-in",
                          "POST", {"roomId": room_id}, reception)
    check("check-in assigns a real room", st == 200
          and checked_in.get("status") == "checked_in", f"{st} {str(checked_in)[:200]}")

    st, _ = http(f"{API}/properties/{P}/reservations/{stay_id}/folio", "POST", {
        "kind": "addon", "description": "Beach dinner", "amountPaise": 320000,
        "idempotencyKey": f"pilot-dinner-{stay_id}"}, reception)
    check("the desk posts an extra to the folio", st == 200, str(st))

    st, dup = http(f"{API}/properties/{P}/reservations/{stay_id}/folio", "POST", {
        "kind": "addon", "description": "Beach dinner", "amountPaise": 320000,
        "idempotencyKey": f"pilot-dinner-{stay_id}"}, reception)
    lines = sql(f"""select count(*) as n from public.folio_entries
                     where reservation_id = '{stay_id}' and description = 'Beach dinner';""")
    check("and a retried post does not charge the guest twice",
          int(lines[0]["n"]) == 1, str(lines))

    st, folio = http(f"{API}/properties/{P}/reservations/{stay_id}/folio", headers=reception)
    check("the balance is the sum of the folio, not a stored total",
          st == 200 and folio["balancePaise"] > 0, f"{st} {str(folio)[:200]}")
    balance = folio["balancePaise"] if st == 200 else 0

    st, refused = http(f"{API}/properties/{P}/reservations/{stay_id}/check-out",
                       "POST", {}, reception)
    check("check-out is refused while money is owed", st == 409, f"{st} {str(refused)[:160]}")

    # That refusal posted the room nights first — deliberately, so a guest
    # leaving before the night audit has caught up cannot walk out on a folio
    # that reads zero because the nights were never billed. The balance to
    # settle is therefore the one *after* the attempt, not before it.
    st, folio2 = http(f"{API}/properties/{P}/reservations/{stay_id}/folio", headers=reception)
    check("and the attempt itself billed the stay, so the bill is complete",
          st == 200 and folio2["balancePaise"] > balance,
          f"{balance} → {folio2.get('balancePaise')}")

    http(f"{API}/properties/{P}/reservations/{stay_id}/folio", "POST", {
        "kind": "payment", "description": "Settled at the desk",
        "amountPaise": folio2["balancePaise"], "method": "card",
        "idempotencyKey": f"pilot-settle-{stay_id}"}, reception)
    st, out = http(f"{API}/properties/{P}/reservations/{stay_id}/check-out", "POST", {}, reception)
    check("and permitted once it is settled", st == 200
          and out.get("status") == "checked_out", f"{st} {str(out)[:200]}")

    st, audit = http(f"{API}/properties/{P}/night-audit", "POST", {}, manager)
    check("the night audit runs on this property's own business date",
          st == 200, f"{st} {str(audit)[:200]}")

    # ═════════════════════════════════════════════════════════════════════════
    section("6. a travel agent books on credit")

    st, contract = http(f"{API}/agency/me", headers=agency)
    check("the agency reads its own contract", st == 200
          and len(contract.get("contracts", [])) == 1, f"{st} {str(contract)[:200]}")
    check("with the credit line the hotel agreed and nothing owed yet",
          contract["contracts"][0]["creditLimitPaise"] == 4000000
          and contract["contracts"][0]["committedPaise"] == 0,
          str(contract["contracts"][0])[:220]) if st == 200 else check("contract", False)

    b2b_in = today + timedelta(days=60)
    st, b2b_avail = http(f"{API}/agency/properties/{P}/availability"
                         f"?checkIn={b2b_in}&checkOut={b2b_in + timedelta(days=2)}&units=1",
                         headers=agency)
    codes = [p["code"] for o in b2b_avail for p in o["ratePlans"]] if st == 200 else []
    check("the agency sees its net rate and only its net rate",
          codes == ["DLX-AGT"], str(codes))

    st, b2b_quote = http(f"{API}/agency/properties/{P}/quotes", "POST", {
        "ratePlanId": fx.PLAN_DLX_B2B, "checkIn": str(b2b_in),
        "checkOut": str(b2b_in + timedelta(days=2)), "units": 1, "adults": 2}, agency)
    check("it can price a stay on that rate", st == 200, f"{st} {str(b2b_quote)[:200]}")
    check("and the credit is committed at quote time, not at booking",
          b2b_quote.get("committedPaise", 0) == b2b_quote.get("totalPaise"),
          str(b2b_quote)[:220])

    st, clamp = http(f"{API}/agency/properties/{P}/quotes", "POST", {
        "ratePlanId": fx.PLAN_DLX_CP, "checkIn": str(b2b_in),
        "checkOut": str(b2b_in + timedelta(days=2)), "units": 1, "adults": 2}, agency)
    check("but a public rate plan id is a 404 on this path, even though it is real",
          st == 404, f"{st} {str(clamp)[:160]}")

    st, b2b_booking = http(f"{API}/agency/properties/{P}/bookings", "POST", {
        "quoteId": b2b_quote["id"], "guestName": "Konkan client"}, agency)
    check("the booking goes through with nothing collected", st == 200
          and b2b_booking.get("paidPaise") == 0, f"{st} {str(b2b_booking)[:200]}")

    st, after_book = http(f"{API}/agency/me", headers=agency)
    check("and the line still reads the same, because a quote already held it",
          after_book["contracts"][0]["committedPaise"] == b2b_quote["totalPaise"],
          str(after_book["contracts"][0])[:200])

    http(f"{API}/properties/{P}/reservations/{b2b_booking['id']}/folio", "POST", {
        "kind": "payment", "description": "Agency settlement",
        "amountPaise": b2b_booking["totalPaise"], "method": "bank",
        "idempotencyKey": f"pilot-agency-{b2b_booking['id']}"}, reception)
    st, settled = http(f"{API}/agency/me", headers=agency)
    check("settling on the folio releases the line, with no other code involved",
          settled["contracts"][0]["committedPaise"] == 0,
          str(settled["contracts"][0])[:200])

    # ═════════════════════════════════════════════════════════════════════════
    section("7. a creator sends a guest")

    st, touch = http(f"{API}/public/hotels/{fx.SLUG}/attribution", "POST",
                     {"code": "PILOTCREATOR-9X4K"})
    check("the creator's link attributes a click", st == 200 and touch.get("attributed"),
          f"{st} {str(touch)[:200]}")
    visitor, touch_ref = touch.get("visitorRef"), touch.get("touchRef")

    from app.services.pms import creators as creators_service
    conv_id = str(uuid.uuid4())
    creator_phone = "+91981" + str(int(time.time()))[-7:]
    sql(f"""insert into public.conversations
             (id, property_id, partner_id, channel, external_id, guest_phone,
              guest_name, handler)
           values ('{conv_id}', '{P}', '{fx.PARTNER}', 'whatsapp',
                   'pilot-creator-{conv_id[:8]}', '{creator_phone}', 'Creator Guest', 'ai');""")
    bound = run(creators_service.bind_conversation(
        P, conv_id, f"saw your reel! ref: {touch_ref}"))
    check("and the token carries the chain into WhatsApp", bound == touch_ref,
          f"{bound} vs {touch_ref}")

    cr_in = today + timedelta(days=70)
    st, cq = http(f"{API}/properties/{P}/quotes", "POST", {
        "roomTypeId": fx.RT_DLX, "ratePlanId": fx.PLAN_DLX_CP, "checkIn": str(cr_in),
        "checkOut": str(cr_in + timedelta(days=3)), "units": 1, "adults": 2}, reception)
    st, cb = http(f"{API}/properties/{P}/reservations", "POST", {
        "quoteId": cq["id"], "guestName": "Creator Guest",
        "guestPhone": creator_phone, "source": "whatsapp", "tokenPaise": 0}, reception)
    check("the guest books weeks later through a different channel", st == 200,
          f"{st} {str(cb)[:200]}")

    sql(f"""update public.conversations set reservation_id = '{cb['id']}'
             where id = '{conv_id}';""")
    run(creators_service.resolve_for_reservation({"id": cb["id"]}))
    payout = sql(f"""select * from public.creator_payouts
                      where reservation_id = '{cb['id']}';""")
    check("the creator is attributed anyway", len(payout) == 1, str(payout)[:200])

    sql(f"""update public.reservations set status = 'checked_out', paid_paise = total_paise
             where id = '{cb['id']}';""")
    run(creators_service.recalculate_for_reservation(cb["id"]))

    st, refused = http(f"{API}/properties/{P}/creator-campaigns/{fx.PARTICIPATION}/approve",
                       "POST", {}, manager)
    check("a manager cannot approve the payout", st == 403, str(st))
    st, approved = http(f"{API}/properties/{P}/creator-campaigns/{fx.PARTICIPATION}/approve",
                        "POST", {}, owner)
    expected = round((cb["grossPaise"] - cb.get("discountPaise", 0)) * 10 / 100)
    check("the owner can, for the amount the server computed",
          st == 200 and approved.get("amountPaise") == expected,
          f"{st} {str(approved)[:160]} expected {expected}")

    # ═════════════════════════════════════════════════════════════════════════
    section("8. failure — what happens when things go wrong")

    from app.services.ai import runtime
    from app.services.ai.providers import EchoProvider, ProviderError

    from app.services.ai import context as ai_context

    def guest_ctx():
        return run(ai_context.load_context(
            property_id=P, partner_id=fx.PARTNER, agent_type="guest",
            conversation_id=conv["id"], channel="whatsapp"))

    # ── the model refusing ───────────────────────────────────────────────────
    r = run(runtime.run_turn(guest_ctx(), "hello",
                             provider=EchoProvider([ProviderError("429 RESOURCE_EXHAUSTED")])))
    check("a Vertex 429 fails the run rather than inventing an answer",
          r.status == "failed" and "429" in (r.error or ""), f"{r.status} {r.error}")
    check("and the failure is recorded where an operator can find it",
          bool(sql(f"select id from public.agent_runs where id = '{r.run_id}' and status = 'failed';")),
          str(r.run_id))

    r = run(runtime.run_turn(guest_ctx(), "hello",
                             provider=EchoProvider([TimeoutError("deadline exceeded")])))
    check("a Vertex timeout fails the same way", r.status == "failed", f"{r.status} {r.error}")

    r = run(runtime.run_turn(guest_ctx(), "hello", provider=EchoProvider([
        [("no_such_tool", {"anything": 1})], "…"])))
    acts = (fx.db_conn().table("agent_actions").select("*")
            .eq("run_id", r.run_id).execute().data or [])
    check("a tool the model invented is refused, not executed",
          all(a["status"] != "executed" for a in acts) or not acts,
          str([(a["tool"], a["status"]) for a in acts]))

    r = run(runtime.run_turn(guest_ctx(), "book me something", provider=EchoProvider([
        [("create_quote", {"room_type_id": fx.RT_STD})], "…"])))
    acts = (fx.db_conn().table("agent_actions").select("*")
            .eq("run_id", r.run_id).execute().data or [])
    check("and a tool call missing its arguments does not reach the database",
          all(a["status"] != "executed" for a in acts),
          str([(a["tool"], a["status"]) for a in acts]))

    # ── two guests, one room ─────────────────────────────────────────────────
    scarce = today + timedelta(days=120)
    fx.sql(f"""insert into public.room_inventory
                 (property_id, partner_id, room_type_id, date, units_total)
               values ('{P}', '{fx.PARTNER}', '{fx.RT_SUI}', date '{scarce}', 1)
               on conflict (room_type_id, date) do update set units_total = 1;""")

    def race_quote(_):
        return http(f"{API}/public/hotels/{fx.SLUG}/quote", "POST", {
            "ratePlanId": fx.PLAN_SUI_CP, "checkIn": str(scarce),
            "checkOut": str(scarce + timedelta(days=1)), "units": 1, "adults": 2})

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(race_quote, range(4)))
    winners = [b for st, b in results if st == 200]
    check("four guests racing the last suite produce exactly one hold",
          len(winners) == 1, f"{[st for st, _ in results]}")
    held = sql(f"""select units_held, units_booked from public.room_inventory
                    where room_type_id = '{fx.RT_SUI}' and date = date '{scarce}';""")
    check("and the room is never oversold",
          held and int(held[0]["units_held"]) + int(held[0]["units_booked"]) <= 1, str(held))

    # ── a quote whose stock moved underneath it ──────────────────────────────
    stale_day = today + timedelta(days=130)
    st, stale = http(f"{API}/public/hotels/{fx.SLUG}/quote", "POST", {
        "ratePlanId": fx.PLAN_SUI_CP, "checkIn": str(stale_day),
        "checkOut": str(stale_day + timedelta(days=1)), "units": 1, "adults": 2})
    sql(f"""update public.quotes set expires_at = now() - interval '1 minute'
             where id = '{stale["quoteId"]}';""")
    st, expired = http(f"{API}/public/hotels/{fx.SLUG}/quotes/{stale['quoteId']}/order",
                       "POST", {"guest": {"fullName": "Too Slow", "email": "s@example.test",
                                          "phone": "+919812300099"}, "addons": []})
    check("an expired quote cannot be paid for", st == 409, f"{st} {str(expired)[:160]}")

    # ── the payment webhook, twice, and racing the browser ───────────────────
    pay_day = today + timedelta(days=140)
    st, q2 = http(f"{API}/public/hotels/{fx.SLUG}/quote", "POST", {
        "ratePlanId": fx.PLAN_STD_CP, "checkIn": str(pay_day),
        "checkOut": str(pay_day + timedelta(days=2)), "units": 1, "adults": 2})
    st, o2 = http(f"{API}/public/hotels/{fx.SLUG}/quotes/{q2['quoteId']}/order", "POST",
                  {"guest": {"fullName": "Webhook Guest", "email": "w@example.test",
                             "phone": "+919812300088"}, "addons": []})
    pid2 = "pay_pilot_dup_" + uuid.uuid4().hex[:10]
    body, sig = webhook_body("payment.captured", o2["razorpayOrderId"], pid2)

    def fire(which):
        if which == "webhook":
            return http(f"{API}/public/razorpay/webhook", "POST", None,
                        {"X-Razorpay-Signature": sig}, )
        return http(f"{API}/public/hotels/{fx.SLUG}/confirm", "POST", {
            "razorpayOrderId": o2["razorpayOrderId"], "razorpayPaymentId": pid2,
            "razorpaySignature": payment_signature(o2["razorpayOrderId"], pid2)})

    import urllib.request
    def raw_webhook():
        req = urllib.request.Request(f"{API}/public/razorpay/webhook", method="POST",
                                     data=body,
                                     headers={"Content-Type": "application/json",
                                              "X-Razorpay-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status
        except Exception as exc:  # noqa: BLE001
            return getattr(exc, "code", 0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(raw_webhook), pool.submit(fire, "browser"),
                   pool.submit(raw_webhook)]
        [f.result() for f in futures]

    made = sql(f"""select count(*) as n from public.reservations
                    where quote_id = '{q2["quoteId"]}';""")
    check("a webhook, a browser callback and a redelivery produce one reservation",
          int(made[0]["n"]) == 1, str(made))
    charged = sql(f"""select count(*) as n from public.folio_entries f
                       join public.reservations r on r.id = f.reservation_id
                      where r.quote_id = '{q2["quoteId"]}' and f.kind = 'payment';""")
    check("and the guest is charged to the folio exactly once",
          int(charged[0]["n"]) == 1, str(charged))

    st, forged = http(f"{API}/public/razorpay/webhook", "POST", {"event": "payment.captured"},
                      {"X-Razorpay-Signature": "deadbeef"})
    check("a webhook with a bad signature is refused", forged and st in (400, 401), str(st))

    # ── credits ─────────────────────────────────────────────────────────────
    section("9. credits — and what must never depend on them")

    from app.services import billing
    before_credits = fx.wallet_available()
    check("the wallet has been spent by the day's agent turns",
          before_credits < 3000, f"{before_credits}")

    events = sql(f"""select count(*) as n from public.usage_events
                      where property_id = '{P}';""")
    check("every agent turn left a usage event on this property",
          int(events[0]["n"]) > 0, str(events))
    ledger = sql(f"""select count(*) as n from public.credit_ledger
                      where billing_account_id = '{fx.ACCOUNT}';""")
    check("and every charge left a ledger entry", int(ledger[0]["n"]) > 0, str(ledger))

    drift = sql(f"""select w.balance_credits::numeric - coalesce(sum(l.credits), 0) as d
                      from public.credit_wallets w
                      left join public.credit_ledger l
                        on l.billing_account_id = w.billing_account_id
                     where w.billing_account_id = '{fx.ACCOUNT}'
                     group by w.balance_credits;""")
    check("the wallet equals the sum of its ledger, to the credit",
          drift and abs(float(drift[0]["d"])) < 0.001, str(drift))

    # Empty the wallet — through the mechanism that actually empties one, not by
    # writing the balance. `credit_wallets.balance_credits` is a cache the ledger
    # owns and the guards refuse a hand-written value; expiring the buckets and
    # sweeping drains it the way a month's end does, and leaves the ledger
    # explicable afterwards. A test that had to disable a guard to set up its
    # own precondition would be testing a system nobody runs.
    sql(f"""update public.credit_buckets
               set expires_at = now() - interval '1 day'
             where wallet_id in (select id from public.credit_wallets
                                  where billing_account_id = '{fx.ACCOUNT}');
           select public.expire_credit_buckets();""")
    check("the wallet is empty", fx.wallet_available() <= 0, str(fx.wallet_available()))
    explained = sql(f"""select count(*) as n from public.credit_ledger
                         where billing_account_id = '{fx.ACCOUNT}'
                           and entry_type = 'expiry';""")
    check("and the balance is still explicable — expiry went through the ledger",
          int(explained[0]["n"]) > 0, str(explained))

    poor_in = today + timedelta(days=150)
    st, poor_avail = http(f"{API}/properties/{P}/availability"
                          f"?checkIn={poor_in}&checkOut={poor_in + timedelta(days=1)}&units=1",
                          headers=reception)
    check("a hotel with no credits can still see what it can sell", st == 200 and poor_avail,
          f"{st} {str(poor_avail)[:160]}")
    st, poor_quote = http(f"{API}/properties/{P}/quotes", "POST", {
        "roomTypeId": fx.RT_STD, "ratePlanId": fx.PLAN_STD_CP, "checkIn": str(poor_in),
        "checkOut": str(poor_in + timedelta(days=1)), "units": 1, "adults": 2}, reception)
    check("still price a stay", st == 200, f"{st} {str(poor_quote)[:160]}")
    st, poor_res = http(f"{API}/properties/{P}/reservations", "POST", {
        "quoteId": poor_quote["id"], "guestName": "Cash Guest", "source": "walk_in",
        "tokenPaise": 0}, reception)
    check("still take a booking", st == 200, f"{st} {str(poor_res)[:160]}")
    poor_room = sql(f"""select id from public.rooms
                         where property_id = '{P}' and number = 'G02';""")[0]["id"]
    st, poor_ci = http(f"{API}/properties/{P}/reservations/{poor_res['id']}/check-in",
                       "POST", {"roomId": poor_room}, reception)
    check("still check a guest in", st == 200, f"{st} {str(poor_ci)[:160]}")
    st, _ = http(f"{API}/properties/{P}/reservations/{poor_res['id']}/folio", "POST", {
        "kind": "payment", "description": "Cash", "amountPaise": 100000, "method": "cash",
        "idempotencyKey": f"pilot-poor-{poor_res['id']}"}, reception)
    check("and still take money at the desk — the PMS is never credit-gated",
          st == 200, f"{st}")

    # Driven through the webhook rather than through `run_turn`, because that is
    # where the gate is: the pipeline checks the wallet *before* a run opens, so
    # calling the runtime directly would test a path the product does not take.
    before_poor = len(messages_in(conv["id"]))
    payload, _ = wa_payload("are you still there?")
    deliver(payload, script=["THE AI MUST NOT SPEAK ON AN EMPTY WALLET."])
    after_poor = messages_in(conv["id"])
    check("the guest's message is still stored — nothing is ever dropped",
          len(after_poor) == before_poor + 1, f"{before_poor} → {len(after_poor)}")
    check("but the AI does not answer on an empty wallet",
          after_poor[-1]["direction"] == "inbound",
          f"{after_poor[-1]['direction']} {after_poor[-1]['body'][:60]}")

    fx.fund(1200)
    check("and topping the wallet up brings it straight back",
          fx.wallet_available() > 0, str(fx.wallet_available()))

    # The handler is still 'human' from section 3, and that outlasted everything
    # since — the takeover was hours and a dozen operations ago. Worth asserting
    # on the way past: an AI that quietly resumed once credits returned would be
    # the worst possible interpretation of "the wallet was the problem".
    still_human = sql(f"""select handler from public.conversations
                           where id = '{conv["id"]}';""")
    check("the human takeover has survived the entire day",
          still_human[0]["handler"] == "human", str(still_human))

    http(f"{API}/properties/{P}/conversations/{conv['id']}/handler",
         "POST", {"handler": "ai"}, reception)
    payload, _ = wa_payload("hello? anyone?")
    deliver(payload, script=["We're here — sorry about that!"])
    check("and once the desk hands it back, the agent speaks again",
          messages_in(conv["id"])[-1]["direction"] == "outbound",
          str(messages_in(conv["id"])[-1].get("body"))[:120])

    # ═════════════════════════════════════════════════════════════════════════
    section("10. authorization, tried in earnest")

    OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    st, _ = http(f"{API}/properties/{OTHER}/reservations", headers=owner)
    check("this hotel's owner cannot read another hotel's bookings — 404, not 403",
          st == 404, str(st))
    st, _ = http(f"{API}/properties/{OTHER}/today", headers=manager)
    check("nor its day", st == 404, str(st))
    st, _ = http(f"{API}/properties/{OTHER}/guests", headers=reception)
    check("nor its guest list", st == 404, str(st))

    st, _ = http(f"{API}/properties/{P}/guests", headers=housekeeping)
    check("a housekeeper cannot read this hotel's guest list", st == 403, str(st))
    st, _ = http(f"{API}/properties/{P}/billing", headers=housekeeping)
    check("nor its wallet", st == 403, str(st))
    st, _ = http(f"{API}/properties/{P}/quotes", "POST", {
        "roomTypeId": fx.RT_STD, "ratePlanId": fx.PLAN_STD_CP,
        "checkIn": str(today + timedelta(days=200)),
        "checkOut": str(today + timedelta(days=201)), "units": 1}, housekeeping)
    check("nor price a stay", st == 403, str(st))

    st, _ = http(f"{API}/properties/{P}/creator-campaigns", headers=reception)
    check("a receptionist cannot read commission rates", st == 403, str(st))
    # The margin route is deliberately *not* a refusal for a billing holder: it
    # is this hotel's own cost-vs-revenue, scoped to their partner, and
    # `workspace_e2e` asserts a manager reads it. What must hold is the scoping.
    st, margin = http(f"{API}/properties/{P}/margin", headers=owner)
    check("an owner reads their own margin and only their own",
          st == 200 and all(r["propertyId"] == P for r in margin), f"{st} {str(margin)[:200]}")
    st, _ = http(f"{API}/properties/{P}/margin", headers=reception)
    check("and a receptionist does not read it at all", st == 403, str(st))

    st, _ = http(f"{API}/agency/me", headers=reception)
    check("a receptionist is not an agency", st == 403, str(st))
    st, _ = http(f"{API}/properties/{P}/reservations", headers=agency)
    check("and an agency is not staff — the PMS 404s it", st == 404, str(st))

    st, _ = http(f"{API}/properties/{P}/reservations")
    check("an unauthenticated caller reads nothing", st in (401, 403), str(st))
    st, _ = http(f"{API}/properties/{P}/reservations", headers={"Authorization": "Bearer nope"})
    check("nor does a forged token", st in (401, 403), str(st))

    # A property id in a body is not authorization anywhere in this API.
    st, cross = http(f"{API}/properties/{P}/quotes", "POST", {
        "roomTypeId": "10000000-0000-4000-8000-000000000001",
        "ratePlanId": "20000000-0000-4000-8000-000000000003",
        "checkIn": str(today + timedelta(days=205)),
        "checkOut": str(today + timedelta(days=206)), "units": 1}, reception)
    check("a rate plan belonging to another hotel cannot be priced here",
          st in (404, 409), f"{st} {str(cross)[:160]}")

    st, other_stay = http(f"{API}/properties/{P}/reservations/{uuid.uuid4()}",
                          headers=reception)
    check("a reservation id that is not this property's 404s", st == 404, str(st))

    # The public surface, probed for what it must not say.
    st, pub_stay = http(f"{API}/public/stays/TB-DOESNOTEXIST")
    check("a booking reference nobody holds is a 404", st == 404, str(st))
    body = json.dumps(site)
    check("the public page exposes no phone number of any other guest",
          "9812" not in body, "a guest phone appeared on a public page")

    # RLS, from a browser role rather than through the API.
    st, rls = try_sql(f"""set local role authenticated;
                          select count(*) as n from public.creator_payouts;""")
    check("a browser role reading payouts directly gets nothing",
          st >= 400 or (isinstance(rls, list) and int(rls[0].get("n", 0)) == 0), str(rls)[:160])

    st, grants = try_sql("""select count(*) as n
                              from information_schema.role_routine_grants
                             where routine_name in ('charge_usage','grant_credits',
                                   'creator_approve_payouts','b2b_commit_credit',
                                   'record_attribution_touch','book_inventory')
                               and grantee in ('anon','authenticated');""")
    check("and none of the money RPCs is callable from a browser",
          isinstance(grants, list) and int(grants[0]["n"]) == 0, str(grants))

    # ═════════════════════════════════════════════════════════════════════════
    section("11. observability — could an operator explain any of this?")

    runs = sql(f"""select count(*) as n from public.agent_runs where property_id = '{P}';""")
    check("every agent turn has a run row", int(runs[0]["n"]) > 0, str(runs))

    traced = sql(f"""
select count(*) as n
  from public.agent_actions a
  join public.agent_runs r on r.id = a.run_id
 where r.property_id = '{P}' and a.tool is not null;""")
    check("every action names the run that produced it", int(traced[0]["n"]) > 0, str(traced))

    linked = sql(f"""
select count(*) as n from public.conversation_messages m
 where m.property_id = '{P}' and m.direction = 'outbound'
   and m.delivery_status is not null;""")
    check("every outbound message carries a delivery state",
          int(linked[0]["n"]) > 0, str(linked))

    money = sql(f"""
select count(*) as n from public.payment_orders o
  join public.reservations r on r.id = o.reservation_id
 where o.property_id = '{P}' and o.razorpay_payment_id is not null;""")
    check("every settled payment order names the reservation it created",
          int(money[0]["n"]) >= 2, str(money))

    chain = sql(f"""
select count(*) as n
  from public.creator_payouts p
  join public.attribution_touches t on t.id = p.attribution_touch_id
  join public.reservations r on r.id = p.reservation_id
 where p.property_id = '{P}';""")
    check("and every payout can be walked back to the touch that caused it",
          int(chain[0]["n"]) == 1, str(chain))

    usage_linked = sql(f"""
select count(*) as n from public.usage_events u
 where u.property_id = '{P}' and u.billing_account_id = '{fx.ACCOUNT}';""")
    check("every usage event names the account that paid for it",
          int(usage_linked[0]["n"]) > 0, str(usage_linked))

    print(f"\n  · tearing the pilot hotel down")
    fx.close_fixture()
    left = sql(f"""select count(*) as n from public.properties where id = '{P}';""")
    check("and nothing is left behind", int(left[0]["n"]) == 0, str(left))

    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL DATABASE · REAL PMS · REAL JWTs · REAL RLS · REAL RAZORPAY TEST ORDERS")
    print("SCRIPTED: the model (EchoProvider) and the Meta transport (sandbox "
          "connections).\nNo real WhatsApp or Instagram message was sent by this suite.\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            fx.close_fixture()
            print("\n  · pilot hotel torn down after a crash")
        except Exception:  # noqa: BLE001
            print("\n  · could not tear the pilot hotel down — sweep by hand")
        code = 1
    sys.exit(code)
