"""
Instagram Business Login: the state token, the signed_request, and the pieces
of the flow that can be wrong without anything failing loudly.

The exchange itself is not mocked here — `scripts/instagram_e2e.py` covers the
storage path against the real database. What is pinned here is everything that
would let somebody attach *their* Instagram account to *someone else's* hotel,
because that is the failure this flow makes possible and no other test would
catch it.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest

import app.services.messaging.instagram.oauth as ig_oauth
from app.config import settings


APP_SECRET = "test-instagram-app-secret"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "instagram_app_id", "1234567890", raising=False)
    monkeypatch.setattr(settings, "instagram_oauth_client_secret", APP_SECRET, raising=False)
    monkeypatch.setattr(
        settings,
        "instagram_redirect_uri",
        "https://stayos-backend.travbuddy.in/api/v1/messaging/instagram/oauth/callback",
        raising=False,
    )
    monkeypatch.setattr(settings, "oauth_state_ttl_seconds", 600, raising=False)


# ── state ───────────────────────────────────────────────────────────────────


def test_state_round_trips_the_property():
    state = ig_oauth.issue_state(property_id="prop-1", partner_id="pa-1", profile_id="u-1")
    claims = ig_oauth.verify_state(state)
    assert claims["p"] == "prop-1"
    assert claims["pa"] == "pa-1"
    assert claims["u"] == "u-1"


def test_state_with_an_edited_property_is_refused():
    """The whole flow rests on this: editing the state must not re-target the hotel."""
    state = ig_oauth.issue_state(property_id="mine", partner_id="pa-1", profile_id="u-1")
    body, _, sig = state.partition(".")

    forged_payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged_payload["p"] = "somebody-elses-hotel"
    forged_body = (
        base64.urlsafe_b64encode(
            json.dumps(forged_payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .decode()
        .rstrip("=")
    )

    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        ig_oauth.verify_state(f"{forged_body}.{sig}")
    assert exc.value.reason == "bad_state"


def test_state_signed_with_another_secret_is_refused(monkeypatch):
    state = ig_oauth.issue_state(property_id="prop-1", partner_id="pa-1", profile_id="u-1")
    monkeypatch.setattr(settings, "instagram_oauth_client_secret", "a-different-secret",
                        raising=False)
    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        ig_oauth.verify_state(state)
    assert exc.value.reason == "bad_state"


def test_expired_state_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "oauth_state_ttl_seconds", 1, raising=False)
    old = int(time.time()) - 30
    payload = json.dumps({"p": "prop-1", "pa": "pa-1", "u": "u-1", "t": old},
                         separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = (
        base64.urlsafe_b64encode(
            hmac.new(APP_SECRET.encode(), body.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        ig_oauth.verify_state(f"{body}.{sig}")
    assert exc.value.reason == "state_expired"


@pytest.mark.parametrize("bad", ["", None, "nodot", "a.b.c.d"])
def test_malformed_state_is_refused(bad):
    with pytest.raises(ig_oauth.InstagramOAuthError):
        ig_oauth.verify_state(bad)


# ── the authorize url ───────────────────────────────────────────────────────


def test_authorize_url_uses_the_instagram_host_not_facebook():
    """Instagram Login, not Facebook Login. The wrong host fails with nothing useful."""
    url = ig_oauth.authorize_url("STATE")
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert "facebook.com" not in url


def test_authorize_url_carries_the_exact_configured_redirect_uri():
    """Meta compares this string byte for byte against its own config."""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(ig_oauth.authorize_url("STATE")).query)
    assert query["redirect_uri"] == [settings.instagram_redirect_uri]
    assert query["client_id"] == ["1234567890"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["STATE"]


def test_authorize_url_asks_for_messaging_and_nothing_more():
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(ig_oauth.authorize_url("S")).query)
    scopes = set(query["scope"][0].split(","))
    assert scopes == {"instagram_business_basic", "instagram_business_manage_messages"}


def test_authorize_url_refuses_when_the_app_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "instagram_app_id", None, raising=False)
    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        ig_oauth.authorize_url("S")
    assert exc.value.reason == "not_configured"


# ── signed_request ──────────────────────────────────────────────────────────


def _signed_request(payload: dict, secret: str = APP_SECRET) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    sig = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    return f"{sig}.{encoded}"


def test_signed_request_parses_when_correctly_signed():
    raw = _signed_request({"user_id": "17841400000000000", "algorithm": "HMAC-SHA256"})
    claims = ig_oauth.parse_signed_request(raw, APP_SECRET)
    assert claims["user_id"] == "17841400000000000"


def test_signed_request_with_a_wrong_signature_is_refused():
    """These endpoints delete things and are public. An unverified body is a delete-anyone button."""
    raw = _signed_request({"user_id": "17841400000000000"}, secret="not-our-secret")
    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        ig_oauth.parse_signed_request(raw, APP_SECRET)
    assert exc.value.reason == "bad_signature"


def test_signed_request_with_a_swapped_payload_is_refused():
    good = _signed_request({"user_id": "ours"})
    sig, _, _payload = good.partition(".")
    other = (
        base64.urlsafe_b64encode(json.dumps({"user_id": "theirs"}).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ig_oauth.InstagramOAuthError):
        ig_oauth.parse_signed_request(f"{sig}.{other}", APP_SECRET)


def test_signed_request_without_a_secret_is_refused():
    raw = _signed_request({"user_id": "x"})
    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        ig_oauth.parse_signed_request(raw, None)
    assert exc.value.reason == "not_configured"


@pytest.mark.parametrize("bad", ["", "nodot", None])
def test_malformed_signed_request_is_refused(bad):
    with pytest.raises(ig_oauth.InstagramOAuthError):
        ig_oauth.parse_signed_request(bad or "", APP_SECRET)


# ── credential storage ──────────────────────────────────────────────────────


def test_a_property_ref_is_recognised_and_a_plain_one_is_not():
    from app.services.messaging import store

    assert store.is_db_ref(store.ref_for_property("abc")) is True
    assert store.is_db_ref("default") is False
    assert store.is_db_ref("sandbox") is False
    assert store.is_db_ref(None) is False


def test_stored_tokens_are_encrypted_not_encoded(monkeypatch):
    from cryptography.fernet import Fernet

    from app.services.messaging import crypto

    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "messaging_credentials_key", Fernet.generate_key().decode(),
                        raising=False)
    try:
        ciphertext = crypto.encrypt("IGQVJsecret-token")
        assert "IGQVJsecret-token" not in ciphertext
        assert base64.urlsafe_b64decode(ciphertext + "===")  # it is a real Fernet token
        assert crypto.decrypt(ciphertext) == "IGQVJsecret-token"
    finally:
        crypto._fernet.cache_clear()


def test_a_missing_key_raises_rather_than_storing_plaintext(monkeypatch):
    from app.services.messaging import crypto

    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "messaging_credentials_key", None, raising=False)
    try:
        with pytest.raises(crypto.CredentialCryptoError):
            crypto.encrypt("token")
    finally:
        crypto._fernet.cache_clear()


def test_resolve_prefers_the_per_hotel_token_over_the_environment(monkeypatch):
    from app.services.messaging import credentials

    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "our-house-token")
    creds = credentials.resolve("prop:abc", "instagram", db_token="the-hotels-own-token")
    assert creds.access_token == "the-hotels-own-token"


def test_resolve_still_falls_back_to_the_environment(monkeypatch):
    from app.services.messaging import credentials

    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "our-house-token")
    creds = credentials.resolve("default", "instagram")
    assert creds.access_token == "our-house-token"


def test_sandbox_still_needs_no_secret_at_all():
    from app.services.messaging import credentials

    creds = credentials.resolve("sandbox", "instagram", db_token="ignored")
    assert creds.is_sandbox is True
    assert creds.access_token is None


# ── telling our misconfiguration apart from their failed attempt ────────────


@pytest.mark.parametrize(
    "meta_message,expected_reason",
    [
        ("Invalid platform app", "app_misconfigured"),
        ("Invalid authorization code", "code_exchange_failed"),
        ("This authorization code has been used", "code_exchange_failed"),
    ],
)
def test_a_wrong_app_id_is_reported_as_ours_not_theirs(
    monkeypatch, meta_message, expected_reason
):
    """
    "Invalid platform app" means the app id is not an Instagram Login app —
    usually the Facebook one, which looks near-identical. An owner can retry
    that forever and it will never work, so it must not share a slug with a
    stale code, which retrying does fix.
    """
    import asyncio

    class _Response:
        status_code = 400
        text = meta_message

        def json(self):
            return {"error_message": meta_message, "error_type": "OAuthException"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_a, **_kw):
            return _Response()

    monkeypatch.setattr(ig_oauth.httpx, "AsyncClient", lambda **_kw: _Client())

    with pytest.raises(ig_oauth.InstagramOAuthError) as exc:
        asyncio.run(ig_oauth.exchange_code("some-code"))
    assert exc.value.reason == expected_reason
