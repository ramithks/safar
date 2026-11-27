"""
Publishing an ad, and being honest when we cannot.

§28 and §29 are unusually specific about the failure they want avoided:

    Do not fake a Meta campaign as created if the API has not actually created
    it. … Never tell the hotel "Ad published" unless the Meta API confirms it.

That is the whole design of this module, and it is enforced in three places
rather than one, because a status is exactly the kind of thing that drifts:

  1. **The database.** `ad_campaigns_published_has_id` refuses any row in
     `published`, `running` or `paused` without an `external_id`. There is no
     code path — here or anywhere later — that can write a published campaign
     Meta does not know about.
  2. **This module.** Every transition out of `draft` comes from a Meta
     response or from a refusal. Nothing here optimistically advances a status.
  3. **The screen.** `not_connected` is a first-class state with its own
     explanation, not an error dialog over a UI that otherwise looks ready.

**What is built and what is not.** StaySuite holds Instagram *messaging*
credentials — `instagram_business_basic` and `instagram_business_manage_messages`
— which are the scopes for reading and replying to DMs. Creating ads needs
`ads_management` on a Meta *Business* account with a funded ad account, a
different OAuth app configuration and a different review. None of that exists
today, and pretending otherwise would produce exactly the lie the brief forbids.

So `publish()` composes the full request, validates everything checkable, and
then reports `not_connected` with the specific thing that is missing. When the
ad account is configured, the one function that posts becomes real and nothing
above it changes — the campaign, the creative, the audience mapping and the
status machine are all already here and already tested.
"""
import logging
from dataclasses import dataclass

from app.config import settings
from app.services.marketing import creatives

logger = logging.getLogger(__name__)

# Meta's call-to-action vocabulary, keyed by the destination a hotelier picked.
#
# Mapped here rather than stored, because it is Meta's list and it changes on
# Meta's schedule — a column full of their enum values is a migration the next
# time they rename one.
META_CTA: dict[str, str] = {
    "whatsapp": "WHATSAPP_MESSAGE",
    "instagram_dm": "MESSAGE_PAGE",
    "website": "BOOK_TRAVEL",
    "hotel_page": "LEARN_MORE",
}

META_OBJECTIVE: dict[str, str] = {
    "messages": "OUTCOME_ENGAGEMENT",
    "traffic": "OUTCOME_TRAFFIC",
    "awareness": "OUTCOME_AWARENESS",
    "engagement": "OUTCOME_ENGAGEMENT",
    "leads": "OUTCOME_LEADS",
}


@dataclass(frozen=True)
class PublishResult:
    """
    What happened, in terms the campaign row can be written from.

    `status` is one of the column's own values and is never invented: a result
    carrying `published` also carries an `external_id`, which the check
    constraint then insists on again.
    """

    status: str
    external_id: str | None = None
    detail: str | None = None
    # What was missing, when the answer is not_connected. Named individually so
    # the screen can say "connect an ad account" rather than "not configured".
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in ("published", "running")

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "externalId": self.external_id,
            "detail": self.detail,
            "missing": list(self.missing),
        }


def _missing_credentials(connection: dict | None) -> tuple[str, ...]:
    """
    What this hotel still needs before an ad can be created.

    Ordered by what a hotelier would fix first. Each entry is a thing they can
    act on, not a variable name.
    """
    missing: list[str] = []
    if not connection or connection.get("status") != "connected":
        missing.append("instagram_account")
    if not getattr(settings, "meta_ad_account_id", None):
        missing.append("ad_account")
    if not getattr(settings, "meta_ads_access_token", None):
        missing.append("ads_permission")
    return tuple(missing)


