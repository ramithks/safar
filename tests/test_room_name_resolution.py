"""
"Actually Garden Standard chahiye" — which room is that?

**The gap this closes, in the words it was left in.** A comment in the
conversation corpus recorded it as an honest boundary rather than a bug:

    Nothing in this system resolves a room *by name*: "Garden Standard" is a
    string, the session holds uuids, and a guest naming a room in prose reaches
    the session only when the model puts the id into a tool that takes one.

So a guest switching rooms on a turn where the model answered in prose changed
nothing. The session kept the old room, the prompt told the model the guest had
settled on it, and the next quote priced the room they had moved away from.

**Three answers, and the middle one is the point.** The resolver returns a room,
a question, or nothing — it never guesses. A hotel with a *Deluxe Balcony* and a
*Deluxe Garden* has a guest who says "the deluxe one", and the only safe answer
is to ask. It also cannot invent: every id it returns came out of the row list it
was handed, so the failure mode where a model produces a plausible uuid is
structurally unavailable.
"""
import pytest

from app.services.pms import room_names

# One hotel's configured rooms. Every assertion below is scoped to these.
CASA = [
    {"id": "rt-deluxe", "name": "Deluxe Balcony"},
    {"id": "rt-garden", "name": "Garden Standard"},
    {"id": "rt-pool", "name": "Pool Villa"},
    {"id": "rt-suite", "name": "Mandala Suite"},
]

# A hotel whose names collide, which is where guessing would be fatal.
TWO_DELUXES = [
    {"id": "rt-a", "name": "Deluxe Balcony"},
    {"id": "rt-b", "name": "Deluxe Garden"},
]


class TestItFindsTheRoomTheGuestNamed:
    @pytest.mark.parametrize("message,expected", [
        # exact
        ("Garden Standard", "rt-garden"),
        ("Actually Garden Standard chahiye", "rt-garden"),
        ("I'd like the Pool Villa please", "rt-pool"),
        # case
        ("garden standard", "rt-garden"),
        ("GARDEN STANDARD", "rt-garden"),
        ("gArDeN sTaNdArD", "rt-garden"),
        # whitespace and punctuation
        ("Garden  Standard", "rt-garden"),
        ("Garden-Standard", "rt-garden"),
        ("  Garden Standard!  ", "rt-garden"),
        ("\"Mandala Suite\"", "rt-suite"),
        # typos, which are what a phone keyboard produces
        ("Gardern Standrd chahiye", "rt-garden"),
        ("delux balcony", "rt-deluxe"),
        ("pool vila", "rt-pool"),
        # Hinglish, where the room name sits inside a Hindi sentence
        ("mujhe Garden Standard wala kamra chahiye", "rt-garden"),
        ("Pool Villa dikhao na", "rt-pool"),
        ("deluxe balcony hi theek hai", "rt-deluxe"),
        # Devanagari, through the transliterator the language rule already uses
        ("गार्डन स्टैंडर्ड chahiye", "rt-garden"),
    ])
    def test_it_resolves(self, message, expected):
        resolved = room_names.resolve(message, CASA)
        assert resolved.room_type_id == expected, (
            f"{message!r} → {resolved.name!r} ({resolved.confidence})")

    def test_it_reports_how_it_matched(self):
        """
        Provenance, in the small. An exact hit and a fuzzy one are different
        kinds of confidence and a caller may want to treat them differently.
        """
        assert room_names.resolve("Garden Standard", CASA).confidence == "exact"
        assert room_names.resolve("Gardern Standrd", CASA).confidence == "fuzzy"

    def test_it_carries_the_hotels_own_spelling_back(self):
        """
        Not the guest's. The session and the prompt say "Garden Standard", so
        the agent writes the hotel's name for its own room rather than echoing
        a typo back at the guest.
        """
        assert room_names.resolve("gardern standrd", CASA).name == "Garden Standard"


class TestItRefusesToGuess:
    def test_two_rooms_that_both_match_produce_a_question(self):
        resolved = room_names.resolve("the deluxe one", TWO_DELUXES)
        assert not resolved.resolved
        assert resolved.ambiguous
        assert "Deluxe Balcony" in resolved.ask and "Deluxe Garden" in resolved.ask

    def test_the_question_names_this_hotels_rooms(self):
        """
        Handed to the model already written, because it lists this property's
        own rooms and the model would otherwise reconstruct them from memory.
        """
        ask = room_names.resolve("deluxe", TWO_DELUXES).ask
        assert ask.startswith("Which one did you mean")

    def test_but_a_full_name_still_wins_over_its_sibling(self):
        """
        Ambiguity is about genuine ties. "Deluxe Balcony" names one of these two
        exactly, and refusing to resolve it would make the resolver useless at
        the hotel that needs it most.
        """
        assert room_names.resolve("Deluxe Balcony", TWO_DELUXES).room_type_id == "rt-a"
        assert room_names.resolve("deluxe garden please", TWO_DELUXES).room_type_id == "rt-b"

    NESTED = [{"id": "a", "name": "Garden Standard"},
              {"id": "b", "name": "Garden Standard Twin"}]

    def test_a_shorter_name_that_is_said_exactly_is_not_ambiguous(self):
        """
        **My first draft asserted the opposite and was wrong**, which is worth
        keeping because the reasoning is the interesting part.

        I expected "Garden Standard" at a hotel selling a *Garden Standard* and
        a *Garden Standard Twin* to be a tie. It is not: only one of those two
        names appears in what the guest wrote. "Garden Standard Twin" is not in
        "Garden Standard" — containment runs the other way. Treating this as
        ambiguous would interrogate a guest who said exactly what they wanted.
        """
        assert room_names.resolve("Garden Standard", self.NESTED).room_type_id == "a"

    def test_and_the_longer_one_wins_when_the_guest_says_all_of_it(self):
        """
        The reverse, where both names really are present in the sentence: "Garden
        Standard Twin" contains "Garden Standard". The longer is the one they
        typed out, and the margin rule requires it to be clearly longer rather
        than longer by a character.
        """
        assert room_names.resolve("Garden Standard Twin", self.NESTED).room_type_id == "b"


