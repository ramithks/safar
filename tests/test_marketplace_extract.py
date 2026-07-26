"""
Normalisation tests for the marketplace AI importer.

These cover the deterministic half of the pipeline — everything between the
model's raw JSON and the draft the partner sees. The LLM calls themselves are
not exercised here; what matters is that a hallucination-shaped or messy
response can't produce a corrupt listing.
"""
from app.services.marketplace_extract import (
    _apply_copy,
    _build_draft,
    _found_lines,
    _price_unit,
    _resolve_kind,
    _str_list,
    _strip_urls,
    _to_int,
)


class TestToInt:
    def test_parses_indian_price_formats(self):
        assert _to_int("Rs. 8,999/-") == 8999
        assert _to_int("₹8999 onwards") == 8999
        assert _to_int("INR 12,500 per person") == 12500
        assert _to_int(8999) == 8999
        assert _to_int("8999.00") == 8999

    def test_rejects_junk(self):
        assert _to_int(None) is None
        assert _to_int("") is None
        assert _to_int("call for price") is None
        assert _to_int(0) is None
        assert _to_int(True) is None  # bool is an int subclass — must not pass

    def test_rejects_absurd_values(self):
        assert _to_int("999999999999") is None


class TestStrList:
    def test_dedupes_case_insensitively_and_trims(self):
        assert _str_list(["  Breakfast ", "breakfast", "Dinner"]) == ["Breakfast", "Dinner"]

    def test_drops_model_placeholders(self):
        assert _str_list(["N/A", "none", "", "Real item"]) == ["Real item"]

    def test_caps_length(self):
        assert len(_str_list([f"item {i}" for i in range(500)], cap=5)) == 5

    def test_non_list_is_empty(self):
        assert _str_list("breakfast") == []
        assert _str_list(None) == []


class TestStripUrls:
    def test_removes_off_platform_funnels(self):
        assert _strip_urls("Book at https://example.com today") == "Book at today"
        assert _strip_urls("Visit www.foo.in now") == "Visit now"

    def test_keeps_plain_text(self):
        assert _strip_urls("A calm riverside stay") == "A calm riverside stay"


class TestResolveKind:
    def test_partner_hint_wins(self):
        assert _resolve_kind("hotel", "experience", []) == "experience"

    def test_falls_back_to_model(self):
        assert _resolve_kind("hotel", None, []) == "hotel"

    def test_multi_day_document_is_a_package_even_when_model_is_unsure(self):
        days = [{"n": "1"}, {"n": "2"}, {"n": "3"}]
        assert _resolve_kind("unknown", None, days) == "group_trip"

    def test_single_day_defaults_to_experience(self):
        assert _resolve_kind("unknown", None, [{"n": "1"}]) == "experience"


class TestPriceUnit:
    def test_respects_extracted_unit(self):
        assert _price_unit("per_person", "hotel") == "per_person"

    def test_defaults_by_kind(self):
        assert _price_unit("unknown", "hotel") == "per_night"
        assert _price_unit(None, "experience") == "per_person"


