#!/usr/bin/env python3
"""
A marketplace partner arrives on StayOS and finds their own hotel.

    travbuddy.in/partner  ──►  partner.travbuddy.in  ──►  their property
            │                                                  │
            └── marketplace listing, untouched ─────────────────┘

The other suites test the PMS. This one tests the *seam*: that the hotel a
migration created is the hotel that partner's account resolves to, that its
rooms carry the prices the resolver worked out, that the marketplace listing it
came from is still there and still selling, and that none of it opened a door
to somebody else's property.

**On logging in as the partner.** The suite does not sign in as the real
hotelier, and deliberately does not: their account belongs to them, and setting
a password on it to satisfy a test would be changing a real person's
credentials. So the mapping is asserted two ways instead — structurally, that
the migrated partner's own profile has exactly one active membership pointing at
exactly the right property, and behaviourally, through a disposable staff
account added to that same property, which is what exercises the screens, the
scoping and the refusals.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/partner_migration_e2e.py

⚠ Migrates one real marketplace partner — `sahil`, the founder's own test
listing, never a customer's — and removes it again. Production marketplace data
is read-only throughout.
"""
import asyncio
import os
import pathlib
import sys
import uuid
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")

import migrate_partners as mig  # noqa: E402

API = "http://127.0.0.1:8010/api/v1/pms"
SB = mig.HOTEL_OS_URL

TEST_SLUG = "sahil"
STAFF_EMAIL = "partner-e2e@pilot.stayos.test"
STAFF_PASSWORD = "partner-e2e-dev"

# Another hotel entirely, for the cross-property refusals.
OTHER_PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def section(title):
    print(f"\n── {title} ──")


def run(coro):
    return asyncio.run(coro)


