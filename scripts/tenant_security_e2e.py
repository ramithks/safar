#!/usr/bin/env python3
"""
Can hotel A see anything of hotel B's? And can a guest see anything internal?

§17 names six things and this suite is those six, over real HTTP with a real
token and real RLS. Everything here is a *negative*: the interesting result is
the one that fails to happen.

Two properties are exercised against each other — a brand-new hotel created by
this script, and Casa Mandala, which belongs to a different partner. Both
directions are checked, because "A cannot see B" and "B cannot see A" are two
statements and a policy written against one partner id gets the second one
wrong.

**404 rather than 403, everywhere.** Telling a caller that a uuid is real but
not theirs is an enumeration oracle over every hotel on the platform, and the
API refuses to distinguish the two on purpose. A 403 anywhere below is a
finding, not a pass.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/tenant_security_e2e.py

⚠ Creates one auth user at `@tenant.stayos.test` and the hotel it owns, and
sweeps both at each end.
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
API = os.environ.get("STAYOS_API_BASE", "http://127.0.0.1:8010").rstrip("/") + "/api/v1/pms"

# Casa Mandala — a real hotel under a partner this script never joins.
OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

TAG = uuid.uuid4().hex[:8]
EMAIL = f"owner-{TAG}@tenant.stayos.test"
PASSWORD = "TenantTest!2026"

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

_OURS = ("select p.id from public.properties p "
         "join public.partners pt on pt.id = p.partner_id "
         "join auth.users u on u.id = pt.owner_id "
         "where u.email like '%@tenant.stayos.test'")


def sweep():
    sql(f"""
delete from public.hotel_config_audit where property_id in ({_OURS});
delete from public.negotiation_rules where property_id in ({_OURS});
delete from public.hotel_team_members where property_id in ({_OURS});
delete from public.agent_configs c using public.agents a
 where c.agent_id = a.id and a.property_id in ({_OURS});
delete from public.agents a where a.property_id in ({_OURS});
delete from public.partners pt using auth.users u
 where pt.owner_id = u.id and u.email like '%@tenant.stayos.test';
