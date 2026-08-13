#!/usr/bin/env python3
"""
The PMS vertical slice, end to end, against a real database.

    availability -> quote (with hold) -> booking -> check-in -> folio -> check-out

Everything the unit tests cannot reach: real ES256 JWTs from GoTrue, real RLS,
real stock RPCs under PostgREST, real check constraints. The pytest suite mocks
the database, so it can only prove the service layer agrees with itself — three
of the defects this script found (a 422 where a 401 belonged, free text going
into an enumerated column, no route behind listRooms) were invisible to it and
would have surfaced as a 500 at a front desk.

Needs the API running against the hotel-OS project:

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/pms_e2e.py

Reads the Supabase management token from the login keychain entry the CLI
writes, so there is no key in this file. It signs in as three different people
on purpose: a receptionist, a housekeeper who must be refused, and a member of
a different partner who must not see the property exists at all.

⚠ Writes real rows (a booking, a folio, a ticket) into whatever project
.env.hotelos points at. That project is disposable; production is not.
"""
import base64, json, random, subprocess, sys, urllib.request, urllib.error, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"
PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

raw = subprocess.check_output(["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()

def http(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:    return e.code, json.loads(body)
        except: return e.code, body[:400]

keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
            headers={"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in keys if k.get("name") == "anon"][0]

def sql(query: str):
    """
    Run SQL against the same project, through the management API.

    Used only to *age* rows — expiring a hold or a quote — so the tests that
    depend on time passing do not have to wait fifteen real minutes. Nothing
    here fakes an assertion: the expiry path itself is the production one.
    """
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    assert st in (200, 201), (st, body)
    return body


def signin(email):
    st, d = http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                 {"email": email, "password": "hotel-os-dev"}, {"apikey": ANON})
    assert st == 200, (st, d)
    return {"Authorization": "Bearer " + d["access_token"]}

PASS = FAIL = 0
def check(label, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {label}")
    else:    FAIL += 1; print(f"  FAIL {label}  {detail}")

reception   = signin("deepak@casamandala.in")    # frontdesk + folio + housekeeping + tasks
housekeeper = signin("anita@casamandala.in")     # housekeeping + tasks only
outsider    = signin("nobody@example.com")       # a different partner entirely
manager     = signin("priya@casamandala.in")     # + rates, which the desk deliberately lacks

# Every run books a real room and never gives it back, so a fixed window would
# quietly exhaust the room type after a dozen runs and the failures would look
# like inventory bugs. Each run gets its own week, far enough out that no
# screenshot or demo is looking at it.
RUN = random.randint(400, 1200)
ci = date.today() + timedelta(days=RUN)
co = ci + timedelta(days=2)

# Holds a previous run abandoned are expired rather than waited out. The expiry
# path exercised here is the production one; only the clock is hurried.
sql(f"""update public.inventory_holds set expires_at = now() - interval '1 minute'
        where property_id = '{PROP}' and status = 'active';""")
sql("select public.expire_holds();")

print("\n── availability ──")
st, avail = http(f"{API}/properties/{PROP}/availability?checkIn={ci}&checkOut={co}&units=1", headers=reception)
check("200 for a member", st == 200, f"{st} {avail}")
std = next((o for o in avail if o["roomTypeCode"] == "STD"), None)
check("STD offered with stock", std and std["unitsLeft"] > 0, str(std)[:120] if std else "no STD")
plan = std["ratePlans"][0]
check("STD carries a rate plan with a price", plan["totalPaise"] > 0, str(plan)[:160])
before_left = std["unitsLeft"]

print("\n── isolation ──")
st, _ = http(f"{API}/properties/{PROP}/availability?checkIn={ci}&checkOut={co}", headers=outsider)
check("another partner's staff gets 404, not 403", st == 404, f"got {st}")
st, _ = http(f"{API}/properties/{uuid.uuid4()}/availability?checkIn={ci}&checkOut={co}", headers=reception)
check("a guessed property id 404s", st == 404, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/availability?checkIn={ci}&checkOut={co}")
check("no token is refused with 401, not 422", st == 401, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/quotes", "POST", {
    "roomTypeId": std["roomTypeId"], "ratePlanId": plan["ratePlanId"],
    "checkIn": str(ci), "checkOut": str(co), "discountReason": "regular guest"}, reception)
check("free-text discount reason is refused at the edge, not 500", st == 422, f"got {st}")

print("\n── quote (with hold) ──")
st, quote = http(f"{API}/properties/{PROP}/quotes", "POST", {
    "roomTypeId": std["roomTypeId"], "ratePlanId": plan["ratePlanId"],
    "checkIn": str(ci), "checkOut": str(co), "units": 1, "adults": 2,
    "discountPct": 90, "discountReason": "negotiation",
}, reception)
check("quote issued", st == 200, f"{st} {quote}")
check("two nights priced", len(quote["nightly"]) == 2, str(quote.get("nightly"))[:120])
check("an over-request is clamped, not rejected", quote.get("clampedFromPct") == 90,
      f"clampedFromPct={quote.get('clampedFromPct')}")
check("nightly sums to gross", sum(n["pricePaise"] for n in quote["nightly"]) == quote["grossPaise"],
      f"{sum(n['pricePaise'] for n in quote['nightly'])} vs {quote['grossPaise']}")
check("total = gross - discount + tax",
      quote["totalPaise"] == quote["grossPaise"] - quote["discountPaise"] + quote["taxPaise"],
      str({k: quote[k] for k in ('grossPaise','discountPaise','taxPaise','totalPaise')}))
check("a hold was taken", bool(quote.get("holdId")), str(quote.get("holdId")))

st, avail2 = http(f"{API}/properties/{PROP}/availability?checkIn={ci}&checkOut={co}&units=1", headers=reception)
std2 = next(o for o in avail2 if o["roomTypeCode"] == "STD")
check("the hold removed a unit from stock", std2["unitsLeft"] == before_left - 1,
      f"{before_left} -> {std2['unitsLeft']}")

print("\n── booking ──")
st, res = http(f"{API}/properties/{PROP}/reservations", "POST", {
    "quoteId": quote["id"], "guestName": "Nisha Kapoor", "guestPhone": "+919812345678",
    "source": "direct", "tokenPaise": 200000, "paymentMethod": "upi",
}, reception)
check("reservation created", st == 200, f"{st} {res}")
check("it has a human-readable ref", isinstance(res.get("ref"), str) and len(res["ref"]) > 3, str(res.get("ref")))
check("money carried over from the quote", res["totalPaise"] == quote["totalPaise"],
      f"{res['totalPaise']} vs {quote['totalPaise']}")
check("the token was recorded as paid", res["paidPaise"] == 200000, str(res["paidPaise"]))
check("balance = total - paid", res["balancePaise"] == res["totalPaise"] - res["paidPaise"])
RES = res["id"]

st, avail3 = http(f"{API}/properties/{PROP}/availability?checkIn={ci}&checkOut={co}&units=1", headers=reception)
std3 = next(o for o in avail3 if o["roomTypeCode"] == "STD")
check("stock stays down once, not twice (hold became the booking)",
      std3["unitsLeft"] == before_left - 1, f"{before_left} -> {std3['unitsLeft']}")

print("\n── the same stay appears on the arrivals list ──")
st, window = http(f"{API}/properties/{PROP}/reservations?from={ci}&to={co}", headers=reception)
check("the date window returns it", st == 200 and any(r["id"] == RES for r in window), f"{st} n={len(window) if isinstance(window,list) else window}")
st, arrivals = http(f"{API}/properties/{PROP}/reservations?bucket=arrivals", headers=reception)
check("arrivals means today, so a stay 10 days out is not on it",
      st == 200 and not any(r["id"] == RES for r in arrivals), f"{st}")
st, found = http(f"{API}/properties/{PROP}/reservations?search=Nisha", headers=reception)
check("search by guest name finds it", any(r["id"] == RES for r in found), str(found)[:120])

print("\n── permissions ──")
st, _ = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", "POST",
             {"kind": "addon", "description": "sneaky", "amountPaise": 100,
              "idempotencyKey": str(uuid.uuid4())}, housekeeper)
check("a housekeeper cannot post to a folio", st == 403, f"got {st}")

print("\n── check-in ──")
st, rooms = http(f"{API}/properties/{PROP}/rooms", headers=reception)
check("rooms listed", st == 200 and isinstance(rooms, list) and rooms, f"{st} {str(rooms)[:120]}")
room = next(r for r in rooms if r.get("roomTypeId") == std["roomTypeId"])
st, ci_res = http(f"{API}/properties/{PROP}/reservations/{RES}/check-in", "POST", {"roomId": room["id"]}, reception)
check("checked in", st == 200 and ci_res.get("status") == "checked_in", f"{st} {str(ci_res)[:160]}")
check("the room is now assigned", ci_res.get("roomId") == room["id"])
st, rooms2 = http(f"{API}/properties/{PROP}/rooms", headers=reception)
occ = next(r for r in rooms2 if r["id"] == room["id"])
check("the room now shows its occupant", (occ.get("occupiedBy") or {}).get("guestName") == "Nisha Kapoor",
      str(occ.get("occupiedBy")))
st, inhouse = http(f"{API}/properties/{PROP}/reservations?bucket=inhouse", headers=reception)
check("in-house bucket returns it", any(r["id"] == RES for r in inhouse), f"{st}")

print("\n── folio ──")
key = str(uuid.uuid4())
st, entry = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", "POST",
                 {"kind": "addon", "description": "Breakfast x2", "amountPaise": 90000,
                  "idempotencyKey": key}, reception)
check("charge posted", st == 200, f"{st} {entry}")
st, dup = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", "POST",
               {"kind": "addon", "description": "Breakfast x2", "amountPaise": 90000,
                "idempotencyKey": key}, reception)
check("replaying the same key does not double-charge the guest",
      st == 200 and dup.get("id") == entry.get("id"), f"{st} {dup.get('id')} vs {entry.get('id')}")
st, folio = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
check("folio reads back", st == 200, f"{st} {str(folio)[:120]}")
check("breakfast appears exactly once",
      sum(1 for e in folio["entries"] if e["description"] == "Breakfast x2") == 1,
      str([e["description"] for e in folio["entries"]]))
before_extra = sum(e["amountPaise"] for e in folio["entries"] if e["description"] != "Breakfast x2")
check("the extra moved the balance by exactly its amount",
      folio["balancePaise"] == before_extra + 90000, f"{folio['balancePaise']} vs {before_extra}+90000")

print("\n── night audit ──")
# The bug this section exists for: check-in used to post one room charge,
# computed pre-tax as (gross - discount) / nights, and nothing ever posted the
# rest. A two-night stay therefore checked out with a settled folio while the
# reservation still showed a balance — observed on TB-TXW3FM, folio ₹0 against
# a reservation balance of ₹3,264.
#
# This run's stay is deliberately hundreds of days out (see RUN), so nothing has
# been slept yet and nothing should be billed. `businessDate` is what lets the
# audit be tested on the day it actually applies rather than by waiting.
st, folio_pre = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
rooms_before = sum(1 for e in folio_pre["entries"] if e["kind"] == "room")
check("checking a guest in early bills no nights they have not had",
      rooms_before == 0, f"{rooms_before} room lines already posted")

st, audit1 = http(f"{API}/properties/{PROP}/night-audit", "POST", {"businessDate": str(ci)}, reception)
check("the audit runs and reports what it did", st == 200 and audit1["entries"] > 0, f"{st} {audit1}")
st, folio_n1 = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
n1_rooms = [e for e in folio_n1["entries"] if e["kind"] == "room"]
n1_tax = [e for e in folio_n1["entries"] if e["kind"] == "tax"]
check("the first night posts a room line", len(n1_rooms) == 1, str([e["description"] for e in n1_rooms]))
check("and its GST as a separate line", len(n1_tax) == 1, str([e["description"] for e in n1_tax]))
check("the second night is not billed before it is slept", len(n1_rooms) == 1)

st, audit2 = http(f"{API}/properties/{PROP}/night-audit", "POST", {"businessDate": str(ci)}, reception)
check("running the audit twice posts nothing the second time", audit2["entries"] == 0, str(audit2))
st, folio_again = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
check("and the balance is unmoved — no duplicate charge",
      folio_again["balancePaise"] == folio_n1["balancePaise"],
      f"{folio_n1['balancePaise']} -> {folio_again['balancePaise']}")
check("the ledger has not grown either",
      len(folio_again["entries"]) == len(folio_n1["entries"]),
      f"{len(folio_n1['entries'])} -> {len(folio_again['entries'])}")

# ── the false settled state ───────────────────────────────────────────────────
# Settle what the folio currently shows, then try to check out. The remaining
# night has not been posted yet, so a check-out that trusted the folio as it
# stands would let the guest walk out owing a night.
st, _ = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", "POST",
             {"kind": "payment", "description": "Settled on the visible balance",
              "amountPaise": folio_again["balancePaise"], "method": "card",
              "idempotencyKey": str(uuid.uuid4())}, reception)
