"""
Google Business Profile: the OAuth state, the API surface, and the words.

Three things are worth holding still here.

**The state is a CSRF token on an unauthenticated callback.** Same shape and
same attacks as `test_instagram_oauth.py`, because it is the same construction —
a second OAuth implementation that behaved differently would be a second set of
mistakes.

**Drafting and publishing are separate routes.** The property the product
depends on is that no code path generates review text *and* sends it to Google.
Asserted structurally, on the routes themselves, so a future refactor that
"helpfully" merges them fails here rather than in a hotel's public reviews.

**No slug reaches a hotel owner.** `messages.humanise` is the only translation
layer, and the test walks every entry in it looking for API vocabulary.
"""
import time

import pytest

from app.api.v1.google import endpoint as google_endpoint
from app.api.v1.google.messages import _MESSAGES, humanise
from app.config import settings
from app.services.google import client as gclient
from app.services.google import oauth


HOTEL = "47f4cbf8-7cad-4369-a4c9-a4a70a0c29c0"
PARTNER = "ed8b3e94-6f79-42cf-be6d-c46eaf3652ac"
PROFILE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def client_credentials(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "test-client-secret")


# ── the state ───────────────────────────────────────────────────────────────


def test_a_state_round_trips():
    state = oauth.issue_state(property_id=HOTEL, partner_id=PARTNER, profile_id=PROFILE)
    claims = oauth.verify_state(state)
    assert claims["p"] == HOTEL
    assert claims["pa"] == PARTNER


def test_a_forged_signature_is_refused():
    state = oauth.issue_state(property_id=HOTEL, partner_id=PARTNER, profile_id=PROFILE)
    body, _, _ = state.partition(".")
    with pytest.raises(oauth.GoogleOAuthError) as exc:
        oauth.verify_state(f"{body}.deadbeef")
    assert exc.value.reason == "bad_state"


def test_a_state_signed_with_another_secret_is_refused(monkeypatch):
    state = oauth.issue_state(property_id=HOTEL, partner_id=PARTNER, profile_id=PROFILE)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "somebody-elses-secret")
    with pytest.raises(oauth.GoogleOAuthError):
        oauth.verify_state(state)


def test_an_old_state_expires(monkeypatch):
    real = time.time
    monkeypatch.setattr(oauth.time, "time", lambda: real() - settings.oauth_state_ttl_seconds - 60)
    state = oauth.issue_state(property_id=HOTEL, partner_id=PARTNER, profile_id=PROFILE)
    monkeypatch.setattr(oauth.time, "time", real)

    with pytest.raises(oauth.GoogleOAuthError) as exc:
        oauth.verify_state(state)
    assert exc.value.reason == "state_expired"


@pytest.mark.parametrize("garbage", [None, "", "nodot", "a.b.c.d"])
def test_malformed_states_are_refused_not_crashed(garbage):
    with pytest.raises(oauth.GoogleOAuthError):
        oauth.verify_state(garbage)


# ── the consent screen ──────────────────────────────────────────────────────


def test_the_authorize_url_asks_for_offline_access():
    """
    Without both of these Google returns no refresh token, and the connection
    dies silently an hour later with no way back except a reconnect nobody
    knows to do.
    """
    url = oauth.authorize_url("some-state")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert url.startswith(oauth.AUTHORIZE_URL)


def test_the_scope_is_the_one_business_profile_publishes():
    assert settings.google_oauth_scopes == "https://www.googleapis.com/auth/business.manage"


def test_connecting_without_credentials_is_refused_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", None)
    assert not oauth.is_configured()
    with pytest.raises(oauth.GoogleOAuthError) as exc:
        oauth.authorize_url("s")
    assert exc.value.reason == "not_configured"


# ── which API answers what ──────────────────────────────────────────────────


def test_reviews_and_posts_address_the_legacy_host():
    """
    Reviews and local posts were never migrated off the v4 API. Pointing them at
    the Business Information host is a 404, and there is no newer endpoint to
    find — so the host split is a fact to hold still, not a deprecation to fix.
    """
    assert gclient.LEGACY_HOST == "https://mybusiness.googleapis.com/v4"
    assert gclient.INFO_HOST == "https://mybusinessbusinessinformation.googleapis.com/v1"
    assert gclient.ACCOUNTS_HOST == "https://mybusinessaccountmanagement.googleapis.com/v1"