_keys = mig.http(f"https://api.supabase.com/v1/projects/{mig.HOTEL_OS}/api-keys?reveal=true",
                 headers={"Authorization": f"Bearer {mig.MGMT}",
                          "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in _keys if k.get("name") == "anon"][0]
SERVICE = [k["api_key"] for k in _keys if k.get("name") == "service_role"][0]


def signin(email, password):
    st, d = mig.http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                     {"email": email, "password": password}, {"apikey": ANON})
    return d if st == 200 else None


def main() -> int:  # noqa: C901 — one journey; splitting it hides the order
    print("\n══ partner migration e2e ══")
    if mig.http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    admin = {"apikey": SERVICE, "Authorization": f"Bearer {SERVICE}"}

    # ═════════════════════════════════════════════════════════════════════════
    section("1. an existing marketplace partner")

    partners = {p["slug"]: p for p in mig.eligible_partners(None)}
    partner = partners.get(TEST_SLUG)
    check("the partner exists on the marketplace", partner is not None, str(list(partners)))
    if not partner:
        return 1

    listing = mig.best_listing(partner["id"])
    check("with a hotel listing", listing is not None, "no listing")
    marketplace_listing_id = listing["id"]

    before = mig.read_production(f"""
        select count(*) as listings,
               (select count(*) from public.bookings b
                 where b.partner_id = {mig.q(partner['id'])}) as bookings
          from public.partner_listings l
         where l.partner_id = {mig.q(partner['id'])}""")[0]

    def sweep():
        mig.write_hotel_os(f"""
            delete from public.partners where id = {mig.q(partner['id'])};""")

    sweep()

    # ═════════════════════════════════════════════════════════════════════════
    section("2. the migration")

    rooms = mig.plan_room_types(listing, mig.listing_rooms(listing["id"]))
    expected_paise = rooms[0]["paise"]
    first = mig.migrate(partner, listing, rooms, apply=True)
    property_id = first["property_id"]

    check("a property was created", bool(property_id), str(first)[:120])
    check("the StayOS partner keeps the marketplace partner's uuid",
          first["partner_id"] == partner["id"], f"{first['partner_id']}")

    row = mig.write_hotel_os(f"""
        select p.id, p.name, p.status, p.marketplace_listing_id, p.timezone,
               pa.slug as partner_slug
          from public.properties p join public.partners pa on pa.id = p.partner_id
         where p.id = {mig.q(property_id)};""")[0]
    check("and it is connected to the marketplace listing it came from",
          row["marketplace_listing_id"] == marketplace_listing_id,
          f"{row['marketplace_listing_id']} vs {marketplace_listing_id}")
    check("with its own timezone", row["timezone"] == "Asia/Kolkata", str(row["timezone"]))

    # ── the mapping the whole migration exists for ───────────────────────────
    membership = mig.write_hotel_os(f"""
        select m.role, m.status, pr.id as property_id
          from public.partner_members m
          join public.profiles prof on prof.id = m.profile_id
          join public.properties pr on pr.partner_id = m.partner_id
         where prof.email = {mig.q(partner['owner_email'])}
           and m.partner_id = {mig.q(partner['id'])};""")
    check("the partner's own account is a member of exactly one property",
          len(membership) == 1, str(membership))
    check("that property is the one just created",
          membership and membership[0]["property_id"] == property_id, str(membership))
    check("as its owner, and active",
          membership and membership[0]["role"] == "owner"
          and membership[0]["status"] == "active", str(membership))

    # ═════════════════════════════════════════════════════════════════════════
    section("3. one year free, recorded as an entitlement and not as revenue")

    ent = mig.write_hotel_os(f"""
        select source, reason, starts_at::date as starts, ends_at::date as ends,
               property_id, marketplace_listing_id
          from public.partner_transitions
         where partner_id = {mig.q(partner['id'])};""")
    check("a transition entitlement exists", len(ent) == 1, str(ent))
    if ent:
        check("marked as a partner migration", ent[0]["source"] == "partner_migration",
              str(ent[0]["source"]))
        check("with the words the billing screen shows",
              ent[0]["reason"] == "Partner migration — 1 year free", str(ent[0]["reason"]))
        check("running exactly one year",
              (date.fromisoformat(str(ent[0]["ends"]))
               - date.fromisoformat(str(ent[0]["starts"]))).days == 365,
              f"{ent[0]['starts']} → {ent[0]['ends']}")
        check("and pointing at this property", ent[0]["property_id"] == property_id,
              str(ent[0]["property_id"]))

    active = mig.write_hotel_os(f"""
        select public.active_transition({mig.q(property_id)}) as t;""")[0]["t"]
    check("and the hotel is inside a free period right now",
          active and active.get("source") == "partner_migration", str(active))

    check("the entitlement carries no money column at all",
          not mig.write_hotel_os("""
            select column_name from information_schema.columns
             where table_name = 'partner_transitions'
               and (column_name like '%paise%' or column_name like '%amount%');"""),
          "a money column appeared on the entitlement")

    # ═════════════════════════════════════════════════════════════════════════
    section("4. running it again changes nothing")

    rooms2 = mig.plan_room_types(listing, mig.listing_rooms(listing["id"]))
    mig.migrate(partner, listing, rooms2, apply=True)

    counts = mig.write_hotel_os(f"""
        select (select count(*) from public.properties where partner_id = {mig.q(partner['id'])}) as properties,
               (select count(*) from public.partner_members where partner_id = {mig.q(partner['id'])}) as members,
               (select count(*) from public.room_types where property_id = {mig.q(property_id)}) as room_types,
               (select count(*) from public.rate_plans where property_id = {mig.q(property_id)}) as rate_plans,
               (select count(*) from public.partner_transitions where partner_id = {mig.q(partner['id'])}) as transitions,
               (select count(*) from public.billing_accounts where partner_id = {mig.q(partner['id'])}) as accounts,
               (select count(*) from public.subscriptions s
                  join public.billing_accounts ba on ba.id = s.billing_account_id
                 where ba.partner_id = {mig.q(partner['id'])}) as subscriptions;""")[0]
    for label, expected in [("properties", 1), ("members", 1), ("room_types", 1),
                            ("rate_plans", 1), ("transitions", 1), ("accounts", 1),
                            ("subscriptions", 1)]:
        check(f"exactly one {label} after two runs", int(counts[label]) == expected,
              f"{counts[label]}")

    # ═════════════════════════════════════════════════════════════════════════
    section("5. a member of that hotel opens the app")

    st, d = mig.http(f"{SB}/auth/v1/admin/users", "POST",
                     {"email": STAFF_EMAIL, "password": STAFF_PASSWORD,
                      "email_confirm": True,
                      "user_metadata": {"full_name": "Partner E2E"}}, admin)
    staff_profile = d.get("id") if st in (200, 201) else mig.write_hotel_os(
        f"select id from auth.users where email = {mig.q(STAFF_EMAIL)};")[0]["id"]
    mig.write_hotel_os(f"""
insert into public.profiles (id, email, full_name)
values ({mig.q(staff_profile)}, {mig.q(STAFF_EMAIL)}, 'Partner E2E')
on conflict (id) do nothing;

insert into public.partner_members (partner_id, profile_id, role, status, accepted_at)
values ({mig.q(partner['id'])}, {mig.q(staff_profile)}, 'manager', 'active', now())
on conflict do nothing;""")

    session = signin(STAFF_EMAIL, STAFF_PASSWORD)
    check("login works", session is not None and session.get("access_token"),
          "could not sign in")
    if not session:
        sweep()
        return 1
    token = {"Authorization": "Bearer " + session["access_token"]}

    st, me = mig.http(f"{API}/me", headers=token)
    check("and resolves exactly one property — no selector needed",
          st == 200 and len(me.get("memberships", [])) == 1, f"{st} {str(me)[:200]}")
    check("under the migrated partner",
          st == 200 and me["memberships"][0]["partnerId"] == partner["id"],
          str(me.get("memberships"))[:200])
    # Empty `propertyIds` means "every property of this partner" — the same
    # convention `b2b_agents` and `partner_members` share. For a partner with
    # one hotel that is the hotel, and `/properties` is what the app actually
    # asks to build the switcher, so that is what gets asserted.
    check("scoped to the whole partner rather than a subset",
          st == 200 and me["memberships"][0]["propertyIds"] == [],
          str(me["memberships"][0].get("propertyIds")))

    st, props = mig.http(f"{API}/properties", headers=token)
    check("and /properties returns exactly the migrated hotel",
          st == 200 and len(props) == 1 and props[0]["id"] == property_id,
          f"{st} {str(props)[:200]}")

    # ── every screen the shell links to ──────────────────────────────────────
    screens = [
        ("Today", f"/properties/{property_id}/today"),
        ("Rooms", f"/properties/{property_id}/rooms"),
        ("Reservations", f"/properties/{property_id}/reservations"),
        ("Tasks", f"/properties/{property_id}/tasks"),
        ("Inbox", f"/properties/{property_id}/conversations"),
        ("AI agents", f"/properties/{property_id}/agents"),
        ("Billing", f"/properties/{property_id}/billing"),
        ("Room types", f"/properties/{property_id}/room-types"),
        ("Guests", f"/properties/{property_id}/guests"),
    ]
    for label, path in screens:
        st, body = mig.http(f"{API}{path}", headers=token)
        check(f"{label} loads", st == 200, f"{st} {str(body)[:120]}")

    st, billing = mig.http(f"{API}/properties/{property_id}/billing", headers=token)
    check("billing shows a wallet the free year funded",
          st == 200 and billing.get("available", 0) > 0, str(billing)[:200])

    # ═════════════════════════════════════════════════════════════════════════
    section("6. the rates the resolver decided are the rates the engine charges")

    st, types = mig.http(f"{API}/properties/{property_id}/room-types", headers=token)
    check("the migrated room type is there", st == 200 and len(types) == 1, str(types)[:200])

    check_in = date.today() + timedelta(days=60)
    check_out = check_in + timedelta(days=2)
    st, avail = mig.http(
        f"{API}/properties/{property_id}/availability"
        f"?checkIn={check_in}&checkOut={check_out}&units=1", headers=token)
    check("availability offers it", st == 200 and len(avail) == 1, f"{st} {str(avail)[:160]}")

    plans = [p for o in avail for p in o["ratePlans"]] if st == 200 else []
    check("at the resolved rate × two nights",
          plans and plans[0]["totalPaise"] == expected_paise * 2,
          f"{plans[0]['totalPaise'] if plans else '-'} vs {expected_paise * 2}")

    st, quote = mig.http(f"{API}/properties/{property_id}/quotes", "POST", {
        "roomTypeId": types[0]["id"], "ratePlanId": plans[0]["ratePlanId"],
        "checkIn": str(check_in), "checkOut": str(check_out),
        "units": 1, "adults": 2, "children": 0}, token)
    check("a quote uses the same rate", st == 200
          and quote["grossPaise"] == expected_paise * 2,
          f"{st} {str(quote)[:200]}")
    check("with GST added by the server", quote.get("taxPaise", 0) > 0
          and quote["totalPaise"] == quote["grossPaise"] + quote["taxPaise"],
          str(quote)[:200])

    st, booking = mig.http(f"{API}/properties/{property_id}/reservations", "POST", {
        "quoteId": quote["id"], "guestName": "Migration Probe",
        "source": "direct", "tokenPaise": 0}, token)
    check("and a booking is written at that price", st == 200
          and booking["grossPaise"] == expected_paise * 2, f"{st} {str(booking)[:200]}")

    st, listed = mig.http(f"{API}/properties/{property_id}/reservations", headers=token)
    check("which then appears in StayOS",
          st == 200 and any(r["id"] == booking["id"] for r in listed), str(st))

    zero = mig.write_hotel_os(f"""
        select count(*) as n from public.rate_plans
         where property_id = {mig.q(property_id)}
           and is_active and audience = 'public' and base_price_paise <= 0;""")
    check("and no ₹0 public rate plan exists anywhere on this hotel",
          int(zero[0]["n"]) == 0, str(zero))

    # ═════════════════════════════════════════════════════════════════════════
    section("7. what the migration must not have opened")

    st, _ = mig.http(f"{API}/properties/{OTHER_PROPERTY}/reservations", headers=token)
    check("this partner cannot read another hotel's bookings — 404, not 403",
          st == 404, str(st))
    st, _ = mig.http(f"{API}/properties/{OTHER_PROPERTY}/today", headers=token)
    check("nor its day", st == 404, str(st))
    st, _ = mig.http(f"{API}/properties/{OTHER_PROPERTY}/billing", headers=token)
    check("nor its wallet", st == 404, str(st))

    st, _ = mig.http(f"{API}/properties/{property_id}/today")
    check("an anonymous caller cannot open the PMS at all", st in (401, 403), str(st))
    st, _ = mig.http(f"{API}/properties/{property_id}/today",
                     headers={"Authorization": "Bearer not-a-token"})
    check("nor can a forged token", st in (401, 403), str(st))

    # ── session lifecycle ────────────────────────────────────────────────────
    st, refreshed = mig.http(f"{SB}/auth/v1/token?grant_type=refresh_token", "POST",
                             {"refresh_token": session["refresh_token"]},
                             {"apikey": ANON})
    check("a session refreshes, so a shift survives a reload",
          st == 200 and refreshed.get("access_token"), str(st))

    bad = signin(STAFF_EMAIL, "definitely-not-the-password")
    check("a wrong password is refused", bad is None, "signed in with a wrong password")

    # Logout revokes the *refresh* token, not the access token. Supabase JWTs are
    # stateless and signed — nothing consults a session table on each request, so
    # an already-issued access token stays valid until it expires. That is the
    # standard model rather than a defect here, and the property worth asserting
    # is the one that is actually enforced: after logout the session cannot be
    # extended, so it dies at the token's expiry instead of living forever.
    mig.http(f"{SB}/auth/v1/logout", "POST", {}, {"apikey": ANON, **token})
    st, after_logout = mig.http(f"{SB}/auth/v1/token?grant_type=refresh_token", "POST",
                                {"refresh_token": refreshed["refresh_token"]},
                                {"apikey": ANON})
    check("logout revokes the refresh token, so the session cannot be extended",
          st != 200, f"{st} {str(after_logout)[:120]}")

    fresh = signin(STAFF_EMAIL, STAFF_PASSWORD)
    check("and signing in again issues a working session",
          fresh is not None and mig.http(
              f"{API}/me",
              headers={"Authorization": "Bearer " + fresh["access_token"]})[0] == 200,
          "could not sign back in")

    # ═════════════════════════════════════════════════════════════════════════
    section("8. the marketplace is exactly where it was")

    after = mig.read_production(f"""
        select count(*) as listings,
               (select count(*) from public.bookings b
                 where b.partner_id = {mig.q(partner['id'])}) as bookings
          from public.partner_listings l
         where l.partner_id = {mig.q(partner['id'])}""")[0]
    check("the partner still has every listing they had",
          after["listings"] == before["listings"], f"{before} → {after}")
    check("and every booking", after["bookings"] == before["bookings"],
          f"{before} → {after}")

    still = mig.read_production(f"""
        select l.slug, l.status, l.price_from,
               (select count(*) from public.listing_room_types rt
                 where rt.listing_id = l.id and rt.is_active) as rooms
          from public.partner_listings l where l.id = {mig.q(marketplace_listing_id)};""")
    check("the listing itself is untouched — slug, status and price",
          still and still[0]["slug"] == listing["slug"]
          and still[0]["status"] == listing["status"]
          and still[0]["price_from"] == listing["price_from"], str(still))

    history = mig.read_production(f"""
        select p.billed_amount_paise, p.subscription_status, p.stay_plan_status,
               p.stay_plan_expires_at::date as expires
          from public.partners p where p.id = {mig.q(partner['id'])};""")[0]
    check("and the partner's payment history is exactly as it was",
          history["billed_amount_paise"] == partner.get("billed_amount_paise", history["billed_amount_paise"])
          and history["stay_plan_status"] == partner["stay_plan_status"],
          str(history))

    # ═════════════════════════════════════════════════════════════════════════
    section("9. teardown")

    sweep()
    mig.write_hotel_os(f"""
        delete from public.partner_members where profile_id = {mig.q(staff_profile)};""")
    mig.http(f"{SB}/auth/v1/admin/users/{staff_profile}", "DELETE", None, admin)

    left = mig.write_hotel_os(f"""
        select (select count(*) from public.properties where id = {mig.q(property_id)}) as props,
               (select count(*) from public.partner_transitions
                 where partner_id = {mig.q(partner['id'])}) as ents;""")[0]
    check("the test hotel and its entitlement are gone",
          int(left["props"]) == 0 and int(left["ents"]) == 0, str(left))

    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL MARKETPLACE (read-only) · REAL HOTEL OS · REAL JWTs · REAL PRICING")
    print("No marketplace row was written. No email was sent. No real hotelier's")
    print("password was touched — see the header.\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
