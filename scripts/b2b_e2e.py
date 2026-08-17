#!/usr/bin/env python3
"""
The B2B agent portal, end to end, against a real database.

    sign in as an agency ──► my contract ──► my hotels ──► availability
                                                              │
              /agency/bookings ◄── reservation ◄── quote ◄─────┘
                                        │
                              credit committed here, not at booking

**What is real here.** Everything. There is no payment provider in this
milestone and therefore nothing to simulate: the property, the rate plans, RLS,
`stay_options`, `create_quote`, the inventory hold, `b2b_commit_credit`,
`create_reservation`, the folio and the JWTs are all the live hotel-OS project.
The one place M5 had to draw a line — a card nobody can type into a script —
does not exist on this path, because an agency does not pay at checkout. It
books against a limit and settles on invoice, and settlement is a folio payment
the front desk posts, which this suite does for real.

The assertions that matter are the ones about the two things a rate sheet does
not have:

  * **the audience clamp** — an agency quoting a public rate plan id directly,
    which is what a compromised portal would do if it could read one
  * **the credit line holding under concurrency** — two quotes at once when only
    one fits, and the money released when a quote is refused or expires

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/b2b_e2e.py

⚠ Creates real reservations against Casa Mandala on the seeded agency's credit
line, and moves that agency's credit limit around. Both are swept on the way
out — by `b2b_agent_id` rather than by a ref prefix, because every row this
suite writes is tagged with the agency and nothing else on the property is.
"""
import base64
import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"

PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_PROP = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

AGENCY = "60000000-0000-4000-8000-000000000001"      # Himalaya Holidays, has a login
AGENCY_NO_LOGIN = "60000000-0000-4000-8000-000000000002"  # Coastline, does not
SEEDED_LIMIT = 5000000                                # what the seed says; restored on exit

DLX = "10000000-0000-4000-8000-000000000002"
B2B_PLAN = "20000000-0000-4000-8000-000000000004"    # DLX-B2B, 'Agent Rate'
PUBLIC_PLAN = "20000000-0000-4000-8000-000000000003"  # DLX-CP, the public one

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


def set_limit(paise: int):
    sql(f"update public.b2b_agents set credit_limit_paise = {paise} where id = '{AGENCY}';")


def committed(agency=AGENCY) -> int:
    return int(sql(f"select public.b2b_committed_paise('{agency}') as n;")[0]["n"])


def sweep():
    """
    Give the property back — the rooms as well as the rows.

    By `b2b_agent_id`, not by a ref prefix. Every reservation this suite writes
    carries the seeded agency and nothing else on Casa Mandala does, so this is
    strictly more precise than `TB-B2…` would be — and unlike a prefix it cannot
    be defeated by a booking whose ref the suite never got to rewrite because it
    died between the insert and the update.

    **Deleting a reservation does not return its stock.** `room_inventory
    .units_booked` is owned by the RPCs — `confirm_booking` adds to it,
    `release_booking` subtracts — and a `delete from reservations` goes past
    both. There is no trigger to catch it because the product has no path that
    deletes a reservation at all; only this suite does.

    So the counter only ever went up. Casa Mandala's deluxe rooms reached 16
    booked out of 16 on the two nights this suite always asks for (today + 200),
    against zero surviving reservations, and section 5 started failing with "no
    longer available in the number of rooms requested" — on a night nobody had
    booked. It passed on a fresh property and failed on replay, which is the
    shape of every idempotency bug in this directory.

    The release is computed from the reservations about to go, per night, and
    floored at zero. It runs in the same statement batch as the delete so a
    crash between them cannot leave the stock released and the stay standing.
    """
    sql(f"""
-- Return the stock first: once the rows are gone there is nothing left to
-- compute the release from.
update public.room_inventory ri
   set units_booked = greatest(ri.units_booked - claimed.units, 0),
       updated_at   = now()
  from (
    select r.room_type_id, night::date as date, sum(r.units)::int as units
      from public.reservations r
      cross join lateral generate_series(
        r.check_in, r.check_out - 1, interval '1 day') as night
     where r.b2b_agent_id = '{AGENCY}'
     group by r.room_type_id, night::date
  ) as claimed
 where ri.room_type_id = claimed.room_type_id
   and ri.date         = claimed.date;

delete from public.reservations where b2b_agent_id = '{AGENCY}';
delete from public.quotes
 where property_id = '{PROP}' and issued_by = 'agent_portal';
update public.b2b_agents
   set credit_limit_paise = {SEEDED_LIMIT}, status = 'active', property_ids = '{{}}'
 where id = '{AGENCY}';""")


