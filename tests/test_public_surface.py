"""
Regression tests for the 2026-07-28 public-surface audit.

What prompted it: a friend of Sahil's in security pointed out that
/api/v1/metrics/dashboard, /openapi.json and /api/v1/marketplace/bookings/{ref}
all answered a stranger. Reviewing around those turned up two more — an
unauthenticated POST that resets the metrics counters, and a rate limiter that
bucketed on an unverified JWT `sub`, so the limit protecting the booking lookup
could be sidestepped by minting a token per request.

Each test below fails against the code as it was.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app.api.v1.marketplace.payments import _mask_name


# ── the booking ref leaks less than it did ───────────────────────────────────


class TestMaskName:
    def test_surname_is_reduced_to_an_initial(self):
        assert _mask_name("Rishi Arora") == "Rishi A."

    def test_middle_names_do_not_survive(self):
        assert _mask_name("Rashmi Pradeep Shetty") == "Rashmi S."

    def test_single_word_names_are_left_whole(self):
        # Nothing to protect by truncating a lone first name.
        assert _mask_name("Sahil") == "Sahil"

    def test_extra_whitespace_does_not_produce_an_empty_initial(self):
        assert _mask_name("  Rishi   Arora  ") == "Rishi A."

    def test_missing_and_blank_names_stay_none(self):
        assert _mask_name(None) is None
        assert _mask_name("   ") is None

    def test_public_model_carries_no_contact_details_or_internal_ids(self):
        from app.api.v1.marketplace.payment_schemas import PublicBooking

        fields = set(PublicBooking.model_fields)
        # A guessed ref must not hand over a way to contact the payer, nor an
        # internal id to correlate on.
        for leaked in ("lead_phone", "lead_email", "partner_id", "user_id",
                       "razorpay_order_id", "razorpay_payment_id",
                       "commission_paise", "payout_paise", "notes"):
            assert leaked not in fields, f"{leaked} is exposed on a ref lookup"


# ── the rate limiter can no longer be stepped around ─────────────────────────


class TestLimiterKeying:
    """
    The limit is the only thing making ref enumeration impractical, so the key
    must not be derived from something the caller can forge.
    """

    @staticmethod
    def _request(headers: dict, ip: str = "203.0.113.7"):
        class _Client:
            host = ip

        class _Request:
            def __init__(self):
                self.headers = headers
                self.client = _Client()

        return _Request()

    def test_a_forged_token_falls_back_to_the_ip_bucket(self):
        import jwt

        from app.core.limiter import _user_or_ip

        # Signed with a key that is not ours — never verified, so never trusted.
        forged = jwt.encode({"sub": "whoever-i-like"}, "not-the-real-secret", algorithm="HS256")
        key = _user_or_ip(self._request({"authorization": f"Bearer {forged}"}))
        assert key == "203.0.113.7"

    def test_rotating_the_sub_does_not_buy_a_fresh_bucket(self):
        import jwt

        from app.core.limiter import _user_or_ip

        keys = {
            _user_or_ip(
                self._request(
                    {"authorization": f"Bearer {jwt.encode({'sub': f'user-{i}'}, 'nope', algorithm='HS256')}"}
                )
            )
            for i in range(25)
        }
        # This was 25 distinct buckets before the fix.
        assert keys == {"203.0.113.7"}

    def test_a_verified_token_still_gets_its_own_bucket(self, monkeypatch):
        from app.core import limiter as limiter_mod

        # Stand in for "this process already checked the signature".
        monkeypatch.setattr(
            limiter_mod, "verified_identity", lambda token: ("real-user-id", False)
        )
        key = limiter_mod._user_or_ip(self._request({"authorization": "Bearer whatever"}))
        assert key == "user:real-user-id"

    def test_anonymous_users_share_the_ip_bucket(self, monkeypatch):
        """
        Guests are free to mint, so a per-user bucket would be no bucket at all:
        one POST /auth/v1/signup buys a fresh `sub` and a fresh allowance. Every
        guest on an IP must land in that IP's bucket, however many they create.
        """
        from app.core import limiter as limiter_mod

        counter = iter(range(100))
        monkeypatch.setattr(
            limiter_mod,
            "verified_identity",
            lambda token: (f"guest-{next(counter)}", True),
        )

        keys = {
            limiter_mod._user_or_ip(self._request({"authorization": f"Bearer tok{i}"}))
            for i in range(25)
        }
        assert keys == {"203.0.113.7"}

    def test_verified_identity_answers_none_for_an_unverified_token(self):
        from app.deps.auth import verified_identity

        assert verified_identity("some.unverified.token") is None

    def test_garbage_in_the_header_does_not_raise(self):
        from app.core.limiter import _user_or_ip

        for header in ("Bearer", "Bearer ", "Bearer not.a.jwt", "Basic abc", ""):
            assert _user_or_ip(self._request({"authorization": header})) == "203.0.113.7"

    def test_verified_sub_answers_none_for_an_unverified_token(self):
        from app.deps.auth import verified_sub

        # Nothing has verified this, so it is not in the cache.
        assert verified_sub("some.unverified.token") is None


# ── the metrics surface is admin-only ────────────────────────────────────────


class TestMetricsAreGated:
    def test_every_metrics_route_requires_admin(self):
        from app.api.v1 import metrics

        routes = [r for r in metrics.router.routes if getattr(r, "path", "").startswith("/metrics")]
        assert routes, "metrics router exposes no routes — test is looking in the wrong place"
        for route in routes:
            names = [d.call.__name__ for d in route.dependant.dependencies if d.call]
            assert "require_admin" in names, f"{route.path} is not behind require_admin"

    def test_the_counter_reset_is_not_a_public_post(self):
        from app.api.v1 import metrics

        reset = next(
            r for r in metrics.router.routes if getattr(r, "path", "") == "/metrics/scrapers/reset"
        )
        assert "POST" in reset.methods
        assert "require_admin" in [d.call.__name__ for d in reset.dependant.dependencies if d.call]


# ── the schema is not served to the public internet ──────────────────────────


class TestApiDocsAreOff:
    @staticmethod
    def _app(monkeypatch, expose: bool):
        from app.config import settings

        monkeypatch.setattr(settings, "expose_api_docs", expose)
        import app.main

        return importlib.reload(app.main).app

    def test_docs_schema_and_redoc_are_all_absent_by_default(self, monkeypatch):
        app = self._app(monkeypatch, expose=False)
        client = TestClient(app)
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert client.get(path).status_code == 404, f"{path} is still public"

    def test_they_come_back_when_explicitly_switched_on(self, monkeypatch):
        app = self._app(monkeypatch, expose=True)
        client = TestClient(app)
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
