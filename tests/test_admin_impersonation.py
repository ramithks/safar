"""
Signing in as a partner.

Onboarding a business means writing their listings the way they would — with
the AI importer, the photo uploader and the plan rules that only exist in the
partner dashboard. Rather than clone that behind the admin passcode, the admin
API mints a one-time magic-link token for their account.

That is an account-takeover primitive, so the tests below are about the safety
rails rather than the happy path: the passcode gate, the mandatory reason, the
audit row, and the fact that the token never lands in that audit row.
"""
import types

import pytest
from fastapi.testclient import TestClient

from app.api.v1.admin import endpoint as admin_endpoint
from app.config import settings
from app.main import app

PASSCODE = "test-passcode"
HEADERS = {"X-Admin-Passcode": PASSCODE, "X-Admin-Actor": "sahil"}

PARTNER = {
    "id": "p-1",
    "brand_name": "Zostel Manali",
    "owner_id": "u-1",
    "status": "active",
}


# ── fakes ───────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Enough of the postgrest builder to answer the two lookups we make."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._filters: dict[str, str] = {}

    def select(self, *_a, **_kw):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def maybe_single(self):
        return self

    def execute(self):
        rows = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._filters.items())
        ]
        return _Result(rows[0] if rows else None)


class _Admin:
    def __init__(self, accounts: dict[str, dict], existing_emails: set[str]):
        self.accounts = accounts
        self.existing_emails = existing_emails
        self.generated: list[dict] = []

    def get_user_by_id(self, uid):
        account = self.accounts.get(uid)
        if account is None:
            raise RuntimeError("no such user")
        return types.SimpleNamespace(user=types.SimpleNamespace(**account))

    def generate_link(self, params):
        kind, email = params["type"], params["email"]
        if kind == "invite" and email in self.existing_emails:
            raise RuntimeError("A user with this email address has already been registered")
        self.generated.append(params)
        self.existing_emails.add(email)
        return types.SimpleNamespace(
            properties=types.SimpleNamespace(
                hashed_token=f"tok-{kind}-secret", verification_type=kind,
            ),
            user=types.SimpleNamespace(id=self.accounts_id_for(email)),
        )

    def accounts_id_for(self, email):
        for uid, account in self.accounts.items():
            if account.get("email") == email:
                return uid
        return "u-new"


class _Db:
    def __init__(self, partners, accounts, existing_emails):
        self._partners = partners
        self.auth = types.SimpleNamespace(admin=_Admin(accounts, existing_emails))

    def table(self, name):
        assert name == "partners", f"unexpected table {name}"
        return _Query(self._partners)


@pytest.fixture
def bench(monkeypatch):
    """Admin API wired to fakes, with the audit rows it wrote kept for asserts."""
    monkeypatch.setattr(settings, "admin_api_passcode", PASSCODE)

    db = _Db(
        partners=[PARTNER],
        accounts={"u-1": {"id": "u-1", "email": "owner@zostel.example"}},
        existing_emails={"owner@zostel.example"},
    )
    audits: list[dict] = []
    events: list[tuple[str, dict]] = []

    async def fake_record(**kwargs):
        audits.append(kwargs)

    async def fake_emit(name, payload):
        events.append((name, payload))

    monkeypatch.setattr(admin_endpoint, "get_db", lambda: db)
    monkeypatch.setattr(admin_endpoint.admin_audit, "record", fake_record)
    monkeypatch.setattr(admin_endpoint, "emit", fake_emit)

    return types.SimpleNamespace(
        client=TestClient(app), db=db, audits=audits, events=events,
    )


# ── the gate ────────────────────────────────────────────────────────────────


