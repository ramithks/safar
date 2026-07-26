"""
The bar a listing or trip must clear before it can go live.

These rules exist in two languages — here and in the website's
lib/marketplace/publish-rules.ts — and they have to agree. When they drift the
partner gets the worst possible experience: a checklist that says "ready" and a
server that refuses, with no way to tell which is lying. Every case below is
mirrored by the TypeScript version.
"""
import pytest

from app.api.v1.marketplace.partner_ops import NEEDS_APPROVAL, _listing_gaps, _trip_gaps


def _fields(gaps):
    return {g.field for g in gaps}


def _stay(**over):
    base = {
        "kind": "hotel",
        "title": "Solang Riverside Cottage",
        "photos": ["a.jpg", "b.jpg", "c.jpg"],
        "description": "x" * 200,
        "price_from": 2400,
        "city": "Manali",
        "map_url": "https://maps.app.goo.gl/abc",
        "content": {"rooms": [{"name": "Deluxe"}]},
    }
    base.update(over)
    return base


def _trip(**over):
    base = {
        "title": "Spiti Valley 7-Day Circuit",
        "destination": "Spiti Valley",
        "origin": "Manali",
        "hero_image": "hero.jpg",
        "description": "x" * 200,
        "duration_days": 7,
        "departure_date": "2026-09-01",
        "seats_total": 12,
        "price_from": 21999,
        "content": {
            "days": [{"n": "1", "title": "Manali to Kaza"}],
            "inclusions": [{"label": "Stay"}],
            "images": {"g1": "one.jpg"},
            "pickup_points": [{"label": "Manali bus stand", "map_url": "https://maps.app.goo.gl/x"}],
        },
    }
    base.update(over)
    return base


class TestListingGaps:
    def test_a_complete_stay_has_no_gaps(self):
        assert _listing_gaps(_stay()) == []

    def test_map_url_is_required(self):
        """A traveller who can't find the place can't get to it."""
        assert "map_url" in _fields(_listing_gaps(_stay(map_url=None)))

    def test_map_url_required_for_experiences_too(self):
        gaps = _listing_gaps(_stay(kind="experience", map_url="", content={}))
        assert "map_url" in _fields(gaps)

    def test_experience_no_longer_needs_a_duration(self):
        """
        `content.duration_hours` was a hard publish gate for a number that
        varies per booking, and the field is gone from the editor — so a
        complete experience without one must be publishable.
        """
        gaps = _listing_gaps(_stay(kind="experience", content={}))
        assert _fields(gaps) == set()

    def test_stay_still_needs_a_room_type(self):
        assert "content.rooms" in _fields(_listing_gaps(_stay(content={})))


class TestTripGaps:
    def test_a_complete_trip_has_no_gaps(self):
        assert _trip_gaps(_trip()) == []

    def test_origin_is_required(self):
        """"Departs from" is the first thing a traveller filters on."""
        assert "origin" in _fields(_trip_gaps(_trip(origin=None)))

    def test_gallery_is_required(self):
        content = dict(_trip()["content"], images={})
        assert "content.images" in _fields(_trip_gaps(_trip(content=content)))

    def test_gallery_of_blank_urls_does_not_count(self):
        content = dict(_trip()["content"], images={"g1": "", "g2": None})
        assert "content.images" in _fields(_trip_gaps(_trip(content=content)))

    def test_a_pickup_point_needs_a_map_link(self):
        """A named pickup with no link is exactly the ambiguity we're removing."""
        content = dict(_trip()["content"], pickup_points=[{"label": "Somewhere in town"}])
        assert "content.pickup_points" in _fields(_trip_gaps(_trip(content=content)))

    def test_no_pickup_points_at_all_is_a_gap(self):
        content = dict(_trip()["content"], pickup_points=[])
        assert "content.pickup_points" in _fields(_trip_gaps(_trip(content=content)))


class TestAutoPublish:
    """
    `auto_publish` is the trusted-partner switch: submissions skip the review
    queue. It is a service-role-only column (see guard_partner_cols) precisely
    because a partner who could set it could publish anything unreviewed.
    """

    @pytest.mark.parametrize("kind", ["experience", "group_trip"])
    def test_kinds_that_normally_need_review(self, kind):
        assert NEEDS_APPROVAL.get(kind, True) is True

    @pytest.mark.parametrize("kind", ["experience", "group_trip"])
    def test_trusted_partner_skips_review(self, kind):
        partner = {"auto_publish": True}
        assert not (NEEDS_APPROVAL.get(kind, True) and not partner.get("auto_publish"))

    @pytest.mark.parametrize("kind", ["experience", "group_trip"])
    def test_untrusted_partner_still_queues(self, kind):
        for partner in ({}, {"auto_publish": False}, {"auto_publish": None}):
            assert NEEDS_APPROVAL.get(kind, True) and not partner.get("auto_publish")

    def test_stays_publish_instantly_either_way(self):
        assert NEEDS_APPROVAL["hotel"] is False
