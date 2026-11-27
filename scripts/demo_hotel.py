"""
Anandi House Udaipur — one hotel that behaves like a real one.

    python scripts/demo_hotel.py            build (or top up) and print it
    python scripts/demo_hotel.py --status   say what exists, change nothing

**Why a demo hotel and not one of the real ones.** Erin Villa Coonoor, Hotel
Lotus and Your Perfect NATURE RETREAT belong to people. Connecting their
WhatsApp, publishing their website or writing a fake booking onto their books to
prove a pipeline works would be doing something to a stranger's business in
order to reassure ourselves. So the proving ground is a hotel of our own, and
this script refuses to touch anything outside it.

**Built through the product, not around it.** Every row here is created by the
same service the app calls — `onboarding.create_hotel`, `add_room_type_with_price`,
`ari.set_inventory`, `website.publish`, `provisioning.provision`. A seed that
inserts rows directly proves the schema; a seed that goes through the services
proves the architecture, which is the only thing worth proving the day before a
launch. Where a service does not exist yet (services, knowledge, staff) the
insert is upserted on a stable id.

**Idempotent, and additive.** Running it twice changes nothing the second time:
ids are fixed, `create_hotel` returns the existing property rather than making a
second, and every insert is an upsert. It never deletes — unlike the e2e
fixtures, this hotel is meant to survive between runs and accumulate a history.
"""
import argparse
import asyncio
import sys
from datetime import date, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.db.client import get_db  # noqa: E402
from app.services.ai import provisioning  # noqa: E402
from app.services.pms import ari, onboarding, website  # noqa: E402

# ── the namespace ────────────────────────────────────────────────────────────
#
# Everything this script owns starts `da…`, so a human reading the database can
# tell a demo row from a real one without knowing what the demo is called.

NS = "da000000-0000-4000-8000-"
OWNER_EMAIL = "owner@anandihouse.demo"
PASSWORD = "AnandiDemo!2026"

HOTEL = "Anandi House Udaipur"
SLUG = "anandi-house-udaipur"
CITY = "Udaipur"
# The numbers the launch brief names. Held here as data, never in business
# logic: routing an inbound message resolves a connection row by
# `phone_number_id`, and a phone number written into code is a second answer to
# "whose hotel is this" that nobody remembers to change.
OWNER_PHONE = "918607391180"
GUEST_PHONE = "918368732396"

ROOMS = [
    # name, code, units, sleeps, ₹/night, room numbers
    ("Lake View Deluxe", "LVD", 6, 2, 4_800, ["201", "202", "203", "204", "205", "206"]),
    ("Heritage Suite", "HSU", 3, 3, 8_500, ["301", "302", "303"]),
    ("Courtyard Standard", "CYS", 8, 2, 3_200,
     ["101", "102", "103", "104", "105", "106", "107", "108"]),
]

SERVICES = [
    # id-suffix, name, description, category, ₹, lead hours, daily capacity
    ("01", "Airport pickup (Maharana Pratap)", "Sedan, up to 3 guests, 30 min from the hotel",
     "transfer", 1_200, 4, None),
    ("02", "Airport drop", "Sedan, up to 3 guests", "transfer", 1_200, 4, None),
    ("03", "Ayurvedic massage", "60 minutes, in our garden spa room", "wellness", 2_500, 3, 6),
    ("04", "Rooftop dinner for two", "Four courses, overlooking Lake Pichola",
     "food", 3_400, 6, 4),
    ("05", "Breakfast (extra guest)", "Served 7:30–10:30 in the courtyard",
     "food", 450, 0, None),
    ("06", "City Palace & Jagdish Temple walk", "Guided, 3 hours, mornings",
     "activity", 1_800, 12, 8),
    ("07", "Sunset boat ride on Lake Pichola", "Shared boat, 1 hour", "activity", 1_100, 6, 10),
    ("08", "Late checkout until 16:00", "Subject to the room being free", "room", 1_500, 0, 3),
]