class TestGate:
    def test_no_passcode_is_a_404(self, bench):
        res = bench.client.post("/api/v1/admin/partners/p-1/login-link", json={"reason": "x"})
        assert res.status_code == 404
        assert not bench.db.auth.admin.generated

    def test_a_reason_is_mandatory(self, bench):
        """The audit row is the only thing separating help from a takeover."""
        for body in ({}, {"reason": ""}, {"reason": "ok"}):
            res = bench.client.post(
                "/api/v1/admin/partners/p-1/login-link", json=body, headers=HEADERS,
            )
            assert res.status_code == 422, body
        assert not bench.db.auth.admin.generated

    def test_unknown_partner_is_a_404(self, bench):
        res = bench.client.post(
            "/api/v1/admin/partners/nope/login-link",
            json={"reason": "onboarding"}, headers=HEADERS,
        )
        assert res.status_code == 404

    def test_a_partner_with_no_account_cannot_be_impersonated(self, bench):
        bench.db._partners.append({"id": "p-2", "brand_name": "No Account", "owner_id": None})
        res = bench.client.post(
            "/api/v1/admin/partners/p-2/login-link",
            json={"reason": "onboarding"}, headers=HEADERS,
        )
        assert res.status_code == 409
        assert not bench.db.auth.admin.generated


# ── signing in as an existing partner ───────────────────────────────────────


class TestLoginLink:
    def test_returns_a_link_into_their_dashboard(self, bench):
        res = bench.client.post(
            "/api/v1/admin/partners/p-1/login-link",
            json={"reason": "adding their trips for them"}, headers=HEADERS,
        )
        assert res.status_code == 200
        body = res.json()

        assert body["email"] == "owner@zostel.example"
        assert body["type"] == "magiclink"
        assert body["next"] == "/partner/dashboard"
        assert body["created_account"] is False
        assert "/auth/confirm?token_hash=" in body["url"]
        assert "next=%2Fpartner%2Fdashboard" in body["url"]

    def test_the_issue_is_audited_but_the_token_is_not(self, bench):
        res = bench.client.post(
            "/api/v1/admin/partners/p-1/login-link",
            json={"reason": "adding their trips for them"}, headers=HEADERS,
        )
        token = res.json()["token_hash"]

        assert len(bench.audits) == 1
        row = bench.audits[0]
        assert row["action"] == "partner.login_link_issued"
        assert row["actor"] == "sahil"
        assert row["entity_id"] == "p-1"
        assert row["reason"] == "adding their trips for them"
        # Anything done after redemption is recorded as the partner, so this row
        # is the only bridge back to a person — it must not double as a way in.
        assert token not in str(row)
        assert bench.events[0][0] == "partner.login_link_issued"


# ── onboarding a business that has never signed up ──────────────────────────


class TestInvite:
    def test_a_new_email_gets_an_account_and_lands_on_partner_start(self, bench):
        res = bench.client.post(
            "/api/v1/admin/partners/invite",
            json={"email": "new@hostel.example", "reason": "onboarding call"},
            headers=HEADERS,
        )
        assert res.status_code == 200
        body = res.json()

        assert body["created_account"] is True
        assert body["type"] == "invite"
        # No partner row yet — the brand details go in at /partner/start.
        assert body["partner_id"] is None
        assert body["next"] == "/partner/start"
        assert bench.db.auth.admin.generated[0]["type"] == "invite"

    def test_an_existing_account_falls_back_to_a_plain_sign_in_link(self, bench):
        res = bench.client.post(
            "/api/v1/admin/partners/invite",
            json={"email": "owner@zostel.example", "reason": "onboarding call"},
            headers=HEADERS,
        )
        assert res.status_code == 200
        body = res.json()

        assert body["created_account"] is False
        assert body["type"] == "magiclink"
        # They already have a partner profile, so skip the start form.
        assert body["partner_id"] == "p-1"
        assert body["next"] == "/partner/dashboard"

    def test_a_malformed_email_is_refused_before_gotrue_sees_it(self, bench):
        res = bench.client.post(
            "/api/v1/admin/partners/invite",
            json={"email": "not-an-email", "reason": "onboarding call"},
            headers=HEADERS,
        )
        assert res.status_code == 422
        assert not bench.db.auth.admin.generated


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
