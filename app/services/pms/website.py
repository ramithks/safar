"""
The hotel's own website — a presentation layer, and nothing more.

    builder  ──► draft      (saved on every edit)
                   │
                   └── publish ──► published ──► /stay/[slug] ──► stay_options
                                                                  create_quote

**The site holds no inventory, no rates and no room list.** `/stay/[slug]`
reads availability from `availability.stay_options`, the same function the front
desk calls, so a rate change or a new room type appears on the website with
nothing to re-publish. Copying rooms into the website config would create the
second system this architecture exists to avoid, and it would be wrong within a
day of the first price change.

What the config holds is what a template cannot derive: which template, the
headline, the accent colour, which sections are on, and the hotel's own words.

**Draft and published are two columns on one row.** A hotelier editing their
headline must not change the page a guest is reading while they type, and a
save must not be a publish. Two rows would be a second answer to "is this
property live"; two tables would be worse.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from app.db.client import get_db, run_db
from app.services.pms import seo_ping

logger = logging.getLogger(__name__)

TEMPLATES = (
    "boutique", "resort", "luxury", "homestay",
    "hostel", "business", "beach", "mountain",
)

# The sections a template can switch on. Closed, because a section nobody has
# drawn renders as an empty band down the middle of a hotel's home page.
SECTIONS = (
    "hero", "rooms", "amenities", "gallery",
    "location", "policies", "offers", "contact",
)


def default_draft(property_name: str, city: str | None) -> dict:
    """
    What a hotel's site says before anybody has edited it.

    Deliberately real rather than lorem: a hotelier who publishes without
    editing gets a plain, accurate page about their own hotel, not placeholder
    text with their name on it.
    """
    return {
        "headline": property_name,
        "tagline": f"A stay in {city}" if city else "Book direct",
        "about": "",
        "accentColor": "#0F766E",
        "heroImageUrl": None,
        "gallery": [],
        "amenities": [],
        "contactPhone": None,
        "contactEmail": None,
        "sections": [{"id": s, "enabled": s in ("hero", "rooms", "contact")}
                     for s in SECTIONS],
    }


def _normalise(draft: dict, existing: dict) -> dict:
    """
    Accept what the builder sends, and only that.

    An allow-list rather than a merge of whatever arrived: the config is served
    to the public, and a field nobody designed is a field nobody escaped.
    """
    out = dict(existing or {})
    # `domain` is deliberately absent from this list and always was — which is
    # why the builder's old "Custom domain" box silently discarded what was
    # typed into it. It is not draft content: it is a claim that has to be
    # proved, so it has its own routes and its own state. See services/pms/domains.
    for key in ("headline", "tagline", "about", "accentColor",
                "heroImageUrl", "contactPhone", "contactEmail"):
        if key in draft:
            value = draft[key]
            out[key] = str(value)[:2000] if value is not None else None
    for key in ("gallery", "amenities"):
        if key in draft and isinstance(draft[key], list):
            out[key] = [str(v)[:500] for v in draft[key][:24]]
    if "sections" in draft and isinstance(draft["sections"], list):
        wanted = {
            s.get("id"): bool(s.get("enabled"))
            for s in draft["sections"] if isinstance(s, dict)
        }
        out["sections"] = [
            {"id": s, "enabled": wanted.get(s, False)} for s in SECTIONS
        ]
    return out


def _read(property_id: str) -> dict | None:
    rows = (
        get_db().table("hotel_websites").select("*")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _property(property_id: str) -> dict | None:
    rows = (
        get_db().table("properties").select("id,partner_id,slug,name,city")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _ensure(property_id: str) -> dict:
    """
    The website row for this property, created on first read.

    Lazily rather than by a trigger on `properties`: a hotel that never opens
    the builder should not carry a row, and the first GET is the moment the
    hotelier has actually asked for one.
    """
    db = get_db()
    prop = _property(property_id)
    if not prop:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")

    existing = _read(property_id)
    if existing:
        # The live slug travels with the row. `project` needs it to say where
        # the site actually is, and reading it here means the four endpoints
        # that project a website do not each have to remember to fetch it.
        return {**existing, "property_slug": prop["slug"]}

    return {
        **db.table("hotel_websites").insert({
            "property_id": property_id,
            "partner_id": prop["partner_id"],
            "draft": default_draft(prop["name"], prop.get("city")),
            "travbuddy_path": f"/stay/{prop['slug']}",
        }).execute().data[0],
        "property_slug": prop["slug"],
    }


async def get_config(property_id: str) -> dict:
    return await run_db(_ensure, property_id)


def _direct_30d(property_id: str) -> dict:
    """
    What the hotel's own site has actually sold in the last 30 days.

    **Bookings and money, and deliberately not page views.** The builder used to
    show a "Performance" card with Views, Bookings and a conversion rate; none
    of the three were ever sent by this service, so `site.views30d` was
    `undefined` in the browser and `.toLocaleString()` on it took the whole
    Website screen down with a React error boundary. The screen worked in
    development because the seed data had the fields.

    The repair is not to start sending a views number. Nothing counts visitors
    to `/stay/[slug]` — there is no analytics on that page — so any figure here
    would be invented, and a conversion rate built on an invented denominator
    is worse than no card at all. Bookings and revenue are rows in a table.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    rows = (
        get_db().table("reservations")
        .select("total_paise,status,created_at")
        .eq("property_id", property_id)
        .eq("source", "direct")
        .gte("created_at", since)
        .execute().data or []
    )
    live = [r for r in rows if r.get("status") not in ("cancelled", "no_show")]
    return {
        "directBookings30d": len(live),
        "directRevenue30dPaise": sum(int(r.get("total_paise") or 0) for r in live),
    }


