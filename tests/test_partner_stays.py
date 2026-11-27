"""
Partner stays in hotel search.

The DB and the availability service are stubbed, so these pin down the rules the
placement depends on — no Supabase, no network:

  1. A partner stay that cannot take the booking is never shown.
  2. Ratings are only ever real ones.
  3. Partner rows stay on top through every re-sort, and survive the caps.
  4. Nothing here can break an ordinary hotel search.
"""
import asyncio

import pytest

from app.services import partner_stays as ps
from app.services.hotels_filters import sort_hotels


# ── fixtures ─────────────────────────────────────────────────────────────────

def listing(**over):
    row = {
        "id": "l1",
        "slug": "riverside-homestay",
        "title": "Riverside Homestay",
        "summary": "A quiet riverside stay ten minutes from Mall Road.",
        "description": "x" * 200,
        "destination": "Manali",
        "city": "Manali",
        "address": "Old Manali",
        "lat": 32.26,
        "lon": 77.18,
        # How partner uploads are really stored: the website's own relative
        # /image/<base64url id> proxy path (lib/marketplace/media.ts).
        "photos": ["/image/aaa111", "/image/bbb222"],
        "price_from": 2400,
        "original_price": 3000,
        "price_unit": "per_night",
        "amenities": ["WiFi", "Parking"],
        "tags": ["homestay"],
        "category": "homestay",
        "content": {"checkin": "12:00", "policies": {"cancellation": "Free cancellation up to 48h"}},
        "booking_mode": "instant",
        "cancellation_policy": None,
        "updated_at": "2026-07-20T00:00:00+00:00",
        "partner": {"brand_name": "Nomad Trails", "slug": "nomad-trails",
                    "verified": True, "response_minutes": 30},
    }
    row.update(over)
    return row


def stub(monkeypatch, rows=(), reviews=None, available=True, room_types=(), units_left=None):
    monkeypatch.setattr(ps, "_fetch_candidates", lambda dest: [dict(r) for r in rows])
    monkeypatch.setattr(ps, "_fetch_review_stats", lambda ids: dict(reviews or {}))

    async def _room_types(listing_id):
        return [dict(r) for r in room_types]

    async def _check(listing_id, ci, co, rooms, room_type_id=None):
        ok = available(listing_id) if callable(available) else available
        return {"ok": ok, "sold_out_nights": [], "units_left": units_left, "tracked": True}

    monkeypatch.setattr(ps, "room_types", _room_types)
    monkeypatch.setattr(ps, "check_stay", _check)


def run(**over):
    args = dict(destination="Manali", checkin="2026-09-10", checkout="2026-09-12", rooms=1)
    args.update(over)
    return asyncio.run(ps.partner_stays(**args))


@pytest.fixture(autouse=True)
def _no_cache():
    ps.clear_cache()
    yield
    ps.clear_cache()


# ── only real, bookable inventory ────────────────────────────────────────────

class TestOnlyBookableInventory:
    def test_a_bookable_stay_is_returned(self, monkeypatch):
        stub(monkeypatch, rows=[listing()])
        out = run()
        assert [h["name"] for h in out] == ["Riverside Homestay"]
        assert out[0]["is_partner"] is True
        assert out[0]["partner_name"] == "Nomad Trails"
        assert out[0]["source"] == ps.PARTNER_SOURCE

    def test_sold_out_stay_is_dropped(self, monkeypatch):
        stub(monkeypatch, rows=[listing()], available=False)
        assert run() == []

    def test_photoless_or_priceless_rows_are_dropped(self, monkeypatch):
        stub(monkeypatch, rows=[listing(id="a", photos=[]), listing(id="b", price_from=None)])
        assert run() == []

    def test_bad_date_range_searches_nothing(self, monkeypatch):
        stub(monkeypatch, rows=[listing()])
        assert run(checkout="2026-09-10") == []

    def test_a_db_failure_is_silent(self, monkeypatch):
        def boom(dest):
            raise RuntimeError("supabase is down")

        monkeypatch.setattr(ps, "_fetch_candidates", boom)
        assert run() == []


# ── the numbers on the card ──────────────────────────────────────────────────

class TestPricingAndRatings:
    def test_cheapest_room_type_sets_the_from_price(self, monkeypatch):
        stub(monkeypatch, rows=[listing()],
             room_types=[{"id": "r1", "name": "Deluxe", "base_price": 3200},
                         {"id": "r2", "name": "Standard", "base_price": 1800}])
        assert run()[0]["price_per_night"] == 1800

    def test_price_from_is_used_when_no_room_types(self, monkeypatch):
        stub(monkeypatch, rows=[listing()])
        assert run()[0]["price_per_night"] == 2400

    def test_a_real_discount_becomes_a_strike_through(self, monkeypatch):
        stub(monkeypatch, rows=[listing(price_from=3600, original_price=3900)])
        h = run()[0]
        assert (h["price_per_night"], h["price_original"]) == (3600, 3900)
        assert (h["savings"], h["discount_pct"]) == (300, 8)

    def test_no_strike_through_without_an_original(self, monkeypatch):
        stub(monkeypatch, rows=[listing(original_price=None)])
        assert "price_original" not in run()[0]

    def test_an_original_below_the_shown_price_is_not_a_saving(self, monkeypatch):
        # The card shows the cheapest room type, which can undercut price_from;
        # if the original doesn't beat THAT, striking it through would be a lie.
        stub(monkeypatch, rows=[listing(price_from=3600, original_price=3900)],
             room_types=[{"id": "r1", "name": "Std", "base_price": 4200}])
        h = run()[0]
        assert h["price_per_night"] == 4200 and "price_original" not in h

    def test_no_reviews_means_no_rating(self, monkeypatch):
        stub(monkeypatch, rows=[listing()])
        h = run()[0]
        assert "review_score" not in h and "review_count" not in h

    def test_real_reviews_become_a_ten_point_score(self, monkeypatch):
        stub(monkeypatch, rows=[listing()], reviews={"l1": (4.5, 12)})
        h = run()[0]
        assert h["review_score"] == 9.0
        assert h["review_count"] == 12
        assert h["review_label"] == "Exceptional"

    def test_explicit_price_filters_are_respected(self, monkeypatch):
        stub(monkeypatch, rows=[listing()])
        assert run(price_max=2000) == []
        assert run(price_min=5000) == []
        assert len(run(price_min=1000, price_max=5000)) == 1

    def test_booking_url_points_at_our_own_page(self, monkeypatch):
        stub(monkeypatch, rows=[listing()])
        assert run()[0]["booking_url"].endswith("/stays/riverside-homestay")