st, folio_zero = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
check("the folio reads zero before the last night is posted",
      folio_zero["balancePaise"] == 0, str(folio_zero["balancePaise"]))

st, refused = http(f"{API}/properties/{PROP}/reservations/{RES}/check-out", "POST", {}, reception)
check("check-out posts the rest of the stay and then refuses — no false settle",
      st == 409, f"got {st} {str(refused)[:160]}")
st, folio_full = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
full_rooms = [e for e in folio_full["entries"] if e["kind"] == "room"]
check("every night of the stay is now on the folio", len(full_rooms) == 2,
      str([e["description"] for e in full_rooms]))
check("and the balance is what is genuinely outstanding",
      folio_full["balancePaise"] > 0, str(folio_full["balancePaise"]))

print("\n── check-out ──")
st, settle = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", "POST",
                  {"kind": "payment", "description": "Settled at desk",
                   "amountPaise": folio_full["balancePaise"], "method": "card",
                   "idempotencyKey": str(uuid.uuid4())}, reception)
check("payment posted", st == 200, f"{st} {settle}")
st, out = http(f"{API}/properties/{PROP}/reservations/{RES}/check-out", "POST", {}, reception)
check("checked out", st == 200 and out.get("status") == "checked_out", f"{st} {str(out)[:160]}")

