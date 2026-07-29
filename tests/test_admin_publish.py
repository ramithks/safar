"""
Publishing a partner's listing from the admin dashboard.

`/review` only ever worked on something already queued at 'pending'. A listing
an admin wrote themselves — onboarding a partner who sent their details over
WhatsApp — never passes through that state, so it could be created, edited and
never published.

The point of these tests is that the new verb is not a way around the rules:
the completeness checklist is the same one the partner's own dashboard enforces
(imported, not restated), and the plan gate holds unless it is overridden on
purpose.
"""
import types

import pytest
from fastapi.testclient import TestClient

from app.api.v1.admin import catalog
from app.config import settings
from app.main import app

PASSCODE = "test-passcode"
HEADERS = {"X-Admin-Passcode": PASSCODE, "X-Admin-Actor": "sahil"}

READY = {
    "id": "l-1",
    "kind": "hotel",
    "slug": "konkan-kuteera",
    "title": "Konkan Kuteera Homestay",
    "status": "draft",
    "partner_id": "p-1",
    "photos": ["/image/a", "/image/b", "/image/c"],
    # Comfortably over MIN_DESCRIPTION_CHARS — the point of these tests is the
    # gates that follow, not the copy length.
    "description": (
        "A sea-facing homestay surrounded by greenery, ten minutes from Malpe beach. "
        "Two air-conditioned bedrooms, a full kitchen and private balconies looking "
        "out over the paddy fields, with parking for two cars."
    ),
    "price_from": 2000,
    "city": "Kundapura",
    "map_url": "https://maps.app.goo.gl/x",
    "address": "Anjuna Beach Road, Anjuna, Goa 403509",
    "content": {
        "rooms": [{"name": "Garden room", "price": 2000, "occupancy": 2, "units": 3}],
        "checkin": "2:00 PM",
        "checkout": "11:00 AM",
    },
}

PAID = {
    "id": "p-1",
    "brand_name": "Rashmi's Homestay",
    "stay_plan_status": "active",
    "stay_plan_expires_at": "2099-01-01T00:00:00+00:00",
}
UNPAID = {**PAID, "stay_plan_status": "inactive", "stay_plan_expires_at": None}


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows, updates):
        self._rows, self._updates = rows, updates
        self._filters, self._patch, self._is_update = {}, None, False

    def select(self, *_a, **_kw):
        return self

    def update(self, patch):
        self._patch, self._is_update = patch, True
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def maybe_single(self):
        return self

    def execute(self):
        rows = [r for r in self._rows if all(r.get(c) == v for c, v in self._filters.items())]
        if self._is_update:
            for r in rows:
                r.update(self._patch)
            self._updates.append(self._patch)
            return _Result(rows)
        return _Result(rows[0] if rows else None)


class _Db:
    def __init__(self, listings, partners):
        self.tables = {"partner_listings": listings, "partners": partners}
        self.updates: list[dict] = []

    def table(self, name):
        return _Query(self.tables[name], self.updates)


@pytest.fixture
def bench(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_passcode", PASSCODE)

    db = _Db(listings=[dict(READY)], partners=[dict(PAID)])
    audits: list[dict] = []
    events: list[tuple[str, dict]] = []

    async def fake_record(**kwargs):
        audits.append(kwargs)

    async def fake_emit(name, payload):
        events.append((name, payload))

    monkeypatch.setattr(catalog, "get_db", lambda: db)
    monkeypatch.setattr(catalog.admin_audit, "record", fake_record)
    monkeypatch.setattr(catalog, "emit", fake_emit)

    return types.SimpleNamespace(
        client=TestClient(app), db=db, audits=audits, events=events,
    )


def publish(bench, body=None, listing_id="l-1"):
    return bench.client.post(
        f"/api/v1/admin/listings/{listing_id}/publish",
        json=body if body is not None else {"live": True},
        headers=HEADERS,
    )


class TestGate:
    def test_no_passcode_is_a_404(self, bench):
        res = bench.client.post("/api/v1/admin/listings/l-1/publish", json={"live": True})
        assert res.status_code == 404
        assert not bench.db.updates

    def test_unknown_listing_is_a_404(self, bench):
        assert publish(bench, listing_id="nope").status_code == 404


class TestPublish:
    def test_a_complete_listing_goes_live(self, bench):
        res = publish(bench)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["status"] == "published"
        assert body["public_url"].endswith("/stays/konkan-kuteera")

        written = bench.db.tables["partner_listings"][0]
        assert written["status"] == "published"
        assert written["published_at"] and written["approved_at"]
        # A previous rejection must not linger on something now live.
        assert written["rejection_reason"] is None

    def test_it_is_audited_and_announced(self, bench):
        publish(bench, {"live": True, "reason": "onboarded over WhatsApp"})
        assert bench.audits[0]["action"] == "listing.published"
        assert bench.audits[0]["reason"] == "onboarded over WhatsApp"
        assert bench.audits[0]["actor"] == "sahil"
        # Same event the review queue emits, so partner notifications still fire.
        assert bench.events[0][0] == "listing.approved"

    def test_live_false_takes_it_back_down(self, bench):
        bench.db.tables["partner_listings"][0]["status"] = "published"
        res = publish(bench, {"live": False})
        assert res.json()["status"] == "draft"
        assert bench.db.tables["partner_listings"][0]["status"] == "draft"
        assert bench.audits[0]["action"] == "listing.unpublished"


class TestGatesAreNotBypassed:
    """An admin publishing for a partner clears the partner's own checklist."""

    def test_an_incomplete_listing_is_refused_with_the_checklist(self, bench):
        listing = bench.db.tables["partner_listings"][0]
        listing["photos"] = ["/image/a"]
        listing["map_url"] = None

        body = publish(bench).json()
        assert body["ok"] is False
        assert listing["status"] == "draft", "refused, but wrote anyway"
        fields = {g["field"] for g in body["gaps"]}
        assert {"photos", "map_url"} <= fields

    def test_an_unpaid_partner_is_refused_by_default(self, bench):
        bench.db.tables["partners"][0].update(UNPAID)
        body = publish(bench).json()
        assert body["ok"] is False
        assert body["needs_plan"] is True
        assert bench.db.tables["partner_listings"][0]["status"] == "draft"

    def test_the_plan_gate_can_be_overridden_on_purpose(self, bench):
        """Commercial rule, not a quality one — comping is a real decision."""
        bench.db.tables["partners"][0].update(UNPAID)
        body = publish(bench, {"live": True, "ignore_plan": True}).json()
        assert body["ok"] is True
        assert bench.db.tables["partner_listings"][0]["status"] == "published"
        assert bench.audits[0]["metadata"]["ignore_plan"] is True

    def test_the_completeness_gate_cannot_be_overridden(self, bench):
        """`ignore_plan` waives the fee, never the quality bar."""
        bench.db.tables["partner_listings"][0]["photos"] = []
        body = publish(bench, {"live": True, "ignore_plan": True}).json()
        assert body["ok"] is False
        assert bench.db.tables["partner_listings"][0]["status"] == "draft"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
