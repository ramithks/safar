"""
Ranking tests for the marketplace matcher.

The DB layer is stubbed so these exercise the ranking, the hard filters and the
explainability rules deterministically — no network, no Supabase.

The bar these enforce, in order of importance:
  1. Irrelevant inventory is never returned (wrong city, over budget).
  2. Nothing is returned when confidence is low — an empty result is correct.
  3. Ranking never moves on commission, plan tier or featured flags.
  4. Every `why` line is traceable to a real value on the row.
"""
from datetime import date

import pytest

from app.services import marketplace_match as mm
from app.services.marketplace_match import DayPlan, MatchContext, match_inventory


# ── fixtures ─────────────────────────────────────────────────────────────────

def partner(name="Nomad Trails", verified=True, rating=4.8, reviews=42, minutes=30):
    return {
        "brand_name": name,
        "slug": "nomad-trails",
        "verified": verified,
        "rating": rating,
        "review_count": reviews,
        "response_minutes": minutes,
    }


def hotel(**over):
    row = {
        "id": "h1",
        "kind": "hotel",
        "slug": "riverside-homestay",
        "title": "Riverside Homestay, Old Manali",
        "summary": "A quiet riverside stay ten minutes from Mall Road.",
        "description": "x" * 200,
        "destination": "Manali",
        "city": "Manali",
        "photos": ["a.jpg", "b.jpg", "c.jpg"],
        "price_from": 2400,
        "price_unit": "per_night",
        "amenities": ["WiFi", "Parking"],
        "tags": ["homestay", "riverside", "nature"],
        "content": {"rooms": [{"name": "Deluxe"}]},
        "booking_mode": "whatsapp",
        "updated_at": "2026-07-20T00:00:00+00:00",
        "partner": partner(),
    }
    row.update(over)
    return row


def experience(**over):
    row = {
        "id": "e1",
        "kind": "experience",
        "slug": "solang-paragliding",
        "title": "Solang Valley Paragliding",
        "summary": "A 15-minute tandem flight with a certified pilot.",
        "description": "y" * 200,
        "destination": "Manali",
        "city": "Manali",
        "photos": ["a.jpg", "b.jpg", "c.jpg"],
        "price_from": 1800,
        "price_unit": "per_person",
        "amenities": [],
        "tags": ["adventure", "paragliding"],
        "content": {"inclusions": ["Pilot", "Gear", "Video"]},
        "booking_mode": "instant",
        "updated_at": "2026-07-20T00:00:00+00:00",
        "partner": partner(),
    }
    row.update(over)
    return row


def package(**over):
    row = {
        "id": "p1",
        "slug": "manali-snow-run",
        "title": "Manali Snow Run 4D/3N",
        "description": "z" * 200,
        "summary": "Four days in Manali from Delhi.",
        "destination": "Manali",
        "origin": "Delhi",
        "duration_days": 4,
        "duration_label": "4D/3N",
        "departure_date": "2026-08-22",
        "departure_label": "22 Aug",
        "seats_left": 9,
        "price_from": 9200,
        "hero_image": "hero.jpg",
        "content": {"inclusions": ["Stay", "Breakfast", "Transport"]},
        "booking_mode": "instant",
        "partner_id": "pt1",
        "updated_at": "2026-07-20T00:00:00+00:00",
        "partner": partner(),
    }
    row.update(over)
    return row


def safar_package(**over):
    """partner_id null + no join = a Safar-run trip."""
    row = package(id="p-tb", slug="udaipur-weekend", title="Udaipur Weekend",
                  destination="Udaipur", partner_id=None, partner=None)
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _no_cache():
    mm.clear_cache()
    yield
    mm.clear_cache()


def stub(monkeypatch, listings=(), trips=()):
    monkeypatch.setattr(mm, "_fetch_inventory", lambda dest: (list(listings), list(trips)))


def ctx(**over) -> MatchContext:
    base = dict(
        destination="Manali",
        days=4,
        travellers=4,
        origin="Delhi",
        budget=15000,
        interests=["Adventure", "Nature"],
        depart_date=date(2026, 8, 22),
    )
    base.update(over)
    return MatchContext(**base)


def ids(result, kind):
    for s in result["sections"]:
        if s["kind"] == kind:
            return [i["id"] for i in s["items"]]
    return []