st, final = http(f"{API}/properties/{PROP}/reservations/{RES}", headers=reception)
st, folio_end = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
check("the folio is settled", folio_end["balancePaise"] == 0, str(folio_end["balancePaise"]))
check("and the reservation agrees with it — the bug that shipped",
      final["totalPaise"] - final["paidPaise"] == folio_end["balancePaise"],
      f"reservation {final['totalPaise'] - final['paidPaise']} vs folio {folio_end['balancePaise']}")
check("the drawer's folio figure is the folio's own",
      final["folioBalancePaise"] == folio_end["balancePaise"],
      f"{final.get('folioBalancePaise')} vs {folio_end['balancePaise']}")
check("room charges plus tax equal what was sold, plus the add-on",
      sum(e["amountPaise"] for e in folio_end["entries"] if e["kind"] in ("room", "tax", "addon"))
      == final["totalPaise"],
      f"{sum(e['amountPaise'] for e in folio_end['entries'] if e['kind'] in ('room','tax','addon'))}"
      f" vs {final['totalPaise']}")

st, after_out = http(f"{API}/properties/{PROP}/night-audit", "POST", {"businessDate": str(co)}, reception)
check("a departed stay is not swept again", after_out["entries"] == 0, str(after_out))

print("\n── tasks ──")
tid = str(uuid.uuid4())
st, task = http(f"{API}/properties/{PROP}/tasks", "POST", {
    "id": tid, "title": "AC not cooling in 104", "category": "maintenance",
    "priority": "high", "roomNumber": "104", "createdBy": "agent",
    "sourceChannel": "whatsapp", "guestName": "Nisha Kapoor"}, reception)
