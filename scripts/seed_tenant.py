#!/usr/bin/env python3
"""
A hotel that did not exist five seconds ago, built the way a real one is.

    ./venv/bin/python scripts/seed_tenant.py --tag alpha
    ./venv/bin/python scripts/seed_tenant.py --tag alpha --minimal
    ./venv/bin/python scripts/seed_tenant.py --tag alpha --destroy

── why this exists ──────────────────────────────────────────────────────────

Every live measurement in this system is Casa Mandala. It has four room names,
no two sharing a word; six rate plans; group tiers; negotiation rules at three
scopes; two years of conversations; and an agent configuration that has been
edited by hand across a dozen passes of this work. **Nothing that passes
against it proves a new hotel works**, because a new hotel has none of that —
and the failures that matter to a hotel on its first day are exactly the ones
a mature tenant cannot exhibit.

So this builds one from nothing, through the same services the signup screen
calls. `onboarding.create_hotel`, `ensure_agents`, `add_room_type_with_price`,
`add_rate_plan`, `add_rooms`, `update_property`, `activate` — in that order,
because that is the order a hotelier does them in, and a seed that INSERTs
around them would prove the tables are writable rather than that the product
works.

── the safety rails ─────────────────────────────────────────────────────────

This writes to the same database production uses. There is no second one. So
the rails are not "be careful", they are structural:

  **Ids are derived, never chosen.** Every id comes from `uuid5` over the tag,
  so the same tag always names the same tenant and no id can collide with a
  real one by accident.

  **A marker in the name, checked before every destructive statement.**
  `_MARKER` appears in the property name, the partner brand and the profile
  email. `--destroy` reads each row back and refuses to delete anything whose
  name does not carry it — so a manifest that has been edited, or a tag that
  happens to match something real, deletes nothing.

  **It refuses to touch a property it did not create.** `_own()` is called on
  every write path and raises on anything whose name lacks the marker.

  **No production data is read, copied or referenced.** Every value below is
  written here. No guest name, phone or message comes from a real row.

── what makes it realistic rather than convenient ───────────────────────────

Seed data that is too clean hides the bugs it exists to find. So, deliberately:

  - two room names that collide (`Garden Standard` / `Garden Deluxe`)
  - a room named with a bare number (`Suite 1`, `Suite 2`)
  - room types with different capacities, one of them zero-availability
  - three rate plans, one of them breakfast-included and one an add-on
  - bookings in six states, including partially paid and cancelled
  - an escalation waiting, one resolved
  - a conversation carrying facts that contradict the reservation
  - and, with `--minimal`, a hotel that configured none of the optional half
"""
import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db.client import get_db  # noqa: E402
from app.services.pms import onboarding  # noqa: E402

# Everything this script creates carries it, and `--destroy` will not remove a
# row without it. Chosen to be something no hotel would ever name itself.
_MARKER = "ZZ-SEED"

# One namespace, so a tag always names the same tenant and never a real one.
_NS = uuid.UUID("5eed0000-0000-4000-8000-000000000000")

MANIFEST_DIR = pathlib.Path(__file__).resolve().parent.parent / ".seed"


def ident(tag: str, thing: str) -> str:
    return str(uuid.uuid5(_NS, f"{tag}:{thing}"))


def manifest_path(tag: str) -> pathlib.Path:
    return MANIFEST_DIR / f"{tag}.json"


class NotOurs(RuntimeError):
    """A row this script did not create. Nothing is written to it."""


def _ensure_account(db, email: str, full_name: str) -> str:
    """
    The auth user a signup would have created, idempotently.

    Returns the profile id. Looks the address up first so a second run of the
    same tag reuses the account rather than failing on a duplicate — which is
    what makes seed → test → cleanup → seed again work.
    """
    existing = (db.table("profiles").select("id,email")
                .eq("email", email).limit(1).execute().data or [])
    if existing:
        return existing[0]["id"]

    made = db.auth.admin.create_user({
        "email": email,
        "password": uuid.uuid4().hex,
        "email_confirm": True,
        "user_metadata": {"full_name": full_name, "seeded": _MARKER},
    })
    user_id = made.user.id

    # The profile row the signup trigger normally writes. Upserted rather than
    # inserted because some projects have that trigger and some do not, and a
    # seed that assumed either would break on the other.
    db.table("profiles").upsert(
        {"id": user_id, "email": email, "full_name": full_name},
        on_conflict="id").execute()
    return user_id


