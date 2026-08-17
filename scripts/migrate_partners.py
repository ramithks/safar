#!/usr/bin/env python3
"""
Move a marketplace partner onto the hotel OS, with a year on the house.

    travbuddy.in (uabxtdhxbamuhthxnhid)          partner.travbuddy.in (rdlozq…)
    ──────────────────────────────────          ──────────────────────────────
    partners                          ──────►   partners
    partner_listings (kind='hotel')   ──────►   properties
      content->rooms[]                ──────►   room_types + rate_plans
    profiles.email                    ──────►   a new auth user + invite link
                                      ──────►   subscription, 1 year, ₹0
                                      ──────►   credit grant

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
               coalesce(jsonb_array_length(l.content->'rooms'), 0) as room_count,
               l.content->'rooms' as rooms
          from public.partner_listings l
         where l.partner_id = {q(partner_id)} and l.kind = 'hotel'
         order by (l.status = 'published') desc,
                  coalesce(jsonb_array_length(l.content->'rooms'), 0) desc,
                  l.updated_at desc
         limit 1""")
    return rows[0] if rows else None


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


def to_paise(price) -> int:
    """
    Rupees to paise, and the only place this conversion happens.

    `content->rooms[].price` is a **rupee** figure a hotelier typed into the
    marketplace form — 1700 means ₹1,700. Everything in the hotel OS is paise.
    Getting this wrong in either direction is a 100× pricing error that would
    reach a real guest, so it is one function with one caller and a range check
    either side of it.
    """
    rupees = float(price or 0)
    if rupees <= 0:
        return 0
    if rupees > 500_000:
        raise RuntimeError(
            f"a nightly rate of ₹{rupees:,.0f} is not a rate — refusing to convert. "
            f"If this is already paise, the marketplace form has changed."
        )
    return int(round(rupees * 100))


def plan_room_types(listing: dict | None) -> list[dict]:
    rooms = (listing or {}).get("rooms") or []
    taken: set[str] = set()
    out = []
    for room in rooms:
        name = (room.get("name") or "Room").strip() or "Room"
        paise = to_paise(room.get("price"))
        out.append({
            "code": room_code(name, taken),
            "name": name,
            "units": max(int(room.get("units") or 1), 1),
            # Clamped. The marketplace form takes whatever was typed, and one
            # of these says a room sleeps 22 — which is a mistake, not a
            # dormitory, and it would put a party of 22 through the occupancy
            # checks as if the room could hold them.
            "occupancy": min(max(int(room.get("occupancy") or 2), 1), 20),
            "paise": paise,
            # A rate plan with no price is not a cheap room, it is an unfinished
            # listing — and the public booking engine would sell it for nothing.
            # The room type is still created, so the hotel is real and the owner
            # can price it on the calendar; the plan arrives switched off.
            "sellable": paise > 0,
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
    today = date.today()
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
   check_in_time, check_out_time)
values ({q(property_id)}, {q(partner['id'])}, {q(plan['slug'])}, {q(plan['hotel'])},
        {q(plan['city'])}, 'Asia/Kolkata', 'active', 'INR', false, '14:00', '11:00')
on conflict (id) do update
   set name = excluded.name, city = excluded.city, status = 'active';""")

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
   base_price_paise, refundable,
  is_active, sort_order)
values ({q(rp_id)}, {q(property_id)}, {q(rt_id)}, {q(partner['id'])},
        {q(room['code'] + '-EP')}, {q(room['name'] + ' — room only')}, 'EP',
        'public', {room['paise']}, true, {q(room['sellable'])}, 1)
on conflict (id) do update
   set base_price_paise = excluded.base_price_paise,
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

def describe(plan: dict, *, apply: bool) -> None:
    if plan.get("skipped"):
        print(f"  ─ {plan['slug']}: skipped — {plan['skipped']}")
        return

    print(f"\n  {plan['brand']}  ({plan['city'] or 'city unknown'})")
    print(f"     hotel        {plan['hotel']}")
    print(f"     url          partner.travbuddy.in  →  /stay/{plan['slug']}")
    print(f"     owner        {plan['email']}"
          + (f"  ({plan['name']})" if plan.get("name") else ""))
    if plan["rooms"]:
        for room in plan["rooms"]:
            rate = (f"₹{room['paise'] / 100:,.0f}/night" if room["sellable"]
                    else "NO PRICE — plan created switched off")
            print(f"     room         {room['code']:<5} {room['name'][:28]:<28} "
                  f"×{room['units']:<3} sleeps {room['occupancy']:<3} {rate}")
        print(f"     inventory    {plan['units']} rooms across "
              f"{len(plan['rooms'])} type(s)")
    else:
        # Not a failure. A hotel with no rooms described yet is a hotel whose
        # owner sets them up on the calendar screen; what matters is that they
        # can sign in and find their own name on it.
        print("     room         none described on the marketplace listing")
    unpriced = [r for r in plan["rooms"] if not r["sellable"]]
    if unpriced:
        print(f"     ⚠ warning    {len(unpriced)} room type(s) have no nightly rate. "
              f"They are not sellable until the hotel prices them.")
    print(f"     free until   {plan['free_until']}  ({plan['credits']:,} AI credits)")

    if apply:
        made = plan.get("account_created")
        print(f"     account      {'created' if made else 'already existed'}")
        if plan.get("invite"):
            print(f"     invite       {plan['invite']}")


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

    done, skipped = 0, 0
    for partner in partners:
        listing = best_listing(partner["id"])
        try:
            rooms = plan_room_types(listing)
        except RuntimeError as exc:
            print(f"  ─ {partner['slug']}: skipped — {exc}")
            skipped += 1
            continue
        plan = migrate(partner, listing, rooms, apply=args.apply)
        describe(plan, apply=args.apply)
        skipped += 1 if plan.get("skipped") else 0
        done += 0 if plan.get("skipped") else 1

    print(f"\n══ {done} migrated, {skipped} skipped ══")
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