check("ticket raised", st == 200, f"{st} {task}")
st, again = http(f"{API}/properties/{PROP}/tasks", "POST", {
    "id": tid, "title": "AC not cooling in 104", "category": "maintenance",
    "priority": "high", "roomNumber": "104", "createdBy": "agent"}, reception)
check("the same complaint cannot become two tickets", st == 200, f"got {st}")
st, done = http(f"{API}/properties/{PROP}/tasks/{tid}/status", "POST", {"status": "resolved"}, housekeeper)
check("a housekeeper may close a ticket", st == 200, f"{st} {str(done)[:160]}")


# ═════════════════════════════════════════════════════════════════════════════
# The workspace routes the PWA boots on
# ═════════════════════════════════════════════════════════════════════════════

print("\n── workspace ──")
st, me = http(f"{API}/me", headers=reception)
check("/me identifies the caller", st == 200 and me.get("fullName") == "Deepak Shetty", f"{st} {str(me)[:120]}")
m = me["memberships"][0]
check("role and permissions resolve", m["role"] == "receptionist" and "frontdesk" in m["permissions"], str(m)[:160])
check("a receptionist is not given rates", "rates" not in m["permissions"], str(m["permissions"]))

st, props = http(f"{API}/properties", headers=reception)
check("/properties returns only what this member may open", st == 200 and [p["id"] for p in props] == [PROP],
      f"{st} {[p.get('name') for p in props] if isinstance(props, list) else props}")
st, oprops = http(f"{API}/properties", headers=outsider)
check("another partner's staff never see this hotel", all(p["id"] != PROP for p in oprops),
      str([p.get("name") for p in oprops]))

st, today = http(f"{API}/properties/{PROP}/today", headers=reception)
check("/today computes occupancy server-side", st == 200 and 0 <= today["occupancyPct"] <= 100, f"{st} {str(today)[:120]}")
check("rooms available is the sum of room-type units", today["roomsAvailable"] == 42, str(today["roomsAvailable"]))

st, rts = http(f"{API}/properties/{PROP}/room-types", headers=reception)
check("room types are camelCase and typed", st == 200 and all("totalUnits" in r for r in rts), str(rts[:1])[:160])
st, rps = http(f"{API}/properties/{PROP}/rate-plans", headers=reception)
check("rate plans carry a base price", st == 200 and all(p["basePricePaise"] > 0 for p in rps), str(rps[:1])[:160])

# ═════════════════════════════════════════════════════════════════════════════
# Regressions — every one of these is a bug that reached this branch once
# ═════════════════════════════════════════════════════════════════════════════

print("\n── regressions ──")

# discountReason typed `str` let free text reach a check constraint as a 500.
st, _ = http(f"{API}/properties/{PROP}/quotes", "POST", {
    "roomTypeId": std["roomTypeId"], "ratePlanId": plan["ratePlanId"],
    "checkIn": str(ci), "checkOut": str(co), "discountReason": "regular guest"}, reception)