class TestBuildDraft:
    def test_experience_draft_from_a_realistic_extraction(self):
        raw = {
            "kind": "group_trip",
            "title": "Manali Adventure 4D/3N",
            "destination": "Manali",
            "duration_days": 4,
            "duration_nights": 3,
            "price_from": "Rs. 8,999/-",
            "inclusions": ["Breakfast", "breakfast", "Transport", "Guide"],
            "exclusions": ["Flights", "Personal expenses"],
            "days": [
                {"n": "1", "title": "Delhi to Manali", "night": "Overnight drive"},
                {"n": "2", "title": "Solang Valley"},
            ],
            "confidence": 0.9,
        }
        draft = _build_draft(raw, "group_trip")

        assert draft["price_from"] == 8999
        assert draft["price_unit"] == "per_person"
        assert draft["destination"] == "Manali"
        # city falls back to destination when the document doesn't name one
        assert draft["city"] == "Manali"
        assert draft["content"]["inclusions"] == ["Breakfast", "Transport", "Guide"]
        assert len(draft["content"]["itinerary"]) == 2
        # group_trip has no editor yet, so it lands as an experience draft
        assert draft["kind"] == "experience"

    def test_hotel_draft_keeps_rooms_and_defaults_occupancy(self):
        raw = {
            "kind": "hotel",
            "title": "Riverside Homestay",
            "city": "Manali",
            "price_from": 2400,
            "amenities": ["WiFi", "Parking"],
            "rooms": [
                {"name": "Deluxe Double", "price": "₹2,400", "occupancy": 2},
                {"name": "Family Suite", "price": "3900"},
            ],
            "cancellation_policy": "Free cancellation up to 7 days before check-in.",
            "confidence": 0.8,
        }
        draft = _build_draft(raw, "hotel")

        rooms = draft["content"]["rooms"]
        assert rooms[0]["price"] == 2400
        assert rooms[1]["price"] == 3900
        assert rooms[1]["occupancy"] == 2  # sensible default, not a crash
        assert draft["price_unit"] == "per_night"
        assert "cancellation" in draft["content"]["policies"]

    def test_price_from_falls_back_to_cheapest_tier(self):
        raw = {
            "kind": "experience",
            "price_from": None,
            "pricing_tiers": [
                {"title": "Double sharing", "amount": "₹9,499"},
                {"title": "Triple sharing", "amount": 8499},
            ],
            "confidence": 0.7,
        }
        draft = _build_draft(raw, "experience")
        assert draft["price_from"] == 8499

    def test_missing_everything_does_not_explode(self):
        draft = _build_draft({"kind": "unknown", "confidence": 0.1}, "experience")
        assert draft["title"] is None
        assert draft["price_from"] is None
        assert draft["photos"] == []


class TestFoundLines:
    def test_reports_only_what_was_actually_extracted(self):
        raw = {"duration_days": 5, "confidence": 0.9}
        draft = _build_draft(
            {
                "kind": "group_trip",
                "destination": "Solang Valley",
                "price_from": 8999,
                "inclusions": [f"inc {i}" for i in range(8)],
                "exclusions": [f"exc {i}" for i in range(6)],
                "days": [{"n": str(i), "title": f"Day {i}"} for i in range(1, 6)],
                "confidence": 0.9,
            },
            "group_trip",
        )
        lines = _found_lines(raw, draft, "group_trip")

        assert "5 day itinerary" in lines
        assert "₹8,999 starting price" in lines
        assert "Solang Valley" in lines
        assert "8 inclusions" in lines
        assert "6 exclusions" in lines

    def test_says_nothing_it_did_not_find(self):
        draft = _build_draft({"kind": "experience", "confidence": 0.2}, "experience")
        assert _found_lines({}, draft, "experience") == []


class TestApplyCopy:
    def test_extracted_facts_win_over_generated_ones(self):
        draft = _build_draft(
            {"kind": "experience", "title": "Real Title From PDF", "confidence": 0.9},
            "experience",
        )
        draft = _apply_copy(
            draft,
            {
                "title": "AI Invented Title",
                "summary": "A one-liner.",
                "description": "Some copy.",
                "tags": ["adventure"],
            },
        )
        assert draft["title"] == "Real Title From PDF"
        assert draft["summary"] == "A one-liner."

    def test_generated_title_fills_a_genuine_gap(self):
        draft = _build_draft({"kind": "experience", "confidence": 0.9}, "experience")
        draft = _apply_copy(draft, {"title": "Solang Paragliding", "summary": "x", "description": "y"})
        assert draft["title"] == "Solang Paragliding"

    def test_generated_description_is_url_stripped(self):
        draft = _build_draft({"kind": "experience", "confidence": 0.9}, "experience")
        draft = _apply_copy(
            draft,
            {"title": "t", "summary": "s", "description": "Book at https://competitor.com now"},
        )
        assert "https://" not in draft["description"]

    def test_does_not_overwrite_extracted_faqs(self):
        draft = _build_draft(
            {
                "kind": "experience",
                "faqs": [{"q": "Real question?", "a": "Real answer."}],
                "confidence": 0.9,
            },
            "experience",
        )
        draft = _apply_copy(
            draft,
            {"title": "t", "summary": "s", "description": "d", "faqs": [{"q": "AI?", "a": "AI."}]},
        )
        assert draft["content"]["faqs"][0]["q"] == "Real question?"