def _own(db, property_id: str) -> dict:
    """
    Read the property back and prove it is ours before writing to it.

    Called on every write path rather than once at the start. The failure this
    prevents is not a typo in the argument — it is a tag colliding with
    something real, or a manifest edited by hand, and both of those survive a
    check that happens only at the beginning.
    """
    rows = (db.table("properties").select("id,name,partner_id,status")
            .eq("id", property_id).limit(1).execute().data or [])
    if not rows:
        raise NotOurs(f"no property {property_id}")
    if _MARKER not in (rows[0].get("name") or ""):
        raise NotOurs(
            f"property {property_id} is named {rows[0].get('name')!r}, which does "
            f"not carry {_MARKER} — refusing to touch it")
    return rows[0]


# ── the hotel ───────────────────────────────────────────────────────────────

ROOM_TYPES = [
    # (name, code, capacity, base price, units, note)
    #
    # The first two collide on their first word and the resolver has to ask.
    # The next two differ only by a digit. The fifth has a capacity nothing
    # else has, and the sixth is deliberately sold out.
    ("Garden Standard", "GST", 2, 320000, 6, "the ordinary room"),
    ("Garden Deluxe", "GDX", 2, 420000, 4, "collides with the above"),
    ("Suite 1", "SU1", 3, 680000, 2, "a bare number distinguishes it"),
    ("Suite 2", "SU2", 3, 680000, 2, "and from this one"),
    ("Family Room", "FAM", 5, 540000, 3, "the only one that sleeps five"),
    ("Attic Single", "ATT", 1, 180000, 1, "deliberately sold out"),
]

MINIMAL_ROOM_TYPES = [
    ("Standard", "STD", 2, 300000, 4, "one generic name, as a new hotel writes it"),
    ("Deluxe", "DLX", 2, 450000, 2, "and a second"),
]

# The plans every room type is additionally sold on. `add_room_type_with_price`
# already creates the room-only plan, so these are the two on top of it: one
# that includes breakfast, and one that is cheaper because it cannot be
# cancelled. A hotel with only one plan can never exercise "which rate is
# this quote on", which is half of what the negotiation telemetry reports.
EXTRA_PLANS = [
    # (name, code suffix, meal plan, price delta)
    ("With Breakfast", "CP", "CP", 45000),
    ("Non-Refundable", "NR", "EP", -30000),
]