check("free-text discountReason is a 422, not a 500", st == 422, f"got {st}")
for reason in ("volume", "negotiation", "coupon", "creator", "loyalty", "manual"):
    st, q = http(f"{API}/properties/{PROP}/quotes", "POST", {
        "roomTypeId": std["roomTypeId"], "ratePlanId": plan["ratePlanId"],
        "checkIn": str(ci), "checkOut": str(co), "discountPct": 5, "discountReason": reason}, reception)
    check(f"'{reason}' is accepted by both the API and the constraint", st == 200, f"{st} {str(q)[:120]}")
    if st == 200 and q.get("holdId"):
        http(f"{API}/properties/{PROP}/quotes/{q['id']}", "DELETE", None, reception)  # best effort

# require_user declared the header required, so no credentials answered 422.
for path in ("/me", "/properties", f"/properties/{PROP}/today", f"/properties/{PROP}/rooms"):
    st, _ = http(f"{API}{path}")
    check(f"no credentials on {path} -> 401", st == 401, f"got {st}")

# listRooms had no route at all; check-in takes a roomId and nothing supplied one.
st, rooms_r = http(f"{API}/properties/{PROP}/rooms", headers=reception)
check("GET /rooms exists and returns the floor", st == 200 and len(rooms_r) == 10, f"{st} n={len(rooms_r) if isinstance(rooms_r,list) else rooms_r}")
check("each room carries its type and status", all(r.get("roomTypeId") and r.get("housekeepingStatus") for r in rooms_r), str(rooms_r[:1])[:200])

print("\n── authorization ──")
st, _ = http(f"{API}/properties/{PROP}/quotes", "POST", {
    "roomTypeId": std["roomTypeId"], "ratePlanId": plan["ratePlanId"],
    "checkIn": str(ci), "checkOut": str(co)}, housekeeper)
