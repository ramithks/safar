#!/usr/bin/env python3
"""
Where a migrated room's price comes from, and what happens if it has none.

    listing_room_types.base_price ─┐
                                   ├─► resolve_room_price ─► rate_plans
    partner_listings.price_from  ──┘                          (or switched off)
                                   │
                             NEEDS_REVIEW ─► not sellable, ever

The resolver decides money for hotels that already have paying guests, so this
suite tests it twice over: as arithmetic, against fabricated rooms where every
edge is reachable; and as a migration, against the real hotel OS, where the
question is not "what number did it pick" but "does the booking engine charge
that number".

The second half is the one that matters. A resolver that returns ₹3,600 and a
`stay_options` that offers the room at something else would be two pricing
engines disagreeing, which is the failure this whole architecture is arranged
to prevent.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010          # for the booking half
    ./venv/bin/python scripts/migration_pricing_e2e.py

⚠ Migrates one real marketplace partner — `sahil`, the developer's own test
listing, never a customer's — into the hotel OS and deletes it again.
"""
import asyncio
import os
import pathlib
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")

import migrate_partners as mig  # noqa: E402

API = "http://127.0.0.1:8010/api/v1/pms"

# The developer's own marketplace listing. Chosen because it is the one partner
# in production that is not somebody's business.
TEST_SLUG = "sahil"

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