async def build(tag: str, *, minimal: bool = False) -> dict:
    """
    Create the tenant. Idempotent: running twice returns the same ids.

    `minimal` is the §14 hotel — rooms, a basic rate, and none of the optional
    half. It exists because "no group pricing configured" has to produce an
    honest answer rather than an invented discount, and the only way to know is
    to have a hotel that genuinely has none.
    """
    db = get_db()
    profile_id = ident(tag, "profile")
    email = f"{_MARKER.lower()}-{tag}@example.invalid"
    hotel_name = f"{_MARKER} {tag.title()} House"

    # **A real auth user, because `profiles.id` references `auth.users`.**
    #
    # The first version of this upserted a profile row with a derived uuid and
    # got a foreign key violation, which is the database being right: a profile
    # without an account behind it is not a state this product can reach, and a
    # seed that faked one would be testing a shape production never has.
    #
    # So the account is created through Supabase Auth's admin API — the same
    # user row a signup produces — and its id is what everything below hangs
    # from. That makes the derived uuid a *lookup key* rather than the id
    # itself: the tag still names one tenant, via the email address.
    profile_id = _ensure_account(db, email, f"{_MARKER} Owner {tag}")

    created = await onboarding.create_hotel(
        profile_id=profile_id, hotel_name=hotel_name, city="Panjim",
        owner_name=f"{_MARKER} Owner {tag}", phone="",
    )
    property_id = created["propertyId"]
    partner_id = created["partnerId"]
    _own(db, property_id)

    record: dict = {
        "tag": tag, "minimal": minimal,
        "profile_id": profile_id, "partner_id": partner_id,
        "property_id": property_id, "name": hotel_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "room_types": {}, "rate_plans": {}, "reservations": {},
        "conversations": {}, "escalations": [],
    }

    # ── rooms ───────────────────────────────────────────────────────────────
    #
    # **A rate plan belongs to a room type, not to the property.** The first
    # version of this created three property-wide plans, which is how a person
    # describes a hotel's pricing and not how this schema holds it — and it is
    # the sort of thing only building a hotel from nothing surfaces, because
    # Casa Mandala's six plans were already there.
    wanted = MINIMAL_ROOM_TYPES if minimal else ROOM_TYPES
    for name, code, capacity, price, units, _note in wanted:
        existing = (db.table("room_types").select("id,name")
                    .eq("property_id", property_id).eq("code", code)
                    .limit(1).execute().data or [])
        if existing:
            record["room_types"][name] = existing[0]["id"]
            continue
        made = await onboarding.add_room_type_with_price(
            property_id=property_id, partner_id=partner_id,
            name=name, code=code, max_occupancy=capacity, total_units=units,
            price_paise=price, meal_plan="EP", bed_type="queen",
        )
        room_type_id = made["id"]
        record["room_types"][name] = room_type_id

        # Real rooms behind the type, numbered as a hotel numbers them.
        await onboarding.add_rooms(
            property_id=property_id, partner_id=partner_id,
            room_type_id=room_type_id,
            numbers=[f"{code}{n:02d}" for n in range(1, units + 1)],
        )

        # The other plans this room is sold on. A full hotel gets breakfast and
        # a non-refundable rate; a minimal one gets neither, which is the point
        # of it.
        if minimal:
            continue
        for plan_name, plan_code, meal, delta in EXTRA_PLANS:
            plan = await onboarding.add_rate_plan(
                property_id=property_id, partner_id=partner_id,
                room_type_id=room_type_id,
                price_paise=max(100000, price + delta),
                name=plan_name, meal_plan=meal, code=f"{code}-{plan_code}",
            )
            record["rate_plans"][f"{name} · {plan_name}"] = plan["id"]

    # ── policies ────────────────────────────────────────────────────────────
    if not minimal:
        await onboarding.update_property(property_id=property_id, fields={
            "check_in_time": "14:00", "check_out_time": "11:00",
            "policies": {
                "breakfast": "Breakfast 7:30–10:30 in the courtyard; included "
                             "on the With Breakfast plan only.",
                "pets": "Small pets welcome, ₹500 a night.",
                "smoking": "No smoking indoors.",
                "cancellation": "Free cancellation up to 48 hours before "
                                "arrival on flexible plans.",
                "parking": "Free on-site parking, six spaces.",
                "wifi": "Free wi-fi throughout.",
            },
        })

    # ── inventory ───────────────────────────────────────────────────────────
    _seed_inventory(db, record, minimal=minimal)

    # ── group tiers, or deliberately none ───────────────────────────────────
    if not minimal:
        _seed_group_tiers(db, record)

    # ── negotiation rules, or deliberately none ─────────────────────────────
    if not minimal:
        _seed_negotiation(db, record)

    # ── bookings, conversations, escalations ────────────────────────────────
    if not minimal:
        _seed_bookings(db, record)
        _seed_conversations(db, record)
        _seed_escalations(db, record)

    await onboarding.activate(property_id)
    record["status"] = (db.table("properties").select("status")
                        .eq("id", property_id).limit(1).execute().data or [{}])[0].get("status")

    # **The manifest is read back, not accumulated.**
    #
    # Built up during creation it recorded only what *this run* made, so a
    # second run against an existing tenant wrote a manifest saying "0 rate
    # plans" about a hotel with eighteen of them. What the file has to describe
    # is the tenant, not the run.
    record["counts"] = _counts(db, property_id)
    record["room_types"] = {
        r["name"]: r["id"] for r in
        (db.table("room_types").select("id,name")
         .eq("property_id", property_id).execute().data or [])}
    record["rate_plans"] = {
        f"{r['code']}": r["id"] for r in
        (db.table("rate_plans").select("id,code")
         .eq("property_id", property_id).execute().data or [])}

    MANIFEST_DIR.mkdir(exist_ok=True)
    manifest_path(tag).write_text(json.dumps(record, indent=1, default=str))
    return record


