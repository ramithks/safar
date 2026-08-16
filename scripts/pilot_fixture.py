"""
One realistic hotel, built from nothing, owned by nobody else.

Every other suite in this directory borrows Casa Mandala or one of the three
properties seeded beside it. That is right for them — they test one surface and
the seed is the cheapest way to have rooms to sell. It is wrong for a pilot
rehearsal, which has to answer a different question:

    **could a hotel that has never existed before be onboarded, and then run?**

A suite that starts from a seeded hotel cannot answer that. It inherits room
types somebody else created, a wallet somebody else funded, a website somebody
else published and an AI agent somebody else provisioned — which is exactly the
list of things that go wrong on a first day and exactly the list it would not
notice were missing.

So this module builds the whole thing: a partner, a property with a timezone and
policies, four real Supabase logins with four different roles, three room types
with rooms in them, public *and* agent rate plans, inventory, services, a
billing account with a funded wallet, five AI agents, a WhatsApp connection, an
Instagram connection, a published website, a travel agency with a credit line,
and a creator on a live campaign.

**Idempotent, and swept at both ends.** `open()` deletes anything a previous run
left behind before it builds, for the reason `e2e_fixture` gives: a run that
dies halfway leaves rows, and the run that finds them should not inherit them.

⚠ Everything here is created under ids beginning `f1…` and emails at
`@pilot.stayos.test`. Nothing outside that namespace is ever touched, so this
cannot disturb Casa Mandala or the three suites that share it.
"""
import base64
import json
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

from app.db.client import get_db

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"

# One namespace, so the sweep can be exact rather than approximate.
PARTNER = "f1000000-0000-4000-8000-000000000001"
PROPERTY = "f1000000-0000-4000-8000-000000000002"
# Resolved after `build()`, not chosen: the partner trigger creates it.
ACCOUNT: str = ""

RT_STD = "f1000000-0000-4000-8000-000000000101"
RT_DLX = "f1000000-0000-4000-8000-000000000102"
RT_SUI = "f1000000-0000-4000-8000-000000000103"

PLAN_STD_CP = "f1000000-0000-4000-8000-000000000201"
PLAN_DLX_CP = "f1000000-0000-4000-8000-000000000202"
PLAN_DLX_B2B = "f1000000-0000-4000-8000-000000000203"
PLAN_SUI_CP = "f1000000-0000-4000-8000-000000000204"

SVC_TRANSFER = "f1000000-0000-4000-8000-000000000301"
SVC_DINNER = "f1000000-0000-4000-8000-000000000302"

AGENCY = "f1000000-0000-4000-8000-000000000401"
CREATOR = "f1000000-0000-4000-8000-000000000501"
CAMPAIGN = "f1000000-0000-4000-8000-000000000502"
PARTICIPATION = "f1000000-0000-4000-8000-000000000503"

SLUG = "sunbird-beach-resort"
PHONE_NUMBER_ID = "pilot-wa-777000111"
IG_ACCOUNT_ID = "pilot-ig-777000222"

PASSWORD = "pilot-os-dev"
NAMES = {
    "owner@pilot.stayos.test": "Nikhil Sequeira",
    "manager@pilot.stayos.test": "Aarti Kamat",
    "reception@pilot.stayos.test": "Riya Menon",
    "housekeeping@pilot.stayos.test": "Sunil Gaonkar",
    "agency@pilot.stayos.test": "Rhea Fernandes",
}
EMAILS = {
    "owner": "owner@pilot.stayos.test",
    "manager": "manager@pilot.stayos.test",
    "reception": "reception@pilot.stayos.test",
    "housekeeping": "housekeeping@pilot.stayos.test",
    "agency": "agency@pilot.stayos.test",
}

# Room counts small enough that "the last room" is reachable in a test and
# large enough that occupancy arithmetic is not degenerate.
UNITS = {RT_STD: 6, RT_DLX: 4, RT_SUI: 2}

# Tariffs in paise. Deliberately not round numbers divisible by the tax rate:
# a rounding bug hides behind ₹5,000 and shows itself at ₹4,850.
RATE_STD = 485000
RATE_DLX = 725000
RATE_DLX_B2B = 615000
RATE_SUI = 1240000

REF_PREFIX = "TB-PL"

