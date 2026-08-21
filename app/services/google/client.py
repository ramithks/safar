"""
Google's Business Profile APIs, and only Google's Business Profile APIs.

**There are three of them, at two versions, and that is Google's doing.** It
matters here because the split is invisible from the outside and unforgiving
from the inside:

    accounts        mybusinessaccountmanagement.googleapis.com/v1
    locations       mybusinessbusinessinformation.googleapis.com/v1
    reviews, posts  mybusiness.googleapis.com/v4          ← the legacy API

Reviews and local posts were never migrated off v4. Calling
`mybusinessbusinessinformation…/reviews` — the shape you would guess — is a 404,
and there is no newer endpoint waiting to be found. The v4 host is not a
deprecation we should be moving off; it is where the feature lives.

**v4 also has a gate the v1 APIs do not.** The Google My Business API is only
visible in a Cloud project after Google approves an access request for it.
Locations and accounts work the moment the APIs are enabled; reviews and posts
answer 403 `PERMISSION_DENIED`/`SERVICE_DISABLED` until that approval lands.
`GoogleApiError` keeps that distinguishable — see `not_approved` — because
"Google has not approved us yet" and "this hotel's connection is broken" want
opposite responses from an owner.

Every function takes an already-live access token. Refreshing is
`oauth.access_token`'s job and nothing here duplicates it.
"""
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

ACCOUNTS_HOST = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO_HOST = "https://mybusinessbusinessinformation.googleapis.com/v1"
LEGACY_HOST = "https://mybusiness.googleapis.com/v4"

_TIMEOUT = 25.0

# `readMask` is required on locations.list — Google rejects the call without it
# rather than defaulting. These are the fields the owner is shown plus the two
# (`metadata`, `name`) the rest of the flow addresses records by.
LOCATION_READ_MASK = ",".join(
    (
        "name",
        "title",
        "storefrontAddress",
        "phoneNumbers",
        "websiteUri",
        "categories",
        "metadata",
    )
)


