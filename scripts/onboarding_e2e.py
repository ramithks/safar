#!/usr/bin/env python3
"""
Can a hotel owner who has never existed sign up and end up sellable?

    signup (public, anon key)  ──► POST /pms/onboarding/hotel
                                        │
                                        ├── partner + property (status: setup)
                                        └── five agents, from the definitions
                                              │
                                   POST .../room-types  (name + price)
                                              │
                                   POST .../activate    (setup → active)
                                              │
                                   GET  .../availability → a real price

`pilot_e2e` already proves a hotel can be *run*. It cannot prove this, because
`pilot_fixture` builds its hotel with service-role SQL — it is the thing a
script does, not the thing a person does. Every assertion below goes through
the public HTTP surface holding a token minted by the ordinary signup endpoint,
which is the only way to find out whether a stranger can get in.

**The account is created the way the PWA creates it** — `/auth/v1/signup` with
the anon key — not through GoTrue's admin API like every other fixture here.
That difference is the point: the admin API confirms the email for you, so a
suite that used it would pass on a project where public signups are disabled.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/onboarding_e2e.py

⚠ Creates one auth user at `@onboarding.stayos.test` and the hotel it owns,
and sweeps both at each end. Nothing outside that namespace is touched.
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

# Casa Mandala — a real property under a different partner. The cross-tenant
# negative: our brand-new owner must not be able to touch it.
OTHER_PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

TAG = uuid.uuid4().hex[:8]
EMAIL = f"owner-{TAG}@onboarding.stayos.test"
PASSWORD = "HotelOwner!2026"
HOTEL = f"Green Fig Retreat {TAG[:4].upper()}"
CITY = "Wayanad"

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")
    return ok


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
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


_raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(_raw.removeprefix("go-keyring-base64:")).decode()


def sql(query):
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    if st not in (200, 201):
        raise RuntimeError(f"SQL failed: {st} {str(body)[:300]}")
    return body


_keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
             headers={"Authorization": f"Bearer {MGMT}",
                      "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in _keys if k.get("name") == "anon"][0]


def sweep():
    """Everything this suite has ever made, in dependency order."""
    sql("""
delete from public.agent_configs c using public.agents a
 where c.agent_id = a.id and a.property_id in (
   select p.id from public.properties p join public.partners pt on pt.id = p.partner_id
    join auth.users u on u.id = pt.owner_id where u.email like '%@onboarding.stayos.test');
delete from public.agents a where a.property_id in (
   select p.id from public.properties p join public.partners pt on pt.id = p.partner_id
    join auth.users u on u.id = pt.owner_id where u.email like '%@onboarding.stayos.test');
delete from public.partners pt using auth.users u
 where pt.owner_id = u.id and u.email like '%@onboarding.stayos.test';