# ── hard filters ─────────────────────────────────────────────────────────────

class TestRelevanceIsAHardGate:
    def test_wrong_destination_is_never_returned(self, monkeypatch):
        stub(monkeypatch, listings=[hotel(destination="Goa", city="Goa")])
        assert match_inventory(ctx())["sections"] == []

    def test_over_budget_hotel_is_dropped(self, monkeypatch):
        # 15k pp x 4 pax x 40% / 3 nights / 2 rooms = ~4000 per room-night
        stub(monkeypatch, listings=[hotel(price_from=25000)])
        assert match_inventory(ctx())["sections"] == []

    def test_over_budget_package_is_dropped(self, monkeypatch):
        stub(monkeypatch, trips=[package(price_from=40000)])
        assert match_inventory(ctx())["sections"] == []

    def test_sold_out_package_is_dropped(self, monkeypatch):
        stub(monkeypatch, trips=[package(seats_left=0)])
        assert match_inventory(ctx())["sections"] == []

    def test_no_inventory_returns_clean_empty(self, monkeypatch):
        stub(monkeypatch)
        r = match_inventory(ctx())
        assert r["sections"] == []
        assert r["day_matches"] == []
        assert r["package_replacement"] is None
        assert r["meta"]["returned"] == 0


class TestLowConfidenceShowsNothing:
    def test_thin_listing_far_from_intent_is_withheld(self, monkeypatch):
        thin = hotel(
            id="thin",
            photos=[],
            description="",
            summary=None,
            price_from=None,
            content={},
            tags=[],
            updated_at="2023-01-01T00:00:00+00:00",
            partner=partner(verified=False, rating=None, reviews=0, minutes=None),
        )
        stub(monkeypatch, listings=[thin])
        r = match_inventory(ctx())
        assert r["sections"] == [], "a thin, unverified, stale listing must not surface"

    def test_confidence_floor_is_reported(self, monkeypatch):
        stub(monkeypatch)
        assert match_inventory(ctx())["meta"]["min_confidence"] == mm.MIN_CONFIDENCE


# ── trust, never money ───────────────────────────────────────────────────────

class TestRankingIgnoresPaidSignals:
    def test_commission_and_featured_do_not_reorder(self, monkeypatch):
        """Two identical experiences; one carries every paid signal we have.
        Order must be driven by trust, so the verified/higher-rated one wins
        regardless of commission or featured status."""
        paid = experience(
            id="paid", slug="paid", commission_pct=30, is_featured=True,
            partner=partner(name="Pays Us", verified=False, rating=3.0, reviews=2),
        )
        trusted = experience(
            id="trusted", slug="trusted", commission_pct=0, is_featured=False,
            partner=partner(name="Trusted", verified=True, rating=4.9, reviews=310),
        )
        stub(monkeypatch, listings=[paid, trusted])
        assert ids(match_inventory(ctx()), "experience")[0] == "trusted"

    def test_verified_partner_outranks_unverified_equal(self, monkeypatch):
        a = hotel(id="unverified", slug="a", partner=partner(verified=False, rating=None, reviews=0))
        b = hotel(id="verified", slug="b", partner=partner(verified=True, rating=4.7, reviews=88))
        stub(monkeypatch, listings=[a, b])
        assert ids(match_inventory(ctx()), "hotel")[0] == "verified"


# ── explainability ───────────────────────────────────────────────────────────