class GoogleApiError(RuntimeError):
    """A failed Google call, with a slug the UI turns into a sentence."""

    def __init__(self, reason: str, detail: str = "", *, status_code: int | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class Account:
    name: str          # "accounts/123456789"
    display_name: str
    type: str


@dataclass(frozen=True)
class Location:
    name: str          # "locations/987654321"
    title: str
    address: str | None
    phone: str | None
    website: str | None
    category: str | None
    is_verified: bool
    maps_uri: str | None
    review_uri: str | None


@dataclass(frozen=True)
class Review:
    name: str          # the full v4 resource name; the reply is addressed to it
    review_id: str
    reviewer_name: str
    reviewer_photo: str | None
    star_rating: int
    comment: str | None
    create_time: str | None
    update_time: str | None
    reply_comment: str | None
    reply_time: str | None


_STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def _classify(status_code: int, payload: dict) -> GoogleApiError:
    error = payload.get("error") or {}
    message = str(error.get("message") or payload)
    status_text = str(error.get("status") or "")

    if status_code == 401:
        return GoogleApiError("reconnect_required", message, status_code=status_code)
    if status_code == 403:
        # Two very different 403s share a code. SERVICE_DISABLED means the API
        # is not enabled or not approved for our Cloud project — our problem,
        # every hotel, and telling the owner to reconnect would be a lie that
        # costs them a trip through Google's consent screen for nothing.
        if "SERVICE_DISABLED" in status_text or "has not been used in project" in message:
            return GoogleApiError("not_approved", message, status_code=status_code)
        return GoogleApiError("no_permission", message, status_code=status_code)
    if status_code == 404:
        return GoogleApiError("not_found", message, status_code=status_code)
    if status_code == 429:
        return GoogleApiError("rate_limited", message, status_code=status_code)
    if status_code >= 500:
        return GoogleApiError("google_unavailable", message, status_code=status_code)
    return GoogleApiError("request_failed", message, status_code=status_code)


async def _call(
    method: str,
    url: str,
    token: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(
                method, url, headers=headers, params=params, json=json_body
            )
    except httpx.HTTPError as exc:
        raise GoogleApiError("google_unavailable", str(exc)) from exc

    if response.status_code == 204 or not response.content:
        return {}

    try:
        payload = response.json()
    except ValueError:
        if response.status_code >= 400:
            raise GoogleApiError(
                "request_failed", response.text[:300], status_code=response.status_code
            )
        return {}

    if response.status_code >= 400:
        raise _classify(response.status_code, payload if isinstance(payload, dict) else {})
    return payload if isinstance(payload, dict) else {}


# ── accounts ────────────────────────────────────────────────────────────────


async def list_accounts(token: str) -> list[Account]:
    """
    Every Business Profile account this person can administer.

    Usually one. An agency managing several hotels has several, which is why the
    owner is asked to choose rather than being given the first.
    """
    out: list[Account] = []
    page_token: str | None = None

    while True:
        params: dict[str, Any] = {"pageSize": 20}
        if page_token:
            params["pageToken"] = page_token
        payload = await _call("GET", f"{ACCOUNTS_HOST}/accounts", token, params=params)

        for row in payload.get("accounts") or []:
            out.append(
                Account(
                    name=str(row.get("name") or ""),
                    display_name=str(row.get("accountName") or row.get("name") or ""),
                    type=str(row.get("type") or ""),
                )
            )

        page_token = payload.get("nextPageToken")
        if not page_token or len(out) >= 100:
            return out


# ── locations ───────────────────────────────────────────────────────────────


def _address_line(storefront: dict | None) -> str | None:
    if not storefront:
        return None
    parts: list[str] = list(storefront.get("addressLines") or [])
    for key in ("locality", "administrativeArea", "postalCode"):
        value = storefront.get(key)
        if value:
            parts.append(str(value))
    return ", ".join(p for p in parts if p) or None


def _location_from(row: dict) -> Location:
    metadata = row.get("metadata") or {}
    categories = row.get("categories") or {}
    primary = categories.get("primaryCategory") or {}
    phones = row.get("phoneNumbers") or {}

    return Location(
        name=str(row.get("name") or ""),
        title=str(row.get("title") or ""),
        address=_address_line(row.get("storefrontAddress")),
        phone=str(phones.get("primaryPhone")) if phones.get("primaryPhone") else None,
        website=str(row.get("websiteUri")) if row.get("websiteUri") else None,
        category=str(primary.get("displayName")) if primary.get("displayName") else None,
        # Google reports this as "can this listing be found on Maps and Search",
        # which is the question an owner is actually asking when they ask
        # whether their listing is verified.
        is_verified=bool(metadata.get("hasVoiceOfMerchant")),
        maps_uri=str(metadata.get("mapsUri")) if metadata.get("mapsUri") else None,
        review_uri=str(metadata.get("newReviewUri")) if metadata.get("newReviewUri") else None,
    )


async def list_locations(token: str, account_name: str) -> list[Location]:
    out: list[Location] = []
    page_token: str | None = None

    while True:
        params: dict[str, Any] = {"readMask": LOCATION_READ_MASK, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        payload = await _call(
            "GET", f"{INFO_HOST}/{account_name}/locations", token, params=params
        )
        out.extend(_location_from(row) for row in payload.get("locations") or [])

        page_token = payload.get("nextPageToken")
        if not page_token or len(out) >= 300:
            return out


async def get_location(token: str, location_name: str) -> Location:
    payload = await _call(
        "GET",
        f"{INFO_HOST}/{location_name}",
        token,
        params={"readMask": LOCATION_READ_MASK},
    )
    return _location_from(payload)


# ── reviews (v4) ────────────────────────────────────────────────────────────


def _review_from(row: dict) -> Review:
    reviewer = row.get("reviewer") or {}
    reply = row.get("reviewReply") or {}
    name = str(row.get("name") or "")

    return Review(
        name=name,
        review_id=str(row.get("reviewId") or name.rsplit("/", 1)[-1]),
        # Google omits the name entirely on anonymous reviews rather than
        # sending a blank, so the fallback is a label and not an empty string —
        # an owner reading "" would think the data was broken.
        reviewer_name=str(reviewer.get("displayName") or "A Google user"),
        reviewer_photo=str(reviewer.get("profilePhotoUrl")) if reviewer.get("profilePhotoUrl") else None,
        star_rating=_STARS.get(str(row.get("starRating") or ""), 0),
        comment=str(row.get("comment")) if row.get("comment") else None,
        create_time=str(row.get("createTime")) if row.get("createTime") else None,
        update_time=str(row.get("updateTime")) if row.get("updateTime") else None,
        reply_comment=str(reply.get("comment")) if reply.get("comment") else None,
        reply_time=str(reply.get("updateTime")) if reply.get("updateTime") else None,
    )


@dataclass(frozen=True)
class ReviewPage:
    reviews: list[Review]
    average_rating: float | None
    total_count: int | None
    next_page_token: str | None


async def list_reviews(
    token: str,
    *,
    account_name: str,
    location_name: str,
    page_size: int = 20,
    page_token: str | None = None,
) -> ReviewPage:
    """
    Reviews for one location, newest first.

    v4 addresses a location as `accounts/{a}/locations/{l}` while the v1 APIs
    return a bare `locations/{l}`. Joining them is this function's job and the
    only place it happens.
    """
    location_id = location_name.rsplit("/", 1)[-1]
    account_id = account_name.rsplit("/", 1)[-1]
    url = f"{LEGACY_HOST}/accounts/{quote(account_id)}/locations/{quote(location_id)}/reviews"

    params: dict[str, Any] = {"pageSize": min(max(page_size, 1), 50), "orderBy": "updateTime desc"}
    if page_token:
        params["pageToken"] = page_token

    payload = await _call("GET", url, token, params=params)
    return ReviewPage(
        reviews=[_review_from(row) for row in payload.get("reviews") or []],
        average_rating=float(payload["averageRating"]) if payload.get("averageRating") else None,
        total_count=int(payload["totalReviewCount"]) if payload.get("totalReviewCount") else None,
        next_page_token=payload.get("nextPageToken"),
    )


async def reply_to_review(token: str, *, review_name: str, comment: str) -> dict:
    """
    Publish (or replace) the hotel's reply to one review.

    PUT, not POST: a location has at most one reply per review and re-replying
    overwrites. The caller has already had this approved by a person — see the
    approval note in `api/v1/google/endpoint.py`.
    """
    return await _call(
        "PUT", f"{LEGACY_HOST}/{review_name}/reply", token, json_body={"comment": comment}
    )


async def delete_review_reply(token: str, *, review_name: str) -> None:
    await _call("DELETE", f"{LEGACY_HOST}/{review_name}/reply", token)


# ── local posts (v4) ────────────────────────────────────────────────────────


async def create_local_post(
    token: str,
    *,
    account_name: str,
    location_name: str,
    summary: str,
    language_code: str = "en",
    call_to_action_type: str | None = None,
    call_to_action_url: str | None = None,
    media_url: str | None = None,
) -> dict:
    """
    Publish a post to the hotel's Google listing.

    `topicType: STANDARD` only. Google also has EVENT and OFFER, and both
    require a schedule Google validates strictly — an offer without a valid
    date range is rejected outright. Those are worth adding behind a proper
    date UI; guessing dates on the owner's behalf is worse than not offering it.
    """
    location_id = location_name.rsplit("/", 1)[-1]
    account_id = account_name.rsplit("/", 1)[-1]
    url = f"{LEGACY_HOST}/accounts/{quote(account_id)}/locations/{quote(location_id)}/localPosts"

    body: dict[str, Any] = {
        "languageCode": language_code,
        "summary": summary,
        "topicType": "STANDARD",
    }
    if call_to_action_type and call_to_action_url:
        body["callToAction"] = {
            "actionType": call_to_action_type,
            "url": call_to_action_url,
        }
    if media_url:
        body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": media_url}]

    return await _call("POST", url, token, json_body=body)


async def list_local_posts(
    token: str, *, account_name: str, location_name: str, page_size: int = 10
) -> list[dict]:
    location_id = location_name.rsplit("/", 1)[-1]
    account_id = account_name.rsplit("/", 1)[-1]
    url = f"{LEGACY_HOST}/accounts/{quote(account_id)}/locations/{quote(location_id)}/localPosts"
    payload = await _call("GET", url, token, params={"pageSize": min(max(page_size, 1), 100)})
    return list(payload.get("localPosts") or [])