class TestPhotos:
    """Partner uploads are stored as the website's relative /image/<id> proxy
    path. Hotel cards drop anything that isn't an absolute URL, so a relative
    path renders as a stay with no pictures — absolutise on the way out."""

    def test_proxy_paths_become_absolute(self, monkeypatch):
        stub(monkeypatch, rows=[listing(photos=["/image/abc123", "/image/def456"])])
        photos = run()[0]["photos"]
        assert photos == [
            "https://safar.example.com/image/abc123",
            "https://safar.example.com/image/def456",
        ]

    def test_absolute_and_protocol_relative_urls_survive(self, monkeypatch):
        stub(monkeypatch, rows=[listing(photos=["https://cdn.example.com/a.jpg", "//cdn.x/b.jpg"])])
        assert run()[0]["photos"] == ["https://cdn.example.com/a.jpg", "https://cdn.x/b.jpg"]

    def test_junk_entries_are_dropped(self, monkeypatch):
        stub(monkeypatch, rows=[listing(photos=[None, "", "   ", "/image/ok"])])
        assert run()[0]["photos"] == ["https://safar.example.com/image/ok"]

    def test_a_listing_with_no_usable_photo_is_not_shown(self, monkeypatch):
        stub(monkeypatch, rows=[listing(photos=["", None])])
        assert run() == []


# ── ordering ─────────────────────────────────────────────────────────────────

class TestOrdering:
    def test_verified_then_rated_then_cheapest(self, monkeypatch):
        rows = [
            listing(id="a", slug="a", title="A", price_from=1000,
                    partner={"brand_name": "A", "slug": "a", "verified": False}),
            listing(id="b", slug="b", title="B", price_from=5000),
            listing(id="c", slug="c", title="C", price_from=3000),
        ]
        stub(monkeypatch, rows=rows, reviews={"c": (5.0, 8)})
        assert [h["name"] for h in run()] == ["C", "B", "A"]

    def test_the_pinned_group_is_capped(self, monkeypatch):
        rows = [listing(id=str(i), slug=f"s{i}", title=f"S{i}") for i in range(8)]
        stub(monkeypatch, rows=rows)
        assert len(run()) == ps.MAX_PARTNER_STAYS

    def test_only_a_shortlist_is_availability_checked(self, monkeypatch):
        rows = [listing(id=str(i), slug=f"s{i}", title=f"S{i}", price_from=1000 + i)
                for i in range(20)]
        checked: list[str] = []
        stub(monkeypatch, rows=rows, available=lambda lid: checked.append(lid) is None)
        run()
        assert len(checked) == ps._AVAILABILITY_CHECK_CAP
        # Cheapest first among equally-verified partners, so the shortlist is
        # the one most likely to survive — not whatever order Postgres returned.
        assert checked == [str(i) for i in range(ps._AVAILABILITY_CHECK_CAP)]


# ── pinning inside a merged list ─────────────────────────────────────────────

def ota(name, price, score=8.0):
    return {"name": name, "price_per_night": price, "review_score": score}


def partner_row(name, price):
    return {"name": name, "price_per_night": price, "is_partner": True}


class TestPinning:
    def test_partners_lead_every_sort(self, monkeypatch):
        merged = ps.merge_partner_stays(
            [ota("Cheap Inn", 500), ota("Grand", 9000, score=9.6)],
            [partner_row("Riverside", 2400)],
        )
        for sort in ("recommended", "price_asc", "price_desc", "rating"):
            assert sort_hotels(merged, sort)[0]["name"] == "Riverside", sort

    def test_an_ota_copy_of_a_partner_stay_is_dropped(self, monkeypatch):
        merged = ps.merge_partner_stays(
            [ota("Riverside  Homestay!", 3100), ota("Other", 800)],
            [partner_row("Riverside Homestay", 2400)],
        )
        assert [h["name"] for h in merged] == ["Riverside Homestay", "Other"]

    def test_a_list_with_no_partners_is_untouched(self):
        rows = [ota("A", 1), ota("B", 2)]
        assert ps.pin_partners(rows) is rows
        assert ps.merge_partner_stays(rows, []) is rows

    def test_relative_order_inside_each_group_survives(self):
        rows = [ota("A", 1), partner_row("P1", 9), ota("B", 2), partner_row("P2", 3)]
        assert [h["name"] for h in ps.pin_partners(rows)] == ["P1", "P2", "A", "B"]