check("a housekeeper cannot price a stay", st == 403, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/rooms/{room['id']}/status", "POST", {"status": "clean"}, housekeeper)
check("a housekeeper may set room status", st == 200, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/rooms", headers=outsider)
check("cross-property rooms -> 404, not 403", st == 404, f"got {st}")

# ═════════════════════════════════════════════════════════════════════════════
# Inventory: the server is the only thing that may decide what can be sold
# ═════════════════════════════════════════════════════════════════════════════

print("\n── holds and inventory ──")

def quote_for(units, headers=reception, ci_=None, co_=None):
    return http(f"{API}/properties/{PROP}/quotes", "POST", {
        "roomTypeId": std["roomTypeId"], "ratePlanId": plan["ratePlanId"],
        "checkIn": str(ci_ or ci), "checkOut": str(co_ or co), "units": units}, headers)

# Take the room type down to its last unit, then race two holds for it.
#
# The dates are far enough out that nothing else touches them, and any holds a
# previous run left behind are expired first — otherwise the second run starts
# with the stock the first one took and the arithmetic below is meaningless.
far_in = date.today() + timedelta(days=RUN + 30)
far_out = far_in + timedelta(days=1)
sql(f"""update public.inventory_holds set expires_at = now() - interval '1 minute'
        where property_id = '{PROP}' and status = 'active';""")
sql("select public.expire_holds();")
st, avail_far = http(f"{API}/properties/{PROP}/availability?checkIn={far_in}&checkOut={far_out}&units=1", headers=reception)
std_far = next(o for o in avail_far if o["roomTypeCode"] == "STD")
left = std_far["unitsLeft"]
st, bulk = quote_for(left - 1, ci_=far_in, co_=far_out)
check("a quote may take all but one unit", st == 200, f"{st} {str(bulk)[:120]}")

results = []
with ThreadPoolExecutor(max_workers=2) as pool:
    futures = [pool.submit(quote_for, 1, reception, far_in, far_out) for _ in range(2)]
    results = [f.result() for f in futures]
won = [r for r in results if r[0] == 200]
lost = [r for r in results if r[0] != 200]
check("two concurrent holds for the last room: exactly one wins",
      len(won) == 1 and len(lost) == 1, f"statuses={[r[0] for r in results]}")
check("the loser is refused, not silently given a second copy",
      all(r[0] in (409, 422, 400) for r in lost), f"{[ (r[0], str(r[1])[:80]) for r in lost ]}")

st, avail_now = http(f"{API}/properties/{PROP}/availability?checkIn={far_in}&checkOut={far_out}&units=1", headers=reception)
std_now = next(o for o in avail_now if o["roomTypeCode"] == "STD")
check("stock is now zero, not negative", std_now["unitsLeft"] == 0, str(std_now["unitsLeft"]))

# Expiry returns the stock. Rather than sleep 15 minutes, age the holds and run
# the sweep the scheduler calls.
sql(f"""update public.inventory_holds set expires_at = now() - interval '1 minute'
        where property_id = '{PROP}' and status = 'active';""")
sql("select public.expire_holds();")
st, avail_after = http(f"{API}/properties/{PROP}/availability?checkIn={far_in}&checkOut={far_out}&units=1", headers=reception)
std_after = next(o for o in avail_after if o["roomTypeCode"] == "STD")
check("expired holds put the rooms back on sale", std_after["unitsLeft"] == left,
      f"{std_after['unitsLeft']} vs {left}")

print("\n── stale and invalid quotes ──")
st, q = quote_for(1, ci_=far_in, co_=far_out)
check("a fresh quote is issued", st == 200, f"{st} {str(q)[:120]}")
sql(f"""update public.quotes set expires_at = now() - interval '1 minute' where id = '{q['id']}';""")
sql("select public.expire_holds();")
st, rejected = http(f"{API}/properties/{PROP}/reservations", "POST", {
    "quoteId": q["id"], "guestName": "Stale Quote", "guestPhone": "+910000000000"}, reception)
check("an expired quote cannot become a booking", st in (409, 410, 400, 404), f"got {st} {str(rejected)[:120]}")

st, _ = http(f"{API}/properties/{PROP}/reservations", "POST", {
    "quoteId": "00000000-0000-4000-8000-000000000000",
    "guestName": "No Such Quote", "guestPhone": "+910000000000"}, reception)
check("an unknown quote id is refused", st in (404, 409, 400), f"got {st}")

st, q2 = quote_for(1, ci_=far_in, co_=far_out)
if q2.get("id"):
    st, first = http(f"{API}/properties/{PROP}/reservations", "POST", {
        "quoteId": q2["id"], "guestName": "Double Submit", "guestPhone": "+910000000001"}, reception)
    st2, second = http(f"{API}/properties/{PROP}/reservations", "POST", {
        "quoteId": q2["id"], "guestName": "Double Submit", "guestPhone": "+910000000001"}, reception)
    check("a quote converts once; the double-submit is refused",
          st == 200 and st2 != 200, f"first={st} second={st2}")

print("\n── calendar reads ──")
cal_from = date.today() + timedelta(days=RUN + 20)
st, inv = http(f"{API}/properties/{PROP}/inventory?from={cal_from}&days=7", headers=reception)
check("inventory reads", st == 200 and isinstance(inv, list) and inv, f"{st} {str(inv)[:120]}")
check("seven nights per room type", all(
    sum(1 for n in inv if n["roomTypeId"] == rt) == 7
    for rt in {n["roomTypeId"] for n in inv}), f"n={len(inv)}")
check("a night with no row of its own still carries the room type's units",
      all(n["unitsTotal"] > 0 for n in inv), str(inv[0]))
st, rates = http(f"{API}/properties/{PROP}/rates?from={cal_from}&days=7", headers=reception)
check("rates read", st == 200 and isinstance(rates, list) and rates, f"{st} {str(rates)[:120]}")
check("and inherit the rate plan's base price where absent",
      all(r["pricePaise"] > 0 for r in rates), str(rates[0]))

print("\n── calendar writes ──")
STD_RT = std["roomTypeId"]
STD_PLAN = plan["ratePlanId"]
w_from, w_to = cal_from, cal_from + timedelta(days=6)

st, wrote = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_from), "toDate": str(w_to),
    "pricePaise": 654300}, manager)
check("a week of pricing is one call", st == 200 and wrote["nights"] == 7, f"{st} {wrote}")
st, rates2 = http(f"{API}/properties/{PROP}/rates?from={w_from}&days=7", headers=reception)
mine = [r for r in rates2 if r["ratePlanId"] == STD_PLAN]
check("every night of the range took the price", all(r["pricePaise"] == 654300 for r in mine),
      str(sorted({r["pricePaise"] for r in mine})))

# The property that makes a sparse calendar safe to edit: an omitted field is
# left alone, not written as zero. Editing min-stay must not freeze the price.
st, wrote2 = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_from), "toDate": str(w_from + timedelta(days=2)),
    "minStay": 2}, manager)
check("a restriction can be set on its own", st == 200 and wrote2["nights"] == 3, f"{st} {wrote2}")
st, rates3 = http(f"{API}/properties/{PROP}/rates?from={w_from}&days=7", headers=reception)
mine3 = [r for r in rates3 if r["ratePlanId"] == STD_PLAN]
check("without disturbing the price — omitted means leave alone",
      all(r["pricePaise"] == 654300 for r in mine3), str(sorted({r["pricePaise"] for r in mine3})))
check("and the minimum stay landed on exactly three nights",
      sum(1 for r in mine3 if r["minStay"] == 2) == 3,
      str([(r["date"], r["minStay"]) for r in mine3]))

st, wrote3 = http(f"{API}/properties/{PROP}/inventory", "PATCH", {
    "roomTypeId": STD_RT, "fromDate": str(w_from), "toDate": str(w_from + timedelta(days=1)),
    "stopSell": True}, manager)
