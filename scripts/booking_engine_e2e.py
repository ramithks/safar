#!/usr/bin/env python3
"""
The direct booking loop, end to end, against a real database and real Razorpay.

    publish site ──► /stay/[slug] ──► availability ──► quote ──► order
                                                                   │
                     PMS ◄── reservation ◄── confirm / webhook ◄────┘

**What is real here.** The property, the website row, `stay_options`,
`create_quote`, the inventory hold, `create_reservation`, the folio, RLS, the
credit ledger and **Razorpay order creation** are the live hotel-OS project and
the live Razorpay account. What is simulated is the *guest paying*: nobody can
type a card into a test script, so the payment signature is computed with the
same HMAC Razorpay computes it with, using the key secret this server already
holds. That is the honest boundary and it is stated again in the summary — the
signature check, the webhook verification and the convergence logic are all
exercised for real; a card actually being charged is not.

The assertions that matter are the ones about money happening once:

  * a webhook arriving before the browser, after it, and twice
  * two confirmations of the same payment
  * a quote used twice
  * the last room sold to two people at the same moment

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/booking_engine_e2e.py

⚠ Creates real Razorpay *orders* (never a payment) and real reservations under
`TB-BE…`, and sweeps the reservations on the way out.
"""
import base64
import concurrent.futures
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
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"

PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SLUG = "casa-mandala-anjuna"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PREFIX = "TB-BE"

TAG = uuid.uuid4().hex[:6].upper()

raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def http(url, method="GET", data=None, headers=None, raw_body=None):
    body = raw_body if raw_body is not None else (
        json.dumps(data).encode() if data is not None else None
    )
    req = urllib.request.Request(
        url, method=method, data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text[:300]
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
                 {"email": email, "password": "hotel-os-dev"}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


# ── the two signatures Razorpay would send ──────────────────────────────────
#
# Computed with the same secret and the same algorithm the server verifies
# with. This does not weaken the check — it exercises it. A wrong signature is
# asserted to be refused a few scenarios below, which is the half that proves
# the verification is doing anything at all.

def payment_signature(order_id: str, payment_id: str) -> str:
    return hmac.new(
        (settings.razorpay_key_secret or "").encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def webhook_body(event: str, order_id: str, payment_id: str) -> tuple[bytes, str]:
    payload = json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }).encode()
    signature = hmac.new(
        (settings.razorpay_webhook_secret or "").encode(), payload, hashlib.sha256,
    ).hexdigest()
    return payload, signature


def sweep():
    sql(f"""
delete from public.payment_orders
 where quote_id in (select id from public.quotes where property_id = '{PROP}'
                     and issued_by = 'booking_engine');
delete from public.reservations
 where property_id = '{PROP}' and ref like '{PREFIX}%';""")


