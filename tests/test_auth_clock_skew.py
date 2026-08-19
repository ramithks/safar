"""
A token issued seconds ago must not be refused for being from the future.

Found while testing onboarding. GoTrue stamps `iat` from its own clock, the PWA
sends the token it was just handed, and PyJWT refuses anything whose `iat` is
ahead of *this* machine's clock by even a moment:

    ImmatureSignatureError: The token is not yet valid (iat)

The result is a 401 on the first authenticated call after signup — the hotel
owner creates their account and is told the session is invalid. It clears on a
refresh, so it presents as random rather than as a clock problem, and it is
worst exactly where it hurts most: a token used milliseconds after issuance.

These tests pin the tolerance from both ends. Removing the leeway breaks the
first two; widening it until a forged far-future token is accepted breaks the
last one.
"""
import time

import jwt
import pytest

from app.deps import auth


SECRET = "test-jwt-secret-for-clock-skew"
SUB = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _hs256_secret(monkeypatch):
    """Verify locally with HS256, so nothing here touches the network."""
    monkeypatch.setattr(auth.settings, "supabase_jwt_secret", SECRET, raising=False)


def _token(iat_offset: int = 0, exp_offset: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": SUB,
            "aud": "authenticated",
            "email": "owner@example.test",
            "iat": now + iat_offset,
            "exp": now + exp_offset,
        },
        SECRET,
        algorithm="HS256",
    )


def test_token_issued_now_is_accepted():
    user = auth._verify_local(_token())
    assert user is not None
    assert user.id == SUB


@pytest.mark.parametrize("skew", [1, 5, 30, 59])
def test_token_from_a_slightly_fast_auth_server_is_accepted(skew):
    """The real case: Supabase's clock is a few seconds ahead of ours."""
    user = auth._verify_local(_token(iat_offset=skew))
    assert user is not None, f"a token issued {skew}s in the future was refused"


def test_a_token_from_the_far_future_is_still_refused():
    """The leeway is for clock drift, not for accepting anything."""
    with pytest.raises(jwt.InvalidTokenError):
        auth._verify_local(_token(iat_offset=600))


def test_an_expired_token_is_still_refused():
    """Leeway applies to `exp` too, so check it did not become unlimited."""
    with pytest.raises(jwt.ExpiredSignatureError):
        auth._verify_local(_token(iat_offset=-7200, exp_offset=-3600))


def test_a_token_signed_with_the_wrong_key_is_refused():
    """Skew tolerance must not have loosened signature verification."""
    now = int(time.time())
    forged = jwt.encode(
        {"sub": SUB, "aud": "authenticated", "iat": now, "exp": now + 3600},
        "not-the-secret",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        auth._verify_local(forged)
