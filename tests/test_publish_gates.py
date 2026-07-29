"""
Publish gates — the backend half.

These mirror lib/marketplace/publish-rules.test.ts in the website, case for
case. The two implementations must agree: if they drift, the editor shows
"Ready to publish" and this refuses the submit with no way for the partner to
see which rule they broke.
"""
import pytest

from app.api.v1.marketplace.partner_ops import (
    MIN_DESCRIPTION_CHARS,
    MIN_TRIP_DESCRIPTION_CHARS,
    _listing_gaps,
)


def stay(**over) -> dict:
    base = {
        "kind": "hotel",
        "title": "Riverside Homestay",
        "description": "A quiet riverside stay ten minutes from Mall Road.",
        "photos": ["/image/a", "/image/b", "/image/c"],
        "price_from": 2400,
        "city": "Manali",
        "address": "Old Manali Road, Manali, Himachal Pradesh 175131",
        "map_url": "https://maps.app.goo.gl/x",
        "content": {
            "rooms": [{"name": "Deluxe", "units": 4}],
            "checkin": "2:00 PM",
            "checkout": "11:00 AM",
        },
    }
    base.update(over)
    return base


def fields(listing: dict) -> list[str]:
    return [g.field for g in _listing_gaps(listing)]


class TestCompleteStay:
    def test_a_complete_stay_has_nothing_left_to_fix(self):
        assert _listing_gaps(stay()) == []


class TestDescription:
    def test_a_single_sentence_is_enough(self):
        short = "Quiet riverside rooms, ten minutes from Mall Road."
        assert len(short) < 120
        assert "description" not in fields(stay(description=short))

    def test_a_description_that_says_nothing_is_still_refused(self):
        assert "description" in fields(stay(description="Nice place"))

    def test_trips_keep_the_higher_bar(self):
        assert MIN_TRIP_DESCRIPTION_CHARS > MIN_DESCRIPTION_CHARS


class TestAddress:
    def test_the_address_is_required(self):
        assert "address" in fields(stay(address=None))
        assert "address" in fields(stay(address=""))

    def test_something_too_short_to_be_an_address_is_refused(self):
        assert "address" in fields(stay(address="Goa"))

    def test_the_map_pin_does_not_substitute_for_it(self):
        # The pin is where to go; the address is what prints on the
        # confirmation. Both are required.
        assert "address" in fields(stay(address=None))


class TestRooms:
    def test_at_least_one_room_is_needed(self):
        assert "content.rooms" in fields(
            stay(content={"rooms": [], "checkin": "2 PM", "checkout": "11 AM"})
        )

    def test_a_blank_row_does_not_count(self):
        assert "content.rooms" in fields(
            stay(content={"rooms": [{"name": "   "}], "checkin": "2 PM", "checkout": "11 AM"})
        )

    def test_every_room_needs_a_capacity(self):
        # Capacity is entered on the listing and nowhere else; a room without
        # it would sell exactly one and stop.
        assert "content.rooms" in fields(
            stay(
                content={
                    "rooms": [{"name": "Deluxe", "units": 4}, {"name": "Suite"}],
                    "checkin": "2 PM",
                    "checkout": "11 AM",
                }
            )
        )

    def test_a_single_room_property_is_fine(self):
        assert "content.rooms" not in fields(
            stay(
                content={
                    "rooms": [{"name": "The cottage", "units": 1}],
                    "checkin": "2 PM",
                    "checkout": "11 AM",
                }
            )
        )

    def test_experiences_are_exempt(self):
        out = fields(stay(kind="experience", content={}))
        assert "content.rooms" not in out
        assert "content.checkin" not in out


class TestStayRules:
    def test_check_in_and_check_out_are_needed(self):
        assert "content.checkin" in fields(
            stay(content={"rooms": [{"name": "Deluxe", "units": 2}]})
        )

    def test_one_of_the_two_is_not_enough(self):
        assert "content.checkin" in fields(
            stay(content={"rooms": [{"name": "Deluxe", "units": 2}], "checkin": "2:00 PM"})
        )

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_whitespace_counts_as_missing(self, blank):
        assert "content.checkin" in fields(
            stay(
                content={
                    "rooms": [{"name": "Deluxe", "units": 2}],
                    "checkin": blank,
                    "checkout": "11 AM",
                }
            )
        )


class TestUntouchedRules:
    def test_title_photos_price_and_location_still_gate(self):
        assert "title" in fields(stay(title="Hi"))
        assert "photos" in fields(stay(photos=["/image/a"]))
        assert "price_from" in fields(stay(price_from=0))
        assert "city" in fields(stay(city=None, destination=None))
        assert "map_url" in fields(stay(map_url=None))
