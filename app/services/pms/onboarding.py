"""
Getting a hotel from "nobody has heard of us" to "sellable".

Everything else in `services/pms` assumes a property already exists, with room
types in it and a rate on each one. Nothing created any of that. Seven hotels
reached the production database and every one arrived through a script — the
seed, the pilot fixture, or `migrate_partners.py`. There was no way for a hotel
owner to make one, which is the whole of what "cannot register" meant.

    signup (Supabase, in the browser)
        └── create_hotel()      partner + property + the five agents
              └── add_room_type()   what you sell
                    └── add_rate_plan()  what it costs
                          └── activate()  status: setup → active

**Why the password never reaches this service.** The account is created by
Supabase Auth directly from the PWA, and this runs afterwards holding the JWT
that produced. So there is no route here that takes a password, no route that
can create a user, and email verification stays Supabase's job rather than
becoming a thing we half-implement.

**Idempotent at the top.** `partners.owner_id` is `not null unique` — one
person owns at most one partner — so `create_hotel` called twice returns the
same hotel rather than failing on the constraint or making a second one. A
hotelier who double-taps "Create my hotel" on a slow connection gets one hotel.

**₹0 is refused here, not in the database.** `rate_plans.base_price_paise` is
`check (>= 0)`, which is right for the column — an internal or comped plan is a
real thing — and wrong for the one path where an owner types a number into a
box. A room selling at ₹0 is the single worst outcome in this system, so the
check that matters lives where the typing happens.
"""
import asyncio
import logging
from datetime import datetime, timezone
import re
import unicodedata

from fastapi import HTTPException, status

from app.db.client import get_db, run_db
from app.services.ai import provisioning
from app.services.ai.errors import AgentNotConfigured

logger = logging.getLogger(__name__)

# The five agents a hotel gets. Provisioned from the code definitions rather
# than copied off a seeded hotel: the pilot fixture copies Casa Mandala's rows,
# which is fine for a fixture and would make a real hotel's AI employee depend
# on a demo property nobody promised to keep.
AGENT_TYPES = ("guest", "frontdesk", "sales", "marketing", "owner")

# A nightly rate above this is a typo — almost always paise typed into a rupee
# field, which is the specific mistake `migrate_partners.py` found in live data
# (a ₹2,222 room listed at ₹22,222). ₹10,00,000 a night is above any real room
# in India and below the paise-shaped mistakes.
MAX_RATE_PAISE = 100_000_000
MIN_RATE_PAISE = 1


def slugify(value: str) -> str:
    """URL-safe, ASCII, no leading or trailing dashes."""
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return re.sub(r"-{2,}", "-", value)


