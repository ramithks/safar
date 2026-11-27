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

# The looks a hotel can choose between.
#
# `minimal` and `heritage` were added with the visual template picker: the eight
# original names are property *types* as much as designs, and an owner choosing
# between "Homestay" and "Hostel" is being asked what their hotel is rather than
# what they want their page to look like. The database CHECK constraint carries
# the same list — see the migration named for this.
TEMPLATES = (
    "boutique", "resort", "luxury", "homestay", "hostel",
    "business", "beach", "mountain", "minimal", "heritage",
)

# The sections a template can switch on. Closed, because a section nobody has
# drawn renders as an empty band down the middle of a hotel's home page.
#
# Six were added when the builder became a website builder rather than a form:
# a trust strip under the hero, the hotel's own story, what there is to do, the
# questions guests actually ask, and the reviews — each of which existed as
# data this service already had and as a band the page did not draw.
SECTIONS = (
    "hero", "highlights", "rooms", "about", "amenities", "gallery",
    "experiences", "offers", "location", "reviews", "faq", "policies", "contact",
)

# Whether a section a draft has never heard of arrives switched on.
#
# **On, every one of them.** A section only draws when it has something in it —
# that rule lives in the renderer and in `compose`, which never fills a band it
# has no facts for — so "enabled" means "show this if there is something to
# show" rather than "reserve a stripe of white space". The alternative, adding
# them switched off, means a hotel that generates a site gets a page missing
# the sections that were the reason to regenerate it, and no way to discover
# they exist short of reading a toggle list.
SECTION_DEFAULTS: dict[str, bool] = {s: True for s in SECTIONS}


def sections_for(draft: dict) -> list[dict]:
    """
    The closed section list, whatever the stored row happens to hold.

    Rows written before a section existed simply do not mention it, and a
    builder rendering `draft["sections"]` verbatim would offer eight toggles on
    an old row and thirteen on a new one. Projected through the defaults here
    so every reader — builder, preview, public page — sees one list in one
    order.
    """
    stored = {
        s.get("id"): bool(s.get("enabled"))
        for s in (draft.get("sections") or [])
        if isinstance(s, dict)
    }
    return [
        {"id": s, "enabled": stored.get(s, SECTION_DEFAULTS[s])} for s in SECTIONS
    ]


# What a highlight strip may say. Closed, because each key is an icon the page
# draws, and an unknown one is a blank square beside a promise.
HIGHLIGHT_ICONS = (
    "rate", "wifi", "breakfast", "parking", "reception", "pool", "ac",
    "restaurant", "pets", "cancellation", "family", "view",
)

# How the type is set and how a button is shaped. Three small closed
# vocabularies rather than a stylesheet: an owner picking "Editorial" is making
# a choice a designer already made, and a free-text font name is a page that
# renders in Times New Roman on somebody else's laptop.
FONTS = ("modern", "editorial", "grotesk", "classic")
BUTTON_STYLES = ("rounded", "pill", "square")
LAYOUTS = ("standard", "wide", "compact")


# What a photograph can be of.
#
# Closed, and short enough that a hotelier picks rather than types. Every one of
# them is a thing a guest looks for before booking — which is also why they make
# usable alt text on their own: "the pool at Casa Mandala, Anjuna" is a true
# sentence about the image and a phrase somebody searches.
PHOTO_CATEGORIES = (
    "exterior", "lobby", "room", "bathroom", "dining",
    "pool", "amenity", "surroundings", "other",
)

# The alt text each category writes when the hotelier has not.
#
# **Derived rather than demanded.** Nobody fills in twenty-four alt-text boxes,
# and a required field here would mean either empty alts or twenty-four copies
# of the hotel's name — which is what a screen reader would then read out
# twenty-four times. A phrase built from what the hotel already told us is true
# without anybody typing, and a hotelier who wants better can still overwrite
# it.
_CATEGORY_PHRASE = {
    "exterior": "Outside {hotel}",
    "lobby": "The lobby at {hotel}",
    "room": "A room at {hotel}",
    "bathroom": "A bathroom at {hotel}",
    "dining": "Dining at {hotel}",
    "pool": "The pool at {hotel}",
    "amenity": "Facilities at {hotel}",
    "surroundings": "Around {hotel}",
    "other": "{hotel}",
}

MAX_GALLERY = 24


def photo_alt(item: dict, *, hotel: str, city: str | None = None,
              subject: str | None = None) -> str:
    """
    What a screen reader says, and what a search engine reads.

    Precedence is deliberate: what the hotelier wrote, then the caption they
    wrote for a human, then a sentence built from the category. The fallback is
    never the empty string — an unlabelled photograph on a hotel page is a
    guest with a screen reader being told nothing at all, and §26 asks for alt
    text rather than for an attribute that exists.
    """
    explicit = (item.get("alt") or "").strip()
    if explicit:
        return explicit
    caption = (item.get("caption") or "").strip()
    if caption:
        return caption
    if subject:
        # A room's own photographs. The room name is more specific than any
        # category, and specific is the whole value of alt text.
        return f"{subject} at {hotel}" + (f", {city}" if city else "")
    phrase = _CATEGORY_PHRASE.get(item.get("category") or "other", "{hotel}")
    return phrase.format(hotel=hotel) + (f", {city}" if city else "")