def _counts(db, property_id: str) -> dict:
    """What the tenant actually holds, for the report and for the repeat test."""
    counts = {}
    for table in ("room_types", "rate_plans", "rooms", "room_inventory",
                  "reservations", "conversations", "group_rate_tiers",
                  "negotiation_rules", "agents", "agent_escalations", "quotes"):
        try:
            counts[table] = (db.table(table).select("id", count="exact")
                             .eq("property_id", property_id).execute().count or 0)
        except Exception:  # noqa: BLE001 — a table without property_id
            continue
    return counts


def _seed_inventory(db, record: dict, *, minimal: bool) -> None:
    """
    Ninety nights of availability, with two deliberate holes.

    `Attic Single` is sold out for the whole window and `Suite 2` is sold out
    for one weekend, because "no rooms on those nights" is an answer the agent
    has to give honestly and a hotel with infinite availability never asks for
    it.
    """
    property_id, partner_id = record["property_id"], record["partner_id"]
    _own(db, property_id)
    today = date.today()
    rows = []
    for name, room_type_id in record["room_types"].items():
        total = (db.table("room_types").select("total_units")
                 .eq("id", room_type_id).limit(1).execute().data or [{}])[0]
        units = total.get("total_units") or 2
        for offset in range(90):
            day = today + timedelta(days=offset)
            # **Sold out is `units_blocked`, not `units_booked`.**
            #
            # The first version wrote the capacity into `units_booked` with no
            # reservation behind it, and `reconcile_inventory` correctly called
            # all 96 of those nights stranded — 90 Attic Single nights and one
            # Suite 2 weekend. It was right: `units_booked` means a stay exists,
            # and a hotel that has taken rooms off the market has *blocked*
            # them. Two different facts that both make a night unsellable, and
            # only one of them has a guest attached.
            #
            # A seed that expressed it the wrong way would have made every
            # freshly seeded hotel fail its own reconciliation on day one.
            blocked = 0
            if name == "Attic Single":
                blocked = units
            elif name == "Suite 2" and 20 <= offset <= 22:
                blocked = units
            rows.append({
                "property_id": property_id, "partner_id": partner_id,
                "room_type_id": room_type_id, "date": day.isoformat(),
                "units_total": units, "units_booked": 0,
                "units_held": 0, "units_blocked": blocked, "stop_sell": False,
                # `room_inventory_source_check` allows partner / booking /
                # hold. A hotel setting its own availability is `partner`,
                # which is what this is — and a seed inventing a fourth value
                # would have been a row the product cannot produce.
                "source": "partner",
            })
    for chunk in range(0, len(rows), 400):
        db.table("room_inventory").upsert(
            rows[chunk:chunk + 400],
            on_conflict="room_type_id,date").execute()


def _seed_group_tiers(db, record: dict) -> None:
    """
    Two bands, and only ever two.

    **The first version inserted unconditionally and four runs left eight
    tiers** — 5–9 at 8% appearing four times. Nothing crashed and nothing
    complained; the hotel simply had a pricing table that made no sense, which
    is the exact failure mode a seed system exists to not have. §13 asks that
    seed → test → cleanup → seed again not accumulate corrupted state, and this
    was accumulating it inside a single seed.
    """
    _own(db, record["property_id"])
    existing = (db.table("group_rate_tiers").select("id,min_units")
                .eq("property_id", record["property_id"]).execute().data or [])
    if existing:
        # Rebuild rather than add to. A partial set from an interrupted run is
        # worse than none, and re-deriving is cheap.
        db.table("group_rate_tiers").delete().eq(
            "property_id", record["property_id"]).execute()
    db.table("group_rate_tiers").insert([
        {"property_id": record["property_id"], "partner_id": record["partner_id"],
         "min_units": 5, "max_units": 9, "discount_pct": 8},
        {"property_id": record["property_id"], "partner_id": record["partner_id"],
         "min_units": 10, "max_units": 20, "discount_pct": 14},
    ]).execute()