def _unique_slug(db, table: str, base: str) -> str:
    """
    `base`, or `base-2`, `base-3`… — the first one free.

    Two hotels called "Green View" in two cities is ordinary, and a signup that
    fails on a unique index with "duplicate key value violates" is not something
    to show a hotel owner.
    """
    base = base or "hotel"
    taken = {
        r["slug"]
        for r in (
            db.table(table).select("slug").like("slug", f"{base}%").execute().data or []
        )
    }
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _bad(message: str) -> HTTPException:
    """422 with a sentence a hotelier can act on, never a constraint name."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)


def check_price_paise(price_paise: int | None) -> None:
    """
    The guard, on its own so it can run *before* anything is written.

    It used to live only inside `_add_rate_plan`, which meant creating a room
    type and its price in one request validated the price second: a ₹0 rate was
    correctly refused, and the room type had already been inserted. The hotel
    was then left holding a room with no price — the exact state this module
    exists to prevent, reached by the code preventing it.
    """
    if price_paise is None or price_paise < MIN_RATE_PAISE:
        raise _bad("A room cannot be sold at ₹0. Enter what one night costs.")
    if price_paise > MAX_RATE_PAISE:
        raise _bad(
            f"That is more than ₹{MAX_RATE_PAISE // 100:,} for one night — "
            "enter the price in rupees, not paise."
        )


# ── creating the hotel ───────────────────────────────────────────────────────

def _create_hotel(
    profile_id: str,
    hotel_name: str,
    city: str,
    owner_name: str | None,
    phone: str | None,
) -> dict:
    db = get_db()

    hotel_name = (hotel_name or "").strip()
    city = (city or "").strip()
    if len(hotel_name) < 2:
        raise _bad("Please enter your hotel's name.")
    if len(city) < 2:
        raise _bad("Please enter the city your hotel is in.")

    # One partner per person, so a second call is a lookup rather than an insert.
    existing = (
        db.table("partners").select("id,slug").eq("owner_id", profile_id).limit(1)
        .execute().data or []
    )

    if existing:
        partner_id = existing[0]["id"]
        created_partner = False
    else:
        partner_id = (
            db.table("partners")
            .insert({
                "owner_id": profile_id,
                "slug": _unique_slug(db, "partners", slugify(hotel_name)),
                "brand_name": hotel_name,
                # `partners.whatsapp` is NOT NULL with no default, and the
                # signup form asks for it as *optional* — StayOS's own advice
                # is not to demand anything a hotel does not need to get
                # started, and a hotel can be set up, priced and booked
                # directly without one. So an owner who skips it wrote a null
                # into a not-null column and got a 500 on "Create my hotel".
                # Empty means "not given yet"; the hotel adds it when it
                # connects WhatsApp.
                "whatsapp": (phone or "").strip(),
                "status": "active",
            })
            .execute()
            .data[0]["id"]
        )
        created_partner = True

    # The owner's own staff row.
    #
    # `deps/property._load` and `workspace._memberships_for` both fall back to
    # `partners.owner_id`, so an owner without this row can still work — which
    # is exactly why its absence went unnoticed. What it broke was quieter:
    # the Staff screen reads `partner_members`, so every hotel that signed up
    # through this route showed a team with nobody in it, not even the person
    # reading the screen, and "Add your first staff member" as the only thing
    # on it. Three hotels in production are in that state.
    #
    # Written after the partner exists and before anything else, upserted on
    # the (partner, profile) pair so the double-tap path does not make a
    # second one. A failure here must not fail a signup — the fallback is
    # still there and the hotel is still usable.
    try:
        db.table("partner_members").upsert(
            {
                "partner_id": partner_id,
                "profile_id": profile_id,
                "role": "owner",
                # Empty means every property this partner has, now and later.
                # Naming the property here would silently exclude the owner
                # from their own second hotel.
                "property_ids": [],
                "status": "active",
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="partner_id,profile_id",
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not write the owner's member row for partner %s", partner_id,
            exc_info=True,
        )

    # A partner who already has a property is finished here — this is the
    # double-tap path, and making a second hotel would be the wrong repair.
    prop = (
        db.table("properties").select("id,slug,name,status")
        .eq("partner_id", partner_id).order("created_at").limit(1)
        .execute().data or []
    )
    if prop:
        return {
            "partnerId": partner_id,
            "propertyId": prop[0]["id"],
            "slug": prop[0]["slug"],
            "name": prop[0]["name"],
            "status": prop[0]["status"],
            "created": False,
        }

    # `status='setup'` is the column default and is said out loud anyway: a
    # hotel with no rooms and no rates must not be sellable, and `activate()`
    # is what promotes it once it is.
    property_row = (
        db.table("properties")
        .insert({
            "partner_id": partner_id,
            "slug": _unique_slug(db, "properties", slugify(f"{hotel_name}-{city}")),
            "name": hotel_name,
            "city": city,
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "status": "setup",
        })
        .execute()
        .data[0]
    )

    # The owner's name, so folio lines and task events say who did something
    # instead of writing a null. Only filled when the profile has nothing.
    if owner_name:
        try:
            current = (
                db.table("profiles").select("full_name").eq("id", profile_id)
                .limit(1).execute().data or []
            )
            if not current or not (current[0].get("full_name") or "").strip():
                db.table("profiles").update({"full_name": owner_name.strip()}) \
                  .eq("id", profile_id).execute()
        except Exception:  # noqa: BLE001 — a display name must not fail a signup
            logger.warning("could not set full_name for %s", profile_id, exc_info=True)

    logger.info(
        "hotel created property=%s partner=%s new_partner=%s",
        property_row["id"], partner_id, created_partner,
    )
    return {
        "partnerId": partner_id,
        "propertyId": property_row["id"],
        "slug": property_row["slug"],
        "name": property_row["name"],
        "status": property_row["status"],
        "created": True,
    }


async def create_hotel(
    *,
    profile_id: str,
    hotel_name: str,
    city: str,
    owner_name: str | None = None,
    phone: str | None = None,
) -> dict:
    result = await run_db(_create_hotel, profile_id, hotel_name, city, owner_name, phone)

    # Agents are provisioned outside the transaction above and failure is
    # logged rather than raised: a hotel that exists without its AI employee is
    # recoverable (`ensure_agents` re-runs), and a signup that 500s after the
    # property row is written is not something a hotelier can retry out of.
    if result.get("created"):
        await ensure_agents(
            property_id=result["propertyId"],
            partner_id=result["partnerId"],
            created_by=profile_id,
        )
    return result


async def ensure_agents(*, property_id: str, partner_id: str, created_by: str | None = None) -> list[str]:
    """
    The five agents, idempotently.

    Separate from `create_hotel` and public because it is also the repair: the
    hotels migrated from the marketplace were created by a script that never
    called this, so every one of them has been running without the AI employee
    they were sold.
    """
    # Concurrently, because the five are independent and doing them in
    # sequence put 8.3 seconds behind the "Create my hotel" button — measured,
    # on the one screen where a hotel owner is deciding whether this product
    # works. Each agent is four inserts and an RPC; five of those in series is
    # the whole of that wait. Together they take about as long as the slowest
    # one.
    #
    # Still awaited rather than backgrounded: the checklist ticks "your AI
    # employee" off the agents table, and returning before they exist would
    # show a new owner an unticked box that silently ticks itself later.
    results = await asyncio.gather(
        *(
            provisioning.provision(
                property_id=property_id,
                partner_id=partner_id,
                agent_type=agent_type,
                created_by=created_by,
            )
            for agent_type in AGENT_TYPES
        ),
        return_exceptions=True,
    )

    provisioned: list[str] = []
    for agent_type, result in zip(AGENT_TYPES, results):
        if isinstance(result, AgentNotConfigured):
            logger.warning("agent %s not available for %s", agent_type, property_id)
        elif isinstance(result, BaseException):
            logger.exception(
                "could not provision %s for %s", agent_type, property_id,
                exc_info=result,
            )
        else:
            provisioned.append(agent_type)
    return provisioned


# ── what you sell ────────────────────────────────────────────────────────────

def _add_room_type(
    property_id: str,
    partner_id: str,
    name: str,
    total_units: int,
    max_occupancy: int,
    code: str | None,
    description: str | None = None,
    bed_type: str | None = None,
    size_sqft: int | None = None,
    amenities: list[str] | None = None,
    photos: list[str] | None = None,
) -> dict:
    db = get_db()
    name = (name or "").strip()
    if len(name) < 2:
        raise _bad("Give this room a name — what your guests would call it, like Deluxe Double.")
    if total_units < 1 or total_units > 999:
        raise _bad("How many of these rooms do you have? Enter a number between 1 and 999.")
    if max_occupancy < 1 or max_occupancy > 30:
        raise _bad("How many people sleep in this room? Enter a number between 1 and 30.")

    code = (code or "").strip().upper() or re.sub(r"[^A-Z0-9]", "", name.upper())[:6] or "ROOM"

    taken = {
        (r["code"] or "").lower()
        for r in (
            db.table("room_types").select("code").eq("property_id", property_id)
            .execute().data or []
        )
    }
    if code.lower() in taken:
        base, n = code[:4], 2
        while f"{base}{n}".lower() in taken:
            n += 1
        code = f"{base}{n}"

    if any(
        (r["name"] or "").strip().lower() == name.lower()
        for r in (
            db.table("room_types").select("name").eq("property_id", property_id)
            .execute().data or []
        )
    ):
        raise _bad(f"You already have a room type called “{name}”.")

    return (
        db.table("room_types")
        .insert({
            "property_id": property_id,
            "partner_id": partner_id,
            "code": code,
            "name": name,
            "total_units": total_units,
            "max_occupancy": max_occupancy,
            "standard_occupancy": min(2, max_occupancy),
            "description": (description or "").strip() or None,
            "bed_type": (bed_type or "").strip() or None,
            "size_sqft": size_sqft,
            # Trimmed and de-duplicated, because a list a person typed is a
            # list with "AC", "ac" and a trailing space in it.
            "amenities": _clean_list(amenities, limit=30, length=60),
            "photos": _clean_list(photos, limit=12, length=500),
        })
        .execute()
        .data[0]
    )


def _clean_list(values: list[str] | None, *, limit: int, length: int) -> list[str]:
    """Trimmed, de-duplicated, bounded. Order preserved — a hotel lists what it
    thinks matters first."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (values or []):
        item = str(raw or "").strip()[:length]
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def add_room_type(
    *, property_id: str, partner_id: str, name: str,
    total_units: int, max_occupancy: int = 2, code: str | None = None,
    description: str | None = None, bed_type: str | None = None,
    size_sqft: int | None = None, amenities: list[str] | None = None,
    photos: list[str] | None = None,
) -> dict:
    return await run_db(
        _add_room_type, property_id, partner_id, name, total_units, max_occupancy, code,
        description, bed_type, size_sqft, amenities, photos,
    )