def main() -> int:  # noqa: C901 — a linear argument; splitting it hides the order
    print("\n══ migration pricing e2e ══")

    # ═════════════════════════════════════════════════════════════════════════
    section("1. rupees → paise, and what it refuses")

    check("a whole rupee amount converts", mig.to_paise(3600) == 360000,
          str(mig.to_paise(3600)))
    check("so does a string, because Postgres numerics arrive as one",
          mig.to_paise("3600") == 360000, str(mig.to_paise("3600")))

    # The reason this is Decimal and not float. 1499.50 * 100 in binary floating
    # point is 149949.99999999997, and int() of that loses a rupee — on a rate
    # that is then multiplied by nights and rooms.
    check("a half-rupee converts exactly, with no float error",
          mig.to_paise("1499.50") == 149950, str(mig.to_paise("1499.50")))
    check("and rounds half up rather than to even",
          mig.to_paise("10.005") == 1001, str(mig.to_paise("10.005")))

    for bad, why in [
        (None, "no price at all"),
        (0, "zero"),
        (-500, "negative"),
        ("", "empty"),
        ("abc", "not a number"),
        ("1,800", "a thousands separator"),
        (float("nan"), "NaN"),
        (float("inf"), "infinity"),
        (99_999_999, "a paise figure in a rupee field"),
    ]:
        try:
            mig.to_paise(bad)
            check(f"{why} is refused", False, f"{bad!r} converted")
        except mig.PriceError:
            check(f"{why} is refused", True)

    check("and the reverse is exact for the report",
          mig.to_rupees(360000) == Decimal("3600.00"), str(mig.to_rupees(360000)))

    # ═════════════════════════════════════════════════════════════════════════
    section("2. the resolver, on every shape a room can have")

    listing = {"price_from": 3600}

    paise, res, src = mig.resolve_room_price({"base_price": 1700}, listing)
    check("an explicit room price wins over the listing price",
          (paise, res) == (170000, mig.EXPLICIT), f"{paise} {res}")

    paise, res, src = mig.resolve_room_price({"base_price": None}, listing)
    check("a room with no price inherits the listing's",
          (paise, res) == (360000, mig.INHERITED), f"{paise} {res}")
    check("and the report says where it came from", src == "₹3,600", src)

    paise, res, _ = mig.resolve_room_price({"base_price": None}, {"price_from": None})
    check("a room with no price and no listing price needs review",
          (paise, res) == (None, mig.NEEDS_REVIEW), f"{paise} {res}")

    paise, res, _ = mig.resolve_room_price({"base_price": 0}, {"price_from": 0})
    check("₹0 is never a price — on the room or the listing",
          (paise, res) == (None, mig.NEEDS_REVIEW), f"{paise} {res}")

    paise, res, _ = mig.resolve_room_price({"base_price": -100}, listing)
    check("a negative room price does not poison the listing fallback",
          (paise, res) == (360000, mig.INHERITED), f"{paise} {res}")

    room = {"base_price": 0}
    mig.resolve_room_price(room, {"price_from": None})
    check("and every rejection is recorded rather than swallowed",
          bool(room.get("_price_notes")), str(room.get("_price_notes")))

    plan = mig.plan_room_types({"price_from": None}, [{"name": "Unpriced", "base_price": None}])
    check("an unpriceable room is planned as not sellable",
          plan[0]["sellable"] is False and plan[0]["paise"] is None, str(plan[0]))

    # ═════════════════════════════════════════════════════════════════════════
    section("3. the real production record this was written for")

    partners = {p["slug"]: p for p in mig.eligible_partners(None)}
    kundapur = partners.get("rashmi-pradeep-shetty")
    check("the affected partner is still eligible", kundapur is not None, str(list(partners)))

    if kundapur:
        listing_row = mig.best_listing(kundapur["id"])
        rooms = mig.listing_rooms(listing_row["id"])
        check("its listing carries a valid price",
              listing_row["price_from"] == 3600, str(listing_row.get("price_from")))
        check("it has exactly one active room type, from listing_room_types",
              len(rooms) == 1 and rooms[0]["name"] == "deluxe",
              str([(r["name"], r["base_price"]) for r in rooms]))
        check("and that room genuinely has no room-level price",
              rooms[0]["base_price"] is None, str(rooms[0]["base_price"]))

        planned = mig.plan_room_types(listing_row, rooms)
        check("so it resolves to the listing price, not to zero",
              planned[0]["paise"] == 360000
              and planned[0]["resolution"] == mig.INHERITED, str(planned[0]))
        check("and arrives sellable rather than switched off",
              planned[0]["sellable"] is True, str(planned[0]["sellable"]))

    # The inactive room the JSON blob still holds and the table knows better of.
    lotus = partners.get("hotel-lotus")
    if lotus:
        lotus_listing = mig.best_listing(lotus["id"])
        lotus_rooms = mig.listing_rooms(lotus_listing["id"])
        names = [r["name"] for r in lotus_rooms]
        check("a deactivated room type is not migrated",
              "Delex Room" not in names, str(names))
        check("and its three live rooms keep their own prices",
              sorted(r["base_price"] for r in lotus_rooms) == [1300, 1700, 2200],
              str([(r["name"], r["base_price"]) for r in lotus_rooms]))

    # ═════════════════════════════════════════════════════════════════════════
    section("4. migrating for real, and doing it twice")

    if mig.http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — the booking half needs it")
        return 1

    partner = partners.get(TEST_SLUG)
    if not partner:
        print(f"  {TEST_SLUG} is not eligible; nothing to migrate")
        return 1

    def sweep():
        mig.write_hotel_os(
            f"delete from public.partners where id = {mig.q(partner['id'])};")

    sweep()
    listing_row = mig.best_listing(partner["id"])
    rooms = mig.listing_rooms(listing_row["id"])
    planned = mig.plan_room_types(listing_row, rooms)
    expected_paise = planned[0]["paise"]

    first = mig.migrate(partner, listing_row, planned, apply=True)
    property_id = first["property_id"]

    plans = mig.write_hotel_os(f"""
        select code, base_price_paise, is_active from public.rate_plans
         where property_id = {mig.q(property_id)} order by code;""")
    check("the rate plan carries the resolved price, in paise",
          len(plans) == 1 and plans[0]["base_price_paise"] == expected_paise,
          str(plans))
    check("and it is active, because the room has a price",
          plans[0]["is_active"] is True, str(plans[0]))

    # ── the requirement that a re-run changes nothing ────────────────────────
    planned2 = mig.plan_room_types(listing_row, mig.listing_rooms(listing_row["id"]))
    mig.migrate(partner, listing_row, planned2, apply=True)

    plans2 = mig.write_hotel_os(f"""
        select code, base_price_paise from public.rate_plans
         where property_id = {mig.q(property_id)} order by code;""")
    check("a second run creates no second rate plan", len(plans2) == 1, str(plans2))
    check("and does not move the price", plans2[0]["base_price_paise"] == expected_paise,
          str(plans2))

    types = mig.write_hotel_os(f"""
        select count(*) as n from public.room_types
         where property_id = {mig.q(property_id)};""")
    check("nor a second room type", int(types[0]["n"]) == 1, str(types))

    # ── the requirement that a hotelier's own price is never clobbered ───────
    hand_set = expected_paise + 45000
    mig.write_hotel_os(f"""
        update public.rate_plans set base_price_paise = {hand_set}
         where property_id = {mig.q(property_id)};""")
    mig.migrate(partner, listing_row, planned2, apply=True)
    after = mig.write_hotel_os(f"""
        select base_price_paise from public.rate_plans
         where property_id = {mig.q(property_id)};""")
    check("a price set in the hotel OS survives a re-migration",
          after[0]["base_price_paise"] == hand_set,
          f"{after[0]['base_price_paise']} vs {hand_set}")

    mig.write_hotel_os(f"""
        update public.rate_plans set base_price_paise = {expected_paise}
         where property_id = {mig.q(property_id)};""")

    # ═════════════════════════════════════════════════════════════════════════
    section("5. does the booking engine charge what the resolver said?")

    from app.services.pms import availability as availability_service

    check_in = date.today() + timedelta(days=45)
    check_out = check_in + timedelta(days=2)

    options = run(availability_service.stay_options(
        property_id, check_in, check_out, units=1, audience="public"))
    check("stay_options offers the migrated room", len(options) == 1, str(options)[:200])

    offered = [p for o in options for p in o.rate_plans]
    check("at exactly one public rate plan", len(offered) == 1, str(offered)[:200])
    check("priced at the resolved rate × two nights, not at anything else",
          offered[0].total_paise == expected_paise * 2,
          f"{offered[0].total_paise} vs {expected_paise * 2}")

    # ── the quote, which is what a guest is actually shown ───────────────────
    from app.services.pms import booking as booking_service

    quote = run(booking_service.create_quote(
        property_id=property_id, rate_plan_id=offered[0].rate_plan_id,
        check_in=check_in, check_out=check_out, units=1, adults=2, children=0,
        issued_by="booking_engine", audience="public"))
    check("a quote is priced from the same rate plan",
          quote.priced.gross_paise == expected_paise * 2,
          f"{quote.priced.gross_paise} vs {expected_paise * 2}")
    check("with GST added on top by the server, not by the migration",
          quote.priced.tax_paise > 0
          and quote.priced.total_paise == quote.priced.gross_paise + quote.priced.tax_paise,
          f"gross {quote.priced.gross_paise} tax {quote.priced.tax_paise} "
          f"total {quote.priced.total_paise}")

    # The GST slab is the server's, from the nightly rate. Asserted as a
    # relationship rather than a constant so a slab change does not fail this.
    nightly = quote.priced.nightly[0].price_paise
    check("and the per-night rate on the quote is the resolved rate",
          nightly == expected_paise, f"{nightly} vs {expected_paise}")

    booked = run(booking_service.create_reservation(
        property_id=property_id, quote_id=quote.id, guest_name="Pricing Probe",
        guest_phone=None, guest_email=None, source="direct", token_paise=0,
        payment_method=None, notes=None, created_by="booking_engine",
        member_id=None, member_name=None))
    check("and the booking is written at that same price",
          booked["gross_paise"] == expected_paise * 2
          and booked["total_paise"] == quote.priced.total_paise,
          f"{booked['gross_paise']} / {booked['total_paise']}")

    # ═════════════════════════════════════════════════════════════════════════
    section("6. an unpriced room can never reach public inventory")

    # Written directly, because the migration will not produce one — which is
    # the point. This asserts the *engine* refuses it too, so the guarantee does
    # not rest on the migration being the only writer.
    rt_id = str(uuid.uuid4())
    mig.write_hotel_os(f"""
insert into public.room_types
  (id, property_id, partner_id, code, name, total_units, max_occupancy, sort_order)
values ('{rt_id}', {mig.q(property_id)}, {mig.q(partner['id'])}, 'NOP',
        'Unpriced room', 2, 2, 9);

insert into public.rate_plans
  (property_id, room_type_id, partner_id, code, name, meal_plan, audience,
   base_price_paise, refundable, is_active, sort_order)
values ({mig.q(property_id)}, '{rt_id}', {mig.q(partner['id'])}, 'NOP-EP',
        'Unpriced room — room only', 'EP', 'public', 0, true, false, 9);""")

    options = run(availability_service.stay_options(
        property_id, check_in, check_out, units=1, audience="public"))
    codes = [p.code for o in options for p in o.rate_plans]
    check("a switched-off ₹0 plan is not offered to the public",
          "NOP-EP" not in codes, str(codes))

    zero_plan = mig.write_hotel_os(f"""
        select id from public.rate_plans
         where property_id = {mig.q(property_id)} and code = 'NOP-EP';""")
    try:
        run(booking_service.create_quote(
            property_id=property_id, rate_plan_id=zero_plan[0]["id"],
            check_in=check_in, check_out=check_out, units=1, adults=2, children=0,
            issued_by="booking_engine", audience="public"))
        check("and cannot be quoted even by naming its id", False,
              "a ₹0 rate plan was quoted")
    except Exception as exc:  # noqa: BLE001 — an HTTPException is the pass
        check("and cannot be quoted even by naming its id", True, str(exc)[:80])

    sweep()
    left = mig.write_hotel_os(f"""
        select count(*) as n from public.properties
         where id = {mig.q(property_id)};""")
    check("the test hotel is removed again", int(left[0]["n"]) == 0, str(left))

    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL MARKETPLACE DATA (read-only) · REAL HOTEL OS · REAL PRICING ENGINE")
    print("No marketplace row was written. No email was sent.\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