async def direct_30d(property_id: str) -> dict:
    return await run_db(_direct_30d, property_id)


def _save(property_id: str, template: str | None, draft: dict) -> dict:
    row = _ensure(property_id)
    patch: dict = {"draft": _normalise(draft, row.get("draft") or {})}
    if template:
        patch["template"] = template
    # Note what is absent: `published`, `status` and `published_at`. A save is
    # not a publish, and the column the public reads is not reachable from here.
    updated = (
        get_db().table("hotel_websites").update(patch)
        .eq("property_id", property_id).execute().data or []
    )
    return {**(updated[0] if updated else row),
            "property_slug": row.get("property_slug")}


async def save_draft(property_id: str, *, template: str | None, draft: dict) -> dict:
    if template and template not in TEMPLATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Unknown template. One of: {', '.join(TEMPLATES)}.")
    return await run_db(_save, property_id, template, draft)


def _publish(property_id: str, profile_id: str | None) -> dict:
    row = _ensure(property_id)
    prop = _property(property_id)
    if not prop:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")

    draft = row.get("draft") or {}
    if not (draft.get("headline") or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Give the site a headline before publishing it.")

    # A copy, taken now. The published column is a snapshot rather than a
    # pointer, which is what lets the draft carry on changing underneath it.
    updated = (
        get_db().table("hotel_websites").update({
            "status": "published",
            "published": dict(draft),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "published_by": profile_id,
            "travbuddy_path": f"/stay/{prop['slug']}",
        }).eq("property_id", property_id).execute().data or []
    )
    return {**(updated[0] if updated else row), "property_slug": prop["slug"]}


async def publish(property_id: str, profile_id: str | None) -> dict:
    row = await run_db(_publish, property_id, profile_id)
    # The page is live on travbuddy.in a minute later without this; with it, the
    # sitemap moves too, which is the half a crawler actually reads. Best
    # effort — see services/pms/seo_ping.
    await seo_ping.hotel_changed(row.get("property_slug"))
    return row


def _unpublish(property_id: str) -> dict:
    row = _ensure(property_id)
    # `published` is left in place. Taking a site down is not throwing its
    # content away, and re-publishing after a quiet weekend should not require
    # rebuilding the page — the check constraint only requires content when the
    # status is 'published'.
    updated = (
        get_db().table("hotel_websites").update({
            "status": "draft", "published_at": None,
        }).eq("property_id", property_id).execute().data or []
    )
    return {**(updated[0] if updated else row),
            "property_slug": row.get("property_slug")}


async def unpublish(property_id: str) -> dict:
    row = await run_db(_unpublish, property_id)
    # Taking a site down matters more urgently than putting one up: the page
    # now 404s and the sitemap must stop asserting it exists.
    await seo_ping.hotel_changed(row.get("property_slug"))
    return row


def project(row: dict, *, slug: str | None = None) -> dict:
    """
    One shape, so the builder and the settings screen cannot disagree.

    **The address is derived from the property's slug, not from the stored
    column.** `travbuddy_path` is written at publish time, so a hotel that
    changes its slug afterwards keeps a stored path that points at a page which
    no longer resolves -- and the settings screen would go on showing the
    owner a dead link as though it were their website. The slug is the one
    source of truth for where the site is; the column is a cache of it, and a
    cache does not get to win an argument with the thing it caches.
    """
    draft = row.get("draft") or {}
    return {
        "propertyId": row["property_id"],
        "template": row.get("template") or "boutique",
        "status": row.get("status") or "draft",
        "travbuddyPath": (
            f"/stay/{slug or row.get('property_slug')}"
            if (slug or row.get("property_slug"))
            else row.get("travbuddy_path") or ""
        ),
        # Only a domain that actually resolves. A pending one is a claim, not
        # an address, and printing it as "Live at grandhotel.com" while the
        # DNS is still missing is the screen telling an owner their site is
        # somewhere it is not. The claim itself lives in `customDomain`.
        "domain": row.get("domain") if row.get("domain_status") == "verified" else None,
        "headline": draft.get("headline") or "",
        "tagline": draft.get("tagline") or "",
        "about": draft.get("about") or "",
        "accentColor": draft.get("accentColor") or "#0F766E",
        "heroImageUrl": draft.get("heroImageUrl"),
        "gallery": draft.get("gallery") or [],
        "amenities": draft.get("amenities") or [],
        "contactPhone": draft.get("contactPhone"),
        "contactEmail": draft.get("contactEmail"),
        "sections": draft.get("sections") or [],
        "publishedAt": row.get("published_at"),
        # The claim and its state, so the builder can render the setup steps
        # without a second round trip. The DNS lookup is not done here — that
        # is `/website/domain`, which an owner asks for by opening the panel.
        "customDomain": {
            "status": row.get("domain_status") or "none",
            "domain": row.get("domain"),
            "verifiedAt": row.get("domain_verified_at"),
            "checkedAt": row.get("domain_checked_at"),
            "error": row.get("domain_error"),
            "records": [],
        },
        # Whether the draft has moved since the last publish. What it drives is
        # a "you have unpublished changes" line, which is the one thing a
        # builder must tell somebody who thinks they have already shipped.
        "hasUnpublishedChanges": bool(
            row.get("status") == "published"
            and row.get("published") is not None
            and row.get("published") != draft
        ),
    }
