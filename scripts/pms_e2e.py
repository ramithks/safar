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
import base64, json, subprocess, sys, urllib.request, urllib.error, uuid
from datetime import date, timedelta

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

ci = date.today() + timedelta(days=10)
co = ci + timedelta(days=2)

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

st, settle = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", "POST",
                  {"kind": "payment", "description": "Settled at desk", "amountPaise": folio["balancePaise"],
                   "method": "card", "idempotencyKey": str(uuid.uuid4())}, reception)
check("payment posted", st == 200, f"{st} {settle}")
st, folio2 = http(f"{API}/properties/{PROP}/reservations/{RES}/folio", headers=reception)
check("folio settles to zero", folio2["balancePaise"] == 0, str(folio2["balancePaise"]))

print("\n── check-out ──")
st, out = http(f"{API}/properties/{PROP}/reservations/{RES}/check-out", "POST", {}, reception)
check("checked out", st == 200 and out.get("status") == "checked_out", f"{st} {str(out)[:160]}")

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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