class TestWhyLines:
    def test_every_item_explains_itself(self, monkeypatch):
        stub(monkeypatch, listings=[hotel(), experience()], trips=[package()])
        r = match_inventory(ctx())
        for section in r["sections"]:
            for item in section["items"]:
                assert item["why"], f"{item['id']} returned with no reason"
                assert len(item["why"]) <= 3

    def test_budget_reason_quotes_the_real_price(self, monkeypatch):
        stub(monkeypatch, trips=[package(price_from=9200)])
        item = match_inventory(ctx())["sections"][0]["items"][0]
        assert any("9,200" in w for w in item["why"])

    def test_no_budget_reason_when_no_budget_given(self, monkeypatch):
        stub(monkeypatch, listings=[hotel()])
        item = match_inventory(ctx(budget=None))["sections"][0]["items"][0]
        assert not any("budget" in w.lower() for w in item["why"])

    def test_duration_reason_only_when_it_actually_covers_the_trip(self, monkeypatch):
        stub(monkeypatch, trips=[package(duration_days=4)])
        assert any("Covers all 4 days" in w for w in match_inventory(ctx())["sections"][0]["items"][0]["why"])

        mm.clear_cache()
        stub(monkeypatch, trips=[package(duration_days=9)])
        r = match_inventory(ctx())
        if r["sections"]:
            assert not any("Covers all" in w for w in r["sections"][0]["items"][0]["why"])

    def test_safar_trip_is_attributed_not_called_a_partner(self, monkeypatch):
        """Attribution rides on `partner_name`, not on a `why` line — "who runs
        it" is not a reason to book it, and burning a scarce why slot on it
        pushed real reasons off the card."""
        stub(monkeypatch, trips=[safar_package()])
        item = match_inventory(ctx(destination="Udaipur"))["sections"][0]["items"][0]
        assert item["partner_name"] == "Safar"
        assert item["verified"] is True
        assert not any("Safar" in w for w in item["why"])

    def test_the_three_why_slots_go_to_the_most_useful_reasons(self, monkeypatch):
        stub(monkeypatch, trips=[package()])
        why = match_inventory(ctx())["sections"][0]["items"][0]["why"]
        assert any("9,200" in w for w in why), "budget must survive truncation"
        assert any("Covers all 4 days" in w for w in why), "fit must survive truncation"


# ── day attachment ───────────────────────────────────────────────────────────

class TestDayLevelRecommendations:
    def test_experience_pins_to_the_day_that_visits_that_place(self, monkeypatch):
        stub(monkeypatch, listings=[experience()])
        r = match_inventory(
            ctx(
                day_plan=[
                    DayPlan(day=1, title="Delhi to Manali", places=["Volvo"]),
                    DayPlan(day=2, title="Solang Valley", places=["Solang", "Mall Road"]),
                ]
            )
        )
        assert len(r["day_matches"]) == 1
        match = r["day_matches"][0]
        assert match["day"] == 2
        assert any("Day 2" in w for w in match["why"])

    def test_no_day_plan_means_no_day_matches(self, monkeypatch):
        stub(monkeypatch, listings=[experience()])
        r = match_inventory(ctx())
        assert r["day_matches"] == []
        assert r["sections"], "it should still appear in the section, just unpinned"

    def test_generic_words_alone_do_not_pin(self, monkeypatch):
        """A day called 'Free time' must not attract an unrelated experience."""
        stub(monkeypatch, listings=[experience(title="Scuba Diving", tags=["adventure"],
                                               city="Manali", destination="Manali")])
        r = match_inventory(ctx(day_plan=[DayPlan(day=1, title="Free time", places=["local sightseeing"])]))
        assert r["day_matches"] == []


# ── package replacement ──────────────────────────────────────────────────────

class TestPackageReplacement:
    def test_offered_only_for_a_strong_full_length_package(self, monkeypatch):
        stub(monkeypatch, trips=[package(duration_days=4, price_from=8000)])
        r = match_inventory(ctx())
        assert r["package_replacement"] is not None
        assert r["package_replacement"]["confidence"] >= mm.REPLACEMENT_CONFIDENCE

    def test_not_offered_when_the_package_is_the_wrong_length(self, monkeypatch):
        stub(monkeypatch, trips=[package(duration_days=8)])
        assert match_inventory(ctx())["package_replacement"] is None

    def test_not_offered_on_a_weak_match(self, monkeypatch):
        weak = package(
            duration_days=4, photos=[], hero_image=None, description="", summary=None,
            content={}, departure_date="2027-03-01",
            partner=partner(verified=False, rating=None, reviews=0, minutes=None),
        )
        stub(monkeypatch, trips=[weak])
        assert match_inventory(ctx())["package_replacement"] is None


# ── budget scenarios ─────────────────────────────────────────────────────────

