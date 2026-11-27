#!/usr/bin/env python3
"""
Web check-in — the only unauthenticated write in the PMS.

    guest's phone ──► GET  /pms/public/stays/{ref}        masked
                 ──► POST /pms/public/stays/{ref}/check-in
                                    │
                                    └──► guest_registrations ──► the desk

Everything else in this API resolves a member from a JWT before the handler
runs. These two routes do not, because the person using them is a guest who has
never had an account. The booking reference is the entire credential.

**So almost every assertion here is about what the reference does not buy.** It
must not return an email or a full phone number. It must not reach another
hotel's booking. It must not check anybody in — that is the desk's `check_in`,
which assigns a room and opens a folio. And the full ID number a guest types
must not exist anywhere afterwards: migration 19 keeps four characters and the
endpoint discards the rest before the row is written, so the assertion is that
the database cannot produce it.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/checkin_e2e.py

⚠ Seeds one stay on Casa Mandala under its own reference prefix and sweeps it.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
# Which StaySuite to test. Defaults to the uvicorn each suite's docstring tells
# you to start; set STAYSUITE_API_BASE=https://api.staysuite.example.com to run
# the same assertions against the deployed one, which is the only way to find
# out whether what is live behaves like what is on this branch.
API = os.environ.get("STAYSUITE_API_BASE", "http://127.0.0.1:8010").rstrip("/") + "/api/v1/pms"

PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOM_TYPE = "10000000-0000-4000-8000-000000000001"
RATE_PLAN = "20000000-0000-4000-8000-000000000003"
# The Somewhere Else property, under a different partner — the cross-property
# negative for the authenticated read.
OTHER_PROPERTY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

PREFIX = "TB-WCI"
TAG = uuid.uuid4().hex[:6].upper()
STAY_REF = f"{PREFIX}{TAG}"
CANCELLED_REF = f"{PREFIX}{TAG}X"
# A booking that has not started: a deposit on the folio and no charges yet.
# This is the one that separates "what the folio sums" from "what the guest
# owes", and without it both answers happen to agree.
FUTURE_REF = f"{PREFIX}{TAG}F"

# What a guest types. Only the last four may survive anywhere.
FULL_ID = "P7742199831"

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


def http(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
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
                 {"email": email, "password": "hotel-os-dev"}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


def sweep():
    sql(f"""delete from public.reservations
             where property_id = '{PROP}' and ref like '{PREFIX}%';""")


def seed():
    """
    Three stays: one to check into, one cancelled to prove it stays hidden, and
    one that has not started, whose folio holds only the deposit.
    """
    check_in = date.today() + timedelta(days=40)
    check_out = check_in + timedelta(days=2)
    rows = []
    for ref, status in ((STAY_REF, "confirmed"), (CANCELLED_REF, "cancelled"),
                        (FUTURE_REF, "confirmed")):
        rows.append(f"""
insert into public.reservations
  (ref, property_id, partner_id, room_type_id, rate_plan_id, guest_name,
   guest_phone, guest_email, check_in, check_out, nights, units, adults,
   children, gross_paise, discount_paise, tax_paise, total_paise, paid_paise,
   source, status, created_by)
values ('{ref}', '{PROP}', '{PARTNER}', '{ROOM_TYPE}', '{RATE_PLAN}',
        'Web Checkin Guest', '+919812345678', 'private@example.com',
        date '{check_in.isoformat()}', date '{check_out.isoformat()}',
        2, 1, 2, 0, 800000, 0, 96000, 896000, 200000,
        'direct', '{status}', 'import');""")
    sql("\n".join(rows))

    # `folio_balance` sums posted entries, not the reservation's columns — so a
    # stay with an empty folio has a zero balance however large its total is.
    # The room charge and the token are what make the number a guest is shown
    # real, and posting them here is what the night audit and check-in do.
    sql(f"""
insert into public.folio_entries
  (reservation_id, property_id, partner_id, kind, description, amount_paise,
   posted_by_name, idempotency_key)
select r.id, '{PROP}', '{PARTNER}', v.kind, v.label, v.amount,
       'Web checkin e2e', 'wci:{TAG}:' || v.kind
  from public.reservations r,
       (values ('room', 'Room charge', 896000),
               ('payment', 'Booking token', -200000)) as v(kind, label, amount)
 where r.ref = '{STAY_REF}';""")

    # The future stay gets the deposit and nothing else — exactly the shape a
    # real booking has between paying and arriving.
    sql(f"""
insert into public.folio_entries
  (reservation_id, property_id, partner_id, kind, description, amount_paise,
   posted_by_name, idempotency_key)