# What the AI employee is allowed to say about the hotel. This is the difference
# between an agent that answers "what time is check-in" from the hotel's own
# rules and one that answers it from whatever the model believes hotels do.
KNOWLEDGE = [
    ("01", "checkin", "What time is check-in?",
     "Check-in is from 2:00 PM. If you arrive earlier we'll hold your bags and "
     "get you into the room as soon as it's ready.", ["check in", "checkin", "arrival", "early"]),
    ("02", "checkout", "What time is checkout?",
     "Checkout is 11:00 AM. Late checkout until 4:00 PM is ₹1,500 if the room is free.",
     ["checkout", "check out", "late", "extend"]),
    ("03", "breakfast", "Is breakfast included?",
     "Yes — breakfast for two is included with every room, served 7:30–10:30 AM "
     "in the courtyard.", ["breakfast", "food", "morning", "included"]),
    ("04", "parking", "Is there parking?",
     "Free parking for 6 cars in our lane. Tell us your arrival time and we'll keep a space.",
     ["parking", "car", "vehicle"]),
    ("05", "wifi", "Is there WiFi?",
     "Free WiFi throughout, including the rooftop. The password is on your key card.",
     ["wifi", "internet", "wi-fi"]),
    ("06", "pets", "Are pets allowed?",
     "We're happy to host well-behaved dogs in the Courtyard rooms. Just let us know in advance.",
     ["pet", "dog", "cat", "animal"]),
    ("07", "cancellation", "What is the cancellation policy?",
     "Free cancellation up to 48 hours before check-in. Inside 48 hours we keep one night.",
     ["cancel", "refund", "change", "postpone"]),
    ("08", "local", "What can I do in Udaipur?",
     "City Palace and Jagdish Temple are a 10-minute walk. Our guided morning walk "
     "is ₹1,800. Most guests do a sunset boat ride on Lake Pichola (₹1,100) — "
     "we can book both for you.",
     ["do", "see", "visit", "sightseeing", "udaipur", "attractions"]),
    ("09", "directions", "How do I get to the hotel?",
     "We're in Lal Ghat, a 30-minute drive from Maharana Pratap Airport. Airport "
     "pickup is ₹1,200 — tell us your flight and we'll send a car.",
     ["directions", "reach", "how to get", "address", "airport"]),
    ("10", "payment", "How do I pay?",
     "You can pay online by UPI, card or netbanking — just ask and we'll send you "
     "a secure payment link. We also take cash and cards at the desk.",
     ["pay", "payment", "upi", "card", "advance", "deposit"]),
]

STAFF = [
    ("manager@anandihouse.demo", "manager", "Priya Rathore"),
    ("reception@anandihouse.demo", "receptionist", "Vikram Singh"),
    ("housekeeping@anandihouse.demo", "housekeeping", "Sunita Devi"),
    ("maintenance@anandihouse.demo", "maintenance", "Ramesh Lal"),
]

POLICIES = {
    "cancellation": "Free cancellation up to 48 hours before check-in. "
                    "Inside 48 hours, one night is retained.",
    "children": "Children under 6 stay free in the existing bedding.",
    "pets": "Dogs welcome in Courtyard Standard rooms, on request.",
    "smoking": "No smoking indoors. The rooftop and courtyard are fine.",
    "id": "Every adult guest needs a government photo ID at check-in.",
}


def sid(suffix: str) -> str:
    """A stable uuid for one demo row. The suffix is what makes it idempotent."""
    return f"{NS}{suffix.rjust(12, '0')}"


# ── Supabase auth, for the people ────────────────────────────────────────────