check("stop-sell is a range edit too", st == 200 and wrote3["nights"] == 2, f"{st} {wrote3}")
st, inv2 = http(f"{API}/properties/{PROP}/inventory?from={w_from}&days=7", headers=reception)
stopped = [n for n in inv2 if n["roomTypeId"] == STD_RT and n["stopSell"]]
check("two nights are off sale", len(stopped) == 2, str([n["date"] for n in stopped]))
st, wrote4 = http(f"{API}/properties/{PROP}/inventory", "PATCH", {
    "roomTypeId": STD_RT, "fromDate": str(w_from), "toDate": str(w_from + timedelta(days=1)),
    "stopSell": False}, manager)
check("and can be put back", st == 200, f"{st} {wrote4}")

print("\n── the calendar cannot oversell or cross a property ──")
# The stay booked at the top of this run is still holding a room on those
# nights. Cutting stock under it must be refused with the night named, not
# clamped — property_availability floors at zero, so a silent accept would hide
# a sold room until arrival day.
st, refused = http(f"{API}/properties/{PROP}/inventory", "PATCH", {
    "roomTypeId": STD_RT, "fromDate": str(ci), "toDate": str(ci),
    "unitsTotal": 0}, manager)
check("cutting stock below what is booked is refused", st == 409, f"got {st} {str(refused)[:200]}")
check("and the refusal says which night", "already has" in str(refused), str(refused)[:200])

st, _ = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_from), "toDate": str(w_from),
    "pricePaise": 100000}, housekeeper)
check("a housekeeper cannot price the property", st == 403, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_from), "toDate": str(w_from),
    "pricePaise": 100000}, reception)
check("nor can a receptionist — pricing is not a front-desk decision", st == 403, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/inventory", "PATCH", {
    "roomTypeId": STD_RT, "fromDate": str(w_from), "toDate": str(w_from),
    "unitsTotal": 1}, housekeeper)
check("nor change its stock", st == 403, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_from), "toDate": str(w_from),
    "pricePaise": 100000}, outsider)
check("another partner's staff get 404, not a price change", st == 404, f"got {st}")

st, _ = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_to), "toDate": str(w_from),
    "pricePaise": 100000}, manager)
check("a backwards range is refused", st == 400, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/rates", "PATCH", {
    "ratePlanId": STD_PLAN, "fromDate": str(w_from),
    "toDate": str(w_from + timedelta(days=500)), "pricePaise": 100000}, manager)
check("and so is a range long enough to be a typo", st == 400, f"got {st}")

print("\n── the property clock ──")
# "Today" used to be the server's date, which on Supabase is UTC. Between 18:30
# and 00:00 UTC — 00:00 to 05:30 IST — every Indian property was shown the wrong
# day: yesterday's arrivals, and folios dated to a day that had already closed.
# Nobody caught it because nobody works a desk during a code review.
st, props = http(f"{API}/properties", headers=reception)
prop_row = next((p for p in props if p["id"] == PROP), None)
check("the property reports its timezone", bool(prop_row and prop_row.get("timezone")),
      str(prop_row and prop_row.get("timezone")))
tz_name = prop_row["timezone"]

utc_now = datetime.now(tz=ZoneInfo("UTC"))
local_today = utc_now.astimezone(ZoneInfo(tz_name)).date()

st, today_clock = http(f"{API}/properties/{PROP}/today", headers=reception)
check("today reports the property's own date, not the server's",
      st == 200 and today_clock["date"] == str(local_today),
      f"api={today_clock.get('date')} local={local_today} utc={utc_now.date()}")

# The assertion that would have caught the bug. It only *differs* from the UTC
# date for part of the day, so it is stated rather than relied on: when the two
# dates disagree, the API must side with the hotel.
if local_today != utc_now.date():
    check("and when the two disagree, the hotel wins",
          today_clock["date"] != str(utc_now.date()),
          f"api={today_clock['date']} utc={utc_now.date()}")
else:
    check("and when the two agree, so does the API",
          today_clock["date"] == str(utc_now.date()))

# The audit with no date closes the night that has just ended, locally. A
# manager pressing the button at 15:00 and the hourly sweep must land on the
# same day, or a night gets billed twice or not at all.
st, audit_default = http(f"{API}/properties/{PROP}/night-audit", "POST", {}, manager)
check("a night audit with no date closes yesterday on the property's clock",
      st == 200 and audit_default["businessDate"] == str(local_today - timedelta(days=1)),
      f"{st} {audit_default.get('businessDate')} expected {local_today - timedelta(days=1)}")

# Whoever is in house has been billed through the close, so re-running it is a
# no-op — which is what makes the hourly sweep safe.
st, audit_repeat = http(f"{API}/properties/{PROP}/night-audit", "POST", {}, manager)
check("and re-running it posts nothing", audit_repeat["entries"] == 0, str(audit_repeat))