def test_locations_are_read_with_a_field_mask():
    # Google rejects locations.list without one rather than defaulting.
    assert "title" in gclient.LOCATION_READ_MASK
    assert "metadata" in gclient.LOCATION_READ_MASK


def test_a_disabled_api_is_ours_to_fix_not_the_hotels():
    """
    Two very different failures share HTTP 403. Telling an owner to reconnect
    because *our* Cloud project is not approved costs them a trip through
    Google's consent screen and fixes nothing.
    """
    ours = gclient._classify(403, {"error": {"status": "SERVICE_DISABLED", "message": "not enabled"}})
    assert ours.reason == "not_approved"
    assert not humanise(ours.reason).actionable

    theirs = gclient._classify(403, {"error": {"status": "PERMISSION_DENIED", "message": "no access"}})
    assert theirs.reason == "no_permission"
    assert humanise(theirs.reason).actionable


def test_an_expired_credential_asks_for_a_reconnect():
    assert gclient._classify(401, {}).reason == "reconnect_required"
    assert oauth._token_error({"error": "invalid_grant"}, 400).reason == "reconnect_required"


def test_a_star_rating_becomes_a_number():
    review = gclient._review_from({"name": "accounts/1/locations/2/reviews/3", "starRating": "FIVE"})
    assert review.star_rating == 5
    # Google omits the reviewer entirely on an anonymous review rather than
    # sending a blank, and "" on screen reads as broken data.
    assert review.reviewer_name == "A Google user"


# ── the promise: the AI drafts, the owner publishes ─────────────────────────


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in google_endpoint.router.routes}


def test_drafting_and_publishing_are_different_routes():
    paths = _paths()
    assert "/google/properties/{property_id}/reviews/draft" in paths
    assert "/google/properties/{property_id}/reviews/reply" in paths
    assert "/google/properties/{property_id}/posts/draft" in paths


def test_the_publish_routes_take_the_text_from_the_request():
    """
    The structural half of "an owner approved this".

    `PublishReplyIn` carries the comment, so the route cannot invent one; if it
    took only a review name it would have to call the model, and "approved"
    would mean "somebody pressed a button next to a draft that was then
    regenerated".
    """
    assert "comment" in google_endpoint.PublishReplyIn.model_fields
    assert "summary" in google_endpoint.PublishPostIn.model_fields
    # And the draft routes carry no text to publish.
    assert set(google_endpoint.DraftReplyIn.model_fields) == {"reviewName"}


def test_publishing_needs_a_stronger_permission_than_reading():
    # Reading a review is part of answering guests ('messaging'); writing under
    # the hotel's name on a public page is an owner's act ('agents').
    import inspect

    reply = inspect.getsource(google_endpoint.publish_reply)
    listing = inspect.getsource(google_endpoint.list_reviews)
    assert 'require_member("agents")' in reply
    assert 'require_member("messaging")' in listing


# ── the words ───────────────────────────────────────────────────────────────

BANNED = (
    "oauth", "token", "api", "endpoint", "scope", "webhook", "credential",
    "http", "json", "null", "invalid_grant", "permission_denied", "client id",
)


@pytest.mark.parametrize("reason", sorted(_MESSAGES))
def test_no_message_contains_developer_vocabulary(reason):
    """
    A hotel owner runs a nine-room haveli. Every sentence they can be shown has
    to be about their business, not about ours.
    """
    text = humanise(reason).message.lower()
    for word in BANNED:
        assert word not in text, f"{reason!r} says {word!r}: {text}"


def test_an_unknown_reason_still_gets_a_sentence():
    fallback = humanise("something_nobody_mapped")
    assert fallback.message
    assert not fallback.actionable
    # And never the slug itself.
    assert "something_nobody_mapped" not in fallback.message


def test_a_problem_the_owner_cannot_fix_is_not_offered_as_one():
    for reason in ("not_approved", "not_configured", "google_unavailable", "rate_limited"):
        assert not humanise(reason).actionable, reason
    for reason in ("not_connected", "reconnect_required", "no_permission"):
        assert humanise(reason).actionable, reason