class TestBudgetScenarios:
    def test_low_budget_trip_keeps_only_what_fits(self, monkeypatch):
        cheap = hotel(id="cheap", slug="cheap", price_from=900)
        pricey = hotel(id="pricey", slug="pricey", price_from=6000)
        stub(monkeypatch, listings=[cheap, pricey])
        assert ids(match_inventory(ctx(budget=5000)), "hotel") == ["cheap"]

    def test_luxury_budget_admits_the_expensive_option(self, monkeypatch):
        pricey = hotel(id="pricey", slug="pricey", price_from=6000)
        stub(monkeypatch, listings=[pricey])
        assert ids(match_inventory(ctx(budget=60000)), "hotel") == ["pricey"]

    def test_budget_is_per_person_unless_told_otherwise(self, monkeypatch):
        stub(monkeypatch, trips=[package(price_from=9200)])
        assert match_inventory(ctx(budget=10000)) ["sections"], "10k pp covers a 9.2k package"

        mm.clear_cache()
        stub(monkeypatch, trips=[package(price_from=9200)])
        r = match_inventory(ctx(budget=10000, budget_is_total=True))
        assert r["sections"] == [], "10k across 4 people does not cover 9.2k each"

    def test_no_budget_still_returns_relevant_inventory(self, monkeypatch):
        stub(monkeypatch, listings=[hotel()], trips=[package()])
        assert match_inventory(ctx(budget=None))["meta"]["returned"] >= 2


# ── mixed inventory + caps ───────────────────────────────────────────────────

class TestInventoryMix:
    def test_partner_and_safar_packages_compete_on_merit(self, monkeypatch):
        tb = safar_package(destination="Manali", slug="tb-manali", id="tb",
                               duration_days=4, price_from=8800)
        stub(monkeypatch, trips=[tb, package()])
        r = match_inventory(ctx())
        assert len(ids(r, "package")) == 1, "only the single best package is offered"

    def test_only_partner_inventory(self, monkeypatch):
        stub(monkeypatch, listings=[hotel(), experience()])
        r = match_inventory(ctx())
        assert {s["kind"] for s in r["sections"]} == {"hotel", "experience"}

    def test_only_safar_inventory(self, monkeypatch):
        stub(monkeypatch, trips=[safar_package(destination="Manali", duration_days=4)])
        r = match_inventory(ctx())
        assert [s["kind"] for s in r["sections"]] == ["package"]

    def test_section_caps_are_respected(self, monkeypatch):
        listings = [hotel(id=f"h{i}", slug=f"h{i}") for i in range(6)]
        listings += [experience(id=f"e{i}", slug=f"e{i}") for i in range(6)]
        trips = [package(id=f"p{i}", slug=f"p{i}") for i in range(4)]
        stub(monkeypatch, listings=listings, trips=trips)
        r = match_inventory(ctx())
        assert len(ids(r, "package")) <= mm.MAX_PACKAGES
        assert len(ids(r, "hotel")) <= mm.MAX_HOTELS
        assert len(ids(r, "experience")) <= mm.MAX_EXPERIENCES


# ── contract / resilience ────────────────────────────────────────────────────

class TestContract:
    def test_items_are_kind_agnostic_and_complete(self, monkeypatch):
        stub(monkeypatch, listings=[hotel(), experience()], trips=[package()])
        required = {"id", "kind", "slug", "title", "url", "booking_type", "cta",
                    "confidence", "why", "day", "price_unit"}
        for section in match_inventory(ctx())["sections"]:
            for item in section["items"]:
                assert required <= set(item), f"{item['id']} missing {required - set(item)}"

    def test_cta_does_not_promise_booking_before_checkout_exists(self, monkeypatch):
        stub(monkeypatch, listings=[experience()])
        item = match_inventory(ctx())["sections"][0]["items"][0]
        assert item["booking_type"] == "instant"
        if not mm.BOOKINGS_LIVE:
            assert item["cta"] == "Check availability"

    def test_urls_point_at_the_public_pages(self, monkeypatch):
        stub(monkeypatch, listings=[hotel(), experience()], trips=[package()])
        urls = {
            i["kind"]: i["url"]
            for s in match_inventory(ctx())["sections"]
            for i in s["items"]
        }
        assert urls["hotel"].startswith("/stays/")
        assert urls["experience"].startswith("/experiences/")
        assert urls["package"].startswith("/trips/")

    def test_a_db_outage_degrades_to_empty_not_an_exception(self, monkeypatch):
        def boom(_dest):
            raise RuntimeError("supabase down")

        monkeypatch.setattr(mm, "_fetch_inventory", boom)
        r = match_inventory(ctx())
        assert r["sections"] == []
        assert r["meta"]["error"] == "inventory_unavailable"