def _admin_headers() -> dict:
    from app.config import settings

    key = settings.supabase_service_key
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def ensure_user(email: str, full_name: str) -> str:
    """
    One login, through GoTrue rather than an insert into `auth.users`.

    Same reason `pilot_fixture` gives: a hand-written auth row signs in today
    and stops the first time the password hashing changes.
    """
    import json
    import urllib.error
    import urllib.request

    from app.config import settings

    base = settings.supabase_url.rstrip("/")
    body = json.dumps({
        "email": email, "password": PASSWORD, "email_confirm": True,
        "user_metadata": {"full_name": full_name},
    }).encode()
    req = urllib.request.Request(f"{base}/auth/v1/admin/users", data=body,
                                 headers=_admin_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            created = json.loads(r.read())
        if created.get("id"):
            _name(created["id"], full_name)
            return created["id"]
    except urllib.error.HTTPError as exc:
        if exc.code not in (422, 400, 409):  # already registered
            raise

    # Already there. Find them by listing — the admin API filters on email.
    from urllib.parse import quote
    req = urllib.request.Request(
        f"{base}/auth/v1/admin/users?filter={quote(email)}",
        headers=_admin_headers(),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        users = json.loads(r.read()).get("users") or []
    for u in users:
        if (u.get("email") or "").lower() == email.lower():
            _name(u["id"], full_name)
            return u["id"]
    raise RuntimeError(f"could not create or find {email}")


def _name(profile_id: str, full_name: str) -> None:
    db = get_db()
    rows = db.table("profiles").select("id,full_name").eq("id", profile_id).limit(1).execute().data
    if rows and not (rows[0].get("full_name") or "").strip():
        db.table("profiles").update({"full_name": full_name}).eq("id", profile_id).execute()


# ── build ────────────────────────────────────────────────────────────────────

async def build(status_only: bool = False) -> dict:
    db = get_db()

    existing = (db.table("properties").select("id,slug,name,status,partner_id")
                .eq("name", HOTEL).limit(1).execute().data or [])

    if status_only:
        if not existing:
            print(f"  {HOTEL} does not exist yet.")
            return {}
        return _status(existing[0])

    owner_id = ensure_user(OWNER_EMAIL, "Anandi House")
    print(f"  owner       {OWNER_EMAIL}  ({owner_id})")

    # The real signup path. Returns the existing hotel on a second run rather
    # than making another, which is the property this script depends on.
    hotel = await onboarding.create_hotel(
        profile_id=owner_id, hotel_name=HOTEL, city=CITY,
        owner_name="Anandi House", phone=OWNER_PHONE,
    )
    property_id, partner_id = hotel["propertyId"], hotel["partnerId"]
    print(f"  hotel       {hotel['slug']}  ({property_id}) "
          f"{'created' if hotel['created'] else 'already there'}")

    # Times, policies and the address. `create_hotel` deliberately asks for two
    # fields; everything a guest actually asks about is filled in here.
    # `create_hotel` slugs "name-city", which for a hotel whose name already
    # ends in its city reads `anandi-house-udaipur-udaipur`. Set once, here,
    # before anything links to it -- a slug is a URL a guest may have saved.
    db.table("properties").update({"slug": SLUG}).eq("id", property_id) \
      .neq("slug", SLUG).execute()

    db.table("properties").update({
        "address": "42 Lal Ghat, near Jagdish Temple",
        "state": "Rajasthan", "country": "IN", "pincode": "313001",
        "lat": 24.5789, "lon": 73.6831,
        "property_type": "hotel",
        "check_in_time": "14:00", "check_out_time": "11:00",
        "policies": POLICIES,
        "total_rooms": sum(r[2] for r in ROOMS),
    }).eq("id", property_id).execute()

    await _rooms(property_id, partner_id)
    await _inventory(property_id)
    _services(property_id, partner_id)
    _knowledge(property_id, partner_id)
    _staff(partner_id)
    await _agents(property_id, partner_id)
    await _website(property_id)
    _channels(property_id, partner_id)
    await _go_live(property_id)

    return _status((db.table("properties").select("id,slug,name,status,partner_id")
                    .eq("id", property_id).limit(1).execute().data or [{}])[0])


async def _rooms(property_id: str, partner_id: str) -> None:
    db = get_db()
    have = {r["name"] for r in (db.table("room_types").select("name")
                                .eq("property_id", property_id).execute().data or [])}
    for name, code, units, sleeps, rupees, numbers in ROOMS:
        if name in have:
            continue
        rt = await onboarding.add_room_type_with_price(
            property_id=property_id, partner_id=partner_id, name=name,
            total_units=units, max_occupancy=sleeps, code=code,
            price_paise=rupees * 100, meal_plan="CP",
        )
        await onboarding.add_rooms(
            property_id=property_id, partner_id=partner_id,
            room_type_id=rt["id"], numbers=numbers,
        )
        print(f"  room type   {name} × {units} at ₹{rupees:,}")


async def _inventory(property_id: str) -> None:
    """
    Ninety days of sellable stock, at each room type's own count.

    Without this the hotel exists and cannot be booked: `property_availability`
    reads `room_inventory`, and a missing row is nothing to sell rather than
    everything. Set for ninety days because a guest asking in August about a
    trip in October is the ordinary case, not an edge one.
    """
    db = get_db()
    today = date.today()
    types = (db.table("room_types").select("id,name,total_units")
             .eq("property_id", property_id).execute().data or [])

    existing = (db.table("room_inventory").select("id")
                .eq("property_id", property_id)
                .gte("date", today.isoformat()).limit(1).execute().data or [])
    if existing:
        print("  inventory   already open")
        return

    for rt in types:
        await ari.set_inventory(
            property_id,
            room_type_id=rt["id"],
            from_date=today, to_date=today + timedelta(days=90),
            units_total=int(rt["total_units"]), units_blocked=0,
            stop_sell=False, oversell=0,
        )
    print(f"  inventory   90 days open across {len(types)} room types")


def _services(property_id: str, partner_id: str) -> None:
    db = get_db()
    rows = [{
        "id": sid(f"3{n}"), "property_id": property_id, "partner_id": partner_id,
        "name": name, "description": desc, "category": cat,
        "price_paise": rupees * 100, "lead_time_hours": lead,
        "daily_capacity": cap, "is_active": True, "sort_order": i,
    } for i, (n, name, desc, cat, rupees, lead, cap) in enumerate(SERVICES)]
    db.table("services").upsert(rows, on_conflict="id").execute()
    print(f"  services    {len(rows)} (transfer, spa, food, activities, late checkout)")


def _knowledge(property_id: str, partner_id: str) -> None:
    db = get_db()
    rows = [{
        "id": sid(f"4{n}"), "property_id": property_id, "partner_id": partner_id,
        "category": cat, "question": q, "answer": a, "keywords": kw,
        "confidence": "confirmed", "is_active": True, "sort_order": i,
    } for i, (n, cat, q, a, kw) in enumerate(KNOWLEDGE)]
    db.table("property_knowledge").upsert(rows, on_conflict="id").execute()
    print(f"  knowledge   {len(rows)} answers in the hotel's own words")


def _staff(partner_id: str) -> None:
    db = get_db()
    for i, (email, role, full_name) in enumerate(STAFF):
        profile_id = ensure_user(email, full_name)
        db.table("partner_members").upsert({
            "id": sid(f"5{i}"), "partner_id": partner_id, "profile_id": profile_id,
            "role": role, "status": "active", "property_ids": [],
        }, on_conflict="id").execute()
    print(f"  staff       {len(STAFF)} (manager, reception, housekeeping, maintenance)")


async def _agents(property_id: str, partner_id: str) -> None:
    """
    The AI employee. `provision` is additive and upserts its tools, so running
    this again is how a hotel picks up a capability that shipped after it was
    set up — which is exactly what the payment-link and upsell tools are.
    """
    from app.services.ai.agents import all_definitions

    for agent_type, definition in all_definitions().items():
        if not definition.available:
            continue
        await provisioning.provision(
            property_id=property_id, partner_id=partner_id, agent_type=agent_type,
        )
    db = get_db()
    n = len(db.table("agents").select("id").eq("property_id", property_id).execute().data or [])
    print(f"  AI employee {n} agents, tools in sync")


async def _website(property_id: str) -> None:
    db = get_db()
    row = (db.table("hotel_websites").select("status")
           .eq("property_id", property_id).limit(1).execute().data or [])
    if row and row[0]["status"] == "published":
        print("  website     already published")
        return

    await website.save_draft(property_id, template="boutique", draft={
        "headline": "Anandi House",
        "tagline": "A 1920s haveli on Lal Ghat, two minutes from Lake Pichola",
        "about": "Nine rooms around a stone courtyard, in the quietest lane of "
                 "Udaipur's old city. Breakfast on the rooftop with the City "
                 "Palace on one side and the lake on the other. Family-run "
                 "since 1994.",
        "amenities": ["Free WiFi", "Rooftop restaurant", "Airport pickup",
                      "Free parking", "Garden spa room", "Lake views",
                      "Air conditioning", "Laundry"],
        "accentColor": "#B45309",
        "contactPhone": f"+{OWNER_PHONE}",
        "contactEmail": OWNER_EMAIL,
        "sections": [
            {"id": s, "enabled": True}
            for s in ("hero", "rooms", "amenities", "location", "policies", "contact")
        ] + [{"id": "gallery", "enabled": False}, {"id": "offers", "enabled": False}],
    })
    await website.publish(property_id, None)
    print("  website     published")


def _sender_display(phone_number_id: str) -> str | None:
    """What Meta says this sender's number is. None if it cannot be read."""
    import json
    import os
    import urllib.request

    token = (os.environ.get("WHATSAPP_ACCESS_TOKEN") or "").strip()
    if not token:
        return None
    try:
        url = (f"https://graph.facebook.com/v21.0/{phone_number_id}"
               f"?fields=display_phone_number&access_token={token}")
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read()).get("display_phone_number")
    except Exception:  # noqa: BLE001 — a label must not fail a seed
        return None


def _channels(property_id: str, partner_id: str) -> None:
    """
    The row that says which hotel an inbound message belongs to.

    Routing is by `phone_number_id` — Meta's id for *our* end of the
    conversation — and nothing else. The guest's number is never consulted, and
    no phone number appears in any branch: a hotel is identified by the sender
    it owns, so adding a second hotel is a row, not a deploy.

    **Which sender the demo gets depends on what Meta has actually given us.**

      `WHATSAPP_TEST_PHONE_NUMBER_ID` set → that sender, live, credentials from
      the environment. Meta restricts a test number to a handful of verified
      recipients, so this can reach the launch team's handsets and cannot reach
      a stranger. That is a real end-to-end WhatsApp connection, and it is the
      only real one available until a WABA is verified.

      Not set → a sandbox row. It routes and resolves exactly like a live one —
      same lookup, same conversation, same agent — but `adapter_for` hands the
      send path a fake, so a demo cannot message anybody at all.

    The number the launch brief names for guests, +91 8368732396, is **not**
    registered with any WABA on this app. Writing it into `phone_number_id`
    would produce a row that looks connected, resolves nothing, and fails at
    the moment a guest first messages it — so it is recorded as the display
    number the hotel intends to use and never as the routing key.
    """
    import os

    db = get_db()
    # A real, verified sender if there is one; Meta's test number only as a
    # fallback. The distinction is not cosmetic — a test number can reach five
    # allow-listed handsets and no guest, so a demo wired to one proves the
    # pipeline and nothing about whether a hotel can trade.
    real_sender = (os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    test_sender = (os.environ.get("WHATSAPP_TEST_PHONE_NUMBER_ID") or "").strip()
    live_sender = real_sender or test_sender

    if live_sender:
        # The number staff see, read from Meta rather than from our own
        # aspirations. It was being written from GUEST_PHONE, so the inbox
        # showed +91 83687 32396 while messages actually came from a different
        # number entirely — and the one thing a display number has to do is
        # match the handset a guest is looking at.
        shown = _sender_display(live_sender) or f"+91 {GUEST_PHONE[2:7]} {GUEST_PHONE[7:]}"
        row = {
            "phone_number_id": live_sender,
            "waba_id": (os.environ.get("WHATSAPP_WABA_ID") or "").strip() or "unknown",
            "display_name": HOTEL,
            "status": "connected",
            "credentials_ref": "default",
            "is_sandbox": False,
        }
        note = (f"live via Meta sender {live_sender}"
                + ("" if real_sender else " (test number — allow-listed recipients only)"))
    else:
        shown = f"+91 {GUEST_PHONE[2:7]} {GUEST_PHONE[7:]}"
        row = {
            "phone_number_id": f"demo-{GUEST_PHONE}",
            "waba_id": "demo-waba",
            "display_name": HOTEL,
            "status": "connected",
            "credentials_ref": "sandbox",
            "is_sandbox": True,
        }
        note = "sandbox — no Meta sender configured"

    # Whoever else holds this sender loses it. Two hotels on one
    # `phone_number_id` is an inbound message that resolves to either of them,
    # which is worse than one that resolves to neither.
    db.table("whatsapp_connections").delete() \
      .eq("phone_number_id", row["phone_number_id"]) \
      .neq("id", sid("60")).execute()

    db.table("whatsapp_connections").upsert({
        "id": sid("60"), "property_id": property_id, "partner_id": partner_id,
        "display_number": shown,
        **row,
    }, on_conflict="id").execute()
    print(f"  WhatsApp    {note}")

    _instagram(property_id, partner_id)


def _instagram(property_id: str, partner_id: str) -> None:
    """
    The demo hotel's Instagram, on the same terms as its WhatsApp.

    An account id is only useful if we hold a token that can *act* as it, so it
    is read from Meta rather than typed: `graph.instagram.com/me` with the
    token in the environment answers with the id of whatever account that token
    actually belongs to. Writing an id we could not authenticate as would
    produce a row that routes an inbound DM to a hotel whose replies then fail.

    A real hotel does not get its connection this way -- it presses "Connect
    Instagram" in Settings and Business Login writes `prop:<id>` into
    `messaging_credentials`. This is the demo standing in for that, using the
    one account we own.
    """
    import json
    import os
    import urllib.request

    db = get_db()
    token = (os.environ.get("INSTAGRAM_ACCESS_TOKEN") or "").strip()

    account_id = username = None
    if token:
        try:
            url = ("https://graph.instagram.com/v21.0/me"
                   f"?fields=id,username&access_token={token}")
            with urllib.request.urlopen(url, timeout=20) as r:
                me = json.loads(r.read())
            account_id, username = me.get("id"), me.get("username")
        except Exception as exc:  # noqa: BLE001
            print(f"  Instagram   could not read the account from Meta ({exc})")

    if not account_id:
        print("  Instagram   not connected — no usable token "
              "(a real hotel connects its own in Settings)")
        return

    db.table("instagram_connections").delete() \
      .eq("instagram_account_id", account_id).neq("id", sid("61")).execute()

    db.table("instagram_connections").upsert({
        "id": sid("61"), "property_id": property_id, "partner_id": partner_id,
        "instagram_account_id": account_id, "username": username,
        "status": "connected", "credentials_ref": "default", "is_sandbox": False,
    }, on_conflict="id").execute()
    print(f"  Instagram   live as @{username} ({account_id})")


async def _go_live(property_id: str) -> None:
    """
    Promote the hotel out of `setup`, through the same gate an owner presses.

    `activate()` re-reads the checklist and refuses if a step is missing, so
    this is not a status column being set -- it is the product agreeing the
    hotel is sellable. A demo that skipped it would prove nothing about the
    onboarding it is meant to be proving.
    """
    db = get_db()
    row = (db.table("properties").select("status")
           .eq("id", property_id).limit(1).execute().data or [{}])[0]
    if row.get("status") == "active":
        print("  live        already")
        return
    state = await onboarding.activate(property_id)
    print(f"  live        yes ({state['counts']['roomTypes']} room types, "
          f"{state['counts']['rooms']} rooms)")


def _status(prop: dict) -> dict:
    if not prop:
        return {}
    db = get_db()
    pid = prop["id"]

    def n(table: str) -> int:
        return len(db.table(table).select("id").eq("property_id", pid)
                   .limit(1000).execute().data or [])

    web = (db.table("hotel_websites").select("status,safar_path")
           .eq("property_id", pid).limit(1).execute().data or [{}])[0]
    return {
        "propertyId": pid, "partnerId": prop["partner_id"], "slug": prop["slug"],
        "status": prop["status"], "roomTypes": n("room_types"), "rooms": n("rooms"),
        "ratePlans": n("rate_plans"), "services": n("services"),
        "knowledge": n("property_knowledge"), "agents": n("agents"),
        "reservations": n("reservations"),
        "website": web.get("status"), "websitePath": web.get("safar_path"),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true",
                    help="report what exists and change nothing")
    args = ap.parse_args()

    print(f"\n  {HOTEL}\n  {'─' * 60}")
    result = await build(status_only=args.status)
    if not result:
        return 1

    print(f"  {'─' * 60}")
    for k, v in result.items():
        print(f"  {k:<14} {v}")
    print(f"\n  owner login  {OWNER_EMAIL} / {PASSWORD}")
    print(f"  hotel site   https://partner.safar.example.com/stay/{result['slug']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