def _seed_negotiation(db, record: dict) -> None:
    """
    Rules at two scopes, so `rule_source` has something to distinguish: a hotel
    default, and one room type held firmer than it.

    Rebuilt rather than inserted, for the reason in `_seed_group_tiers` — and
    because the hotel-scope row and the room-type row collide on the same
    unique index when `room_type_id` is null, so a second run of the naive
    version left exactly one rule and no error.
    """
    _own(db, record["property_id"])
    db.table("negotiation_rules").delete().eq(
        "property_id", record["property_id"]).execute()

    rows = [{
        "property_id": record["property_id"], "partner_id": record["partner_id"],
        # `property`, not `hotel` — the scope vocabulary is property /
        # room_type / rate_plan, and `negotiation_rules_scope_check` enforces
        # it. Worth the failure: "hotel default" is what the telemetry calls
        # this scope in prose, and the prose is not the schema.
        "scope": "property", "enabled": True,
        "auto_pct": 8, "approval_pct": 15, "floor_pct": 20,
    }]
    suite = record["room_types"].get("Suite 1")
    if suite:
        rows.append({
            "property_id": record["property_id"], "partner_id": record["partner_id"],
            "scope": "room_type", "room_type_id": suite, "enabled": True,
            "auto_pct": 3, "approval_pct": 6, "floor_pct": 10,
        })
    for row in rows:
        try:
            db.table("negotiation_rules").insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"negotiation rule ({row['scope']}) could not be seeded: {exc}"
            ) from exc


def _seed_bookings(db, record: dict) -> None:
    """
    Six reservations in six states.

    Written directly rather than through the booking path, and that is a
    deliberate exception with a cost attached: the states below include ones
    the product cannot *produce* on demand — a failed payment, a partially paid
    stay, a booking that expired. `first_guest_e2e` covers the real creation
    flow, and this covers the shapes a hotel accumulates over months.

    They move no counter, exactly like the fixtures that caused the oversell —
    so `_seed_inventory` above accounts for them, and `--destroy` removes them.
    """
    property_id, partner_id = record["property_id"], record["partner_id"]
    _own(db, property_id)
    today = date.today()
    garden = record["room_types"].get("Garden Standard")
    family = record["room_types"].get("Family Room")

    states = [
        ("confirmed", "paid", 2, 640000, 640000, garden, 30),
        ("confirmed", "token_paid", 1, 320000, 64000, garden, 40),
        ("pending", "unpaid", 1, 320000, 0, garden, 45),
        ("cancelled", "refunded", 1, 320000, 0, garden, 50),
        ("confirmed", "partially_paid", 1, 540000, 200000, family, 55),
        # **There is no `failed` payment status, and that is a finding.**
        #
        # The brief asks for a failed payment among the seeded states, and
        # `reservations_payment_status_check` does not admit one: the closed set
        # is unpaid / token_paid / partially_paid / paid / refunded. A payment
        # that fails leaves the reservation `unpaid`, and the attempt lives in
        # `payment_orders` rather than on the stay.
        #
        # That is defensible — a stay is not "failed", it is simply not paid for
        # — but it means "did this guest's card decline?" cannot be answered
        # from the reservation, and the agent answering a guest about a failed
        # payment has to be looking somewhere else. Seeded as the state the
        # product actually reaches, with the order beside it.
        ("pending", "unpaid", 1, 540000, 0, family, 60),
    ]
    # `reservations.rate_plan_id` is NOT NULL — a stay is always sold *on* a
    # plan, and a seed that left it out was describing a booking that cannot
    # exist. Read back per room type rather than assumed.
    def plan_for(room_type_id: str) -> str | None:
        found = (db.table("rate_plans").select("id")
                 .eq("property_id", property_id).eq("room_type_id", room_type_id)
                 .order("created_at").limit(1).execute().data or [])
        return found[0]["id"] if found else None

    made = []
    for i, (status, payment, units, total, paid, room_type_id, offset) in enumerate(states):
        if not room_type_id:
            continue
        rate_plan_id = plan_for(room_type_id)
        if not rate_plan_id:
            continue
        check_in = today + timedelta(days=offset)
        row = {
            "id": ident(record["tag"], f"reservation-{i}"),
            "ref": f"{_MARKER[:2]}-{record['tag'][:3].upper()}{i}{i}{i}",
            "property_id": property_id, "partner_id": partner_id,
            "room_type_id": room_type_id, "rate_plan_id": rate_plan_id,
            "guest_name": f"{_MARKER} Guest {i}",
            "guest_phone": f"+9199000000{i:02d}",
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=2)).isoformat(),
            "nights": 2, "units": units, "adults": 2, "children": 0,
            "gross_paise": total, "discount_paise": 0, "tax_paise": 0,
            "total_paise": total, "paid_paise": paid,
            "source": "direct", "status": status, "payment_status": payment,
            # `reservations_created_by_check` is a closed set. "import" is what
            # a hotel's own back-office writes and is the honest label for a
            # row that did not come through the booking path — inventing a
            # "seed" value would have been a row the product cannot produce.
            "created_by": "import",
        }
        db.table("reservations").upsert(row, on_conflict="id").execute()
        made.append(row["id"])
        record["reservations"][f"{status}/{payment}/{i}"] = row["id"]

    # Every live one of those has to be in the counter, or the hotel is
    # oversold from the moment it is seeded — which is the exact bug this whole
    # pass began with.
    _account_for(db, record)