def main() -> int:
    print("\n══ b2b agent portal e2e ══\n")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    agent = signin("farhan@himalayaholidays.in")
    reception = signin("deepak@casamandala.in")
    manager = signin("priya@casamandala.in")
    outsider = signin("nobody@example.com")
    if not (agent and reception and manager and outsider):
        print("  could not sign in — has bootstrap/01_seed_identities.sql been applied?")
        return 1

    sweep()

    check_in = date.today() + timedelta(days=200)
    check_out = check_in + timedelta(days=2)

    # ── 1. an agency is a principal that is not staff ────────────────────────
    print("── 1. who the caller is ──")

    st, me = http(f"{API}/agency/me", headers=agent)
    check("the agency reads its own contract", st == 200 and len(me["contracts"]) == 1,
          f"{st} {str(me)[:200]}")
    contract = me["contracts"][0] if st == 200 else {}
    check("with the code and commission the hotel agreed",
          contract.get("code") == "TB-AG-204" and contract.get("commissionPct") == 12,
          str(contract)[:160])
    check("and only its own — the other agency at the same hotel is not listed",
          "TB-AG-311" not in json.dumps(me), "another agency's contract leaked")

    st, _ = http(f"{API}/agency/me", headers=reception)
    check("a receptionist is not an agency", st == 403, str(st))
    st, _ = http(f"{API}/agency/me", headers=outsider)
    check("nor is a stranger with a valid account", st == 403, str(st))
    st, _ = http(f"{API}/agency/me")
    check("nor an unauthenticated caller", st in (401, 403), str(st))

    st, staff_me = http(f"{API}/me", headers=agent)
    check("the agency is a member of no hotel at all",
          st == 200 and staff_me["memberships"] == [], str(staff_me)[:160])
    st, _ = http(f"{API}/properties/{PROP}/reservations", headers=agent)
    check("so the PMS answers it with a 404, not a 403", st == 404, str(st))
    st, _ = http(f"{API}/properties/{PROP}/today", headers=agent)
    check("and it cannot read the hotel's day", st == 404, str(st))

    # ── 2. what it may sell ──────────────────────────────────────────────────
    print("\n── 2. the hotels on the contract ──")

    st, props = http(f"{API}/agency/properties", headers=agent)
    check("the portal lists the hotels the contract covers",
          st == 200 and any(p["id"] == PROP for p in props), f"{st} {str(props)[:200]}")
    check("each one carrying the contract that covers it",
          st == 200 and all(p["agencyId"] == AGENCY for p in props), str(props)[:200])
    check("and no hotel of the partner it has no contract with",
          st == 200 and not any(p["id"] == OTHER_PROP for p in props), str(props)[:200])

    st, _ = http(f"{API}/agency/properties/{OTHER_PROP}/availability"
                 f"?checkIn={check_in}&checkOut={check_out}", headers=agent)
    check("an uncontracted hotel 404s rather than 403s — no enumeration oracle",
          st == 404, str(st))

    # A contract that names its hotels does not cover the ones it omits.
    sql(f"""update public.b2b_agents
               set property_ids = array['{OTHER_PROP}']::uuid[]
             where id = '{AGENCY}';""")
    st, _ = http(f"{API}/agency/properties/{PROP}/availability"
                 f"?checkIn={check_in}&checkOut={check_out}", headers=agent)
    check("a contract scoped to other hotels does not cover this one", st == 404, str(st))
    st, scoped = http(f"{API}/agency/properties", headers=agent)
    check("and the scoped-out hotel leaves the list too",
          st == 200 and not any(p["id"] == PROP for p in scoped), str(scoped)[:160])
    sql(f"update public.b2b_agents set property_ids = '{{}}' where id = '{AGENCY}';")

    # ── 3. one calendar, two audiences ───────────────────────────────────────
    print("\n── 3. the same rooms, the agency's rates ──")

    st, options = http(f"{API}/agency/properties/{PROP}/availability"
                       f"?checkIn={check_in}&checkOut={check_out}", headers=agent)
    check("availability answers", st == 200 and options, f"{st} {str(options)[:200]}")
    plans = [p for o in options for p in o["ratePlans"]]
    check("every plan returned is an agent rate",
          plans and all(p["audience"] == "b2b" for p in plans),
          str(sorted({p["audience"] for p in plans})))
    check("the hotel's public rates are not in the response",
          "DLX-CP" not in json.dumps(options), "a public plan reached the agency")

    dlx = next((o for o in options if o["roomTypeId"] == DLX), None)
    check("the room type the contract covers is sellable", dlx is not None, str(options)[:160])

    st, staff_view = http(f"{API}/properties/{PROP}/availability"
                          f"?checkIn={check_in}&checkOut={check_out}", headers=reception)
    staff_dlx = next((o for o in staff_view if o["roomTypeId"] == DLX), None) if st == 200 else None
    check("and it is the same stock the front desk sees — one ARI calendar",
          dlx and staff_dlx and dlx["unitsLeft"] == staff_dlx["unitsLeft"],
          f"agency {dlx and dlx['unitsLeft']} vs desk {staff_dlx and staff_dlx['unitsLeft']}")

    units_before = dlx["unitsLeft"] if dlx else 0

    # ── 4. the audience clamp ────────────────────────────────────────────────
    print("\n── 4. a rate plan the agency was not offered ──")

    st, refused = http(f"{API}/agency/properties/{PROP}/quotes", "POST", {
        "ratePlanId": PUBLIC_PLAN, "checkIn": str(check_in), "checkOut": str(check_out),
        "units": 1, "adults": 2,
    }, agent)
    check("quoting a public rate plan id directly is refused", st == 404,
          f"{st} {str(refused)[:160]}")
    check("with the same answer as a plan that never existed — no cheaper-rate hint",
          st == 404 and "not for you" not in json.dumps(refused).lower(),
          str(refused)[:160])
    check("and nothing was held for it",
          committed() == 0, f"committed {committed()}")

    # ── 5. the quote, and the credit it takes ────────────────────────────────
    print("\n── 5. a quote holds the room and the money together ──")

    set_limit(90000000)
    st, quote = http(f"{API}/agency/properties/{PROP}/quotes", "POST", {
        "ratePlanId": B2B_PLAN, "checkIn": str(check_in), "checkOut": str(check_out),
        "units": 1, "adults": 2,
    }, agent)
    check("the agent rate prices", st == 200 and quote.get("totalPaise", 0) > 0,
          f"{st} {str(quote)[:200]}")
    check("the quote says the portal issued it",
          quote.get("issuedBy") == "agent_portal", str(quote.get("issuedBy")))
    check("nothing is collected now — an agency settles on invoice",
          quote.get("tokenPaise") == 0 and quote.get("balancePaise") == quote.get("totalPaise"),
          f"token {quote.get('tokenPaise')} balance {quote.get('balancePaise')}")

    hold = sql(f"""select source, status from public.inventory_holds
                    where id = '{quote["holdId"]}';""")
    check("the hold is real stock, taken by the b2b path",
          hold and hold[0]["source"] == "b2b" and hold[0]["status"] == "active", str(hold))

    st, after = http(f"{API}/agency/properties/{PROP}/availability"
                     f"?checkIn={check_in}&checkOut={check_out}", headers=agent)
    after_dlx = next((o for o in after if o["roomTypeId"] == DLX), None) if st == 200 else None
    check("and the room is off sale while the quote lives",
          after_dlx and after_dlx["unitsLeft"] == units_before - 1,
          f"{units_before} → {after_dlx and after_dlx['unitsLeft']}")

    check("the credit is committed at quote time, not at booking",
          committed() == quote["totalPaise"], f"{committed()} vs {quote['totalPaise']}")
    st, me2 = http(f"{API}/agency/me", headers=agent)
    check("and the portal is told the same figure the server refuses on",
          st == 200 and me2["contracts"][0]["committedPaise"] == committed(),
          f"{st} {str(me2)[:200]}")

    # ── 6. the limit ─────────────────────────────────────────────────────────
    print("\n── 6. the line, and what it refuses ──")

    # Exactly enough room for the live quote and nothing more.
    set_limit(quote["totalPaise"])
    st, over = http(f"{API}/agency/properties/{PROP}/quotes", "POST", {
        "ratePlanId": B2B_PLAN, "checkIn": str(check_in + timedelta(days=10)),
        "checkOut": str(check_out + timedelta(days=10)), "units": 1, "adults": 2,
    }, agent)
    check("a second quote past the limit is refused", st == 409, f"{st} {str(over)[:200]}")
    check("with a sentence an agent can act on",
          st == 409 and "credit line" in json.dumps(over), str(over)[:200])
    check("counting the live quote, not only what has been invoiced",
          committed() == quote["totalPaise"], f"{committed()}")

    far = check_in + timedelta(days=10)
    left = sql(f"""select units_held from public.room_inventory
                    where room_type_id = '{DLX}' and date = date '{far}';""")
    check("and the room the refused quote had held is given straight back",
          not left or int(left[0]["units_held"]) == 0, str(left))

    # Two at once, when only one fits. The reason `b2b_commit_credit` takes a
    # row lock rather than reading a total and hoping.
    set_limit(quote["totalPaise"] * 2)
    race_in = check_in + timedelta(days=20)

    def race(offset):
        return http(f"{API}/agency/properties/{PROP}/quotes", "POST", {
            "ratePlanId": B2B_PLAN,
            "checkIn": str(race_in + timedelta(days=offset * 3)),
            "checkOut": str(race_in + timedelta(days=offset * 3 + 2)),
            "units": 1, "adults": 2,
        }, agent)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(race, 0), pool.submit(race, 1)]]
    codes = sorted(s for s, _ in results)
    check("two simultaneous quotes, room on the line for one: exactly one wins",
          codes == [200, 409], f"{codes}")
    check("and the line is never over-committed",
          committed() <= quote["totalPaise"] * 2, f"{committed()}")

    # Clear the winner so the rest of the run reads a clean line.
    sql(f"""update public.quotes set status = 'superseded'
             where property_id = '{PROP}' and issued_by = 'agent_portal'
               and id <> '{quote["id"]}';""")

    set_limit(90000000)
    sql(f"update public.b2b_agents set status = 'suspended' where id = '{AGENCY}';")
    st, susp = http(f"{API}/agency/properties/{PROP}/quotes", "POST", {
        "ratePlanId": B2B_PLAN, "checkIn": str(check_in + timedelta(days=40)),
        "checkOut": str(check_in + timedelta(days=42)), "units": 1, "adults": 2,
    }, agent)
    check("a suspended agency cannot book", st == 403, f"{st} {str(susp)[:160]}")
    st, still = http(f"{API}/agency/me", headers=agent)
    check("but can still read what it owes — that is the list it settles from",
          st == 200, f"{st} {str(still)[:160]}")
    sql(f"update public.b2b_agents set status = 'active' where id = '{AGENCY}';")

    # ── 7. the booking ───────────────────────────────────────────────────────
    print("\n── 7. quote → reservation ──")

    st, other_quote = http(f"{API}/agency/properties/{PROP}/quotes", "POST", {
        "ratePlanId": B2B_PLAN, "checkIn": str(check_in + timedelta(days=60)),
        "checkOut": str(check_in + timedelta(days=62)), "units": 1, "adults": 2,
    }, agent)
    if st == 200:
        sql(f"""update public.quotes set b2b_agent_id = '{AGENCY_NO_LOGIN}'
                 where id = '{other_quote["id"]}';""")
        st, stolen = http(f"{API}/agency/properties/{PROP}/bookings", "POST", {
            "quoteId": other_quote["id"], "guestName": "Someone Else",
            "guestPhone": "+919812300099",
        }, agent)
        check("a quote committed to another agency is not bookable here",
              st == 404, f"{st} {str(stolen)[:160]}")
        sql(f"""update public.quotes set status = 'superseded'
                 where id = '{other_quote["id"]}';""")

    st, booking = http(f"{API}/agency/properties/{PROP}/bookings", "POST", {
        "quoteId": quote["id"], "guestName": "Ritu Malhotra",
        "guestPhone": "+919812300011", "guestEmail": "ritu@example.com",
        "notes": "Window room if possible",
    }, agent)
    check("the committed quote becomes a booking", st == 200 and booking.get("ref"),
          f"{st} {str(booking)[:200]}")
    ref = booking.get("ref", "")
    check("recorded as an agency sale", booking.get("source") == "b2b",
          str(booking.get("source")))
    check("with nothing paid and the whole total outstanding",
          booking.get("paidPaise") == 0 and booking.get("balancePaise") == quote["totalPaise"],
          f"paid {booking.get('paidPaise')} balance {booking.get('balancePaise')}")

    row = sql(f"""select b2b_agent_id, created_by, source from public.reservations
                   where ref = '{ref}';""")
    check("and billed to the agency the quote was committed to",
          row and row[0]["b2b_agent_id"] == AGENCY, str(row))
    check("attributed to the portal rather than to a member of staff",
          row and row[0]["created_by"] == "b2b", str(row))

    check("the line reads the same after conversion — the money moved, it did not double",
          committed() == quote["totalPaise"], f"{committed()} vs {quote['totalPaise']}")

    st, again = http(f"{API}/agency/properties/{PROP}/bookings", "POST", {
        "quoteId": quote["id"], "guestName": "Ritu Malhotra",
    }, agent)
    check("a double submit does not sell the room twice", st == 409, f"{st} {str(again)[:160]}")

    st, mine = http(f"{API}/agency/bookings", headers=agent)
    check("the booking is on the agency's list",
          st == 200 and any(b["ref"] == ref for b in mine), f"{st} {len(mine) if st == 200 else mine}")
    check("and the list is scoped by the token, not by anything the caller sent",
          st == 200 and all(b["source"] == "b2b" for b in mine), str(mine)[:160])

    # ── 8. the hotel's side ──────────────────────────────────────────────────
    print("\n── 8. it is a reservation like any other ──")

    st, listed = http(f"{API}/properties/{PROP}/reservations?search={ref}", headers=reception)
    check("the front desk sees it on the arrivals it will handle",
          st == 200 and any(r["ref"] == ref for r in listed),
          f"{st} {len(listed) if st == 200 else listed}")

    st, stay = http(f"{API}/public/stays/{ref}")
    check("and the agency's guest can web check in like anybody else", st == 200, str(st))

    # ── 9. settlement needed no new code ─────────────────────────────────────
    print("\n── 9. settling the invoice ──")

    reservation_id = booking["id"]
    part = quote["totalPaise"] // 2
    st, entry = http(f"{API}/properties/{PROP}/reservations/{reservation_id}/folio", "POST", {
        "kind": "payment", "description": "Agency settlement",
        "amountPaise": part, "method": "bank",
        "idempotencyKey": f"b2b-e2e-{reservation_id}",
    }, reception)
    check("the desk posts a payment on the folio, as it would for any guest",
          st == 200, f"{st} {str(entry)[:160]}")
    check("and the credit line falls by exactly that, with no other code involved",
          committed() == quote["totalPaise"] - part,
          f"{committed()} vs {quote['totalPaise'] - part}")

    st, bad = http(f"{API}/properties/{PROP}/reservations/{reservation_id}/folio", "POST", {
        "kind": "payment", "description": "Typo", "amountPaise": 100,
        "method": "bank_transfer", "idempotencyKey": f"b2b-e2e-bad-{reservation_id}",
    }, reception)
    check("a payment method outside the enumeration is a 422, not a 500",
          st == 422, f"{st} {str(bad)[:160]}")

    st, me3 = http(f"{API}/agency/me", headers=agent)
    check("the portal shows the agency the room it has just got back",
          st == 200 and me3["contracts"][0]["committedPaise"] == committed(),
          f"{st} {str(me3)[:200]}")

    sql(f"""update public.reservations set status = 'cancelled'
             where id = '{reservation_id}';""")
    check("and a cancelled booking leaves the line entirely",
          committed() == 0, f"{committed()}")

    # ── 10. who may read a contract ──────────────────────────────────────────
    print("\n── 10. the hotel's view of its agencies ──")

    seen = sql(f"""select count(*) as n from public.b2b_agents
                    where partner_id = '{PARTNER}';""")
    check("the hotel has both its agencies on file", int(seen[0]["n"]) == 2, str(seen))

    st, _ = http(f"{API}/agency/bookings", headers=manager)
    check("a manager is not an agency either — staff read agencies through the PMS",
          st == 403, str(st))

    sweep()
    print("\n  · swept this agency's bookings and quotes, and restored its limit")
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL DATABASE · REAL PMS SERVICES · REAL JWTs · NOTHING SIMULATED\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")
    sys.exit(main())
