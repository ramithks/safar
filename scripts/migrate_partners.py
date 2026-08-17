#!/usr/bin/env python3
"""
Move a marketplace partner onto the hotel OS, with a year on the house.

    travbuddy.in (uabxtdhxbamuhthxnhid)          partner.travbuddy.in (rdlozq…)
    ──────────────────────────────────          ──────────────────────────────
    partners                          ──────►   partners
    partner_listings (kind='hotel')   ──────►   properties
    listing_room_types (is_active)    ──────►   room_types
      base_price ─┐
                  ├── resolve ────────►   rate_plans (or switched off)
    price_from ───┘
    profiles.email                    ──────►   a new auth user + invite link
                                      ──────►   subscription, 1 year, ₹0
                                      ──────►   credit grant

**Where a room's price comes from.** `listing_room_types.base_price` when the
hotelier set one. When they did not, `partner_listings.price_from` — which is
not a fallback but *the* price the marketplace charges for a hotel room today:
`api/v1/marketplace/payments.py` reads it directly into `quote_stay` with
`quantity = rooms`, and never consults `listing_room_types` at all. A room with
neither is `NEEDS_REVIEW` and reaches the hotel OS with its rate plan switched
off. Nothing here ever produces a sellable ₹0 room. See `resolve_room_price`.

**Read-only against production.** Every statement sent to the marketplace
project is a select. Nothing about the partner's listing, bookings or plan
changes there — the two products run side by side after this: the marketplace
keeps listing, coupons and payouts; the hotel OS runs the hotel.

**Dry run unless you say otherwise.** Without `--apply` this prints exactly what
it would create and writes nothing. Run it that way first; the output is the
review.

**It does not email anybody.** Accounts are created and an invite link is
printed for each one. Sending those is a decision about contacting real
customers, and it belongs to a person, not to this script.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/migrate_partners.py              # dry run, all eligible
    ./venv/bin/python scripts/migrate_partners.py --slug hotel-lotus
    ./venv/bin/python scripts/migrate_partners.py --apply

⚠ Idempotent by construction: the hotel-OS partner keeps the marketplace
partner's uuid. Re-running finds the row and updates rather than duplicating,
and the shared id is what makes "which marketplace partner is this?" answerable
later without a mapping table nobody maintains.
"""
import argparse
import base64
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

MARKETPLACE = "uabxtdhxbamuhthxnhid"
HOTEL_OS = "rdlozqvuywtgsodunait"
HOTEL_OS_URL = f"https://{HOTEL_OS}.supabase.co"

# What a migrated hotel gets. One year, at no charge, on the only plan that
# exists — see `subscription_plans`. The credit figure is the Starter monthly
# allowance times twelve, so a year free means a year's worth of AI rather than
# a month's stretched thin.
FREE_MONTHS = 12
STARTER_PLAN = "starter"
FREE_CREDITS = 2000 * FREE_MONTHS

_raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(_raw.removeprefix("go-keyring-base64:")).decode()


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
            return e.code, text[:400]
    except urllib.error.URLError as e:
        return 0, str(e)