def gallery_item(value) -> dict | None:
    """
    One gallery entry, from either shape it can arrive in.

    Rows written before this existed hold bare URL strings, and they are still
    the majority. Reading them as `{"url": ...}` costs nothing and means no
    migration touches a jsonb column that a hotelier may be editing.
    """
    if isinstance(value, str):
        url = value.strip()
        return {"url": url[:500], "category": "other", "caption": None, "alt": None} if url else None
    if not isinstance(value, dict):
        return None
    url = str(value.get("url") or "").strip()
    if not url:
        return None
    category = value.get("category")
    return {
        "url": url[:500],
        "category": category if category in PHOTO_CATEGORIES else "other",
        "caption": (str(value["caption"])[:160] or None) if value.get("caption") else None,
        "alt": (str(value["alt"])[:160] or None) if value.get("alt") else None,
    }


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
        "highlights": [],
        "faq": [],
        "offers": [],
        "seo": {},
        "design": {"font": "modern", "buttons": "rounded", "layout": "standard"},
        "contactPhone": None,
        "contactEmail": None,
        "sections": [{"id": s, "enabled": SECTION_DEFAULTS[s]} for s in SECTIONS],
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
    if "amenities" in draft and isinstance(draft["amenities"], list):
        out["amenities"] = [str(v)[:500] for v in draft["amenities"][:24]]
    if "gallery" in draft and isinstance(draft["gallery"], list):
        # Stored as objects whichever shape arrived, so every reader downstream
        # — the page, the preview, the structured data — handles one.
        seen: set[str] = set()
        gallery = []
        for value in draft["gallery"][:MAX_GALLERY]:
            item = gallery_item(value)
            if item and item["url"] not in seen:
                seen.add(item["url"])
                gallery.append(item)
        out["gallery"] = gallery
    if "sections" in draft and isinstance(draft["sections"], list):
        wanted = {
            s.get("id"): bool(s.get("enabled"))
            for s in draft["sections"] if isinstance(s, dict)
        }
        # A section the *client* did not mention keeps what it had, and only
        # falls back to the default when the row has never held it either.
        #
        # It used to fall to `False`, which meant an older builder — one that
        # knows eight section ids — saving a headline would silently switch off
        # the five it has never heard of, and the hotel's FAQ would vanish from
        # a live page because somebody edited a sentence in an old tab.
        current = {s["id"]: s["enabled"] for s in sections_for(existing or {})}
        out["sections"] = [
            {"id": s, "enabled": wanted.get(s, current.get(s, SECTION_DEFAULTS[s]))}
            for s in SECTIONS
        ]

    # ── the bands the builder fills in ───────────────────────────────────────
    #
    # Each of these is a list of small objects rather than prose, for the same
    # reason the rest of this file is an allow-list: they are rendered onto a
    # page a guest reads, so what may appear in them is decided here and not by
    # whatever arrived.
    if "highlights" in draft and isinstance(draft["highlights"], list):
        out["highlights"] = [
            {
                "icon": item.get("icon") if item.get("icon") in HIGHLIGHT_ICONS else "rate",
                "label": str(item.get("label") or "")[:60],
            }
            for item in draft["highlights"][:6]
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        ]
    if "faq" in draft and isinstance(draft["faq"], list):
        out["faq"] = [
            {"q": str(item.get("q") or "")[:200], "a": str(item.get("a") or "")[:1000]}
            for item in draft["faq"][:12]
            if isinstance(item, dict)
            and str(item.get("q") or "").strip()
            and str(item.get("a") or "").strip()
        ]
    if "offers" in draft and isinstance(draft["offers"], list):
        out["offers"] = [
            {
                "title": str(item.get("title") or "")[:80],
                "detail": str(item.get("detail") or "")[:300],
                "terms": str(item["terms"])[:300] if item.get("terms") else None,
            }
            for item in draft["offers"][:6]
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
    if "seo" in draft and isinstance(draft["seo"], dict):
        # Cut to what a search result actually shows. A 300-character meta
        # description is not a longer snippet, it is the same snippet with an
        # ellipsis and a hotelier who thinks they wrote the second half.
        out["seo"] = {
            "title": str(draft["seo"].get("title") or "")[:70],
            "description": str(draft["seo"].get("description") or "")[:180],
        }
    if "design" in draft and isinstance(draft["design"], dict):
        design = draft["design"]
        out["design"] = {
            "font": design.get("font") if design.get("font") in FONTS else "modern",
            "buttons": (
                design.get("buttons") if design.get("buttons") in BUTTON_STYLES else "rounded"
            ),
            "layout": design.get("layout") if design.get("layout") in LAYOUTS else "standard",
        }

    # **Editing a field takes it away from the generator.**
    #
    # `generatedFields` is the list of things `website_compose` wrote itself,
    # and the only thing it is allowed to write over next time. The moment a
    # hotelier saves their own headline, the name leaves the list here — so
    # pressing Generate again after uploading six photographs rebuilds the
    # gallery and the highlights and leaves the sentence they wrote alone.
    #
    # Done in `_normalise` rather than in the endpoint because this is the one
    # function every draft write passes through, and a second path that forgot
    # would silently reclaim a hotelier's words.
    claimed = set(out.get("generatedFields") or [])
    if claimed:
        edited = {k for k in draft if k in claimed}
        if edited:
            out["generatedFields"] = sorted(claimed - edited)
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
            "safar_path": f"/stay/{prop['slug']}",
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


def _save_composed(property_id: str, template: str | None, draft: dict) -> dict:
    """
    Write a draft this server composed.

    Deliberately not routed through `_normalise`: that function is the door a
    *client* comes through, and the two keys it does not accept —
    `generatedAt` and `generatedFields` — are precisely the ones a client must
    never set, because they decide what the next generate is allowed to
    overwrite. What is written here was built by `website_compose` from rows in
    this database, on top of a draft that had already been normalised.
    """
    row = _ensure(property_id)
    patch: dict = {"draft": draft}
    if template:
        patch["template"] = template
    updated = (
        get_db().table("hotel_websites").update(patch)
        .eq("property_id", property_id).execute().data or []
    )
    return {**(updated[0] if updated else row),
            "property_slug": row.get("property_slug")}


async def generate(property_id: str, *, force: bool = False) -> dict:
    """
    Build this hotel's site from this hotel's records, and save it as the draft.

    **A draft, never a publish.** The generate button is not a publish button:
    an owner presses it to see what their page would look like, and a page that
    went live because they were curious is the single worst thing this feature
    could do.
    """
    # Imported here rather than at module scope: the composer imports this
    # module for its section list and its gallery normaliser, and a top-level
    # import in both directions is a cycle.
    from app.services.pms import website_compose

    found = await website_compose.facts(property_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")

    row = _read(property_id) or {}
    template = website_compose.template_for(found, row.get("template"))
    draft = website_compose.compose(found, force=force)

    # An accent colour only when the hotel has never had one — a template
    # suggestion should not repaint a page somebody has already coloured.
    if template and not (draft.get("accentColor") or "").strip():
        draft["accentColor"] = website_compose.accent_for(template)

    saved = await run_db(_save_composed, property_id, template, draft)
    return saved


async def inventory(property_id: str) -> dict:
    """What a generate would be built from, counted, before it runs."""
    from app.services.pms import website_compose

    found = await website_compose.facts(property_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    return website_compose.inventory(found)


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
            "safar_path": f"/stay/{prop['slug']}",
        }).eq("property_id", property_id).execute().data or []
    )
    return {**(updated[0] if updated else row), "property_slug": prop["slug"]}