def _account_for(db, record: dict) -> None:
    """
    Put every live reservation into `room_inventory`, night by night.

    **This incremented, and three runs left the counter at three times the
    truth** — 96 stranded room-nights on a hotel that had been seeded three
    times, which is the same defect this whole pass began with, produced by the
    tool written to test for it. Adding is only correct once; the truth about a
    night is a *sum over the reservations*, not a history of edits to a counter,
    which is exactly what `reconcile_inventory` says in its own docstring.

    So it recomputes from every live reservation the property has and writes
    that, and running it any number of times leaves the same numbers.
    """
    property_id = record["property_id"]
    _own(db, property_id)

    rows = (db.table("reservations")
            .select("id,status,check_in,check_out,units,room_type_id")
            .eq("property_id", property_id).limit(2000).execute().data or [])
    sold: dict[tuple[str, str], int] = {}
    for row in rows:
        if (row.get("status") or "").lower() in ("cancelled", "no_show"):
            continue
        if not (row.get("room_type_id") and row.get("check_in") and row.get("check_out")):
            continue
        day = date.fromisoformat(row["check_in"][:10])
        end = date.fromisoformat(row["check_out"][:10])
        while day < end:
            key = (row["room_type_id"], day.isoformat())
            sold[key] = sold.get(key, 0) + (row.get("units") or 1)
            day += timedelta(days=1)

    inventory = (db.table("room_inventory")
                 .select("id,room_type_id,date,units_booked")
                 .eq("property_id", property_id).limit(20000).execute().data or [])
    for row in inventory:
        want = sold.get((row["room_type_id"], row["date"][:10]), 0)
        if (row.get("units_booked") or 0) == want:
            continue
        db.table("room_inventory").update({"units_booked": want}) \
            .eq("id", row["id"]).execute()


def _seed_conversations(db, record: dict) -> None:
    """
    Two threads. One ordinary, and one carrying a guest claim that the
    reservation contradicts — which is the §2 case with a real row behind it.
    """
    _own(db, record["property_id"])
    tag = record["tag"]
    booked = record["reservations"].get("confirmed/token_paid")
    for key, handle, profile, reservation in (
        ("ordinary", f"{_MARKER.lower()}_{tag}_a",
         {"language": "hinglish"}, None),
        ("contradicted", f"{_MARKER.lower()}_{tag}_b",
         {"language": "en", "stage": "quoted"}, booked),
    ):
        row = {
            "id": ident(tag, f"conversation-{key}"),
            "property_id": record["property_id"],
            "partner_id": record["partner_id"],
            "channel": "whatsapp", "guest_handle": handle,
            "handler": "ai", "status": "open",
            "guest_profile": profile,
        }
        if reservation:
            row["reservation_id"] = reservation
        db.table("conversations").upsert(row, on_conflict="id").execute()
        record["conversations"][key] = row["id"]