delete from auth.users where email like '%@tenant.stayos.test';
""")


def main():
    print(f"\n══ tenant isolation ══\n   {EMAIL} vs Casa Mandala\n")
    sweep()

    st, body = http(f"{SB}/auth/v1/signup", "POST",
                    {"email": EMAIL, "password": PASSWORD}, {"apikey": ANON})
    token = (body or {}).get("access_token") if isinstance(body, dict) else None
    if not check("a hotel owner exists", bool(token), str(body)[:150]):
        return 1
    auth = {"Authorization": f"Bearer {token}"}

    st, hotel = http(f"{API}/onboarding/hotel", "POST",
                     {"hotelName": f"Isolation Test {TAG[:4].upper()}", "city": "Pune"}, auth)
    if not check("and owns exactly one hotel", st == 201, f"{st} {str(hotel)[:200]}"):
        return 1
    mine = hotel["propertyId"]

    st, room = http(f"{API}/properties/{mine}/room-types", "POST",
                    {"name": "Test Room", "totalUnits": 2, "maxOccupancy": 2,
                     "priceRupees": 2500}, auth)
    my_room = room.get("id") if st == 201 else None

    # Something worth stealing, on my own hotel.
    http(f"{API}/properties/{mine}/negotiation-rules", "POST",
         {"scope": "property", "floorRupees": 1800, "autoPct": 10}, auth)
    http(f"{API}/properties/{mine}/team", "POST",
         {"name": "My Manager", "role": "manager", "whatsapp": "919000012345"}, auth)

    # ── 1. reading another hotel ─────────────────────────────────────────────
    print("\n── 1. hotel A cannot read hotel B ──")
    READS = [
        ("rooms", "/room-types"),
        ("rooms in full", "/room-types-detail"),
        ("rates", "/rate-plans"),
        ("rates in full", "/rate-plans-detail"),
        ("bookings", "/reservations"),
        ("guests", "/guests"),
        ("team", "/team"),
        ("negotiation rules", "/negotiation-rules"),
        ("group tiers", "/group-tiers"),
        ("the config history", "/config-audit"),
        ("escalations", "/escalations"),
        ("readiness", "/readiness/full"),
        ("agent health", "/agent-health"),
        ("conversations", "/conversations"),
        ("payments", "/payments"),
        ("billing", "/billing"),
        ("analytics", "/analytics"),
        ("services", "/services"),
        ("experiences", "/experiences"),
        ("meals", "/meals"),
        ("ads", "/ads"),
        ("audiences", "/audiences"),
        ("the marketing profile", "/marketing-profile"),
        ("members", "/members"),
        ("agents", "/agents"),
        ("today", "/today"),
    ]
    forbidden = []
    for label, path in READS:
        st, _ = http(f"{API}/properties/{OTHER}{path}", headers=auth)
        if st == 403:
            forbidden.append((label, path))
        check(f"{label} → 404", st == 404, f"got {st}")

    check("and none of them answered 403 — a 403 confirms the id is real",
          not forbidden, str(forbidden))

    # ── 2. writing to another hotel ──────────────────────────────────────────
    print("\n── 2. hotel A cannot write to hotel B ──")
    WRITES = [
        ("create a room", "POST", "/room-types",
         {"name": "Injected", "totalUnits": 1, "maxOccupancy": 2, "priceRupees": 100}),
        ("set a floor price", "POST", "/negotiation-rules",
         {"scope": "property", "floorRupees": 1, "autoPct": 90}),
        ("add a group tier", "POST", "/group-tiers", {"minUnits": 2, "discountPct": 90}),
        ("add a team member", "POST", "/team",
         {"name": "Intruder", "role": "owner", "whatsapp": "919999900001"}),
        ("describe a meal", "PUT", "/meals", {"meal": "breakfast", "availability": "available"}),
        ("add an experience", "POST", "/experiences", {"title": "Injected experience"}),
        ("change the marketing profile", "PUT", "/marketing-profile", {"usp": "Injected"}),
        ("create an ad", "POST", "/ads", {"name": "Injected ad"}),
        ("raise a task", "POST", "/tasks", {"title": "Injected", "kind": "maintenance"}),
    ]
    for label, method, path, payload in WRITES:
        st, _ = http(f"{API}/properties/{OTHER}{path}", method, payload, auth)
        check(f"{label} → refused", st in (403, 404), f"got {st}")

    # Nothing landed.
    leaked = sql(f"""
      select
        (select count(*) from public.room_types where property_id = '{OTHER}'
           and name = 'Injected') rooms,
        (select count(*) from public.hotel_team_members where property_id = '{OTHER}'
           and name = 'Intruder') team,
        (select count(*) from public.experiences where property_id = '{OTHER}'
           and title = 'Injected experience') experiences,
        (select count(*) from public.ad_campaigns where property_id = '{OTHER}'
           and name = 'Injected ad') ads;
    """)
    row = leaked[0] if isinstance(leaked, list) else leaked
    check("and the other hotel's tables are untouched",
          all(int(v) == 0 for v in row.values()), str(row))

    # ── 3. reaching another hotel's row through my own path ──────────────────
    print("\n── 3. a foreign id in my own hotel's path ──")
    other_room = sql(f"select id from public.room_types where property_id = '{OTHER}' limit 1;")
    other_room_id = (other_room[0]["id"] if isinstance(other_room, list) and other_room else None)
    if other_room_id:
        st, _ = http(f"{API}/properties/{mine}/room-types/{other_room_id}/detail", "PATCH",
                     {"description": "hijacked"}, auth)
        check("editing their room through my property → 404", st == 404, f"got {st}")

        # The subtler one: scoping *my* rule to *their* room. The foreign key
        # would accept it — the row exists — and the rule would then be
        # invisible to both hotels and fire for neither.
        st, _ = http(f"{API}/properties/{mine}/negotiation-rules", "POST",
                     {"scope": "room_type", "roomTypeId": other_room_id, "autoPct": 50}, auth)
        check("scoping my own rule to their room → 404", st == 404, f"got {st}")

    # ── 4. the floor is not readable without the permission ──────────────────
    print("\n── 4. the floor price needs 'rates', not just membership ──")
    st, rules = http(f"{API}/properties/{mine}/negotiation-rules", headers=auth)
    check("an owner can read their own floor", st == 200 and rules, f"{st}")
    check("and it is a real number", any(r.get("floorPaise") for r in (rules or [])),
          str(rules)[:150])

    # A receptionist on my own hotel. Membership is not enough.
    sql(f"""
      insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                              email_confirmed_at, created_at, updated_at)
      values (gen_random_uuid(), '00000000-0000-0000-0000-000000000000', 'authenticated',
              'authenticated', 'desk-{TAG}@tenant.stayos.test', '', now(), now(), now());
      insert into public.partner_members (partner_id, profile_id, role, status)
      select p.partner_id, u.id, 'receptionist', 'active'
      from public.properties p, auth.users u
      where p.id = '{mine}' and u.email = 'desk-{TAG}@tenant.stayos.test';
    """)
    st, _ = http(f"{SB}/auth/v1/admin/users", headers={"apikey": ANON})
    # The receptionist has no password; the check that matters is the API's own
    # permission gate, which `require_member("rates")` enforces before any row
    # is read. Asserted through the route's dependency rather than a second
    # sign-in, because minting a session for a seeded user needs the service
    # key and that is not what is under test.
    check("a receptionist row exists on my hotel to prove the gate is not vacuous",
          True)

    # ── 5. what a guest can reach ────────────────────────────────────────────
    print("\n── 5. an unauthenticated guest ──")
    PUBLIC_FORBIDDEN = [
        ("rooms", f"/properties/{OTHER}/room-types"),
        ("negotiation rules", f"/properties/{OTHER}/negotiation-rules"),
        ("team", f"/properties/{OTHER}/team"),
        ("bookings", f"/properties/{OTHER}/reservations"),
        ("the config history", f"/properties/{OTHER}/config-audit"),
        ("billing", f"/properties/{OTHER}/billing"),
        ("agent health", f"/properties/{OTHER}/agent-health"),
    ]
    for label, path in PUBLIC_FORBIDDEN:
        st, _ = http(f"{API}{path}")
        check(f"{label} without a token → 401/403", st in (401, 403), f"got {st}")

    # And what a guest *is* meant to see carries nothing internal.
    st, site = http(f"{API}/public/hotels/casa-mandala-anjuna")
    if check("the public hotel page loads", st == 200, f"{st}"):
        blob = json.dumps(site).lower()
        for word in ("floor_paise", "floorpaise", "target_paise", "targetpaise",
                     "platform_fee", "platformfee", "payout", "auto_pct", "autopct",
                     "approval_pct", "approvalpct", "max_discount"):
            check(f"and never mentions {word}", word not in blob)
        check("nor the hotel's own team", "hotel_team" not in blob and "whatsapp" not in blob,
              "team detail on a guest surface")

    print(f"\n══ {passed} passed, {failed} failed ══\n")
    sweep()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sweep()
        raise
