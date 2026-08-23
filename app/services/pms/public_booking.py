"""
The public booking boundary.

    /stay/[slug]  ──►  this module  ──►  availability.stay_options
                                    ──►  booking.create_quote
                                    ──►  booking.create_reservation

**There is no pricing in this file, and there must never be.** Every number a
guest is shown comes from `availability.stay_options` and `booking.create_quote`
— the same two functions the front desk, the Sales agent and the B2B portal
call. A receptionist, an AI agent and a stranger on the internet asking about
the same three nights get the same answer because they are asking the same
function, not because three implementations were kept in step.

What this module *does* add is the three things a public caller needs that an
authenticated one does not:

**Resolution from a slug, not an id.** A public caller never names a property
id, a partner id or a rate plan they were not offered. `resolve_published`
turns `/stay/casa-mandala-anjuna` into a property, and refuses one whose website
is not published — an unpublished site is not a hidden page, it is a 404.

**Add-ons, priced and capacity-checked server-side.** The browser sends a
service id and a quantity. It does not send a price, and if it does the price is
ignored: `price_addons` reads `services.price_paise` and computes the total. It
also enforces the two rules a client cannot be trusted with — daily capacity
across every other booking, and the lead time before the service date.

**An audience.** `stay_options(audience="public")` is what stops a stranger
being quoted the B2B contracted rate. It is passed here, once, and the service
does the filtering.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status

from app.db.client import get_db, run_db
from app.services.pms import clock, onboarding

logger = logging.getLogger(__name__)

# How many add-on lines one booking may carry. A guest buying nine different
# extras is plausible; ninety is a script.
MAX_ADDON_LINES = 10


@dataclass
class ResolvedProperty:
    """A published hotel, resolved from its public slug."""

    id: str
    partner_id: str
    slug: str
    name: str
    city: str | None
    currency: str
    timezone: str
    check_in_time: str
    check_out_time: str
    policies: dict
    website: dict = field(default_factory=dict)
    template: str = "boutique"

    # Seeded or test data. Carried through to the page so the template can
    # force `noindex` regardless of how complete the hotel looks — a demo hotel
    # that passes the quality gate is the worst case, not the safe one, because
    # it is the one a crawler will happily index. See §36 and the `is_demo`
    # column comment.
    is_demo: bool = False

    # The facts a *page about this hotel* needs, as opposed to the facts a
    # booking needs. Added when the hotel website moved to server rendering:
    # an address, a map pin and a property type are what turn a booking form
    # into something a search engine — or an answer engine — can describe.
    # None of them is new data; every one has been on `properties` since it was
    # created and simply never left the building.
    address: str | None = None
    state: str | None = None
    country: str | None = None
    pincode: str | None = None
    lat: float | None = None
    lon: float | None = None
    property_type: str | None = None
    total_rooms: int | None = None
    updated_at: str | None = None


def _read_published(slug: str) -> dict | None:
    db = get_db()
    rows = (
        db.table("properties")
        .select("id,partner_id,slug,name,city,currency,timezone,check_in_time,"
                "check_out_time,policies,status,address,state,country,pincode,"
                "lat,lon,property_type,total_rooms,updated_at,is_demo")
        .ilike("slug", slug.strip())
        .limit(1).execute().data or []
    )
    if not rows or rows[0].get("status") != "active":
        return None
    prop = rows[0]

    site = (
        db.table("hotel_websites")
        .select("template,status,published,published_at")
        .eq("property_id", prop["id"]).limit(1).execute().data or []
    )
    if not site or site[0].get("status") != "published" or not site[0].get("published"):
        return None
    return {"property": prop, "site": site[0]}


async def resolve_published(slug: str) -> ResolvedProperty:
    """
    A published hotel, or 404.

    **An unpublished website is indistinguishable from a slug that does not
    exist.** That is deliberate: a hotelier drafting a site has not agreed to
    anybody seeing it, and "this hotel exists but is not live yet" is a fact
    about a business they have not announced.

    A suspended property is refused the same way — its status is the hotel's
    business, not a stranger's.
    """
    found = await run_db(_read_published, slug)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that hotel.")

    prop, site = found["property"], found["site"]
    return ResolvedProperty(
        id=prop["id"],
        partner_id=prop["partner_id"],
        slug=prop["slug"],
        name=prop["name"],
        city=prop.get("city"),
        currency=prop.get("currency") or "INR",
        timezone=prop.get("timezone") or clock.DEFAULT_TIMEZONE,
        check_in_time=str(prop.get("check_in_time") or "")[:5],
        check_out_time=str(prop.get("check_out_time") or "")[:5],
        # House rules only. `properties.policies` is one column holding both
        # the hotelier's house rules and their commercial settings
        # (`token_pct`, `max_discount_pct`). Everything downstream of this
        # resolver is a guest-facing surface, so the split happens here
        # rather than at each of them.
        policies=onboarding.guest_policies(prop.get("policies")),
        is_demo=bool(prop.get("is_demo")),
        website=site.get("published") or {},
        template=site.get("template") or "boutique",
        address=prop.get("address"),
        state=prop.get("state"),
        country=prop.get("country"),
        pincode=prop.get("pincode"),
        lat=prop.get("lat"),
        lon=prop.get("lon"),
        property_type=prop.get("property_type"),
        total_rooms=prop.get("total_rooms"),
        # The website's own publish time when there is one, because that is
        # when the *page* last changed — a property row touched by a rate edit
        # is not a content change, and a sitemap lastmod that moves for reasons
        # a reader cannot see is one Google learns to ignore.
        updated_at=str(site.get("published_at") or prop.get("updated_at") or "") or None,
    )


# ── dates ────────────────────────────────────────────────────────────────────

def validate_stay_dates(prop: ResolvedProperty, check_in: date, check_out: date) -> None:
    """
    The three date rules, refused with sentences a guest can act on.

    Checked against the *property's* today, not the server's. A guest in London
    booking a Goa hotel at 23:00 GMT is asking about tomorrow there, and a
    server-side `date.today()` would refuse a date that is perfectly bookable.
    """
    if check_out <= check_in:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Check-out must be after check-in.")
    nights = (check_out - check_in).days
    if nights > 30:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Please contact the hotel directly for stays over 30 nights.")
    today = clock.business_date_in(prop.timezone)
    if check_in < today:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That arrival date has passed.")
    if check_in > today + timedelta(days=400):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That is too far ahead to book online.")


# ── add-ons ──────────────────────────────────────────────────────────────────

@dataclass
class PricedAddon:
    service_id: str
    name: str
    quantity: int
    unit_price_paise: int
    total_paise: int
    service_date: date | None

    def as_dict(self) -> dict:
        return {
            "serviceId": self.service_id,
            "name": self.name,
            "quantity": self.quantity,
            "unitPricePaise": self.unit_price_paise,
            "totalPaise": self.total_paise,
            "serviceDate": self.service_date.isoformat() if self.service_date else None,
        }


def _read_services(property_id: str, ids: list[str]) -> tuple[list[dict], dict[str, int]]:
    """The requested services, and how much of each is already spoken for."""
    db = get_db()
    rows = (
        db.table("services").select("*")
        .eq("property_id", property_id).in_("id", ids)
        .eq("is_active", True).execute().data or []
    )
    booked: dict[str, int] = {}
    if rows:
        for r in (db.table("reservation_services")
                  .select("service_id,service_date,quantity,status")
                  .in_("service_id", [s["id"] for s in rows])
                  .neq("status", "cancelled")
                  .limit(5000).execute().data or []):
            key = f"{r['service_id']}:{r.get('service_date') or ''}"
            booked[key] = booked.get(key, 0) + int(r.get("quantity") or 0)
    return rows, booked


async def price_addons(
    property_id: str,
    selections: list[dict],
    *,
    check_in: date,
    check_out: date,
    timezone_name: str,
) -> list[PricedAddon]:
    """
    Turn `[{serviceId, quantity, serviceDate}]` into priced lines, or refuse.

    **The price is never read from the request.** If the browser sends one it is
    ignored — the total is `services.price_paise × quantity`, computed here.
    That is the whole reason this function exists rather than the endpoint
    trusting a client total, and it is the same argument `price_stay` makes for
    the room.

    Four ways this refuses, all of them things a client cannot be trusted to
    check:

      * a service that is not this hotel's, or is retired
      * a date outside the stay — an airport transfer on a night the guest is
        not here is a job nobody will do
      * daily capacity already taken by other bookings
      * a service booked inside its own lead time
    """
    if not selections:
        return []
    if len(selections) > MAX_ADDON_LINES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Too many extras on one booking.")

    wanted: dict[str, dict] = {}
    for row in selections:
        sid = str(row.get("serviceId") or "").strip()
        if not sid:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "An extra is missing its id.")
        if sid in wanted:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "The same extra was listed twice.")
        quantity = int(row.get("quantity") or 0)
        if quantity < 1 or quantity > 50:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "That quantity is not available.")
        raw_date = row.get("serviceDate")
        when = date.fromisoformat(str(raw_date)) if raw_date else check_in
        # Half-open, like every other stay window here: a guest is not present
        # on the morning they leave.
        if not (check_in <= when < check_out):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "An extra was requested for a date outside the stay.",
            )
        wanted[sid] = {"quantity": quantity, "date": when}

    services, booked = await run_db(_read_services, property_id, list(wanted))
    by_id = {s["id"]: s for s in services}

    now_local = datetime.now(tz=clock.zone(timezone_name))
    priced: list[PricedAddon] = []
    for sid, req in wanted.items():
        service = by_id.get(sid)
        if service is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "One of those extras is no longer offered.")

        lead = int(service.get("lead_time_hours") or 0)
        if lead:
            earliest = datetime.combine(req["date"], datetime.min.time(),
                                        tzinfo=clock.zone(timezone_name))
            if earliest - now_local < timedelta(hours=lead):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{service['name']} needs {lead} hours' notice.",
                )

        capacity = service.get("daily_capacity")
        if capacity is not None:
            taken = booked.get(f"{sid}:{req['date'].isoformat()}", 0)
            if taken + req["quantity"] > int(capacity):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{service['name']} is fully booked for that day.",
                )

        unit = int(service.get("price_paise") or 0)
        priced.append(PricedAddon(
            service_id=sid,
            name=service["name"],
            quantity=req["quantity"],
            unit_price_paise=unit,
            total_paise=unit * req["quantity"],
            service_date=req["date"],
        ))

    return priced


def _write_addons(reservation: dict, priced: list[dict]) -> list[dict]:
    """
    Attach add-ons to a reservation and put them on its folio.

    One folio line per add-on rather than a single "extras" total: a guest
    querying a bill asks about the dinner, not about a subtotal, and the night
    audit and the front desk both read the folio line by line.

    `idempotency_key` is derived from the reservation and the service, so a
    retried confirmation cannot post the same dinner twice.
    """
    db = get_db()
    written: list[dict] = []
    for line in priced:
        row = db.table("reservation_services").insert({
            "reservation_id": reservation["id"],
            "service_id": line["serviceId"],
            "property_id": reservation["property_id"],
            "partner_id": reservation["partner_id"],
            "name": line["name"],
            "quantity": line["quantity"],
            "unit_price_paise": line["unitPricePaise"],
            "total_paise": line["totalPaise"],
            "service_date": line.get("serviceDate"),
        }).execute().data[0]

        folio = db.table("folio_entries").insert({
            "reservation_id": reservation["id"],
            "property_id": reservation["property_id"],
            "partner_id": reservation["partner_id"],
            "kind": "addon",
            "description": (
                f"{line['name']}"
                + (f" × {line['quantity']}" if line["quantity"] > 1 else "")
            ),
            "amount_paise": line["totalPaise"],
            "posted_by_name": "Booking engine",
            "idempotency_key": f"addon:{reservation['id']}:{line['serviceId']}",
        }).execute().data[0]

        db.table("reservation_services").update(
            {"folio_entry_id": folio["id"]}
        ).eq("id", row["id"]).execute()
        row["folio_entry_id"] = folio["id"]
        written.append(row)
    return written


async def attach_addons(reservation: dict, priced: list[dict]) -> list[dict]:
    if not priced:
        return []
    return await run_db(_write_addons, reservation, priced)


def _read_reservation_services(reservation_id: str) -> list[dict]:
    return (
        get_db().table("reservation_services").select("*")
        .eq("reservation_id", reservation_id)
        .order("created_at").execute().data or []
    )


async def list_for_reservation(reservation_id: str) -> list[dict]:
    return await run_db(_read_reservation_services, reservation_id)


# ── what a guest is asked to pay now ─────────────────────────────────────────

def checkout_amount_paise(quote_total: int, quote_token: int, addons_total: int) -> int:
    """
    The figure that goes to Razorpay.

    **The room's booking token plus the extras in full.** PRD §11.1 lets a
    property configure the token — fixed, a percentage, or full prepayment —
    and `create_quote` has already resolved that into `token_paise` from
    `properties.settings`. The remainder of the room is balance due on the
    folio, which is exactly what the folio is for.

    Add-ons are charged in full rather than tokenised, and that is a decision
    worth stating: an add-on holds a capacity slot that another guest cannot
    then buy, so a hotel that has turned away a second airport transfer should
    not be relying on somebody paying for the first one at the desk.

    Never less than ₹1, because Razorpay refuses an order below 100 paise. A
    property on pay-at-hotel with no extras therefore cannot use this path at
    all, which is correct — there is nothing to charge, and the endpoint says so
    rather than creating a one-rupee order nobody asked for.
    """
    return max(0, min(quote_token, quote_total)) + max(0, addons_total)