def _seed_escalations(db, record: dict) -> None:
    """
    One waiting, one resolved.

    The columns are `reason` and `payload`, not `question` — the first version
    guessed and the insert failed into a `print`, so the seed reported success
    while creating nothing. Escalations are now raised loudly: a seed that
    silently omits half of what it claims is worse than one that stops.
    """
    _own(db, record["property_id"])
    tag = record["tag"]
    now = datetime.now(timezone.utc).isoformat()
    # The status vocabulary is open / notified / responded — not
    # waiting / resolved, which is how a person describes them. "waiting" is
    # `notified`: the hotel has been asked and has not answered.
    for key, status in (("waiting", "notified"), ("resolved", "responded")):
        row = {
            "id": ident(tag, f"escalation-{key}"),
            "property_id": record["property_id"],
            "partner_id": record["partner_id"],
            "conversation_id": record["conversations"].get("ordinary"),
            # The kind vocabulary is the one `escalation.compose` writes for:
            # discount_request / group_inquiry / special_request /
            # transfer_request / payment_issue / cancellation_exception /
            # unanswered. "approval" is what this is *about* and not what the
            # column holds — a distinction the check constraint made for me.
            "kind": "discount_request" if key == "waiting" else "payment_issue",
            "status": status,
            "reason": f"{_MARKER} seeded {key} escalation",
            "dedupe_key": f"{_MARKER}:{tag}:{key}",
            "payload": {"seeded": True, "tag": tag},
        }
        if status == "notified":
            row["notified_at"] = now
        else:
            row["notified_at"] = now
            row["responded_at"] = now
            row["resolution"] = "approved"
        db.table("agent_escalations").upsert(row, on_conflict="id").execute()
        record["escalations"].append(row["id"])


# ── removal ─────────────────────────────────────────────────────────────────

# Child-first, so a foreign key never blocks the parent.
_TABLES = (
    "agent_escalations", "agent_actions", "agent_runs", "conversation_messages",
    "conversations", "quotes", "reservations", "room_inventory", "rooms",
    "negotiation_rules", "group_rate_tiers", "rate_plans", "room_types",
    "agent_configs", "agents",
)


def destroy(tag: str) -> dict:
    """
    Remove everything this tag created, and nothing else.

    The property is read back and its name checked *first*. A manifest is a
    file on disk and can be edited, moved or written by something else; the
    marker in the row is the thing that cannot be faked by editing json.
    """
    path = manifest_path(tag)
    if not path.exists():
        return {"tag": tag, "removed": 0, "note": "no manifest"}
    record = json.loads(path.read_text())
    db = get_db()

    try:
        _own(db, record["property_id"])
    except NotOurs as exc:
        return {"tag": tag, "removed": 0, "refused": str(exc)}

    removed = 0
    for table in _TABLES:
        try:
            before = (db.table(table).select("id", count="exact")
                      .eq("property_id", record["property_id"]).execute())
            db.table(table).delete().eq("property_id", record["property_id"]).execute()
            removed += before.count or 0
        except Exception:  # noqa: BLE001 — a table without property_id, or none to remove
            continue

    for table, column, value in (
        ("properties", "id", record["property_id"]),
        ("partner_members", "partner_id", record["partner_id"]),
        ("partners", "id", record["partner_id"]),
        ("profiles", "id", record["profile_id"]),
    ):
        try:
            db.table(table).delete().eq(column, value).execute()
            removed += 1
        except Exception:  # noqa: BLE001
            continue

    # The account itself. Last, because the profile references it, and guarded
    # on the seeded marker in its metadata — an id from an edited manifest must
    # not be able to delete somebody's login.
    try:
        user = db.auth.admin.get_user_by_id(record["profile_id"])
        if (user.user.user_metadata or {}).get("seeded") == _MARKER:
            db.auth.admin.delete_user(record["profile_id"])
            removed += 1
    except Exception:  # noqa: BLE001
        pass

    path.unlink()
    return {"tag": tag, "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True,
                        help="names the tenant; the same tag always names the same one")
    parser.add_argument("--minimal", action="store_true",
                        help="rooms and a basic rate, and none of the optional half")
    parser.add_argument("--destroy", action="store_true")
    args = parser.parse_args()

    if not args.tag.isalnum():
        print("the tag must be alphanumeric — it becomes part of a slug and an email")
        return 2

    if args.destroy:
        print(json.dumps(destroy(args.tag), indent=1))
        return 0

    record = asyncio.run(build(args.tag, minimal=args.minimal))
    print(json.dumps({k: v for k, v in record.items()
                      if k not in ("room_types", "rate_plans")}, indent=1))
    print(f"   {len(record['room_types'])} room types, "
          f"{len(record['rate_plans'])} rate plans, "
          f"{len(record['reservations'])} reservations, status={record.get('status')}")
    print(f"   manifest: {manifest_path(args.tag)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