# Where the AI provisioning is copied from. Not one property: the five agents
# are spread across the seeded hotels — Casa Mandala has guest, front desk and
# owner; sales and marketing live on the second and third properties, because
# `e2e-fixture-isolation` gave those suites wallets of their own. So each type
# is sourced from wherever it actually exists. Read-only; nothing here writes
# to any of them.
SEED_PROPERTIES = (
    "cccccccc-cccc-4ccc-8ccc-cccccccccccc",  # guest, front desk, owner
    "dddddddd-dddd-4ddd-8ddd-dddddddddddd",  # sales
    "ffffffff-ffff-4fff-8fff-ffffffffffff",  # marketing
)


# ── the two transports this module needs ─────────────────────────────────────

def _mgmt_token() -> str:
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
    ).strip()
    return base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()


MGMT = _mgmt_token()


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


def sql(query: str, *, attempts: int = 4):
    """
    One statement through the Management API, retried past its weather.

    A 5xx or a dropped connection from `api.supabase.com` is the transport
    having a moment, not the database disagreeing — and a suite that died on one
    would report a red run for a reason that has nothing to do with the code
    under test. A 4xx is never retried: that is Postgres answering, and the
    answer will not change.
    """
    import time as _time

    last = ""
    for attempt in range(attempts):
        st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                        {"query": query},
                        {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
        if st in (200, 201):
            return body
        last = f"{st} {str(body)[:400]}"
        if st and st < 500:
            break
        _time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"SQL failed: {last}")


def try_sql(query: str) -> tuple[int, object]:
    """`sql`, but a refusal is the answer rather than an exception.

    Used by the security section, where the expected result *is* an error and
    raising would end the run instead of recording a pass."""
    return http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                {"query": query},
                {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})


_keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
             headers={"Authorization": f"Bearer {MGMT}",
                      "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in _keys if k.get("name") == "anon"][0]
SERVICE_KEY = [k["api_key"] for k in _keys if k.get("name") == "service_role"][0]


def ensure_user(email: str) -> str:
    """
    One auth user, through GoTrue's admin API rather than an insert.

    The password hash, the identity row and the confirmation are GoTrue's
    business; a hand-written `auth.users` row signs in today and stops the first
    time the hashing changes. `handle_new_user()` mirrors it into `profiles`.
    """
    admin = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    # A name, because several things record *who did this* — a staff reply in the
    # inbox, a folio line, a payout approval — and every one of them reads
    # `profiles.full_name`. A fixture whose staff are nameless would assert that
    # an audit trail saying "somebody" is acceptable.
    st, d = http(f"{SB}/auth/v1/admin/users", "POST",
                 {"email": email, "password": PASSWORD, "email_confirm": True,
                  "user_metadata": {"full_name": NAMES.get(email, "Pilot Staff")}}, admin)
    if st in (200, 201) and isinstance(d, dict) and d.get("id"):
        _ensure_name(d["id"], NAMES.get(email, "Pilot Staff"))
        return d["id"]
    rows = sql(f"select id from auth.users where email = '{email}' limit 1;")
    if rows:
        _ensure_name(rows[0]["id"], NAMES.get(email, "Pilot Staff"))
        return rows[0]["id"]
    raise RuntimeError(f"could not create or find {email}: {st} {str(d)[:200]}")


def _ensure_name(profile_id: str, full_name: str) -> None:
    """`handle_new_user()` mirrors the auth row into `profiles`; this fills the
    name in for an account that already existed from a previous run."""
    sql(f"""update public.profiles set full_name = '{full_name}'
             where id = '{profile_id}' and coalesce(full_name, '') = '';""")


def signin(email: str):
    st, d = http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                 {"email": email, "password": PASSWORD}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


# ── build ────────────────────────────────────────────────────────────────────

PROFILES: dict[str, str] = {}


def sweep() -> None:
    """
    Give everything back, in dependency order.

    Reservations before the campaign, because a payout cascades from the
    reservation and its audit trail refuses a direct delete; the campaign before
    the property, because touches cascade from it. Written out rather than left
    to `on delete cascade` from `partners` so that a failure names the table it
    failed on.
    """
    sql(f"""
-- Payment orders before quotes. `payment_orders.quote_id` is `on delete
-- restrict` since migration 24 — a paid order pins the quote it paid for,
-- because deleting the priced stay would orphan the financial evidence. A
-- fixture removing its own test payments is the one legitimate case.
delete from public.payment_orders where property_id = '{PROPERTY}';
delete from public.reservations where property_id = '{PROPERTY}';
delete from public.creator_campaigns where property_id = '{PROPERTY}';
delete from public.creators where id = '{CREATOR}';
delete from public.quotes where property_id = '{PROPERTY}';
delete from public.inventory_holds where property_id = '{PROPERTY}';
delete from public.conversations where property_id = '{PROPERTY}';
delete from public.b2b_agents where id = '{AGENCY}';
delete from public.partners where id = '{PARTNER}';""")


def build() -> None:
    for role, email in EMAILS.items():
        PROFILES[role] = ensure_user(email)

    today = date.today()
    sql(f"""
insert into public.partners (id, owner_id, slug, brand_name, whatsapp, status)
values ('{PARTNER}', '{PROFILES["owner"]}', 'pilot-sunbird', 'Sunbird Hotels',
        '+919000000450', 'active')
on conflict (id) do update set owner_id = excluded.owner_id, status = 'active';

insert into public.properties
  (id, partner_id, slug, name, city, timezone, status, currency, tax_inclusive,
   check_in_time, check_out_time, policies, settings)
values ('{PROPERTY}', '{PARTNER}', '{SLUG}', 'Sunbird Beach Resort', 'Goa',
        'Asia/Kolkata', 'active', 'INR', false, '14:00', '11:00',
        '{{"cancellation": "Free until 48 hours before arrival.",
           "children": "Under 6 stay free.",
           "pets": "No pets."}}'::jsonb,
        '{{"booking_token_pct": 25}}'::jsonb)
on conflict (id) do update
   set status = 'active', timezone = excluded.timezone,
       policies = excluded.policies, settings = excluded.settings;

-- Four roles, because the point of a pilot rehearsal is that they do not all
-- see the same thing. The owner is the partner's owner_id and needs no row.
insert into public.partner_members (partner_id, profile_id, role, status, accepted_at)
values ('{PARTNER}', '{PROFILES["manager"]}',      'manager',      'active', now()),
       ('{PARTNER}', '{PROFILES["reception"]}',    'receptionist', 'active', now()),
       ('{PARTNER}', '{PROFILES["housekeeping"]}', 'housekeeping', 'active', now())
on conflict do nothing;

-- No billing account is created here. `partners_provision_billing` makes one on
-- insert and `properties_attach_billing` points the property at it — which is
-- exactly the onboarding path a real hotel takes, and inserting one by hand
-- would have tested a code path nobody uses. The id is read back below.

insert into public.room_types
  (id, property_id, partner_id, code, name, total_units, max_occupancy, sort_order)
values ('{RT_STD}', '{PROPERTY}', '{PARTNER}', 'STD', 'Garden Room', {UNITS[RT_STD]}, 2, 1),
       ('{RT_DLX}', '{PROPERTY}', '{PARTNER}', 'DLX', 'Sea View',    {UNITS[RT_DLX]}, 3, 2),
       ('{RT_SUI}', '{PROPERTY}', '{PARTNER}', 'SUI', 'Beach Suite', {UNITS[RT_SUI]}, 4, 3)
on conflict (id) do update
   set total_units = excluded.total_units, is_active = true;

insert into public.rate_plans
  (id, property_id, room_type_id, partner_id, code, name, meal_plan, audience,
   base_price_paise, refundable, inclusions, sort_order)
values
  ('{PLAN_STD_CP}',  '{PROPERTY}', '{RT_STD}', '{PARTNER}', 'STD-CP', 'Garden + Breakfast',
   'CP', 'public', {RATE_STD}, true, '{{Breakfast}}', 1),
  ('{PLAN_DLX_CP}',  '{PROPERTY}', '{RT_DLX}', '{PARTNER}', 'DLX-CP', 'Sea View + Breakfast',
   'CP', 'public', {RATE_DLX}, true, '{{Breakfast,"Sea view"}}', 2),
  ('{PLAN_DLX_B2B}', '{PROPERTY}', '{RT_DLX}', '{PARTNER}', 'DLX-AGT', 'Agent Net Rate',
   'CP', 'b2b', {RATE_DLX_B2B}, false, '{{Breakfast}}', 3),
  ('{PLAN_SUI_CP}',  '{PROPERTY}', '{RT_SUI}', '{PARTNER}', 'SUI-CP', 'Suite + Breakfast',
   'CP', 'public', {RATE_SUI}, true, '{{Breakfast,"Private deck"}}', 4)
on conflict (id) do update
   set base_price_paise = excluded.base_price_paise, is_active = true,
       audience = excluded.audience;

insert into public.rooms (property_id, partner_id, room_type_id, number, floor,
                          housekeeping_status)
select '{PROPERTY}', '{PARTNER}', t.rt, t.num, t.flr, 'clean'
  from (values
    ('{RT_STD}'::uuid, 'G01', 'Ground'), ('{RT_STD}'::uuid, 'G02', 'Ground'),
    ('{RT_STD}'::uuid, 'G03', 'Ground'), ('{RT_STD}'::uuid, 'G04', 'Ground'),
    ('{RT_STD}'::uuid, 'G05', 'Ground'), ('{RT_STD}'::uuid, 'G06', 'Ground'),
    ('{RT_DLX}'::uuid, '201', 'First'),  ('{RT_DLX}'::uuid, '202', 'First'),
    ('{RT_DLX}'::uuid, '203', 'First'),  ('{RT_DLX}'::uuid, '204', 'First'),
    ('{RT_SUI}'::uuid, 'S1',  'Second'), ('{RT_SUI}'::uuid, 'S2',  'Second')
  ) as t(rt, num, flr)
 where not exists (select 1 from public.rooms r
                    where r.property_id = '{PROPERTY}' and r.number = t.num);

insert into public.services
  (id, property_id, partner_id, name, description, category, price_paise,
   lead_time_hours, daily_capacity, is_active, sort_order)
values
  ('{SVC_TRANSFER}', '{PROPERTY}', '{PARTNER}', 'Airport transfer',
   'Dabolim to the resort, one way', 'transfer', 180000, 12, 4, true, 1),
  ('{SVC_DINNER}', '{PROPERTY}', '{PARTNER}', 'Beach dinner',
   'Set menu on the sand', 'food', 320000, 6, 6, true, 2)
on conflict (id) do update set is_active = true, price_paise = excluded.price_paise;

insert into public.hotel_websites (property_id, partner_id, template, status, draft, published,
                                   published_at)
values ('{PROPERTY}', '{PARTNER}', 'beach', 'published',
        '{{"headline": "Sunbird Beach Resort", "tagline": "Book direct"}}'::jsonb,
        '{{"headline": "Sunbird Beach Resort", "tagline": "Book direct",
           "about": "Twelve rooms on the sand at Morjim.",
           "contactPhone": "+919000000450",
           "sections": [{{"id": "hero", "enabled": true}},
                        {{"id": "rooms", "enabled": true}},
                        {{"id": "contact", "enabled": true}}]}}'::jsonb,
        now())
on conflict (property_id) do update
   set status = 'published', published = excluded.published, published_at = now();

insert into public.whatsapp_connections
  (property_id, partner_id, phone_number_id, display_number, display_name,
   status, is_sandbox, webhook_verified_at)
values ('{PROPERTY}', '{PARTNER}', '{PHONE_NUMBER_ID}', '919000000450',
        'Sunbird Beach Resort', 'connected', true, now())
on conflict (property_id) do update
   set status = 'connected', is_sandbox = true,
       phone_number_id = excluded.phone_number_id;

insert into public.instagram_connections
  (property_id, partner_id, instagram_account_id, username, page_id,
   status, is_sandbox)
values ('{PROPERTY}', '{PARTNER}', '{IG_ACCOUNT_ID}', 'sunbirdbeach',
        'pilot-page-1', 'connected', true)
-- Keyed on the Instagram account, not the property: unlike WhatsApp, a property
-- may legitimately hold more than one professional account, so the uniqueness
-- rule lives on the account id.
on conflict (instagram_account_id) do update
   set status = 'connected', is_sandbox = true;

insert into public.b2b_agents
  (id, partner_id, profile_id, code, company_name, contact_name, city, phone, email,
   commission_pct, credit_limit_paise, property_ids, status)
values ('{AGENCY}', '{PARTNER}', '{PROFILES["agency"]}', 'TB-AG-PILOT',
        'Konkan Travel Desk', 'Rhea Fernandes', 'Pune', '+919000000451',
        'desk@konkantravel.test', 10, 4000000, '{{}}', 'active')
on conflict (id) do update set status = 'active', credit_limit_paise = 4000000;

insert into public.creators
  (id, profile_id, display_name, handle, platform, followers, city, status)
values ('{CREATOR}', null, 'Pilot Creator', '@pilotcreator', 'instagram',
        56000, 'Goa', 'active')
on conflict (id) do nothing;

insert into public.creator_campaigns
  (id, property_id, partner_id, name, status, start_date, end_date,
   commission_pct, guest_discount_pct, attribution_window_days, min_nights)
values ('{CAMPAIGN}', '{PROPERTY}', '{PARTNER}', 'Pilot monsoon push', 'live',
        date '{today - timedelta(days=10)}', date '{today + timedelta(days=80)}',
        10, 5, 30, 2)
on conflict (id) do update set status = 'live';

insert into public.campaign_creators
  (id, campaign_id, creator_id, property_id, partner_id, public_code, status)
values ('{PARTICIPATION}', '{CAMPAIGN}', '{CREATOR}', '{PROPERTY}', '{PARTNER}',
        'PILOTCREATOR-9X4K', 'active')
on conflict (id) do update set status = 'active';
""")


def resolve_account() -> str:
    """
    The billing account the partner trigger made, and the property it attached.

    Read rather than assumed, because "is this property attached to an account"
    is a genuine first-day question — a property with no account has a wallet
    nobody can top up, and every AI turn on it fails at metering rather than at
    anything a hotelier would recognise.
    """
    global ACCOUNT
    rows = sql(f"""select billing_account_id as id from public.properties
                    where id = '{PROPERTY}';""")
    ACCOUNT = (rows[0]["id"] if rows else None) or ""
    if not ACCOUNT:
        raise RuntimeError("the property was not attached to a billing account")
    return ACCOUNT


def open_inventory(days: int = 400) -> None:
    """
    Nothing. Deliberately, and this is the assertion rather than the omission.

    `room_inventory.units_total` and `rate_plan_rates.price_paise` are both
    nullable and both **inherit** — from `room_types.total_units` and
    `rate_plans.base_price_paise` — so a hotel that has typed in no calendar at
    all is fully bookable from its room types alone. That is the state every
    real property is in on its first day, and a fixture that pre-seeded 400
    nights of ARI would have tested a hotel that had already done a week of
    work.

    Kept as a function so the intent has somewhere to live, and so a later
    scenario that genuinely needs an override has an obvious place to put it.
    """
    return None


def close_out(room_type_id: str, on: date) -> None:
    """Take one room type off sale for one night — an override, written the way
    the calendar screen writes it."""
    sql(f"""
insert into public.room_inventory (property_id, partner_id, room_type_id, date,
                                   units_total, is_closed)
values ('{PROPERTY}', '{PARTNER}', '{room_type_id}', date '{on.isoformat()}', 0, true)
on conflict (room_type_id, date) do update set units_total = 0, is_closed = true;""")


def provision_agents() -> None:
    """
    The five agents, copied from the seeded hotels rather than written out.

    Copied, because an agent is a row *and* a published config *and* a set of
    permissions *and* a set of tool grants — four tables that have to agree, and
    a fixture that hand-wrote them would be provisioning a hotel nobody ships.
    Changing what the Sales agent may do reaches this pilot hotel automatically,
    which is the whole point.

    One source row per agent type, chosen deterministically, because the five
    types are spread across two seeded properties and a join without a
    tie-break would copy an arbitrary one twice.

    `active_config_id` is set last and points at this property's own config row,
    never the one it was copied from: two properties sharing a config row would
    make an edit at one hotel silently change the other's agent.
    """
    sources = "', '".join(SEED_PROPERTIES)
    sql(f"""
create temporary view pilot_agent_source as
select distinct on (a.agent_type) a.*
  from public.agents a
 where a.property_id in ('{sources}')
 order by a.agent_type, a.property_id, a.created_at;

insert into public.agents
  (property_id, partner_id, agent_type, name, status, autonomy_level, channels, limits)
select '{PROPERTY}', '{PARTNER}', s.agent_type, s.name, 'active',
       s.autonomy_level, s.channels, s.limits
  from pilot_agent_source s
 where not exists (select 1 from public.agents b
                    where b.property_id = '{PROPERTY}' and b.agent_type = s.agent_type);

insert into public.agent_permissions (agent_id, scope, property_ids)
select mine.id, p.scope, '{{}}'::uuid[]
  from public.agents mine
  join pilot_agent_source s on s.agent_type = mine.agent_type
  join public.agent_permissions p on p.agent_id = s.id
 where mine.property_id = '{PROPERTY}'
on conflict do nothing;

insert into public.agent_tools (agent_id, tool_name, enabled, autonomy_override,
                                requires_approval, constraints)
select mine.id, t.tool_name, t.enabled, t.autonomy_override,
       t.requires_approval, t.constraints
  from public.agents mine
  join pilot_agent_source s on s.agent_type = mine.agent_type
  join public.agent_tools t on t.agent_id = s.id
 where mine.property_id = '{PROPERTY}'
on conflict do nothing;

insert into public.agent_configs
  (agent_id, version, system_prompt, persona, provider, model, params,
   knowledge_scope, escalation_rules, published_at)
select mine.id, c.version, c.system_prompt, c.persona, c.provider, c.model,
       c.params, c.knowledge_scope, c.escalation_rules, now()
  from public.agents mine
  join pilot_agent_source s on s.agent_type = mine.agent_type
  join public.agent_configs c on c.id = s.active_config_id
 where mine.property_id = '{PROPERTY}'
   and not exists (select 1 from public.agent_configs x where x.agent_id = mine.id);

update public.agents a
   set active_config_id = c.id
  from public.agent_configs c
 where c.agent_id = a.id
   and a.property_id = '{PROPERTY}'
   and a.active_config_id is null;

drop view pilot_agent_source;""")


def knowledge() -> None:
    sql(f"""
insert into public.property_knowledge (property_id, partner_id, question, answer, category)
values ('{PROPERTY}', '{PARTNER}', 'What time is breakfast?',
        'Breakfast is 08:00 to 10:30 on the deck, and it is included on every rate.',
        'dining'),
       ('{PROPERTY}', '{PARTNER}', 'Is there parking?',
        'Yes, free open parking for six cars.', 'parking')
on conflict do nothing;""")


def fund(credits: int = 3000) -> None:
    """A wallet a pilot hotel could actually run on, granted the way a hotel buys."""
    import asyncio

    from app.services import billing

    state = asyncio.run(billing.wallet_state(ACCOUNT))
    have = float(state.get("available") or 0)
    if have < credits:
        # A distinct key per top-up, not per day: this is called again after the
        # suite deliberately drains the wallet, and a key that collided with the
        # opening grant would make the refill a silent no-op.
        asyncio.run(billing.grant(
            billing_account_id=ACCOUNT,
            credits=int(credits - have + 500),
            kind="purchased",
            idempotency_key=f"pilot-fixture-{uuid.uuid4().hex[:12]}",
            metadata={"reason": "pilot fixture top-up"},
        ))


def wallet_available() -> float:
    """Read through `billing.wallet_state`, not off the table.

    The wallet's balance column is a cache the ledger owns and the available
    figure subtracts live holds; a suite that read the column directly would
    disagree with `usage_allowed()` the moment a campaign reserved credits."""
    import asyncio

    from app.services import billing

    return float(asyncio.run(billing.wallet_state(ACCOUNT)).get("available") or 0)


def open_fixture() -> dict:
    """Sweep, build, and hand back the logins."""
    sweep()
    build()
    resolve_account()
    open_inventory()
    provision_agents()
    knowledge()
    fund()
    return {role: signin(email) for role, email in EMAILS.items()}


def close_fixture() -> None:
    sweep()


def new_ref() -> str:
    return f"{REF_PREFIX}{uuid.uuid4().hex[:6].upper()}"


def db_conn():
    return get_db()