async def publish(property_id: str, profile_id: str | None) -> dict:
    row = await run_db(_publish, property_id, profile_id)
    # The page is live on safar.example.com a minute later without this; with it, the
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
    column.** `safar_path` is written at publish time, so a hotel that
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
        "safarPath": (
            f"/stay/{slug or row.get('property_slug')}"
            if (slug or row.get("property_slug"))
            else row.get("safar_path") or ""
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
        # Normalised on the way out too, because rows written before the
        # builder had a gallery still hold bare strings and the builder must
        # not have to know that.
        "gallery": [i for i in (gallery_item(v) for v in (draft.get("gallery") or [])) if i],
        "amenities": draft.get("amenities") or [],
        "contactPhone": draft.get("contactPhone"),
        "contactEmail": draft.get("contactEmail"),
        # Projected through the closed list rather than returned verbatim, so a
        # row written before a section existed does not hand the builder a
        # shorter list of toggles than the one it draws. See `sections_for`.
        "sections": sections_for(draft),
        "highlights": draft.get("highlights") or [],
        "faq": draft.get("faq") or [],
        "offers": draft.get("offers") or [],
        "seo": draft.get("seo") or {},
        "design": draft.get("design") or {
            "font": "modern", "buttons": "rounded", "layout": "standard",
        },
        # When this site was last composed from the hotel's own records. What
        # it drives is one sentence in the builder — "built from your rooms and
        # photos on 26 August" — which is the difference between a page an
        # owner trusts and one they assume is a demo.
        "generatedAt": draft.get("generatedAt"),
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