def _query(project: str, sql_text: str, *, attempts: int = 4):
    import time as _time
    last = ""
    for attempt in range(attempts):
        st, body = http(f"https://api.supabase.com/v1/projects/{project}/database/query",
                        "POST", {"query": sql_text},
                        {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
        if st in (200, 201):
            return body
        last = f"{st} {str(body)[:400]}"
        if st and st < 500:
            break
        _time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"SQL failed on {project}: {last}")


def read_production(sql_text: str):
    """
    The only way this script reaches the marketplace, and it refuses anything
    that is not a read.

    Not a comment asking the next person to be careful — a check. Everything
    here is a select, and the one thing that would make this script dangerous is
    somebody adding a statement that is not.
    """
    stripped = re.sub(r"--[^\n]*", "", sql_text).strip().lower()
    if not stripped.startswith("select") or ";" in stripped.rstrip(";"):
        raise RuntimeError(f"refusing to run a non-select against production: {sql_text[:120]}")
    return _query(MARKETPLACE, sql_text)


def write_hotel_os(sql_text: str):
    return _query(HOTEL_OS, sql_text)


_keys = http(f"https://api.supabase.com/v1/projects/{HOTEL_OS}/api-keys?reveal=true",
             headers={"Authorization": f"Bearer {MGMT}",
                      "User-Agent": "SupabaseCLI/2.0.0"})[1]
SERVICE_KEY = [k["api_key"] for k in _keys if k.get("name") == "service_role"][0]


def q(value) -> str:
    """A SQL literal, or NULL. Every value from production goes through here."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# ── what the marketplace knows ───────────────────────────────────────────────

def ineligible_partners() -> list[dict]:
    """
    Active partners this migration will not move, and why.

    Reported rather than omitted. A partner who never appears in the output is
    indistinguishable from one that was fine, and "we migrated everybody" is a
    claim somebody will make on the strength of a report that quietly listed
    four of eighteen.
    """
    return read_production("""
        select p.slug, p.brand_name, p.city, p.stay_plan_status,
               pr.email as owner_email,
               (select count(*) from public.partner_listings l
                 where l.partner_id = p.id) as listings,
               (select count(*) from public.partner_listings l
                 where l.partner_id = p.id and l.kind = 'hotel') as hotel_listings
          from public.partners p
          left join public.profiles pr on pr.id = p.owner_id
         where p.status = 'active'
           and p.stay_plan_status <> 'active'
           and not exists (select 1 from public.partner_listings l
                            where l.partner_id = p.id and l.kind = 'hotel')
         order by p.brand_name""")


def eligible_partners(slug: str | None) -> list[dict]:
    """
    Partners worth moving: one with a hotel listing, or a live stay plan.

    A partner with neither has nothing to run — no rooms, no rates, no bookings
    — and creating an empty hotel for them would hand somebody a login to a
    product with nothing in it.
    """
    where = f"and p.slug = {q(slug)}" if slug else ""
    return read_production(f"""
        select p.id, p.slug, p.brand_name, p.city, p.state, p.country,
               p.whatsapp, p.phone, p.email, p.about,
               p.stay_plan_status, p.stay_plan_expires_at,
               pr.email as owner_email, pr.full_name as owner_name
          from public.partners p
          left join public.profiles pr on pr.id = p.owner_id
         where p.status = 'active'
           and (p.stay_plan_status = 'active'
                or exists (select 1 from public.partner_listings l
                            where l.partner_id = p.id and l.kind = 'hotel'))
           {where}
         order by p.brand_name""")


def best_listing(partner_id: str) -> dict | None:
    """
    The listing to build the hotel from — published if there is one.

    A partner with three drafts and one published listing means the published
    one; a partner with only drafts still gets their hotel, built from the
    fullest draft, because a draft with rooms in it is a hotel somebody
    described and did not finish publishing.
    """
    rows = read_production(f"""
        select l.id, l.slug, l.title, l.city, l.address, l.status,
               l.description, l.summary, l.whatsapp,
               l.price_from, l.original_price, l.price_unit,
               (select count(*) from public.listing_room_types rt
                 where rt.listing_id = l.id and rt.is_active) as room_count
          from public.partner_listings l
         where l.partner_id = {q(partner_id)} and l.kind = 'hotel'
         order by (l.status = 'published') desc,
                  (select count(*) from public.listing_room_types rt
                    where rt.listing_id = l.id and rt.is_active) desc,
                  l.updated_at desc
         limit 1""")
    return rows[0] if rows else None


def marketplace_history(partner_id: str) -> dict:
    """
    What the partner already has on the marketplace, counted and left alone.

    Reported rather than migrated. A marketplace booking is a consumer-side
    record with its own payment, commission and payout attached; copying it into
    the PMS would create a second copy of a financial fact and no way to say
    which is authoritative. The marketplace stays the system of record for
    everything it has already sold.
    """
    rows = read_production(f"""
        select (select count(*) from public.bookings b
                 where b.partner_id = {q(partner_id)}) as bookings,
               (select count(*) from public.bookings b
                 where b.partner_id = {q(partner_id)} and b.status = 'paid') as paid,
               (select count(*) from public.partner_listings l
                 where l.partner_id = {q(partner_id)}) as listings""")
    return rows[0] if rows else {"bookings": 0, "paid": 0, "listings": 0}


def listing_rooms(listing_id: str) -> list[dict]:
    """
    The hotel's rooms, from `listing_room_types` rather than from
    `partner_listings.content->'rooms'`.

    Both exist and they are not the same thing. The JSON is what the listing
    editor posts; `listing_room_types` is what it writes, and it is the only one
    of the two that carries `is_active`. Reading the JSON migrated a room a
    hotelier had already deactivated — Hotel Lotus has a "Delex Room" beside its
    "Deluxe Room", a typo switched off months ago, and the JSON still holds it.
    """
    return read_production(f"""
        select rt.id, rt.name, rt.base_price, rt.default_units, rt.occupancy,
               rt.beds, rt.size, rt.sort_order
          from public.listing_room_types rt
         where rt.listing_id = {q(listing_id)} and rt.is_active
         order by rt.sort_order, rt.name""")


# ── mapping ──────────────────────────────────────────────────────────────────

def room_code(name: str, taken: set[str]) -> str:
    """A short code from the room's name, unique within the property."""
    letters = re.sub(r"[^A-Za-z0-9]", "", name or "").upper() or "ROOM"
    stem = letters[:3] or "RM"
    code, n = stem, 1
    while code in taken:
        n += 1
        code = f"{stem}{n}"
    taken.add(code)
    return code


class PriceError(ValueError):
    """A rupee value that must not become a price."""


# A nightly rate above this is not a rate. The most expensive suite on the
# marketplace is ₹15,500; a lakh and a half a night means somebody typed a
# paise figure into a rupee field, or added zeros. Refused rather than clamped —
# a clamp would invent a price, which is the one thing this must never do.
MAX_NIGHTLY_RUPEES = Decimal("150000")


def to_paise(rupees) -> int:
    """
    Rupees to paise. The only conversion in this file, in either direction.

    **Decimal, never float.** `float("1499.50") * 100` is 149949.99999999997,
    and `int()` of that is ₹1,499.49 — a rupee lost on a rate that then gets
    multiplied by nights and rooms. The marketplace stores these columns as
    integer rupees today, so this is defensive rather than load-bearing; it
    stops being defensive the first time somebody widens the column, and by
    then nobody will be looking.

    Refuses rather than coerces. Every rejection here is a room that does not
    become sellable at a wrong price.
    """
    if rupees is None:
        raise PriceError("no price")
    try:
        value = Decimal(str(rupees).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise PriceError(f"{rupees!r} is not a number") from exc

    if not value.is_finite():
        raise PriceError(f"{rupees!r} is not a finite amount")
    if value < 0:
        raise PriceError(f"₹{value} is negative")
    if value == 0:
        raise PriceError("₹0")
    if value > MAX_NIGHTLY_RUPEES:
        raise PriceError(
            f"₹{value:,} a night is not a rate — refusing to convert. "
            f"If this is already paise, the marketplace column has changed."
        )

    paise = (value * 100).to_integral_value(rounding=ROUND_HALF_UP)
    if paise <= 0 or paise > 2**31 - 1:
        raise PriceError(f"₹{value} does not fit an integer paise column")
    return int(paise)


def to_rupees(paise: int) -> Decimal:
    """The other direction, for the report. Exact, so what is printed is what was stored."""
    return (Decimal(int(paise)) / 100).quantize(Decimal("0.01"))


# ── the resolver ─────────────────────────────────────────────────────────────

EXPLICIT = "explicit_room_price"
INHERITED = "inherited_from_listing"
NEEDS_REVIEW = "needs_review"


def resolve_room_price(room: dict, listing: dict) -> tuple[int | None, str, str]:
    """
    What one room costs a night, and where that number came from.

    Two sources, in this order, and the order is not a preference — it is what
    the marketplace's own data model says:

    **1. `listing_room_types.base_price`.** The hotelier typed a price against
    this specific room. Nothing outranks that.

    **2. `partner_listings.price_from`.** Not a fallback and not an
    approximation: for a hotel it is *the* price the marketplace charges. The
    quote path that takes the money —
    `api/v1/marketplace/payments.py` — reads it directly:

        if listing["kind"] == "hotel":
            base = listing.get("price_from")
            q = pricing.quote_stay(base_price_rupees=float(base), rooms=body.rooms, …)

    and `quote_stay` passes it through as `unit_prices_paise` with
    `quantity=rooms`. `listing_room_types.base_price` never enters that path at
    all; it is what the room card on the page displays. So a guest booking this
    hotel today is already charged `price_from` per room per night whichever
    room they pick, and inheriting it is not a guess about what the room should
    cost — it is the price it is being sold at right now.

    Anything else is `NEEDS_REVIEW`. A room with no defensible price does not
    become a room priced at zero.
    """
    for source, label in ((room.get("base_price"), EXPLICIT),
                          (listing.get("price_from"), INHERITED)):
        if source is None:
            continue
        try:
            return to_paise(source), label, f"₹{Decimal(str(source)):,}"
        except PriceError as exc:
            # A bad explicit price does not fall through to the listing price
            # silently — it is reported, and then the listing price is tried,
            # because a typo in one field is not a reason to refuse the hotel.
            room.setdefault("_price_notes", []).append(f"{label}: {exc}")

    return None, NEEDS_REVIEW, "—"


def plan_room_types(listing: dict | None, rooms: list[dict]) -> list[dict]:
    taken: set[str] = set()
    out = []
    for room in rooms:
        name = (room.get("name") or "Room").strip() or "Room"
        paise, resolution, source = resolve_room_price(room, listing or {})
        out.append({
            "code": room_code(name, taken),
            "name": name,
            "units": min(max(int(room.get("default_units") or 1), 1), 999),
            # Clamped. The marketplace form takes whatever was typed, and one
            # of these says a room sleeps 22 — which is a mistake, not a
            # dormitory, and it would put a party of 22 through the occupancy
            # checks as if the room could hold them.
            "occupancy": min(max(int(room.get("occupancy") or 2), 1), 20),
            "paise": paise,
            "resolution": resolution,
            "source": source,
            "notes": room.get("_price_notes") or [],
            # A room reaches public inventory only with a price behind it. This
            # is the flag `rate_plans.is_active` is written from, and the one
            # thing that guarantees no ₹0 room is ever sellable.
            "sellable": paise is not None,
        })
    return out


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or f"hotel-{uuid.uuid4().hex[:6]}"


# ── the hotel OS side ────────────────────────────────────────────────────────

def ensure_auth_user(email: str, full_name: str | None) -> tuple[str, bool]:
    """
    An account on the hotel OS for this hotelier, without a password.

    Their marketplace login does not carry over: these are two Supabase
    projects with two separate auth systems, and there is no way to move a
    password hash between them that is not worse than asking them to set a new
    one. So the account is created unconfirmed and an invite link is generated
    for it — see `invite_link`.
    """
    admin = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    st, d = http(f"{HOTEL_OS_URL}/auth/v1/admin/users", "POST",
                 {"email": email, "email_confirm": True,
                  "user_metadata": {"full_name": full_name or ""}}, admin)
    if st in (200, 201) and isinstance(d, dict) and d.get("id"):
        return _with_profile(d["id"], email, full_name), True

    rows = write_hotel_os(f"select id from auth.users where email = {q(email)} limit 1;")
    if rows:
        return _with_profile(rows[0]["id"], email, full_name), False
    raise RuntimeError(f"could not create or find an account for {email}: {st} {str(d)[:200]}")


def _with_profile(profile_id: str, email: str, full_name: str | None) -> str:
    """
    Make sure the account has a `profiles` row, rather than assuming it does.

    `handle_new_user()` mirrors auth.users into profiles on insert, and for a
    user this script creates that is enough. It is not enough for one that
    already existed: this project was the old agency database until its `public`
    schema was dropped on 2026-08-13, which took `profiles` with it and left
    `auth.users` — a different schema — untouched. Every account predating that
    is an orphan, and the first partner this migration touched turned out to be
    one.

    `partners.owner_id` is a foreign key to `profiles`, so without this the
    migration fails on a constraint several statements later, naming a table
    nobody was writing to.
    """
    write_hotel_os(f"""
insert into public.profiles (id, email, full_name)
values ({q(profile_id)}, {q(email)}, {q(full_name)})
on conflict (id) do update
   set email = coalesce(public.profiles.email, excluded.email),
       full_name = coalesce(public.profiles.full_name, excluded.full_name);""")
    return profile_id


def invite_link(email: str) -> str | None:
    """
    A one-time link that lets them set a password and sign in.

    Generated, not sent. GoTrue will happily email it; whether a real customer
    hears from us today is not this script's call.
    """
    admin = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    st, d = http(f"{HOTEL_OS_URL}/auth/v1/admin/generate_link", "POST",
                 {"type": "recovery", "email": email,
                  "redirect_to": "https://partner.travbuddy.in/"}, admin)
    if st in (200, 201) and isinstance(d, dict):
        return d.get("action_link") or (d.get("properties") or {}).get("action_link")
    return None


def migrate(partner: dict, listing: dict | None, room_types: list[dict],
            *, apply: bool) -> dict:
    """
    Write one hotel, or describe what would be written.

    The marketplace partner's uuid becomes the hotel-OS partner's uuid. That is
    the whole idempotency story and the whole traceability story: re-running
    updates instead of duplicating, and anybody asking "which marketplace
    partner is this hotel?" can answer it with an equality rather than a
    lookup table that drifts.
    """
    email = partner.get("owner_email") or partner.get("email")
    if not email:
        return {"slug": partner["slug"], "skipped": "no email to create an account with"}

    property_id = str(uuid.uuid5(uuid.UUID(partner["id"]), "property"))

    # **UTC, not the operator's laptop.** `starts_at` is compared against
    # Postgres `now()` by `active_transition()`, and `date.today()` is local.
    # Run this from India after 18:30 IST and the local date is already
    # tomorrow while `now()` is still today in UTC — so the row lands with
    # `starts_at` in the *future* and the hotel is not inside its own free
    # period until UTC midnight. Up to five and a half hours in which a
    # partner who was just given a free year reads as chargeable.
    #
    # Found by `partner_migration_e2e` at 00:56 IST; it had passed all day.
    # A date this side of the boundary has to be the same date the database
    # will compare it to, so it is taken from the same clock.
    today = datetime.now(timezone.utc).date()
    free_until = today + timedelta(days=365)

    plan = {
        "partner_id": partner["id"],
        "property_id": property_id,
        "email": email,
        "name": partner.get("owner_name"),
        "brand": partner["brand_name"],
        "hotel": (listing or {}).get("title") or partner["brand_name"],
        "city": (listing or {}).get("city") or partner.get("city"),
        "slug": slugify((listing or {}).get("slug") or partner["slug"]),
        "listing_price": (listing or {}).get("price_from"),
        "listing_id": (listing or {}).get("id"),
        "listing_status": (listing or {}).get("status"),
        "history": marketplace_history(partner["id"]),
        "rooms": room_types,
        "units": sum(r["units"] for r in room_types),
        "free_until": free_until,
        "credits": FREE_CREDITS,
    }

    if not apply:
        return plan

    profile_id, created = ensure_auth_user(email, partner.get("owner_name"))
    plan["account_created"] = created

    # `partners.whatsapp` is not null in the hotel OS. A partner who never gave
    # one gets the phone, and failing that a placeholder rather than a failed
    # migration — the number is display-only here and the hotel can fix it.
    whatsapp = partner.get("whatsapp") or partner.get("phone") or "+910000000000"

    write_hotel_os(f"""
insert into public.partners (id, owner_id, slug, brand_name, whatsapp, status)
values ({q(partner['id'])}, {q(profile_id)}, {q('mp-' + partner['slug'])},
        {q(partner['brand_name'])}, {q(whatsapp)}, 'active')
on conflict (id) do update
   set brand_name = excluded.brand_name, status = 'active';

insert into public.properties
  (id, partner_id, slug, name, city, timezone, status, currency, tax_inclusive,
   check_in_time, check_out_time, marketplace_listing_id)
values ({q(property_id)}, {q(partner['id'])}, {q(plan['slug'])}, {q(plan['hotel'])},
        {q(plan['city'])}, 'Asia/Kolkata', 'active', 'INR', false, '14:00', '11:00',
        {q((listing or {}).get('id'))})
on conflict (id) do update
   set name = excluded.name, city = excluded.city, status = 'active',
       marketplace_listing_id = excluded.marketplace_listing_id;

-- The owner is a member of their own hotel, not merely the partner's owner_id.
-- `require_member` falls back to ownership, so this is belt and braces — but
-- the members screen reads this table, and a hotel whose owner is absent from
-- its own staff list reads as a bug to the person looking at it.
insert into public.partner_members
  (partner_id, profile_id, role, status, accepted_at)
values ({q(partner['id'])}, {q(profile_id)}, 'owner', 'active', now())
on conflict do nothing;""")

    # Room types and one public rate plan each. `on conflict (code)` per
    # property, so a re-run corrects a price rather than adding a second
    # "Deluxe Room" beside the first.
    for room in room_types:
        rt_id = str(uuid.uuid5(uuid.UUID(property_id), "rt:" + room["code"]))
        rp_id = str(uuid.uuid5(uuid.UUID(property_id), "rp:" + room["code"]))
        write_hotel_os(f"""
insert into public.room_types
  (id, property_id, partner_id, code, name, total_units, max_occupancy, sort_order)
values ({q(rt_id)}, {q(property_id)}, {q(partner['id'])}, {q(room['code'])},
        {q(room['name'])}, {room['units']}, {room['occupancy']}, 1)
on conflict (id) do update
   set name = excluded.name, total_units = excluded.total_units,
       max_occupancy = excluded.max_occupancy, is_active = true;

insert into public.rate_plans
  (id, property_id, room_type_id, partner_id, code, name, meal_plan, audience,
   base_price_paise, refundable, is_active, sort_order)
values ({q(rp_id)}, {q(property_id)}, {q(rt_id)}, {q(partner['id'])},
        {q(room['code'] + '-EP')}, {q(room['name'] + ' — room only')}, 'EP',
        'public', {room['paise'] or 0}, true, {q(room['sellable'])}, 1)
-- **base_price_paise is not in the update list, deliberately.** This migration
-- fills a price in; it never corrects one. Once a hotel is live its rates are
-- set on the calendar screen and the hotel OS is the system of record for them,
-- so a re-run reaching back to the marketplace would silently undo a hotelier's
-- own pricing. That is a far worse failure than a stale row. The name and the
-- active flag are refreshed because neither of them is money.
on conflict (id) do update
   set name = excluded.name,
       is_active = excluded.is_active;""")

    # A year, free. The subscription row is what the usage screen reads for the
    # period; the grant is what the AI actually spends. Both are keyed so a
    # second run tops nobody up twice.
    account = write_hotel_os(f"""
        select id from public.billing_accounts
         where partner_id = {q(partner['id'])} and is_default limit 1;""")
    if account:
        account_id = account[0]["id"]
        plan["billing_account"] = account_id
        write_hotel_os(f"""
insert into public.subscriptions
  (billing_account_id, plan_id, property_id, status,
   current_period_start, current_period_end)
select {q(account_id)}, sp.id, {q(property_id)}, 'active', {q(today)}, {q(free_until)}
  from public.subscription_plans sp
 where sp.code = {q(STARTER_PLAN)}
   and not exists (select 1 from public.subscriptions s
                    where s.billing_account_id = {q(account_id)}
                      and s.status in ('trialing', 'active'));""")

        # The entitlement, which is not the subscription. `subscriptions` is the
        # billing record and a promotional year written there is
        # indistinguishable from a paid one later; this row says what was given
        # and why. Unique on (partner_id, source), so a second run grants
        # nothing further — see migration 25.
        write_hotel_os(f"""
insert into public.partner_transitions
  (partner_id, property_id, billing_account_id, source, reason,
   starts_at, ends_at, marketplace_partner_id, marketplace_listing_id)
values ({q(partner['id'])}, {q(property_id)}, {q(account_id)}, 'partner_migration',
        'Partner migration — 1 year free',
        {q(today)}, {q(free_until)}, {q(partner['id'])},
        {q((listing or {}).get('id'))})
on conflict (partner_id, source) do nothing;""")

        from app.services import billing
        import asyncio
        asyncio.run(billing.grant(
            billing_account_id=account_id, kind="promotional", credits=FREE_CREDITS,
            idempotency_key=f"migrate:{partner['id']}:year-one",
            expires_at=free_until.isoformat(), source_type="migration",
            actor_type="admin",
        ))

    plan["invite"] = invite_link(email)
    return plan


# ── output ───────────────────────────────────────────────────────────────────

RESOLUTION_LABEL = {
    EXPLICIT: "explicit_room_price",
    INHERITED: "inherited_from_listing",
    NEEDS_REVIEW: "needs_review",
}

READY, WARNING, REVIEW, SKIPPED = "READY", "WARNING", "NEEDS_REVIEW", "SKIPPED"


def verdict_for(plan: dict) -> tuple[str, list[str]]:
    """
    One word for the partner, and the reasons behind it.

    Every partner gets one. A record that produced no output at all is the
    failure mode this exists to prevent — "it didn't appear in the report" is
    indistinguishable from "it was fine" when somebody is reading three hundred
    lines looking for what to fix.
    """
    warnings: list[str] = []

    if plan.get("skipped"):
        return SKIPPED, [plan["skipped"]]

    unpriced = [r for r in plan["rooms"] if not r["sellable"]]
    if not plan["rooms"]:
        warnings.append("no active room types on the marketplace listing — "
                        "the hotel is created with nothing to sell")
    if unpriced:
        warnings.append(f"{len(unpriced)} room(s) have no resolvable price and "
                        f"arrive switched off")
    if plan.get("listing_status") and plan["listing_status"] != "published":
        warnings.append(f"built from a {plan['listing_status']} listing, not a "
                        f"published one")
    for room in plan["rooms"]:
        for note in room["notes"]:
            warnings.append(f"{room['name']}: {note}")

    if unpriced:
        return REVIEW, warnings
    if warnings:
        return WARNING, warnings
    return READY, warnings


def describe(plan: dict, *, apply: bool) -> str:
    """
    Where every price came from, room by room, and what state the hotel is in.

    The point of this output is that somebody can audit it before it becomes
    money. A total is not auditable; "₹3,600, inherited from the listing price,
    because the marketplace already charges that per room per night" is.

    Returns the verdict so the caller can count them.
    """
    verdict, warnings = verdict_for(plan)

    if plan.get("skipped"):
        print(f"\n  ── {plan.get('slug', 'unknown')} " + "─" * 40)
        print(f"     Status                SKIPPED — {plan['skipped']}")
        return verdict

    print(f"\n  ── {plan['brand']} " + "─" * max(4, 56 - len(plan["brand"])))
    print(f"     Partner ID            {plan['partner_id']}")
    print(f"     Hotel                 {plan['hotel']}")
    print(f"     City                  {plan['city'] or 'unknown'}")
    print(f"     Owner                 {plan['email']}"
          + (f"  ({plan['name']})" if plan.get("name") else ""))
    print(f"     Listing price         "
          + (f"₹{plan['listing_price']:,}/night" if plan.get("listing_price")
             else "not set"))
    print(f"     Marketplace listing   "
          + (f"CONNECTED  {plan['listing_id']}  ({plan['listing_status']})"
             if plan.get("listing_id") else "NONE"))
    # Reserved, not live — and the difference matters enough to print.
    #
    # `/stay/{slug}` resolves through `public_booking.resolve_published`, which
    # needs a `hotel_websites` row that is `published` with a snapshot in it.
    # This migration deliberately does not create one: publishing a page under a
    # hotel's own name is theirs to do, and a migration that quietly put three
    # real businesses on the public internet would be doing it for them.
    #
    # The earlier version of this line printed the URL flat, which read as "this
    # exists now". It does not, until the owner opens Website in the PWA and
    # presses Publish — one click, and the slug below is the one they get.
    print(f"     Booking page          partner.travbuddy.in/stay/{plan['slug']}"
          "  — reserved; live once the owner publishes it (PWA → Website)")

    history = plan.get("history") or {}
    print(f"     Marketplace history   {history.get('listings', 0)} listing(s), "
          f"{history.get('bookings', 0)} booking(s) — left in place, not copied")

    if not plan["rooms"]:
        print("\n     Rooms                 none active on the listing")
    for room in plan["rooms"]:
        ready = room["sellable"]
        print(f"\n     Room                  {room['name']}")
        print(f"       source price        "
              f"{room['source'] if room['resolution'] == EXPLICIT else 'missing'}")
        print(f"       resolved price      "
              + (f"₹{to_rupees(room['paise']):,}/night" if ready else "—"))
        print(f"       resolution          {RESOLUTION_LABEL[room['resolution']]}")
        print(f"       inventory           {room['units']} room(s), sleeps {room['occupancy']}")
        print(f"       status              {'READY' if ready else 'DISABLED — needs review'}")
        for note in room["notes"]:
            print(f"       ⚠ rejected          {note}")

    print(f"\n     Rooms                 {len(plan['rooms'])}")
    print(f"     Rates                 {sum(1 for r in plan['rooms'] if r['sellable'])} sellable")
    print(f"     StayOS billing        {'CREATED' if apply else 'would be created'}")
    print(f"     Free entitlement      {plan['free_until'].replace(year=plan['free_until'].year - 1)}"
          f" → {plan['free_until']}  ({plan['credits']:,} AI credits)")
    print(f"     Warnings              " + (warnings[0] if warnings else "None"))
    for extra in warnings[1:]:
        print(f"                           {extra}")
    print(f"     STATUS                {verdict}")

    if apply:
        made = plan.get("account_created")
        print(f"     Account               {'created' if made else 'already existed'}")
        if plan.get("invite"):
            print(f"     Invite                {plan['invite']}")
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually write to the hotel OS (default: dry run)")
    parser.add_argument("--slug", help="one marketplace partner slug")
    args = parser.parse_args()

    print("\n══ marketplace → hotel OS ══")
    print(f"  reading   {MARKETPLACE}  (production, read-only)")
    print(f"  writing   {HOTEL_OS}" + ("" if args.apply else "  — DRY RUN, nothing written"))

    partners = eligible_partners(args.slug)
    if not partners:
        print("\n  no eligible partners found")
        return 1
    print(f"  eligible  {len(partners)} partner(s)")

    verdicts = {READY: 0, WARNING: 0, REVIEW: 0, SKIPPED: 0}
    for partner in partners:
        listing = best_listing(partner["id"])
        rooms = plan_room_types(listing, listing_rooms(listing["id"]) if listing else [])
        plan = migrate(partner, listing, rooms, apply=args.apply)
        verdicts[describe(plan, apply=args.apply)] += 1

    # Everybody else, named. See `ineligible_partners`.
    if not args.slug:
        rest = ineligible_partners()
        if rest:
            print(f"\n  ── not migrated ({len(rest)}) " + "─" * 30)
            for row in rest:
                why = ("no hotel listing" if not row["hotel_listings"]
                       else "no active stay plan")
                if not row["listings"]:
                    why = "no listings at all — never finished signing up"
                if not row.get("owner_email"):
                    why += ", and no owner email"
                print(f"     {row['brand_name'][:34]:<34} NEEDS_REVIEW — {why}")
                verdicts[REVIEW] += 1

    print("\n══ " + " · ".join(f"{n} {word.lower()}" for word, n in verdicts.items() if n)
          + " ══")
    if not args.apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply to create these.\n")
    else:
        print("""
Accounts exist and each has an invite link above. **No email has been sent.**
Those links let someone set a password on partner.travbuddy.in; sending them is
a decision about contacting real customers and it is yours to make.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
