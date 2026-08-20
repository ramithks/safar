#!/usr/bin/env python3
"""
The whole journey, once, on the demo hotel.

    discovery ─► question ─► availability ─► quote ─► booking ─► payment link
        ─► paid ─► confirmation ─► web check-in ─► arrival ─► request ─► task
        ─► staff ─► upsell ─► checkout ─► owner asks how the day went

Every other suite in this directory proves one surface deeply. This one proves
they **join up**, which is a different claim and the only one that matters the
day a hotel starts using the product: a guest does not experience a booking
engine and a task queue, they experience one conversation that has to keep
working from "do you have a room" to "here's your bill".

So it runs in order, and a step that fails does not stop the ones after it —
the point is a map of where the journey breaks, not the first place it breaks.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/golden_e2e.py

⚠ Runs entirely on **Anandi House Udaipur**, the demo hotel, and writes one
booking under its own reference prefix. The three real pilot hotels are never
read and never written; `scripts/demo_hotel.py` is what built the hotel this
uses, and there is an assertion below that this is still the case.
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"

HOTEL_SLUG = "anandi-house-udaipur"
OWNER_EMAIL = "owner@anandihouse.demo"
RECEPTION_EMAIL = "reception@anandihouse.demo"
PASSWORD = "AnandiDemo!2026"

# Never touched. Asserted, not merely intended.
PILOTS = ("Erin Villa Coonoor", "Hotel Lotus", "Your Perfect NATURE RETREAT")

TAG = uuid.uuid4().hex[:6].upper()
# A phone number of this run's own. Every run used to share one, which meant
# the second booking correctly matched the *first* run's guest record and kept
# its name -- the product being right and the suite reading it as a break.
GUEST_PHONE = "9199" + str(abs(hash(TAG)))[:8]

raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()

PASS = FAIL = 0
STEP = 0


def section(title: str) -> None:
    global STEP
    STEP += 1
    print(f"\n── {STEP}. {title} " + "─" * max(0, 58 - len(title)))


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {str(detail)[:220]}")
    return bool(cond)


def http(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


def sql(query: str):
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    if st not in (200, 201):
        raise RuntimeError(f"SQL failed: {st} {str(body)[:300]}")
    return body


keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
            headers={"Authorization": f"Bearer {MGMT}",
                     "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in keys if k.get("name") == "anon"][0]


def signin(email):
    st, d = http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                 {"email": email, "password": PASSWORD}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


def main() -> int:
    st, _ = http("http://127.0.0.1:8010/api/v1/health")
    if st != 200:
        print("The API is not running on :8010. Start uvicorn first.")
        return 2

    state: dict = {}

    # ── 1. the hotel a guest can find ────────────────────────────────────────
    section("a guest finds the hotel")

    st, site = http(f"{API}/public/hotels/{HOTEL_SLUG}")
    ok = check("the hotel's own website answers", st == 200, f"{st} {site}")
    if not ok:
        print("\nThe demo hotel is not published. Run scripts/demo_hotel.py first.")
        return 2

    check("it names the hotel, not a template", site.get("name") == "Anandi House Udaipur",
          site.get("name"))
    check("rooms are on it", len(site.get("roomTypes") or []) >= 3,
          len(site.get("roomTypes") or []))
    check("so are the services it sells", len(site.get("services") or []) >= 6,
          len(site.get("services") or []))
    # The brochure carries no prices or counts on purpose -- those depend on
    # dates, and a "from" price shown next to "0 left" is worse than neither.
    # What it must not do is claim availability it has not checked.
    check("it claims no availability without dates",
          all((rt.get("unitsLeft") or 0) == 0 and not rt.get("ratePlans")
              for rt in site["roomTypes"]),
          [rt["name"] for rt in site["roomTypes"] if rt.get("ratePlans")])

    prop_id = sql(f"select id from properties where slug = '{HOTEL_SLUG}';")[0]["id"]
    state["property"] = prop_id

    # ── 2. what the AI employee knows ────────────────────────────────────────
    section("the AI employee has the hotel's own answers")

    knowledge = sql(f"""select category, answer from property_knowledge
                         where property_id = '{prop_id}' and is_active;""")
    have = {k["category"] for k in knowledge}
    check("check-in time is written down, not guessed", "checkin" in have, have)
    check("so is the cancellation policy", "cancellation" in have, have)
    check("and what there is to do locally", "local" in have, have)
    check("the answers are the hotel's words, not a model's",
          any("2:00 PM" in (k["answer"] or "") for k in knowledge),
          [k["answer"][:40] for k in knowledge][:3])

    agents = sql(f"""select a.agent_type, count(t.tool_name) tools
                       from agents a left join agent_tools t on t.agent_id = a.id
                      where a.property_id = '{prop_id}' group by 1;""")
    guest = next((a for a in agents if a["agent_type"] == "guest"), None)
    check("a guest agent exists", guest is not None, agents)
    if guest:
        check("it can take a payment, check a guest in and sell an extra",
              int(guest["tools"]) >= 22, guest)

    tools = {t["tool_name"] for t in sql(
        f"""select t.tool_name from agent_tools t join agents a on a.id = t.agent_id
             where a.property_id = '{prop_id}' and a.agent_type = 'guest';""")}
    for name in ("create_payment_link", "get_check_in_link", "order_service",
                 "search_availability", "create_task", "request_human_handoff"):
        check(f"  · {name}", name in tools)

    # ── 3. availability and a price ──────────────────────────────────────────
    section("the guest asks for a room, and is quoted")

    # A different fortnight for every run. A quote holds inventory for fifteen
    # minutes, so two runs inside that window on the same dates make the second
    # one fail on a 409 that says nothing is available -- which is the booking
    # engine working correctly and the suite reading it as a break.
    arrive = date.today() + timedelta(days=14 + int(TAG, 16) % 60)
    depart = arrive + timedelta(days=2)
    print(f"       this run books {arrive} → {depart}")
    st, avail = http(f"{API}/public/hotels/{HOTEL_SLUG}/availability"
                     f"?from={arrive}&to={depart}&units=1&adults=2")
    check("availability answers for a real date range", st == 200, f"{st} {avail}")
    options = [o for o in (avail.get("rooms") or []) if o.get("ratePlans")]
    check("at least one room is sellable", len(options) >= 1, avail)

    if not options:
        return report()

    room = options[0]
    plan = room["ratePlans"][0]
    st, quote = http(f"{API}/public/hotels/{HOTEL_SLUG}/quote", "POST", {
        "checkIn": str(arrive), "checkOut": str(depart), "units": 1,
        "adults": 2, "children": 0,
        "roomTypeId": room["roomTypeId"], "ratePlanId": plan["ratePlanId"],
    })
    check("a quote is priced by the server", st == 200 and quote.get("quoteId"),
          f"{st} {quote}")
    if st != 200:
        return report()

    # The room price the site showed is the quote's *gross*. Tax is added on
    # top and broken out, which is what a guest needs in order to not feel
    # ambushed at checkout -- and is the assertion worth making: the total must
    # be the room price plus a tax the guest can see, never a bigger number
    # with no explanation.
    check("the room price is the one the site showed",
          quote["grossPaise"] == plan["totalPaise"],
          f"{quote['grossPaise']} vs {plan['totalPaise']}")
    check("tax is shown separately, and adds up",
          quote["grossPaise"] - quote["discountPaise"] + quote["taxPaise"]
          == quote["totalPaise"], quote)
    check("the deposit asked for is less than the whole stay",
          0 < quote["tokenPaise"] < quote["totalPaise"], quote)
    check("the quote expires", bool(quote.get("expiresAt")), quote)
    state["quote"] = quote

    # ── 4. the booking, and the money ────────────────────────────────────────
    section("the booking is made and paid for")

    guest_name = f"E2E Guest {TAG}"

    # Booked through the front desk rather than the payment path, because this
    # is the case the payment link exists for: a stay that is already real.
    desk = signin(RECEPTION_EMAIL)
    check("the receptionist can sign in", desk is not None)
    if not desk:
        return report()

    st, made = http(f"{API}/properties/{prop_id}/quotes", "POST", {
        "checkIn": str(arrive), "checkOut": str(depart), "units": 1,
        "adults": 2, "children": 0,
        "roomTypeId": room["roomTypeId"], "ratePlanId": plan["ratePlanId"],
    }, desk)
    check("the desk can price the same stay", st == 200, f"{st} {made}")
    check("and its quote holds the room", st == 200 and bool(made.get("holdId")), made)
    if st != 200:
        return report()

    st, booking = http(f"{API}/properties/{prop_id}/reservations", "POST", {
        "quoteId": made["id"], "guestName": guest_name,
        "guestPhone": GUEST_PHONE, "source": "whatsapp",
    }, desk)
    check("a reservation is created", st in (200, 201) and booking.get("ref"),
          f"{st} {booking}")
    if st not in (200, 201):
        return report()

    state["reservation"] = booking["id"]
    state["ref"] = booking["ref"]
    print(f"       reference {booking['ref']}")

    check("it starts unpaid", (booking.get("paidPaise") or 0) == 0, booking)

    # The payment link the AI employee would send.
    order = sql(f"""select razorpay_order_id, amount_paise, purpose, status
                      from payment_orders where reservation_id = '{booking['id']}';""")
    check("no payment has been taken yet", order == [], order)

    st, paylink = http(f"{API}/properties/{prop_id}/reservations/"
                       f"{booking['id']}/payment-link", "POST", {}, desk)
    check("the desk can raise a payment link", st in (200, 201), f"{st} {paylink}")
    if st in (200, 201):
        check("it asks for exactly what is owed",
              paylink["amountPaise"] == paylink["balancePaise"] > 0, paylink)
        check("it points at the hotel's own page, not a bare checkout",
              paylink["payUrl"].endswith(f"/pay/{paylink['orderId']}"), paylink)

        st2, again = http(f"{API}/properties/{prop_id}/reservations/"
                          f"{booking['id']}/payment-link", "POST", {}, desk)
        check("asking twice gives the same link, not two live orders",
              st2 in (200, 201) and again["orderId"] == paylink["orderId"],
              f"{paylink.get('orderId')} vs {again.get('orderId')}")

        open_orders = sql(f"""select count(*) n from payment_orders
                               where reservation_id = '{booking['id']}'
                                 and status = 'created';""")
        check("exactly one open order exists for the stay",
              int(open_orders[0]["n"]) == 1, open_orders)

        st3, over = http(f"{API}/properties/{prop_id}/reservations/"
                         f"{booking['id']}/payment-link", "POST",
                         {"amountRupees": 999_999}, desk)
        check("a link can never ask for more than the balance",
              st3 in (200, 201) and over["amountPaise"] <= paylink["balancePaise"],
              f"{st3} {over}")

        st4, anon = http(f"{API}/public/payments/{paylink['orderId']}")
        check("the guest can open it without signing in", st4 == 200, f"{st4} {anon}")
        if st4 == 200:
            check("the page names the hotel and the booking",
                  anon["hotelName"] == "Anandi House Udaipur"
                  and anon["ref"] == booking["ref"], anon)
            check("and leaks nothing else about the guest",
                  GUEST_PHONE not in json.dumps(anon)
                  and "@" not in json.dumps(anon), anon)

        st5, forged = http(f"{API}/public/payments/{paylink['orderId']}/confirm",
                           "POST", {"razorpayOrderId": paylink["orderId"],
                                    "razorpayPaymentId": "pay_FAKE",
                                    "razorpaySignature": "0" * 64})
        check("a forged payment signature is refused", st5 == 400, f"{st5} {forged}")

        still = sql(f"""select paid_paise from reservations
                         where id = '{booking['id']}';""")[0]
        check("and the stay is still unpaid after the attempt",
              int(still["paid_paise"]) == 0, still)

    # ── 5. what a guest cannot do ────────────────────────────────────────────
    section("saying 'I have paid' does not pay")

    balance_before = sql(f"""select paid_paise, total_paise from reservations
                              where id = '{booking['id']}';""")[0]
    check("the stay is genuinely unpaid",
          int(balance_before["paid_paise"]) == 0, balance_before)
    check("and its total is real money", int(balance_before["total_paise"]) > 0,
          balance_before)

    # There must be no tool, and no route, that settles a folio on a claim.
    settling_tools = sql("""
        select distinct t.tool_name from agent_tools t
          join agents a on a.id = t.agent_id
         where a.agent_type = 'guest'
           and t.tool_name in ('post_folio_charge','check_in','check_out');""")
    check("the guest agent cannot touch a folio or check anybody in",
          settling_tools == [], settling_tools)

    # ── 6. web check-in ──────────────────────────────────────────────────────
    section("the guest checks in online, before arriving")

    st, stay = http(f"{API}/public/stays/{booking['ref']}")
    check("the reference opens their own booking", st == 200, f"{st} {stay}")
    if st == 200:
        check("it shows the hotel and the dates",
              stay.get("propertyName") == "Anandi House Udaipur"
              and stay.get("checkIn") == str(arrive), stay)
        check("the phone number is masked",
              GUEST_PHONE not in json.dumps(stay), stay.get("guestPhoneMasked"))

    st, reg = http(f"{API}/public/stays/{booking['ref']}/check-in", "POST", {
        "fullName": guest_name, "email": "e2e@anandihouse.demo",
        "phone": GUEST_PHONE, "address": "12 MG Road, Bengaluru",
        "city": "Bengaluru", "country": "India",
        "idType": "passport", "idNumber": "P7742199831",
        "travellingWith": 1, "estimatedArrival": "15:30",
    })
    check("the registration is accepted", st in (200, 201), f"{st} {reg}")

    stored = sql(f"""select id_last4, full_name from guest_registrations
                      where reservation_id = '{booking['id']}';""")
    check("the registration reached the desk", len(stored) == 1, stored)
    if stored:
        check("only the last four of the ID survive",
              stored[0]["id_last4"] == "9831", stored[0])
    kept = sql(f"""select count(*) n from guest_registrations
                    where reservation_id = '{booking['id']}'
                      and id_last4 like '%7742%';""")
    check("the full ID number is nowhere in the database",
          int(kept[0]["n"]) == 0, kept)

    check("web check-in did not check them in",
          sql(f"select status from reservations where id = '{booking['id']}';")[0]["status"]
          != "checked_in")

    # ── 7. arrival, and a request during the stay ────────────────────────────
    section("the guest arrives and asks for something")

    st, tasks_before = http(f"{API}/properties/{prop_id}/tasks", headers=desk)
    check("the desk can read the task list", st == 200, st)

    # The id comes from the client. That is the offline-first contract: a
    # receptionist on a dead lift who taps "raise ticket" twice must not
    # produce two tickets when the connection comes back.
    task_id = str(uuid.uuid4())
    st, task = http(f"{API}/properties/{prop_id}/tasks", "POST", {
        "id": task_id,
        "category": "housekeeping", "title": f"Two extra towels — room for {TAG}",
        "priority": "normal", "guestName": guest_name, "sourceChannel": "whatsapp",
        "createdBy": "agent",
    }, desk)
    check("a guest request becomes a task", st in (200, 201) and task.get("id"),
          f"{st} {task}")

    if task.get("id"):
        state["task"] = task["id"]
        check("it is numbered so staff can refer to it", bool(task.get("code")), task)
        st, dupe = http(f"{API}/properties/{prop_id}/tasks", "POST", {
            "id": task_id,
            "category": "housekeeping", "title": f"Two extra towels — room for {TAG}",
            "priority": "normal", "guestName": guest_name, "sourceChannel": "whatsapp",
            "createdBy": "agent",
        }, desk)
        raised = sql(f"""select count(*) n from tasks
                          where property_id = '{prop_id}' and id = '{task_id}';""")
        check("the same request twice raises one ticket",
              int(raised[0]["n"]) == 1, raised)

        st, moved = http(f"{API}/properties/{prop_id}/tasks/{task['id']}/status",
                         "POST", {"status": "in_progress"}, desk)
        check("staff can pick it up", st == 200, f"{st} {moved}")
        st, done = http(f"{API}/properties/{prop_id}/tasks/{task['id']}/status",
                        "POST", {"status": "resolved"}, desk)
        check("and finish it", st == 200 and done.get("status") == "resolved",
              f"{st} {done}")
        st, bogus = http(f"{API}/properties/{prop_id}/tasks/{task['id']}/status",
                         "POST", {"status": "teleported"}, desk)
        check("an invented status is refused, not stored", bogus_refused(st, bogus),
              f"{st} {bogus}")

        events = sql(f"""select count(*) n from task_events where task_id = '{task['id']}';""")
        check("every move is on the record", int(events[0]["n"]) >= 2, events)

    # ── 8. an upsell ─────────────────────────────────────────────────────────
    section("the guest buys something else")

    services = sql(f"""select id, name, price_paise from services
                        where property_id = '{prop_id}' and category = 'transfer'
                        order by sort_order limit 1;""")
    check("the hotel sells an airport transfer", len(services) == 1, services)

    if services:
        before = sql(f"""select coalesce(sum(amount_paise),0) t from folio_entries
                          where reservation_id = '{booking['id']}';""")[0]["t"]
        st, folio = http(f"{API}/properties/{prop_id}/reservations/"
                         f"{booking['id']}/folio", "POST", {
                             "kind": "addon",
                             "description": services[0]["name"],
                             "amountPaise": services[0]["price_paise"],
                             "idempotencyKey": f"e2e-addon-{TAG}",
                         }, desk)
        check("it goes on the guest's bill", st in (200, 201), f"{st} {folio}")

        after = sql(f"""select coalesce(sum(amount_paise),0) t from folio_entries
                         where reservation_id = '{booking['id']}';""")[0]["t"]
        check("the bill moved by exactly the price",
              int(after) - int(before) == int(services[0]["price_paise"]),
              f"{before} -> {after}")

        st, again = http(f"{API}/properties/{prop_id}/reservations/"
                         f"{booking['id']}/folio", "POST", {
                             "kind": "addon",
                             "description": services[0]["name"],
                             "amountPaise": services[0]["price_paise"],
                             "idempotencyKey": f"e2e-addon-{TAG}",
                         }, desk)
        repeat = sql(f"""select coalesce(sum(amount_paise),0) t from folio_entries
                          where reservation_id = '{booking['id']}';""")[0]["t"]
        check("charging twice with the same key charges once",
              int(repeat) == int(after), f"{after} -> {repeat}")

    # ── 9. arrival, departure, and the bill ──────────────────────────────────
    section("the guest arrives, stays, and settles up")

    st, rooms = http(f"{API}/properties/{prop_id}/rooms", headers=desk)
    free = [r for r in (rooms or [])
            if r.get("roomTypeId") == room["roomTypeId"]
            and (r.get("status") or "vacant") == "vacant"]
    check("there is a clean room of the type they booked", len(free) >= 1,
          f"{len(rooms or [])} rooms, none free of {room['name']}")

    if free:
        st, arrived = http(f"{API}/properties/{prop_id}/reservations/"
                           f"{booking['id']}/check-in", "POST",
                           {"roomId": free[0]["id"]}, desk)
        check("the desk can check them in", st == 200
              and arrived.get("status") == "checked_in", f"{st} {arrived}")

        # A stay cannot be closed with money outstanding. That refusal is the
        # feature -- a guest walking out on an unpaid folio is the failure the
        # check exists for -- so the balance is settled first, the way a desk
        # would take cash.
        st, early = http(f"{API}/properties/{prop_id}/reservations/"
                         f"{booking['id']}/check-out", "POST", {}, desk)
        check("checking out with a balance owing is refused", st == 409,
              f"{st} {early}")

        st, folio = http(f"{API}/properties/{prop_id}/reservations/"
                         f"{booking['id']}/folio", headers=desk)
        owed = folio.get("balancePaise") if isinstance(folio, dict) else None
        if owed is None:
            owed = sum(e["amountPaise"] for e in (folio.get("entries") or []))
        check("the folio says exactly what is owed", owed > 0, folio)

        http(f"{API}/properties/{prop_id}/reservations/{booking['id']}/folio",
             "POST", {"kind": "payment", "description": "Settled at the desk",
                      "amountPaise": owed, "method": "cash",
                      "idempotencyKey": f"e2e-settle-{TAG}"}, desk)

        st, left = http(f"{API}/properties/{prop_id}/reservations/"
                        f"{booking['id']}/check-out", "POST", {}, desk)
        check("and once settled they can check out", st == 200
              and left.get("status") == "checked_out", f"{st} {left}")

        dirty = sql(f"""select housekeeping_status from rooms
                         where id = '{free[0]["id"]}';""")
        check("the room they left is queued for housekeeping",
              dirty and dirty[0]["housekeeping_status"] == "dirty", dirty)

        # The loyalty loop. Until a departure counts, `stays_count` is zero for
        # everybody and the repeat-guest segment can never match anybody.
        counted = sql(f"""select g.stays_count, g.total_spend_paise, g.last_stay_at
                            from guests g join reservations r on r.guest_id = g.id
                           where r.id = '{booking['id']}';""")
        check("the stay is counted against the guest",
              counted and int(counted[0]["stays_count"]) >= 1, counted)
        check("and what they spent is on their record",
              counted and int(counted[0]["total_spend_paise"]) > 0, counted)
        check("with the date they left",
              counted and counted[0]["last_stay_at"] is not None, counted)

    # ── 10. the owner asks how the day went ──────────────────────────────────
    section("the owner can see it all")

    owner = signin(OWNER_EMAIL)
    check("the owner can sign in", owner is not None)

    if owner:
        st, overview = http(f"{API}/properties/{prop_id}/owner/overview", headers=owner)
        check("the owner overview answers", st == 200, f"{st} {overview}")

        st, today = http(f"{API}/properties/{prop_id}/today", headers=owner)
        check("today's board answers", st == 200, st)

        st, guests = http(f"{API}/properties/{prop_id}/guests"
                          f"?search={guest_name.replace(' ', '%20')}", headers=owner)
        check("the guest list finds this guest by name",
              st == 200 and any(guest_name in json.dumps(g) for g in (guests or [])),
              f"{st} {str(guests)[:200]}")

        # Book the same person again. A hotel that makes a second guest record
        # for a returning guest cannot answer "have they stayed before", which
        # is the question loyalty and post-stay both rest on.
        st, q2 = http(f"{API}/properties/{prop_id}/quotes", "POST", {
            "checkIn": str(arrive + timedelta(days=60)),
            "checkOut": str(arrive + timedelta(days=62)),
            "units": 1, "adults": 2, "children": 0,
            "roomTypeId": room["roomTypeId"], "ratePlanId": plan["ratePlanId"],
        }, desk)
        if st == 200:
            http(f"{API}/properties/{prop_id}/reservations", "POST", {
                "quoteId": q2["id"], "guestName": guest_name,
                "guestPhone": GUEST_PHONE, "source": "whatsapp",
            }, desk)
        people = sql(f"""select count(*) n from guests
                          where property_id = '{prop_id}' and phone = '{GUEST_PHONE}';""")
        check("a returning guest is one person, not two",
              int(people[0]["n"]) == 1, people)
        linked = sql(f"""select count(*) n from reservations
                          where property_id = '{prop_id}'
                            and guest_phone = '{GUEST_PHONE}' and guest_id is not null;""")
        check("both of their stays are linked to them",
              int(linked[0]["n"]) == 2, linked)

        st, res_list = http(f"{API}/properties/{prop_id}/reservations"
                            f"?search={booking['ref']}", headers=owner)
        check("and the booking is findable by its reference",
              st == 200 and json.dumps(res_list).count(booking["ref"]) >= 1,
              f"{st} {str(res_list)[:200]}")

        st, payments = http(f"{API}/properties/{prop_id}/payments", headers=owner)
        check("the payment history answers", st == 200, st)

    # ── 10. one hotel cannot read another ────────────────────────────────────
    section("nothing leaks between hotels")

    other = sql("""select id from properties
                    where name = 'Casa Mandala' limit 1;""")[0]["id"]
    st, _ = http(f"{API}/properties/{other}/today", headers=owner)
    check("the demo owner cannot open another hotel's day", st == 404, st)

    st, _ = http(f"{API}/properties/{other}/reservations", headers=desk)
    check("nor can its receptionist", st == 404, st)

    st, _ = http(f"{API}/properties/{prop_id}/today")
    check("and neither can a stranger with no token", st in (401, 403), st)

    # ── 11. the real hotels were not touched ─────────────────────────────────
    section("the pilot hotels are exactly as they were")

    names = "','".join(PILOTS)
    pilots = sql(f"""
        select p.name,
               (select count(*) from reservations r where r.property_id = p.id) res,
               (select count(*) from whatsapp_connections c where c.property_id = p.id) wa,
               (select count(*) from instagram_connections c where c.property_id = p.id) ig,
               (select count(*) from hotel_websites w
                 where w.property_id = p.id and w.status = 'published') pub
          from properties p where p.name in ('{names}');""")
    check("all three are present", len(pilots) == 3, pilots)
    for row in pilots:
        check(f"  · {row['name']}: no bookings, no channels, nothing published",
              int(row["res"]) == 0 and int(row["wa"]) == 0
              and int(row["ig"]) == 0 and int(row["pub"]) == 0, row)

    return report()


def bogus_refused(st, body) -> bool:
    return st == 400 and "status" in str(body).lower()


def report() -> int:
    print(f"\n╔═ {PASS} passed, {FAIL} failed")
    if FAIL:
        print("╚═ the journey does not join up yet")
    else:
        print("╚═ discovery → booking → payment → check-in → stay → bill → owner")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