class TestItNeverInvents:
    def test_an_unknown_room_resolves_to_nothing(self):
        assert not room_names.resolve("Presidential Suite", CASA).resolved
        assert not room_names.resolve("sea view double", CASA).resolved

    def test_it_can_only_return_a_room_it_was_handed(self):
        """
        The structural property. Every id comes out of the row list, so the
        failure where a model produces a plausible uuid cannot happen here.
        """
        allowed = {r["id"] for r in CASA}
        for message in ("Garden Standard", "delux balcony", "pool vila",
                        "something", "the deluxe one", ""):
            resolved = room_names.resolve(message, CASA)
            assert resolved.room_type_id in allowed or resolved.room_type_id is None

    def test_a_passing_noun_is_not_a_room_selection(self):
        """
        "I saw a garden" shares a token with the Garden Standard and names
        nothing. Every significant token of a name has to be present, which is
        what keeps a partial word from becoming a booking decision.
        """
        assert not room_names.resolve("I saw a garden", CASA).resolved
        assert not room_names.resolve("is there a pool?", CASA).resolved
        assert not room_names.resolve("guard duty", CASA).resolved

    def test_a_near_miss_is_not_a_match(self):
        assert not room_names.resolve("Guardian Standard", CASA).resolved

    def test_no_rooms_configured_resolves_nothing(self):
        assert not room_names.resolve("Garden Standard", []).resolved
        assert not room_names.resolve("Garden Standard", None).resolved

    def test_an_empty_message_resolves_nothing(self):
        assert not room_names.resolve("", CASA).resolved
        assert not room_names.resolve(None, CASA).resolved

    def test_a_malformed_room_row_is_skipped_rather_than_crashing(self):
        """
        A row with no name or no id is a configuration problem, and a resolver
        that raised on one would take the guest's turn down with it.
        """
        broken = [{"id": "x"}, {"name": "Nameless"}, {"id": "ok", "name": "Pool Villa"}]
        assert room_names.resolve("pool villa", broken).room_type_id == "ok"


class TestScopedToOneHotel:
    def test_it_cannot_reach_another_hotels_room(self):
        """
        The resolver is handed one property's rows and has no database of its
        own — the same shape as `get_booking_status` taking no arguments.
        """
        other_hotel = [{"id": "rt-elsewhere", "name": "Sea View Double"}]
        assert not room_names.resolve("Sea View Double", CASA).resolved
        assert room_names.resolve("Sea View Double", other_hotel).resolved


class TestTheTransliterationFix:
    def test_devanagari_survives_the_doubled_vowel(self):
        """
        `आ` romanises to `aa`, so "गार्डन" arrives as `gaardan` and scores 0.769
        against `garden` — below the bar. Squeezing doubled letters lifts it to
        0.833 while leaving a real near-miss below.
        """
        assert room_names._squeeze("gaardan") == "gardan"
        assert room_names.resolve("गार्डन स्टैंडर्ड", CASA).room_type_id == "rt-garden"

    def test_a_short_devanagari_name_is_a_known_limitation(self):
        """
        **Recorded rather than fixed, because the fix would cost more than it
        buys.**

        "पूल" romanises to `pul`, which scores 0.667 against `pool` — far below
        the bar, because a three-letter word carries almost no signal for a
        sequence matcher. Lowering the threshold to admit it would also admit
        `pal`, `pile` and `pole`, and stripping vowels to match consonant
        skeletons would admit more still.

        So Devanagari resolution is best-effort and works on names long enough
        to carry signal — "गार्डन स्टैंडर्ड" resolves, "पूल" does not. The
        failure is the safe one: no resolution, the session unchanged, and the
        model answering from the room list in its prompt exactly as it did
        before this resolver existed.
        """
        assert not room_names.resolve("पूल विला", CASA).resolved

    def test_and_the_near_miss_it_must_not_admit(self):
        """
        The pair that decides the threshold. If squeezing ever let `guard`
        match `garden`, the fix would have cost more than it bought.
        """
        assert not room_names.resolve("guard standard", CASA).resolved