delete from auth.users where email like '%@onboarding.stayos.test';
""")


def main():
    print(f"\n══ onboarding: a hotel owner who did not exist ══\n   {EMAIL}\n")
    sweep()

    # ── 1. signing up ────────────────────────────────────────────────────────
    print("── 1. a stranger can create an account ──")
    st, body = http(f"{SB}/auth/v1/signup", "POST",
                    {"email": EMAIL, "password": PASSWORD}, {"apikey": ANON})
    check("the public signup endpoint accepts a new hotel owner", st == 200,
          f"{st} {str(body)[:200]}")
    token = (body or {}).get("access_token") if isinstance(body, dict) else None
    if not check("and hands back a session they can use immediately", bool(token),
                 "no access_token — email confirmation is on, and the PWA has no "
                 "screen that tells an owner to go and check their inbox"):
        return 1
    auth = {"Authorization": f"Bearer {token}"}

    # ── 2. before they have a hotel ──────────────────────────────────────────
    print("\n── 2. before they have a hotel ──")
    st, props = http(f"{API}/properties", headers=auth)
    check("they are authenticated", st == 200, f"{st} {str(props)[:120]}")
    check("and have no hotels at all", props == [], str(props)[:120])
    st, _ = http(f"{API}/properties/{OTHER_PROPERTY}/today", headers=auth)
    check("somebody else's hotel is 404, not 403", st == 404, str(st))

    # ── 3. creating it ───────────────────────────────────────────────────────
    print("\n── 3. creating the hotel ──")
    st, hotel = http(f"{API}/onboarding/hotel", "POST",
                     {"hotelName": HOTEL, "city": CITY, "ownerName": "Anjali Nair",
                      "phone": "+919000000777"}, auth)
    if not check("the hotel is created", st == 201, f"{st} {str(hotel)[:250]}"):
        return 1
    prop_id = hotel["propertyId"]
    check("it belongs to them", bool(hotel["partnerId"]), str(hotel)[:150])
    check("it is NOT sellable yet — no rooms, no rates",
          hotel["status"] == "setup", hotel["status"])
    check("it got a slug from its own name",
          hotel["slug"].startswith("green-fig-retreat"), hotel["slug"])

    st, props = http(f"{API}/properties", headers=auth)
    check("and it now appears as theirs", len(props) == 1 and props[0]["id"] == prop_id,
          str(props)[:150])

    # Idempotency — the double-tap on a slow connection.
    st, again = http(f"{API}/onboarding/hotel", "POST",
                     {"hotelName": HOTEL, "city": CITY}, auth)
    check("creating it twice returns the same hotel, not a second one",
          again.get("propertyId") == prop_id and again.get("created") is False,
          str(again)[:150])
    st, props = http(f"{API}/properties", headers=auth)
    check("so they still own exactly one", len(props) == 1, str(props)[:150])

    # The WhatsApp number is optional on the form, and `partners.whatsapp` is
    # NOT NULL with no default — so the owner who skips it is the one who used
    # to get a 500 on "Create my hotel". Every other assertion in this file
    # passes a phone, which is exactly why the browser found this and the API
    # suite did not.
    st, bare = http(f"{SB}/auth/v1/signup", "POST",
                    {"email": f"nophone-{TAG}@onboarding.stayos.test", "password": PASSWORD},
                    {"apikey": ANON})
    bare_auth = {"Authorization": "Bearer " + bare["access_token"]}
    st, made = http(f"{API}/onboarding/hotel", "POST",
                    {"hotelName": f"No Phone Lodge {TAG[:4]}", "city": "Munnar"}, bare_auth)
    check("a hotel can be created without a WhatsApp number", st == 201,
          f"{st} {str(made)[:200]}")

    # ── 4. the AI employee ───────────────────────────────────────────────────
    print("\n── 4. the AI employee is hired with the hotel ──")
    st, agents = http(f"{API}/properties/{prop_id}/agents", headers=auth)
    check("agents are provisioned without anyone asking", st == 200 and len(agents) >= 5,
          f"{st} {len(agents) if isinstance(agents, list) else agents}")
    if isinstance(agents, list) and agents:
        types = {a.get("agentType") for a in agents}
        check("all five: guest, front desk, sales, marketing, owner",
              {"guest", "frontdesk", "sales", "marketing", "owner"} <= types, str(types))
        # `provider` is read off the agent's active config row, so a non-empty
        # one is the API's own evidence that a published config exists — the
        # agents table alone cannot produce it.
        check("each one has a published configuration, not a bare row",
              all(a.get("provider") for a in agents),
              str([a.get("agentType") for a in agents if not a.get("provider")]))
        check("and each is switched on", all(a.get("status") == "active" for a in agents),
              str([a.get("status") for a in agents]))

    # ── 5. the checklist is real ─────────────────────────────────────────────
    print("\n── 5. the checklist reads the hotel, not a stored step ──")
    st, cl = http(f"{API}/properties/{prop_id}/onboarding", headers=auth)
    check("it loads", st == 200, f"{st} {str(cl)[:150]}")
    check("hotel details are already done",
          next(s["done"] for s in cl["steps"] if s["id"] == "hotel"), str(cl["steps"]))
    check("rooms and rates are not",
          not next(s["done"] for s in cl["steps"] if s["id"] == "rooms")
          and not next(s["done"] for s in cl["steps"] if s["id"] == "rates"), str(cl["steps"]))
    check("the AI employee shows as done",
          next(s["done"] for s in cl["steps"] if s["id"] == "team"), str(cl["steps"]))
    check("two required things remain", cl["remaining"] == 2, str(cl["remaining"]))
    check("and it says the hotel cannot go live yet", cl["canGoLive"] is False, str(cl))

    st, r = http(f"{API}/properties/{prop_id}/activate", "POST", {}, auth)
    check("going live is refused while a rate is missing", st == 422, f"{st} {str(r)[:160]}")
    check("and the refusal names what is missing, in their words",
          isinstance(r, dict) and "room" in str(r.get("detail", "")).lower(),
          str(r)[:200])

    # ── 6. what must never be creatable ──────────────────────────────────────
    print("\n── 6. the prices a hotel must never be able to sell at ──")
    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "Free Room", "totalUnits": 2, "priceRupees": 0}, auth)
    check("₹0 a night is refused", st == 422, f"{st} {str(r)[:160]}")
    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "Negative Room", "totalUnits": 2, "priceRupees": -500}, auth)
    check("a negative rate is refused", st == 422, f"{st} {str(r)[:160]}")
    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "Paise Room", "totalUnits": 2, "priceRupees": 2_000_000}, auth)
    check("a rate above the ₹10,00,000 ceiling — paise typed into a rupee box — "
          "is refused", st == 422, f"{st} {str(r)[:160]}")
    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "X", "totalUnits": 2, "priceRupees": 3000}, auth)
    check("a one-character room name is refused", st == 422, f"{st} {str(r)[:160]}")
    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "Zero Rooms", "totalUnits": 0, "priceRupees": 3000}, auth)
    check("zero of a room is refused", st == 422, f"{st} {str(r)[:160]}")
    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "Crowded", "totalUnits": 1, "maxOccupancy": 99,
                  "priceRupees": 3000}, auth)
    check("99 people in one room is refused", st == 422, f"{st} {str(r)[:160]}")

    st, rooms_now = http(f"{API}/properties/{prop_id}/room-types", headers=auth)
    check("and none of those six wrote anything", rooms_now == [], str(rooms_now)[:200])

    # ── 7. the real thing ────────────────────────────────────────────────────
    print("\n── 7. adding a room they can actually sell ──")
    st, rt = http(f"{API}/properties/{prop_id}/room-types", "POST",
                  {"name": "Garden Cottage", "totalUnits": 4, "maxOccupancy": 3,
                   "priceRupees": 4200}, auth)
    if not check("the room type is created", st == 201, f"{st} {str(rt)[:200]}"):
        return 1
    rt_id = rt["id"]
    check("with the units they entered", rt["totalUnits"] == 4, str(rt))
    check("and a code derived for them", bool(rt["code"]), str(rt))

    st, plans = http(f"{API}/properties/{prop_id}/rate-plans", headers=auth)
    check("a rate plan was created in the same step", len(plans) == 1, str(plans)[:200])
    check("at exactly the price they typed, in paise",
          plans and plans[0]["basePricePaise"] == 420000,
          str(plans[0]["basePricePaise"]) if plans else "none")

    st, r = http(f"{API}/properties/{prop_id}/room-types", "POST",
                 {"name": "Garden Cottage", "totalUnits": 2, "priceRupees": 3000}, auth)
    check("the same room name twice is refused", st == 422, f"{st} {str(r)[:160]}")

    st, r = http(f"{API}/properties/{prop_id}/rooms", "POST",
                 {"roomTypeId": rt_id, "numbers": ["101", "102", "101"]}, auth)
    check("two rooms with the same number is refused", st == 422, f"{st} {str(r)[:160]}")
    st, made = http(f"{API}/properties/{prop_id}/rooms", "POST",
                    {"roomTypeId": rt_id, "numbers": ["101", "102", "103", "104"]}, auth)
    check("four real rooms are created", st == 201 and len(made) == 4,
          f"{st} {str(made)[:160]}")
    st, r = http(f"{API}/properties/{prop_id}/rooms", "POST",
                 {"roomTypeId": rt_id, "numbers": ["101"]}, auth)
    check("and room 101 cannot be created a second time", st == 422, f"{st} {str(r)[:160]}")

    # ── 8. cross-tenant ──────────────────────────────────────────────────────
    print("\n── 8. none of this reaches another hotel ──")
    st, r = http(f"{API}/properties/{OTHER_PROPERTY}/room-types", "POST",
                 {"name": "Intruder", "totalUnits": 1, "priceRupees": 100}, auth)
    check("they cannot add a room to Casa Mandala", st == 404, f"{st} {str(r)[:120]}")
    st, r = http(f"{API}/properties/{OTHER_PROPERTY}/rate-plans", "POST",
                 {"roomTypeId": rt_id, "priceRupees": 1}, auth)
    check("nor a rate", st == 404, f"{st} {str(r)[:120]}")
    st, r = http(f"{API}/properties/{OTHER_PROPERTY}/activate", "POST", {}, auth)
    check("nor take it live", st == 404, f"{st} {str(r)[:120]}")
    st, r = http(f"{API}/properties/{OTHER_PROPERTY}/onboarding", headers=auth)
    check("nor read its setup progress", st == 404, f"{st} {str(r)[:120]}")
    # Their own rate plan must not be attachable to another hotel's room type.
    st, r = http(f"{API}/properties/{prop_id}/rate-plans", "POST",
                 {"roomTypeId": "10000000-0000-4000-8000-000000000001", "priceRupees": 2000},
                 auth)
    check("and another hotel's room type is not theirs to price", st == 422,
          f"{st} {str(r)[:160]}")

    # ── 9. going live ────────────────────────────────────────────────────────
    print("\n── 9. going live ──")
    st, cl = http(f"{API}/properties/{prop_id}/onboarding", headers=auth)
    check("the checklist now says they can", cl["canGoLive"] is True, str(cl)[:200])
    check("with nothing required left", cl["remaining"] == 0, str(cl["remaining"]))
    st, cl = http(f"{API}/properties/{prop_id}/activate", "POST", {}, auth)
    check("the hotel goes live", st == 200 and cl["isLive"] is True, f"{st} {str(cl)[:160]}")
    check("its status is active", cl["status"] == "active", str(cl["status"]))

    # ── 10. sellable, which is the whole point ───────────────────────────────
    print("\n── 10. a room can now actually be sold ──")
    ci = date.today() + timedelta(days=7)
    co = ci + timedelta(days=2)
    st, options = http(
        f"{API}/properties/{prop_id}/availability?checkIn={ci}&checkOut={co}", headers=auth)
    check("availability answers", st == 200, f"{st} {str(options)[:160]}")
    check("the room they created is sellable", isinstance(options, list) and len(options) >= 1,
          str(options)[:200])
    if isinstance(options, list) and options:
        opt = options[0]
        check("with inventory inherited from the room type — no ARI calendar typed in",
              opt.get("unitsLeft", 0) == 4, str(opt.get("unitsLeft")))
        plans_out = opt.get("ratePlans") or []
        check("and priced at the ₹4,200 they entered",
              any(p.get("perNightPaise") == 420000 for p in plans_out),
              str([p.get("perNightPaise") for p in plans_out]))
        check("two nights totalling ₹8,400",
              any(p.get("totalPaise") == 840000 for p in plans_out),
              str([p.get("totalPaise") for p in plans_out]))

    print(f"\n╔═ {passed} passed, {failed} failed")
    print("╚═ a hotel owner who did not exist can now sign up and sell a room\n")
    sweep()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sweep()
        raise