def main() -> int:
    print("\n══ booking engine e2e ══\n")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1
    if not (settings.razorpay_key_secret and settings.razorpay_webhook_secret):
        print("  Razorpay is not configured — this suite cannot run")
        return 1

    manager = signin("priya@casamandala.in")     # rates → the website builder
    reception = signin("deepak@casamandala.in")
    if not manager or not reception:
        print("  could not sign in")
        return 1

    check_in = date.today() + timedelta(days=120)
    check_out = check_in + timedelta(days=2)

    # ── the website has to be published before any of this answers ───────────
    print("── an unpublished hotel is a 404, not a hidden page ──")
    sql(f"""update public.hotel_websites set status = 'draft'
             where property_id = '{PROP}';""")
    st, _ = http(f"{API}/public/hotels/{SLUG}")
    check("an unpublished slug 404s", st == 404, str(st))
    st, _ = http(f"{API}/public/hotels/{SLUG}/availability"
                 f"?from={check_in}&to={check_out}")
    check("and so does its availability", st == 404, str(st))

    st, draft = http(f"{API}/properties/{PROP}/website", headers=manager)
    check("the builder reads a draft", st == 200 and draft["status"] == "draft",
          f"{st} {str(draft)[:160]}")
    st, saved = http(f"{API}/properties/{PROP}/website", "PATCH", {
        "template": "beach", "headline": f"Casa Mandala {TAG}",
        "tagline": "Book direct", "accentColor": "#0F766E",
    }, manager)
    check("a save changes the draft", st == 200 and saved["headline"].endswith(TAG),
          f"{st} {str(saved)[:160]}")
    st, _ = http(f"{API}/public/hotels/{SLUG}")
    check("saving a draft does NOT make the site public", st == 404, str(st))

    st, published = http(f"{API}/properties/{PROP}/website/publish", "POST", {}, manager)
    check("publishing works", st == 200 and published["status"] == "published",
          f"{st} {str(published)[:160]}")

    st, site = http(f"{API}/public/hotels/{SLUG}")
    check("the public page answers once published", st == 200, f"{st} {str(site)[:160]}")
    check("it serves the published headline",
          st == 200 and site["content"]["headline"].endswith(TAG),
          str(site.get("content"))[:160])
    check("it carries the hotel's rooms and services from the live PMS",
          st == 200 and site["roomTypes"] and site["services"] is not None,
          str(len(site.get("roomTypes", []))))
    check("and no internal identifiers",
          PROP not in json.dumps(site) and PARTNER not in json.dumps(site),
          "an internal id reached the public page")

    # The draft moves on; the published copy must not.
    http(f"{API}/properties/{PROP}/website", "PATCH",
         {"headline": "EDITED AFTER PUBLISH"}, manager)
    st, still = http(f"{API}/public/hotels/{SLUG}")
    check("editing the draft leaves the published page alone",
          st == 200 and still["content"]["headline"].endswith(TAG),
          str(still.get("content", {}).get("headline")))
    st, cfg = http(f"{API}/properties/{PROP}/website", headers=manager)
    check("and the builder is told there are unpublished changes",
          st == 200 and cfg["hasUnpublishedChanges"] is True, str(cfg.get("hasUnpublishedChanges")))
    # Put it back so the rest of the run reads a sane page.
    http(f"{API}/properties/{PROP}/website", "PATCH",
         {"headline": f"Casa Mandala {TAG}"}, manager)

    st, _ = http(f"{API}/properties/{PROP}/website", "PATCH", {"headline": "x"}, reception)
    check("a receptionist cannot rewrite the hotel's home page", st == 403, str(st))

    # ── availability and quote ───────────────────────────────────────────────
    print("\n── availability and price come from the PMS, not from here ──")
    st, avail = http(f"{API}/public/hotels/{SLUG}/availability"
                     f"?from={check_in}&to={check_out}&units=1&adults=2")
    check("availability answers", st == 200, f"{st} {str(avail)[:200]}")
    check("with rooms", st == 200 and avail["rooms"], "nothing bookable 120 days out")
    if not (st == 200 and avail["rooms"]):
        print("  cannot continue without availability")
        return 1

    room = avail["rooms"][0]
    plan = room["ratePlans"][0]
    check("no B2B rate plan is offered to the public",
          all(p.get("audience") in (None, "public") for r in avail["rooms"] for p in r["ratePlans"]),
          "a contracted rate reached a stranger")

    st, _ = http(f"{API}/public/hotels/{SLUG}/availability"
                 f"?from={check_out}&to={check_in}")
    check("check-out before check-in is refused", st == 400, str(st))
    st, _ = http(f"{API}/public/hotels/{SLUG}/availability"
                 f"?from={date.today() - timedelta(days=5)}&to={date.today()}")
    check("a date in the past is refused", st == 400, str(st))

    st, quote = http(f"{API}/public/hotels/{SLUG}/quote", "POST", {
        "checkIn": str(check_in), "checkOut": str(check_out),
        "ratePlanId": plan["ratePlanId"], "units": 1, "adults": 2, "children": 0,
    })
    check("a quote is issued", st == 200, f"{st} {str(quote)[:200]}")
    check("it carries the server's own arithmetic",
          st == 200 and quote["totalPaise"] == quote["grossPaise"] - quote["discountPaise"] + quote["taxPaise"],
          str(quote)[:200])
    check("and an expiry", st == 200 and quote.get("expiresAt"), str(quote.get("expiresAt")))

    quote_row = sql(f"select * from public.quotes where id = '{quote['quoteId']}';")[0]
    check("the quote holds stock", bool(quote_row.get("hold_id")), str(quote_row.get("hold_id")))
    check("the quote's total matches what the guest was shown",
          quote_row["total_paise"] == quote["totalPaise"],
          f"{quote_row['total_paise']} vs {quote['totalPaise']}")

    # ── add-ons ──────────────────────────────────────────────────────────────
    print("\n── add-ons are priced by the server ──")
    services = sql(f"""select id, name, price_paise from public.services
                        where property_id = '{PROP}' and is_active order by sort_order limit 1;""")
    service = services[0] if services else None
    check("the hotel has a service to sell", service is not None)

    guest = {"fullName": f"Booking Engine {TAG}", "email": "guest@example.com",
             "phone": "+919812300011"}

    if service:
        # Price tampering: the client sends a price, and it is not a field.
        st, order = http(f"{API}/public/hotels/{SLUG}/quotes/{quote['quoteId']}/order", "POST", {
            "guest": guest,
            "addons": [{"serviceId": service["id"], "quantity": 2,
                        "unitPricePaise": 1, "totalPaise": 1, "pricePaise": 1}],
        })
        check("an order is created", st == 200, f"{st} {str(order)[:250]}")
        if st == 200:
            check("the add-on is priced from the hotel's own list, not the request",
                  order["addonsTotalPaise"] == service["price_paise"] * 2,
                  f"{order['addonsTotalPaise']} vs {service['price_paise'] * 2}")
            check("the amount charged is the room token plus the extras",
                  order["amountPaise"] == order["roomTokenPaise"] + order["addonsTotalPaise"],
                  str(order)[:200])
            check("a real Razorpay order was created",
                  order["razorpayOrderId"].startswith("order_"),
                  str(order.get("razorpayOrderId")))
            check("and the browser is given only the public key id",
                  order["keyId"] and not order["keyId"].startswith("rzp_test_secret"),
                  "a secret reached the browser")

    st, _ = http(f"{API}/public/hotels/{SLUG}/quotes/{quote['quoteId']}/order", "POST", {
        "guest": guest, "addons": [{"serviceId": str(uuid.uuid4()), "quantity": 1}],
    })
    check("a service that is not this hotel's is refused", st == 400, str(st))
    if service:
        st, _ = http(f"{API}/public/hotels/{SLUG}/quotes/{quote['quoteId']}/order", "POST", {
            "guest": guest,
            "addons": [{"serviceId": service["id"], "quantity": 1,
                        "serviceDate": str(check_out + timedelta(days=3))}],
        })
        check("an extra for a date outside the stay is refused", st == 400, str(st))
        st, _ = http(f"{API}/public/hotels/{SLUG}/quotes/{quote['quoteId']}/order", "POST", {
            "guest": guest, "addons": [{"serviceId": service["id"], "quantity": 0}],
        })
        check("a zero quantity is refused", st == 422, str(st))

    # ── payment: the browser path ────────────────────────────────────────────
    print("\n── one payment, one booking ──")
    st, order = http(f"{API}/public/hotels/{SLUG}/quotes/{quote['quoteId']}/order", "POST",
                     {"guest": guest, "addons": (
                         [{"serviceId": service["id"], "quantity": 1}] if service else [])})
    check("an order for the real booking", st == 200, f"{st} {str(order)[:200]}")
    if st != 200:
        sweep()
        return 1

    rzp_order = order["razorpayOrderId"]
    payment_id = f"pay_TEST{uuid.uuid4().hex[:14]}"

    st, _ = http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": "deadbeef",
    })
    check("a forged signature is refused", st == 400, str(st))
    check("and nothing was booked",
          not sql(f"select id from public.reservations where quote_id = '{quote['quoteId']}';"))

    signature = payment_signature(rzp_order, payment_id)
    st, booked = http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": signature,
    })
    check("a valid signature books the stay", st == 200, f"{st} {str(booked)[:250]}")
    if st != 200:
        sweep()
        return 1

    ref = booked["ref"]
    check("the confirmation carries a booking reference", bool(ref), str(booked)[:200])
    check("and links to web check-in", booked["checkInUrl"] == f"/checkin/{ref}",
          str(booked.get("checkInUrl")))
    check("and no internal identifiers",
          PROP not in json.dumps(booked) and "reservationId" not in json.dumps(booked))

    rows = sql(f"select * from public.reservations where quote_id = '{quote['quoteId']}';")
    check("exactly one reservation exists", len(rows) == 1, str(len(rows)))
    reservation = rows[0]
    check("it is confirmed", reservation["status"] == "confirmed", reservation["status"])
    check("it was created by the booking engine",
          reservation["created_by"] == "booking_engine", reservation["created_by"])
    check("what the guest paid is on the reservation",
          reservation["paid_paise"] == order["amountPaise"],
          f"{reservation['paid_paise']} vs {order['amountPaise']}")

    # Rename it into this suite's sweep prefix now that it exists.
    sql(f"""update public.reservations set ref = '{PREFIX}{TAG}'
             where id = '{reservation["id"]}';""")
    ref = f"{PREFIX}{TAG}"

    check("the quote was consumed",
          sql(f"select status from public.quotes where id = '{quote['quoteId']}';")[0]["status"]
          == "converted")

    if service:
        addons = sql(f"""select * from public.reservation_services
                          where reservation_id = '{reservation["id"]}';""")
        check("the add-on is attached to the reservation", len(addons) == 1, str(len(addons)))
        check("at the hotel's price, frozen on the row",
              addons and addons[0]["unit_price_paise"] == service["price_paise"],
              str(addons[:1])[:200])
        check("and it produced a folio line",
              addons and addons[0].get("folio_entry_id"), str(addons[:1])[:200])

    # ── the second confirmation, and the webhook ─────────────────────────────
    st, again = http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": signature,
    })
    check("confirming the same payment twice returns the same booking",
          st == 200 and again["ref"] == ref, f"{st} {str(again)[:160]}")
    check("and did not create a second reservation",
          len(sql(f"select id from public.reservations where quote_id = '{quote['quoteId']}';")) == 1)

    body, sig = webhook_body("payment.captured", rzp_order, payment_id)
    st, hook = http(f"{API}/public/razorpay/webhook", "POST",
                    raw_body=body, headers={"X-Razorpay-Signature": sig})
    check("a webhook for an already-settled order is a no-op",
          st == 200 and hook.get("handled") == "already settled", f"{st} {hook}")
    check("still exactly one reservation",
          len(sql(f"select id from public.reservations where quote_id = '{quote['quoteId']}';")) == 1)

    st, _ = http(f"{API}/public/razorpay/webhook", "POST",
                 raw_body=body, headers={"X-Razorpay-Signature": "nope"})
    check("an unsigned webhook is refused", st == 400, str(st))

    # ── the webhook arriving FIRST ───────────────────────────────────────────
    print("\n── the webhook wins the race ──")
    st, avail2 = http(f"{API}/public/hotels/{SLUG}/availability"
                      f"?from={check_in}&to={check_out}&units=1&adults=2")
    plan2 = avail2["rooms"][0]["ratePlans"][0] if st == 200 and avail2["rooms"] else None
    if plan2:
        st, quote2 = http(f"{API}/public/hotels/{SLUG}/quote", "POST", {
            "checkIn": str(check_in), "checkOut": str(check_out),
            "ratePlanId": plan2["ratePlanId"], "units": 1, "adults": 2,
        })
        st, order2 = http(f"{API}/public/hotels/{SLUG}/quotes/{quote2['quoteId']}/order",
                          "POST", {"guest": guest})
        pay2 = f"pay_TEST{uuid.uuid4().hex[:14]}"
        body2, sig2 = webhook_body("payment.captured", order2["razorpayOrderId"], pay2)

        st, hook2 = http(f"{API}/public/razorpay/webhook", "POST",
                         raw_body=body2, headers={"X-Razorpay-Signature": sig2})
        check("the webhook books it with no browser involved",
              st == 200 and hook2.get("handled") == "booking", f"{st} {hook2}")
        made = sql(f"select * from public.reservations where quote_id = '{quote2['quoteId']}';")
        check("one reservation from the webhook alone", len(made) == 1, str(len(made)))

        st, late = http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
            "razorpayOrderId": order2["razorpayOrderId"], "razorpayPaymentId": pay2,
            "razorpaySignature": payment_signature(order2["razorpayOrderId"], pay2),
        })
        check("the browser arriving late gets the same booking back",
              st == 200 and made and late.get("ref") == made[0]["ref"],
              f"{st} {str(late)[:160]}")
        check("and still one reservation",
              len(sql(f"select id from public.reservations where quote_id = '{quote2['quoteId']}';")) == 1)

        # A duplicate webhook delivery, twice more.
        for _ in range(2):
            http(f"{API}/public/razorpay/webhook", "POST",
                 raw_body=body2, headers={"X-Razorpay-Signature": sig2})
        check("three deliveries of the same webhook produce one reservation",
              len(sql(f"select id from public.reservations where quote_id = '{quote2['quoteId']}';")) == 1)

        if made:
            sql(f"""update public.reservations set ref = '{PREFIX}{TAG}W'
                     where id = '{made[0]["id"]}';""")

    # ── an expired quote, and a consumed one ─────────────────────────────────
    print("\n── a stale price cannot be paid ──")
    st, quote3 = http(f"{API}/public/hotels/{SLUG}/quote", "POST", {
        "checkIn": str(check_in), "checkOut": str(check_out),
        "ratePlanId": plan["ratePlanId"], "units": 1, "adults": 2,
    })
    if st == 200:
        sql(f"""update public.quotes set expires_at = now() - interval '1 minute'
                 where id = '{quote3["quoteId"]}';""")
        st, _ = http(f"{API}/public/hotels/{SLUG}/quotes/{quote3['quoteId']}/order",
                     "POST", {"guest": guest})
        check("an expired quote cannot start a payment", st == 409, str(st))

    st, _ = http(f"{API}/public/hotels/{SLUG}/quotes/{quote['quoteId']}/order",
                 "POST", {"guest": guest})
    check("a quote already converted cannot start another payment", st == 409, str(st))

    # ── the last room, sold twice at once ────────────────────────────────────
    print("\n── two guests, one room, at the same moment ──")
    far_in = date.today() + timedelta(days=200)
    far_out = far_in + timedelta(days=1)
    sql(f"""
insert into public.room_inventory
  (property_id, partner_id, room_type_id, date, units_total, units_booked)
values ('{PROP}', '{PARTNER}', '10000000-0000-4000-8000-000000000003',
        date '{far_in}', 1, 0)
on conflict (room_type_id, date) do update set units_total = 1, units_booked = 0;""")

    st, avail3 = http(f"{API}/public/hotels/{SLUG}/availability"
                      f"?from={far_in}&to={far_out}&units=1&adults=2")
    villa = next((r for r in (avail3.get("rooms") or [])
                  if r["roomTypeId"] == "10000000-0000-4000-8000-000000000003"), None) \
        if st == 200 else None
    if villa and villa["ratePlans"]:
        rp = villa["ratePlans"][0]["ratePlanId"]
        quotes = []
        for _ in range(2):
            s, q = http(f"{API}/public/hotels/{SLUG}/quote", "POST", {
                "checkIn": str(far_in), "checkOut": str(far_out),
                "ratePlanId": rp, "units": 1, "adults": 2,
            })
            quotes.append((s, q))
        held = [q for s, q in quotes if s == 200]
        check("the hold means the second guest cannot even get a price",
              len(held) == 1, f"{[s for s, _ in quotes]}")

        if len(held) == 1:
            s, o = http(f"{API}/public/hotels/{SLUG}/quotes/{held[0]['quoteId']}/order",
                        "POST", {"guest": guest})
            if s == 200:
                pid = f"pay_TEST{uuid.uuid4().hex[:14]}"
                sg = payment_signature(o["razorpayOrderId"], pid)

                def confirm():
                    return http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
                        "razorpayOrderId": o["razorpayOrderId"],
                        "razorpayPaymentId": pid, "razorpaySignature": sg,
                    })

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = [f.result() for f in
                               [pool.submit(confirm), pool.submit(confirm)]]
                booked_now = sql(f"""select id from public.reservations
                                      where quote_id = '{held[0]["quoteId"]}';""")
                check("two simultaneous confirmations create one reservation",
                      len(booked_now) == 1,
                      f"{len(booked_now)} — statuses {[r[0] for r in results]}")
                if booked_now:
                    sql(f"""update public.reservations set ref = '{PREFIX}{TAG}R'
                             where id = '{booked_now[0]["id"]}';""")
                left = sql(f"""select units_total - units_booked as n from public.room_inventory
                                where room_type_id = '10000000-0000-4000-8000-000000000003'
                                  and date = date '{far_in}';""")
                check("and the room is decremented exactly once",
                      left and int(left[0]["n"]) == 0, str(left))

    # ── it is a reservation like any other ───────────────────────────────────
    print("\n── the PMS sees it ──")
    st, listed = http(f"{API}/properties/{PROP}/reservations?search={ref}", headers=reception)
    check("the booking appears in the PMS",
          st == 200 and any(r["ref"] == ref for r in listed), f"{st} {len(listed) if st == 200 else listed}")
    st, stay = http(f"{API}/public/stays/{ref}")
    check("and the guest can open it for web check-in", st == 200, str(st))
    check("web check-in still masks the phone",
          st == 200 and "9812300011" not in json.dumps(stay), "the phone leaked")

    sweep()
    print(f"\n  · swept {PREFIX}… reservations and their orders")
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL DATABASE · REAL PMS SERVICES · REAL RAZORPAY ORDERS · "
          "SIMULATED CARD (signatures computed with the live secret)\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")
    sys.exit(main())