def _update_room_type(property_id: str, room_type_id: str, fields: dict) -> dict:
    """
    Change a room that already exists.

    **The gap this closes.** Every one of the 21 room types in production was
    created before the form asked for a description, amenities or photos — and
    there was no route that could add them afterwards. The AI employee that had
    just been taught to send room photos had nothing to send, and a hotelier
    who wanted to fix that could only delete the room, which takes its
    inventory, its rate plans and its history with it.

    `fields` has already been reduced to what the caller actually set: a PATCH
    that could not tell "unchanged" from "cleared" would empty a hotel's
    description the first time somebody renamed a room.
    """
    db = get_db()

    existing = (
        db.table("room_types").select("*")
        .eq("id", room_type_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    if not existing:
        raise _bad("That room type is not one of yours.")
    row = existing[0]

    patch: dict = {}

    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if len(name) < 2:
            raise _bad("Give this room a name — what your guests would call it.")
        clash = [
            r for r in (
                db.table("room_types").select("id,name")
                .eq("property_id", property_id).execute().data or []
            )
            if (r["name"] or "").strip().lower() == name.lower() and r["id"] != room_type_id
        ]
        if clash:
            raise _bad(f"You already have a room type called \u201c{name}\u201d.")
        patch["name"] = name

    if "total_units" in fields:
        units = int(fields["total_units"])
        if units < 1 or units > 999:
            raise _bad("How many of these rooms do you have? Enter a number between 1 and 999.")
        patch["total_units"] = units

    if "max_occupancy" in fields:
        occupancy = int(fields["max_occupancy"])
        if occupancy < 1 or occupancy > 30:
            raise _bad("How many people sleep in this room? Enter a number between 1 and 30.")
        patch["max_occupancy"] = occupancy
        # Kept consistent rather than left behind: a room whose maximum drops to
        # 1 must not still claim a standard occupancy of 2, which is what every
        # indicative price is computed from.
        patch["standard_occupancy"] = min(row.get("standard_occupancy") or 2, occupancy)

    for key, column in (("description", "description"), ("bed_type", "bed_type")):
        if key in fields:
            patch[column] = (str(fields[key] or "").strip() or None)

    if "size_sqft" in fields:
        patch["size_sqft"] = fields["size_sqft"]

    if "amenities" in fields:
        patch["amenities"] = _clean_list(fields["amenities"], limit=30, length=60)

    if "photos" in fields:
        patch["photos"] = _clean_list(fields["photos"], limit=12, length=500)

    if "is_active" in fields:
        patch["is_active"] = bool(fields["is_active"])

    if not patch:
        return row

    patch["updated_at"] = "now()"
    updated = (
        db.table("room_types").update(patch)
        .eq("id", room_type_id).eq("property_id", property_id)
        .execute().data or []
    )
    return updated[0] if updated else row


async def update_room_type(*, property_id: str, room_type_id: str, fields: dict) -> dict:
    """See `_update_room_type`. `fields` is already the set the caller sent."""
    return await run_db(_update_room_type, property_id, room_type_id, fields)


def _delete_room_type(property_id: str, room_type_id: str) -> None:
    db = get_db()
    db.table("room_types").delete().eq("id", room_type_id) \
      .eq("property_id", property_id).execute()


async def add_room_type_with_price(
    *, property_id: str, partner_id: str, name: str, total_units: int,
    max_occupancy: int, code: str | None, price_paise: int | None, meal_plan: str,
    description: str | None = None, bed_type: str | None = None,
    size_sqft: int | None = None, amenities: list[str] | None = None,
    photos: list[str] | None = None,
) -> dict:
    """
    A room and what it costs, as one act.

    Two writes with no transaction between them, so the order is the safety:
    the price is checked before the room type is created, and if the rate insert
    still fails — a code collision, a dropped connection — the room type is
    removed again. A hotel never ends a request holding a room it cannot price.
    """
    if price_paise is not None:
        check_price_paise(price_paise)

    room_type = await add_room_type(
        property_id=property_id, partner_id=partner_id, name=name,
        total_units=total_units, max_occupancy=max_occupancy, code=code,
        description=description, bed_type=bed_type, size_sqft=size_sqft,
        amenities=amenities, photos=photos,
    )
    if price_paise is None:
        return room_type

    try:
        await add_rate_plan(
            property_id=property_id, partner_id=partner_id,
            room_type_id=room_type["id"], price_paise=price_paise, meal_plan=meal_plan,
        )
    except Exception:
        logger.exception(
            "rate plan failed after room type %s was created — rolling it back",
            room_type["id"],
        )
        try:
            await run_db(_delete_room_type, property_id, room_type["id"])
        except Exception:  # noqa: BLE001
            logger.exception("could not roll back room type %s", room_type["id"])
        raise
    return room_type


# ── what it costs ────────────────────────────────────────────────────────────

def _add_rate_plan(
    property_id: str,
    partner_id: str,
    room_type_id: str,
    price_paise: int,
    name: str | None,
    meal_plan: str,
    audience: str,
    code: str | None,
) -> dict:
    db = get_db()

    room_type = (
        db.table("room_types").select("id,name,code")
        .eq("id", room_type_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    if not room_type:
        raise _bad("That room type is not part of this hotel.")

    # The whole reason this function exists rather than a bare insert.
    check_price_paise(price_paise)
    if meal_plan not in ("EP", "CP", "MAP", "AP"):
        raise _bad("Choose what the price includes: room only, breakfast, or meals.")
    if audience not in ("public", "b2b", "corporate", "creator", "internal"):
        raise _bad("That rate audience is not one this hotel can sell to.")

    name = (name or "").strip() or (
        f"{room_type[0]['name']} + Breakfast" if meal_plan == "CP" else room_type[0]["name"]
    )
    code = (code or "").strip().upper() or f"{room_type[0]['code']}-{meal_plan}"

    taken = {
        (r["code"] or "").lower()
        for r in (
            db.table("rate_plans").select("code").eq("property_id", property_id)
            .execute().data or []
        )
    }
    if code.lower() in taken:
        base, n = code[:8], 2
        while f"{base}-{n}".lower() in taken:
            n += 1
        code = f"{base}-{n}"

    return (
        db.table("rate_plans")
        .insert({
            "property_id": property_id,
            "partner_id": partner_id,
            "room_type_id": room_type_id,
            "code": code,
            "name": name,
            "meal_plan": meal_plan,
            "audience": audience,
            "base_price_paise": int(price_paise),
            "refundable": True,
            "inclusions": ["Breakfast"] if meal_plan == "CP" else [],
        })
        .execute()
        .data[0]
    )


async def add_rate_plan(
    *, property_id: str, partner_id: str, room_type_id: str, price_paise: int,
    name: str | None = None, meal_plan: str = "CP", audience: str = "public",
    code: str | None = None,
) -> dict:
    return await run_db(
        _add_rate_plan, property_id, partner_id, room_type_id, price_paise,
        name, meal_plan, audience, code,
    )


# ── the physical rooms ───────────────────────────────────────────────────────

def _add_rooms(property_id: str, partner_id: str, room_type_id: str, numbers: list[str]) -> list[dict]:
    db = get_db()

    room_type = (
        db.table("room_types").select("id").eq("id", room_type_id)
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    if not room_type:
        raise _bad("That room type is not part of this hotel.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in numbers:
        number = (raw or "").strip()
        if not number:
            continue
        if number.lower() in seen:
            raise _bad(f"Room {number} is listed twice.")
        seen.add(number.lower())
        cleaned.append(number)

    if not cleaned:
        raise _bad("Enter at least one room number.")

    taken = {
        (r["number"] or "").lower()
        for r in (
            db.table("rooms").select("number").eq("property_id", property_id)
            .execute().data or []
        )
    }
    clash = [n for n in cleaned if n.lower() in taken]
    if clash:
        raise _bad(f"Room {clash[0]} already exists at this hotel.")

    return (
        db.table("rooms")
        .insert([
            {
                "property_id": property_id,
                "partner_id": partner_id,
                "room_type_id": room_type_id,
                "number": n,
                "housekeeping_status": "clean",
            }
            for n in cleaned
        ])
        .execute()
        .data
    )


async def add_rooms(
    *, property_id: str, partner_id: str, room_type_id: str, numbers: list[str],
) -> list[dict]:
    return await run_db(_add_rooms, property_id, partner_id, room_type_id, numbers)


# ── am I ready ───────────────────────────────────────────────────────────────

def _checklist(property_id: str) -> dict:
    """
    Real progress, read from the rows themselves.

    Nothing here is stored. A `setup_step` column would be a second answer to
    "does this hotel have a rate", and the two would disagree the first time
    somebody deleted one.
    """
    db = get_db()

    prop = (
        db.table("properties").select("id,name,city,status,slug")
        .eq("id", property_id).limit(1).execute().data or []
    )
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    prop = prop[0]

    room_types = (
        db.table("room_types").select("id").eq("property_id", property_id)
        .eq("is_active", True).execute().data or []
    )
    rate_plans = (
        db.table("rate_plans").select("id,base_price_paise")
        .eq("property_id", property_id).eq("is_active", True).execute().data or []
    )
    rooms = (
        db.table("rooms").select("id").eq("property_id", property_id).execute().data or []
    )
    website = (
        db.table("hotel_websites").select("status").eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    agents = (
        db.table("agents").select("id").eq("property_id", property_id)
        .eq("status", "active").execute().data or []
    )

    sellable = [r for r in rate_plans if (r.get("base_price_paise") or 0) > 0]

    steps = [
        {
            "id": "hotel",
            "label": "Hotel details",
            "done": bool(prop["name"] and prop["city"]),
            "required": True,
            "href": "/settings",
        },
        {
            "id": "rooms",
            "label": "Your first room",
            "done": bool(room_types),
            "required": True,
            "href": "/rooms",
        },
        {
            "id": "rates",
            "label": "What a night costs",
            "done": bool(sellable),
            "required": True,
            "href": "/rooms",
        },
        {
            "id": "team",
            "label": "Your AI employee",
            "done": bool(agents),
            "required": False,
            "href": "/agents",
        },
        {
            "id": "website",
            "label": "Your hotel website",
            "done": bool(website) and website[0].get("status") == "published",
            "required": False,
            "href": "/website",
        },
    ]

    required_left = [s for s in steps if s["required"] and not s["done"]]

    return {
        "propertyId": prop["id"],
        "name": prop["name"],
        "status": prop["status"],
        "slug": prop["slug"],
        "steps": steps,
        "remaining": len(required_left),
        # The one question the Today screen actually asks.
        "canGoLive": not required_left,
        "isLive": prop["status"] == "active",
        "counts": {
            "roomTypes": len(room_types),
            "ratePlans": len(sellable),
            "rooms": len(rooms),
            "agents": len(agents),
        },
    }


async def checklist(property_id: str) -> dict:
    return await run_db(_checklist, property_id)


def _activate(property_id: str) -> dict:
    db = get_db()
    state = _checklist(property_id)
    if not state["canGoLive"]:
        missing = ", ".join(s["label"].lower() for s in state["steps"]
                            if s["required"] and not s["done"])
        raise _bad(f"Before you can go live, finish: {missing}.")

    db.table("properties").update({"status": "active"}).eq("id", property_id).execute()
    logger.info("property %s is live", property_id)
    return _checklist(property_id)


async def activate(property_id: str) -> dict:
    return await run_db(_activate, property_id)
