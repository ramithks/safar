"""
A number is not a room count until something says it is.

**The brief's own example, and it had been wrong twice.**

    "room 204 mein AC nahi chal raha"   → room_number 204, a complaint
    "204 wala room"                     → room_number 204
    "204 rooms chahiye"                 → room_count 204
    "4 rooms for 8 people"              → room_count 4, party_size 8

The first read as a *count* of 204 rooms and forced group pricing on a guest
reporting a broken air conditioner. Refusing to count it fixed half the problem;
the second still read as a count afterwards, because the pass that widened the
count pattern to allow a room name between the number and the noun — "2 Deluxe
rooms" — matches "204 wala room" exactly.

Refusing to count them was never the whole job. `create_task` takes a
`room_number`, and a guest reporting a fault has just told us which door. Not
capturing it means the ticket goes to whichever room the model remembered.

**A room number is a string, not an integer.** A hotel with rooms "04" and "4"
has two doors, and `int` would make them one.
"""
import pytest

from app.services.ai import required_actions as ra


class TestTheBriefsFourCases:
    def test_a_complaint_names_a_door_not_a_quantity(self):
        message = "room 204 mein AC nahi chal raha"
        assert ra.room_number(message) == "204"
        assert ra.room_count(message) is None
        assert not [r for r in ra.detect(message, {})
                    if r.intent == ra.GROUP_BOOKING], (
            "a broken air conditioner raised a group-pricing requirement")

    def test_wala_names_a_door_too(self):
        assert ra.room_number("204 wala room") == "204"
        assert ra.room_count("204 wala room") is None

    def test_but_a_stated_count_is_still_a_count(self):
        assert ra.room_count("204 rooms chahiye") == 204
        assert ra.room_number("204 rooms chahiye") is None

    def test_and_two_numbers_land_on_the_right_entities(self):
        message = "4 rooms for 8 people"
        assert ra.room_count(message) == 4
        assert ra.people_count(message) == 8
        assert ra.room_number(message) is None


class TestTheShapesGuestsActuallyWrite:
    @pytest.mark.parametrize("message,door", [
        ("room 204 mein AC nahi chal raha", "204"),
        ("kamra 302 mein paani nahi hai", "302"),
        ("room no 415 ka AC band hai", "415"),
        ("room number 108 please", "108"),
        ("204 wala room", "204"),
        ("302 wale kamre mein towel chahiye", "302"),
        ("suite 501 mein problem hai", "501"),
    ])
    def test_it_reads_the_door(self, message, door):
        assert ra.room_number(message) == door

    @pytest.mark.parametrize("message", [
        "2 rooms", "5 kamre chahiye", "room 5 chahiye", "2 Deluxe rooms chahiye",
        "my phone is 9876543210", "budget 5000 hai", "10 log hain",
        "breakfast included hai?",
    ])
    def test_and_nothing_else(self, message):
        assert ra.room_number(message) is None, message

    def test_a_low_number_stays_a_count(self):
        """
        "room 5" is as likely to be five rooms as door five, and `room_count`
        already claims it. The threshold is where the ambiguity stops: no hotel
        is asked for a hundred rooms in a chat window, and every hotel numbers
        its doors that way.
        """
        assert ra.room_number("room 5 chahiye") is None
        assert ra.room_count("room 5 chahiye") == 5

    def test_it_comes_back_as_a_label(self):
        """
        A hotel with rooms "04" and "4" has two doors. An int makes them one.

        The leading zero also settles the ambiguity the threshold cannot:
        "room 04" is below a hundred and is unmistakably a label, because
        nobody has ever written "04 rooms". A hotel numbering its ground floor
        01–09 would otherwise lose every complaint from it.
        """
        assert ra.room_number("room 04 mein problem hai") == "04"
        assert ra.room_number("room 007 mein") == "007"
        assert isinstance(ra.room_number("room 204 mein"), str)

    def test_and_the_same_digits_are_not_also_counted(self):
        """
        The count pattern reads "04" as 4, so the two readings have to be
        compared as numbers — a string comparison let one door be a door *and*
        four rooms at once.
        """
        assert ra.room_count("room 04 mein problem hai") is None
        assert ra.room_count("room 007 mein") is None


class TestItReachesTheSession:
    def test_a_stated_door_becomes_a_fact(self):
        assert ra.stated_facts("room 204 mein AC nahi chal raha") == {
            "room_number": "204"}

    def test_and_does_not_bring_a_room_count_with_it(self):
        assert "units" not in ra.stated_facts("room 204 mein AC nahi chal raha")

    def test_a_guest_may_correct_it(self):
        """
        "Sorry, 205 not 204" is a correction like any other, so it belongs in
        the set the correction graph watches.
        """
        from app.services.messaging import session

        assert "room_number" in session.CORRECTABLE

    def test_nothing_is_derived_from_it(self):
        """
        A door is a label, not an input to a price. Putting it in the
        dependency graph would make "sorry, 205" invalidate a quote.
        """
        from app.services.messaging import session

        for sources in session.DERIVED_FROM.values():
            assert "room_number" not in sources

    def test_it_goes_when_the_stay_does(self):
        """
        A guest who comes back next month is in a different room.
        """
        from app.services.messaging import session

        assert "room_number" in session._TRANSACTIONAL