st, audit_forbidden = http(f"{API}/properties/{PROP}/night-audit", "POST", {}, housekeeper)
check("a housekeeper cannot close the day", st == 403, f"{st} {str(audit_forbidden)[:80]}")

print("\n── today's arrivals: the count and the list agree ──")
# These were two different definitions in two files — the count included
# checked-out stays and the list did not — so Today said one number and the
# screen below it showed another, with no way to tell which was right.
st, today_stats = http(f"{API}/properties/{PROP}/today", headers=reception)
check("today reads", st == 200, f"{st} {str(today_stats)[:120]}")
st, arrivals_list = http(f"{API}/properties/{PROP}/reservations?bucket=arrivals", headers=reception)
check("the arrivals count equals the length of the arrivals list",
      today_stats["arrivals"] == len(arrivals_list),
      f"count={today_stats['arrivals']} list={len(arrivals_list)}")
st, dep_list = http(f"{API}/properties/{PROP}/reservations?bucket=departures", headers=reception)
check("and the same holds for departures",
      today_stats["departures"] == len(dep_list),
      f"count={today_stats['departures']} list={len(dep_list)}")
st, inhouse_list = http(f"{API}/properties/{PROP}/reservations?bucket=inhouse", headers=reception)
check("in-house counts units, and every unit belongs to a listed stay",
      today_stats["inHouse"] == sum(r["units"] for r in inhouse_list),
      f"count={today_stats['inHouse']} list={sum(r['units'] for r in inhouse_list)}")

print("\n── sync and tombstones ──")
st, sync1 = http(f"{API}/properties/{PROP}/sync", headers=reception)
check("a first sync reads", st == 200, f"{st} {str(sync1)[:160]}")
check("and asks for a full resync, because there was no cursor",
      sync1["fullResync"] is True, str(sync1.get("fullResync")))
check("it carries the server's own clock as the next cursor",
      isinstance(sync1.get("serverTime"), str) and sync1["serverTime"], str(sync1.get("serverTime")))
cursor = sync1["serverTime"]

st, sync2 = http(f"{API}/properties/{PROP}/sync?since={cursor}", headers=reception)
check("an immediate second sync has nothing to say", st == 200 and not sync2["deleted"],
      str(sync2.get("deleted")))

# A reservation deleted on the server. This is the case an offline cache cannot
# discover on its own: it refreshes by merging, and a merge never removes.
st, doomed_q = quote_for(1, ci_=far_in, co_=far_out)
st, doomed = http(f"{API}/properties/{PROP}/reservations", "POST", {
    "quoteId": doomed_q["id"], "guestName": "Will Be Deleted",
    "guestPhone": "+910000000009"}, reception)
check("a reservation to delete was created", st == 200, f"{st} {str(doomed)[:120]}")
st, sync3 = http(f"{API}/properties/{PROP}/sync?since={cursor}", headers=reception)
# Not in the changes: sync mirrors the client's working set, which is
# [today-2, today+14], and every booking this script makes is hundreds of days
# out so it cannot exhaust near-term stock. Asserting its absence is the point —
# shipping rows the client prunes on arrival is wasted bandwidth on a hotel
# connection.
check("a booking outside the working-set window is not shipped to the cache",
      not any(r["id"] == doomed["id"] for r in sync3["reservations"]),
      f"n={len(sync3['reservations'])}")
cursor2 = sync3["serverTime"]

# Rooms are not date-windowed, so this is what proves changes flow at all.
st, room_touch = http(f"{API}/properties/{PROP}/rooms/{room['id']}/status", "POST",
                      {"status": "inspected"}, reception)
st, sync_rooms = http(f"{API}/properties/{PROP}/sync?since={cursor2}", headers=reception)
check("a changed room comes back as a change",
      any(r["id"] == room["id"] for r in sync_rooms["rooms"]),
      f"n={len(sync_rooms['rooms'])}")
check("and an unchanged one does not",
      len(sync_rooms["rooms"]) == 1, f"n={len(sync_rooms['rooms'])}")

sql(f"delete from public.reservations where id = '{doomed['id']}';")
st, sync4 = http(f"{API}/properties/{PROP}/sync?since={cursor2}", headers=reception)
check("the deletion comes back as a tombstone",
      doomed["id"] in (sync4["deleted"].get("reservation") or []),
      str(sync4.get("deleted")))
check("and not as a change, because there is no row left to send",
      not any(r["id"] == doomed["id"] for r in sync4["reservations"]),
      f"n={len(sync4['reservations'])}")

st, _ = http(f"{API}/properties/{PROP}/sync", headers=outsider)
check("another partner's staff cannot sync this property", st == 404, f"got {st}")
st, _ = http(f"{API}/properties/{PROP}/sync")
check("and an unauthenticated sync is 401, not 422", st == 401, f"got {st}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