select r.id, '{PROP}', '{PARTNER}', 'payment', 'Booking token', -200000,
       'Web checkin e2e', 'wci:{TAG}:future'
  from public.reservations r
 where r.ref = '{FUTURE_REF}';""")


def main() -> int:
    print("\n══ web check-in e2e ══\n")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    reception = signin("deepak@casamandala.in")
    housekeeping = signin("anita@casamandala.in")
    outsider = signin("nobody@example.com")
    if not all((reception, housekeeping, outsider)):
        print("  could not sign in as the dev accounts")
        return 1

    sweep()
    seed()
    print(f"  · seeded {STAY_REF} (confirmed) and {CANCELLED_REF} (cancelled)\n")

    stay_id = sql(f"select id from public.reservations where ref = '{STAY_REF}';")[0]["id"]

    # ── the public read ──────────────────────────────────────────────────────
    print("── the reference buys a masked view, and nothing else ──")
    st, stay = http(f"{API}/public/stays/{STAY_REF}")
    check("a guest reads their own booking with no account", st == 200,
          f"{st} {str(stay)[:200]}")
    check("it names the hotel and the stay",
          st == 200 and stay["propertyName"] and stay["nights"] == 2,
          str(stay)[:200])

    blob = json.dumps(stay) if st == 200 else ""
    check("the phone is masked to four digits",
          st == 200 and stay["guestPhoneMasked"].endswith("5678")
          and "9812345678" not in blob,
          str(stay.get("guestPhoneMasked")))
    check("the email is not returned at all",
          "private@example.com" not in blob,
          "the guest's email reached an unauthenticated response")
    check("no internal id is returned",
          stay_id not in blob and "propertyId" not in blob,
          "an internal identifier reached an unauthenticated response")
    check("the balance is there, because the guest will be asked for it",
          st == 200 and stay["balancePaise"] > 0, str(stay.get("balancePaise")))

    st, _ = http(f"{API}/public/stays/{PREFIX}NOTAREF")
    check("a reference nobody recognises is 404", st == 404, str(st))
    st, _ = http(f"{API}/public/stays/{CANCELLED_REF}")
    check("a cancelled booking answers exactly the same way",
          st == 404, f"{st} — a stranger learned that this booking exists")

    # Case-insensitive, because a guest retypes a reference off an email.
    st, lower = http(f"{API}/public/stays/{STAY_REF.lower()}")
    check("a lowercased reference still works", st == 200, str(st))

    # ── the public write ─────────────────────────────────────────────────────
    print("\n── the submission ──")
    st, reg = http(f"{API}/public/stays/{STAY_REF}/check-in", "POST", {
        "fullName": "Web Checkin Guest",
        "email": "guest@example.com",
        "phone": "+919812345678",
        "address": "1 Beach Road", "city": "Anjuna", "country": "India",
        "idType": "passport", "idNumber": FULL_ID,
        "travellingWith": 1, "estimatedArrival": "19:30",
        "specialRequests": "Arriving late",
    })
    check("a guest files their details", st == 200, f"{st} {str(reg)[:200]}")
    check("only four characters of the document come back",
          st == 200 and reg["idLast4"] == FULL_ID[-4:], str(reg.get("idLast4")))
    check("the arrival time survives as a time",
          st == 200 and reg["estimatedArrival"] == "19:30",
          str(reg.get("estimatedArrival")))

    # The assertion this table exists for.
    stored = sql(f"""select * from public.guest_registrations
                      where reservation_id = '{stay_id}';""")
    check("exactly one registration row was written", len(stored) == 1, str(len(stored)))
    row_blob = json.dumps(stored, default=str)
    check("the full ID number is nowhere in the database",
          FULL_ID not in row_blob,
          "the document number a guest typed was stored")
    check("and there is no column that could hold it",
          not any(k in row_blob for k in ("id_number", "id_encrypted")),
          str(list(stored[0]))[:200])
    check("it is stamped as a guest's own submission",
          stored[0]["source"] == "web", str(stored[0].get("source")))

    # Re-submission amends rather than stacks.
    st, again = http(f"{API}/public/stays/{STAY_REF}/check-in", "POST", {
        "fullName": "Web Checkin Guest",
        "email": "guest@example.com", "phone": "+919812345678",
        "address": "2 Beach Road", "city": "Anjuna", "country": "India",
        "idType": "passport", "idNumber": FULL_ID,
        "travellingWith": 2, "estimatedArrival": "21:00",
    })
    check("correcting a typo amends the same row", st == 200, str(st))
    rows = sql(f"""select address, travelling_with from public.guest_registrations
                    where reservation_id = '{stay_id}';""")
    check("there is still only one registration", len(rows) == 1, str(len(rows)))
    check("and it carries the correction",
          rows and rows[0]["address"] == "2 Beach Road" and rows[0]["travelling_with"] == 2,
          str(rows))

    # It records; it does not check anybody in.
    status_now = sql(f"select status from public.reservations where id = '{stay_id}';")
    check("the stay has NOT been checked in — that is the desk's decision",
          status_now[0]["status"] == "confirmed", str(status_now))

    st, _ = http(f"{API}/public/stays/{CANCELLED_REF}/check-in", "POST", {
        "fullName": "Nobody", "idNumber": "X",
    })
    check("a cancelled booking cannot be registered against", st == 404, str(st))
    st, _ = http(f"{API}/public/stays/{STAY_REF}/check-in", "POST", {"fullName": "X"})
    check("a one-character name is refused", st == 422, str(st))
    st, _ = http(f"{API}/public/stays/{STAY_REF}/check-in", "POST", {
        "fullName": "Web Checkin Guest", "estimatedArrival": "half past seven",
    })
    check("a malformed arrival time is refused rather than dropped",
          st == 422, str(st))
    st, _ = http(f"{API}/public/stays/{STAY_REF}/check-in", "POST", {
        "fullName": "Web Checkin Guest", "idType": "library_card",
    })
    check("an id type outside the six is refused", st == 422, str(st))

    # There is no field pointing anywhere else.
    st, elsewhere = http(f"{API}/public/stays/{STAY_REF}/check-in", "POST", {
        # A complete payload, as the real form sends. A re-submission is the
        # record, so one that omitted fields would legitimately clear them —
        # asserting on a stripped-down body would test the fixture, not the
        # scoping this case is about.
        "fullName": "Web Checkin Guest",
        "email": "guest@example.com", "phone": "+919812345678",
        "address": "2 Beach Road", "city": "Anjuna", "country": "India",
        "idType": "passport", "idNumber": FULL_ID,
        "travellingWith": 2, "estimatedArrival": "21:00",
        # Neither of these is a field on the request model. If either were
        # honoured, this registration would land on somebody else's booking.
        "reservationId": str(uuid.uuid4()), "propertyId": OTHER_PROPERTY,
    })
    check("extra ids in the body are ignored, not honoured", st == 200, str(st))
    still = sql(f"""select count(*) as n from public.guest_registrations
                     where reservation_id = '{stay_id}';""")
    check("the registration still belongs to the stay in the path",
          int(still[0]["n"]) == 1, str(still))

    # ── the desk's read ──────────────────────────────────────────────────────
    print("\n── what the desk sees ──")
    st, desk = http(f"{API}/properties/{PROP}/reservations/{stay_id}/registration",
                    headers=reception)
    check("the desk reads the registration", st == 200, f"{st} {str(desk)[:200]}")
    check("and still only four characters of the document",
          st == 200 and desk["idLast4"] == FULL_ID[-4:] and FULL_ID not in json.dumps(desk),
          str(desk.get("idLast4")))
    st, _ = http(f"{API}/properties/{PROP}/reservations/{stay_id}/registration",
                 headers=housekeeping)
    check("a housekeeper does not", st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/reservations/{stay_id}/registration",
                 headers=outsider)
    check("another partner gets 404", st == 404, str(st))
    st, _ = http(f"{API}/properties/{OTHER_PROPERTY}/reservations/{stay_id}/registration",
                 headers=reception)
    check("and the registration cannot be read through another property's path",
          st in (403, 404), str(st))
    st, _ = http(f"{API}/properties/{PROP}/reservations/{uuid.uuid4()}/registration",
                 headers=reception)
    check("a stay nobody registered for is 404, not an empty object",
          st == 404, str(st))

    # ── the booking page ─────────────────────────────────────────────────────
    #
    # The other public view of the same reservation, and the one whose URL is a
    # credential rather than a reference somebody reads aloud.
    print("\n── the link, which is a secret ──")
    tokens = sql(f"""select ref, public_token from public.reservations
                      where ref in ('{STAY_REF}', '{CANCELLED_REF}',
                                    '{FUTURE_REF}');""")
    by_ref = {r["ref"]: r["public_token"] for r in tokens}
    token = by_ref[STAY_REF]

    check("every reservation was issued a token",
          all(len(t or "") == 32 for t in by_ref.values()), str(by_ref))
    check("and no two bookings share one",
          len(set(by_ref.values())) == len(by_ref), str(by_ref))

    st, page = http(f"{API}/public/bookings/{token}")
    check("the link opens the booking", st == 200, f"{st} {str(page)[:200]}")
    check("with the price broken down, which the reference view does not show",
          st == 200 and page["grossPaise"] > 0 and page["taxPaise"] > 0,
          str(page)[:200])
    check("and the folio, line by line",
          st == 200 and len(page["receipt"]) >= 2,
          str(page.get("receipt"))[:200])

    # The number this page exists to get right.
    #
    # On a stay whose room charges have been posted, the folio and the
    # reservation happen to agree, so this alone would prove nothing. The
    # discriminating case is `FUTURE_REF` below: a booking with a deposit on
    # the folio and no charges yet, where `folio_balance` returns *minus* the
    # deposit and the guest owes the rest of the total.
    check("an in-house folio's own sum is what is owed",
          st == 200 and page["balancePaise"] == 896000 - 200000,
          str(page.get("balancePaise")))

    st, future = http(f"{API}/public/bookings/{by_ref[FUTURE_REF]}")
    check("a booking that has not started owes the rest of its total, "
          "not minus its deposit",
          st == 200 and future["balancePaise"] == 696000,
          f"{future.get('balancePaise')} — the old answer here was -200000, "
          f"which the check-in page rendered as nothing at all")
    st, ref_view = http(f"{API}/public/stays/{FUTURE_REF}")
    check("and the check-in page is told the same number, not zero",
          st == 200 and ref_view["balancePaise"] == 696000,
          str(ref_view.get("balancePaise")))

    blob = json.dumps(page) if st == 200 else ""
    check("the guest's own document details are not on the page they forward",
          FULL_ID not in blob and FULL_ID[-4:] not in blob
          and "guest@example.com" not in blob,
          "a registration detail reached the booking page")
    check("only that they filed them, and when",
          st == 200 and page.get("registrationSubmittedAt"),
          str(page.get("registrationSubmittedAt")))
    check("the page does not reprint its own credential",
          token not in blob, "the booking page echoed its token back")
    check("no internal id either", stay_id not in blob and "propertyId" not in blob,
          "an internal identifier reached the booking page")

    # A cancelled booking behaves *differently* here on purpose.
    st, killed = http(f"{API}/public/bookings/{by_ref[CANCELLED_REF]}")
    check("a cancelled booking opens rather than 404s, because this caller "
          "holds its own link",
          st == 200 and killed["status"] == "cancelled", f"{st} {str(killed)[:120]}")
    check("and it is not presented as owing anything",
          st == 200 and killed["balancePaise"] == 0, str(killed.get("balancePaise")))

    st, _ = http(f"{API}/public/bookings/{'0' * 32}")
    check("a token nobody was issued is 404", st == 404, str(st))
    st, _ = http(f"{API}/public/bookings/{token.upper()}")
    check("and the token is not case-insensitive the way a reference is",
          st == 404, str(st))
    st, _ = http(f"{API}/public/bookings/{'%' * 32}")
    # 400 as well as 404: behind Coolify the edge rejects a malformed
    # percent-escape before FastAPI sees it, where a bare uvicorn hands it to
    # the handler. Both refuse — pinning this to 404 failed against production
    # for doing the stricter thing. See tenant_isolation_e2e for the wider set.
    check("a wildcard is not a pattern here", st in (400, 404), str(st))
    st, _ = http(f"{API}/public/bookings/{STAY_REF}")
    check("the reference does not open the booking page", st == 404, str(st))

    # ── revocation ───────────────────────────────────────────────────────────
    print("\n── and it can be taken back ──")
    st, rotated = http(f"{API}/properties/{PROP}/reservations/{stay_id}/rotate-link",
                       "POST", {}, headers=reception)
    check("the desk can reissue the link", st == 200, f"{st} {str(rotated)[:160]}")
    fresh = (rotated or {}).get("url", "").rsplit("/", 1)[-1]
    check("which is a different token", st == 200 and fresh and fresh != token,
          f"{fresh} vs {token}")

    st, _ = http(f"{API}/public/bookings/{token}")
    check("the forwarded link stops working from that moment", st == 404, str(st))
    st, _ = http(f"{API}/public/bookings/{fresh}")
    check("and the new one works", st == 200, str(st))

    st, _ = http(f"{API}/properties/{PROP}/reservations/{stay_id}/rotate-link",
                 "POST", {}, headers=housekeeping)
    check("a housekeeper cannot reissue somebody's booking link",
          st == 403, str(st))
    st, _ = http(f"{API}/properties/{OTHER_PROPERTY}/reservations/{stay_id}/rotate-link",
                 "POST", {}, headers=reception)
    check("and it cannot be rotated through another property's path",
          st in (403, 404), str(st))

    sweep()
    print(f"\n  · swept {PREFIX}… — the registration went with the stay")
    left = sql(f"""select count(*) as n from public.guest_registrations
                    where reservation_id = '{stay_id}';""")
    check("deleting the stay took its registration with it",
          int(left[0]["n"]) == 0, str(left))

    print(f"\n══ {PASS} passed, {FAIL} failed ══\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