def build_request(
    *,
    campaign: dict,
    creative: dict,
    audience: dict | None,
    page_id: str | None,
    whatsapp_number: str | None,
) -> dict:
    """
    The Meta request this campaign would make, composed in full.

    Built and returned even when it cannot be sent. Two reasons, and the second
    is the one that matters:

    * A hotelier can be shown exactly what would be created, which is the
      difference between "we will make an ad" and a reviewable artefact.
    * When the credentials arrive, the thing that goes on the wire is the thing
      that has been under test since this shipped, rather than a payload written
      for the first time on the day it first runs.
    """
    fmt = creative.get("format") or "single_image"
    media = creative.get("media") or []

    spec: dict = {
        "objective": META_OBJECTIVE.get(campaign.get("objective") or "messages", "OUTCOME_ENGAGEMENT"),
        "name": campaign.get("name"),
        "status": "PAUSED",  # never live on creation; a human presses go
        "daily_budget": campaign.get("daily_budget_paise"),
        "start_time": campaign.get("starts_on"),
        "end_time": campaign.get("ends_on"),
        "creative": {
            "format": fmt,
            "headline": creative.get("headline"),
            "primary_text": creative.get("primary_text"),
            "call_to_action": META_CTA.get(campaign.get("destination") or "whatsapp", "LEARN_MORE"),
        },
        "targeting": _targeting(audience),
        "placements": ["instagram_stories", "instagram_reels", "facebook_feed"]
        if campaign.get("channel") == "instagram" else ["facebook_feed"],
    }

    if fmt == "existing_post":
        # Boosting something already published: Meta needs the post, and nothing
        # else. No creative is uploaded, which is why this format carries no
        # media of its own.
        spec["creative"]["object_story_id"] = creative.get("instagram_media_id")
    elif fmt == "carousel":
        spec["creative"]["child_attachments"] = [
            {
                "picture": card.get("url"),
                "name": card.get("caption") or creative.get("headline"),
                "call_to_action": {"type": spec["creative"]["call_to_action"]},
            }
            for card in sorted(media, key=lambda c: c.get("position") or 0)
        ]
    elif fmt == "video":
        spec["creative"]["video_url"] = (media[0] or {}).get("url") if media else None
    else:
        spec["creative"]["image_url"] = (media[0] or {}).get("url") if media else None

    if (campaign.get("destination") or "whatsapp") == "whatsapp" and whatsapp_number:
        # A click-to-WhatsApp ad opens a thread rather than a page, which is the
        # whole point of it for a hotel: the guest lands in the inbox the AI is
        # already answering.
        spec["destination"] = {"type": "whatsapp", "number": whatsapp_number}
    elif page_id:
        spec["destination"] = {"type": "page", "page_id": page_id}

    return spec


def _targeting(audience: dict | None) -> dict:
    """
    An audience, in roughly the shape Meta wants.

    **Interests are passed as free text and not as Meta interest ids**, because
    those ids come from a search endpoint this cannot call yet, and inventing
    them would produce an ad targeted at something arbitrary. `ad_audiences.
    meta_spec` exists to hold what Meta actually accepted once it can be asked,
    which is also the only honest source for that mapping.
    """
    if not audience:
        return {}
    return {
        "age_min": audience.get("age_min"),
        "age_max": audience.get("age_max"),
        "genders": audience.get("genders") or [],
        "geo_locations": audience.get("locations") or [],
        # Kept under a name that says what they are, so nobody downstream reads
        # them as resolved targeting.
        "interests_unresolved": audience.get("interests") or [],
        "travel_intent_unresolved": audience.get("travel_intent") or [],
    }


async def publish(
    *,
    campaign: dict,
    creative: dict,
    audience: dict | None,
    connection: dict | None,
    page_id: str | None = None,
    whatsapp_number: str | None = None,
) -> PublishResult:
    """
    Create this campaign on Meta, or say precisely why not.

    The order is deliberate: everything checkable locally is checked *first*, so
    a hotel with a one-photo carousel is told that rather than being told it has
    no ad account. A refusal a hotelier can act on beats a refusal that is
    technically the first thing wrong.
    """
    problem = creatives.format_problem(
        creative.get("format") or "single_image",
        creative.get("media") or [],
        creative.get("instagram_media_id"),
    )
    if problem:
        return PublishResult(status="draft", detail=problem)

    unauthorised = creatives.check_copy(
        creative.get("headline") or "",
        creative.get("primary_text") or "",
        campaign.get("offer") or {},
    )
    if unauthorised:
        # The same rule campaign bodies obey. An ad is a bigger audience than a
        # broadcast, so if anything the bar should be higher, not lower.
        return PublishResult(status="draft", detail=unauthorised[0])

    missing = _missing_credentials(connection)
    if missing:
        # Not an error, and deliberately not `failed`. Nothing went wrong: this
        # hotel has not connected an ad account, and `failed` would suggest Meta
        # refused something we sent — which would send somebody looking for a
        # rejection that does not exist.
        logger.info(
            "ad publish unavailable for campaign %s: missing %s",
            campaign.get("id"), ", ".join(missing),
        )
        return PublishResult(
            status="draft",
            detail="Connect a Meta ad account to publish this.",
            missing=missing,
        )

    _spec = build_request(
        campaign=campaign, creative=creative, audience=audience,
        page_id=page_id, whatsapp_number=whatsapp_number,
    )
    # The one line that will post to Meta once `ads_management` exists on a
    # funded ad account. Until then this is unreachable — `_missing_credentials`
    # returns non-empty for every hotel — and it is left as the single seam so
    # that turning this on is a credential change rather than a rewrite.
    raise NotImplementedError(
        "Meta ad creation is not wired up. This line is unreachable while "
        "_missing_credentials() returns anything, which it does for every hotel "
        "today. See the module docstring."
    )
