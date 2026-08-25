"""
Every live measurement in this system is Casa Mandala.

Four room names — Garden Standard, Pool Villa, Mandala Suite, Deluxe Balcony —
each distinctive, each two words, each in title case, and no two sharing a
significant word. That is a benign naming scheme, and a resolver tuned against
it has never been asked the questions real hotels ask.

The ones that actually exist in this database already include `deluxe room` and
`deluxe king bed` at the same property, lowercase, where "deluxe" identifies
nothing. And `Standard` / `Deluxe` / `Suite` at another, where every name is a
single generic word and the guest's own adjective is the room's whole name.

This is the corpus the brief asks for: naming styles that collide, repeat,
abbreviate and disappear, tested for four properties at once —

    a room is never invented          nothing outside this hotel's list
    a room is never borrowed          another hotel's id is not reachable
    ambiguity is asked about          not guessed, in either direction
    a near miss is still a hit        typos, spacing, word order, case

The safety asymmetry throughout: guessing wrong sells the wrong room, asking
costs one message. So an ambiguous name must produce a question, and a name
that is not on the list must produce nothing at all.
"""
import pytest

from app.services.pms import room_names

# ── four hotels, four naming philosophies ───────────────────────────────────

DISTINCT = [                      # Casa Mandala: no two names share a word
    {"id": "cm-garden", "name": "Garden Standard"},
    {"id": "cm-pool", "name": "Pool Villa"},
    {"id": "cm-suite", "name": "Mandala Suite"},
    {"id": "cm-balcony", "name": "Deluxe Balcony"},
]

GENERIC = [                       # every name a single generic word
    {"id": "g-standard", "name": "Standard"},
    {"id": "g-deluxe", "name": "Deluxe"},
    {"id": "g-suite", "name": "Suite"},
]

NUMBERED = [                      # names distinguished only by a digit
    {"id": "n-suite1", "name": "Suite 1"},
    {"id": "n-suite2", "name": "Suite 2"},
    {"id": "n-a", "name": "Room A"},
    {"id": "n-b", "name": "Room B"},
]

OVERLAPPING = [                   # a shared adjective doing no work
    {"id": "o-king", "name": "Deluxe King"},
    {"id": "o-twin", "name": "Deluxe Twin"},
    {"id": "o-premium", "name": "Premium King"},
    {"id": "o-garden-s", "name": "Garden Standard"},
    {"id": "o-garden-d", "name": "Garden Deluxe"},
]

LOWERCASE = [                     # as one real property in this database has it
    {"id": "l-room", "name": "deluxe room"},
    {"id": "l-king", "name": "deluxe king bed"},
]

EVERY_HOTEL = {
    "distinct": DISTINCT, "generic": GENERIC, "numbered": NUMBERED,
    "overlapping": OVERLAPPING, "lowercase": LOWERCASE,
}


def one(message, rooms):
    return room_names.resolve(message, rooms)


# ── a name that is on the list is found ─────────────────────────────────────

@pytest.mark.parametrize("message,rooms,expected", [
    # exact, across every scheme
    ("Garden Standard chahiye", DISTINCT, "cm-garden"),
    ("I'd like the Pool Villa", DISTINCT, "cm-pool"),
    ("Suite 2 book kar do", NUMBERED, "n-suite2"),
    ("Room B please", NUMBERED, "n-b"),
    ("Deluxe Twin available hai?", OVERLAPPING, "o-twin"),
    ("Premium King chahiye", OVERLAPPING, "o-premium"),
    ("Garden Deluxe", OVERLAPPING, "o-garden-d"),
    ("deluxe king bed", LOWERCASE, "l-king"),
    # case, spacing and punctuation are not part of the name
    ("GARDEN STANDARD", DISTINCT, "cm-garden"),
    ("garden  standard", DISTINCT, "cm-garden"),
    ("Garden-Standard", DISTINCT, "cm-garden"),
    ("DELUXE KING BED chahiye", LOWERCASE, "l-king"),
    ("suite 1", NUMBERED, "n-suite1"),
    # word order
    ("Standard Garden wala room", DISTINCT, "cm-garden"),
    ("King Deluxe chahiye", OVERLAPPING, "o-king"),
    # typos and near misses
    ("Gardn Standard", DISTINCT, "cm-garden"),
    ("Pool Vila", DISTINCT, "cm-pool"),
    ("Mandala Suit chahiye", DISTINCT, "cm-suite"),
    ("Delux Twin", OVERLAPPING, "o-twin"),
    # the name inside a sentence
    ("Actually mujhe Deluxe Balcony chahiye 20 tarikh ko", DISTINCT, "cm-balcony"),
    ("we were looking at the Mandala Suite for our anniversary", DISTINCT, "cm-suite"),
])
def test_a_name_on_the_list_resolves(message, rooms, expected):
    assert one(message, rooms).room_type_id == expected


# ── a name that is not on the list is not invented ──────────────────────────

@pytest.mark.parametrize("message", [
    "Presidential Suite chahiye",
    "I want the Ocean View room",
    "Penthouse available hai?",
    "Executive Lounge Double",
    "the honeymoon cottage",
    "Room Z",
    "Suite 9",
])
@pytest.mark.parametrize("hotel", sorted(EVERY_HOTEL))
def test_a_room_this_hotel_does_not_have_is_never_returned(message, hotel):
    """
    The resolver's whole safety property. Nothing outside the list it was given
    may come back, whatever the guest wrote.
    """
    found = one(message, EVERY_HOTEL[hotel])
    ids = {r["id"] for r in EVERY_HOTEL[hotel]}
    assert found.room_type_id is None or found.room_type_id in ids


@pytest.mark.parametrize("message,expected", [
    ("Pool Villa chahiye", None),
    ("Mandala Suite", None),
    ("Deluxe Balcony", None),
])
def test_another_hotels_room_name_reaches_nothing(message, expected):
    """
    Casa Mandala's names, asked of a hotel that does not have them. A guest who
    read a different hotel's listing must not be sold something here.
    """
    assert one(message, NUMBERED).room_type_id is expected


def test_a_raw_identifier_from_another_hotel_is_not_honoured():
    """
    The id itself, pasted in. It is a valid room type — somewhere else.
    """
    assert one("book cm-pool for me", NUMBERED).room_type_id is None
    assert one("room_type_id 10000000-0000-4000-8000-000000000001",
               GENERIC).room_type_id is None


# ── ambiguity is asked about, never guessed ─────────────────────────────────

class TestAmbiguity:
    """
    "If ambiguous: ASK. Never guess a business entity." Guessing sells the
    wrong room; asking costs one message.
    """

    def test_a_shared_adjective_identifies_nothing(self):
        found = one("deluxe wala room chahiye", OVERLAPPING)
        assert found.room_type_id is None
        assert found.ask, "no room chosen and no question to ask"

    def test_and_the_question_names_the_candidates(self):
        found = one("deluxe chahiye", OVERLAPPING)
        assert "Deluxe King" in (found.ask or "")
        assert "Deluxe Twin" in (found.ask or "")

    def test_the_real_lowercase_property_is_ambiguous_on_deluxe(self):
        """
        `deluxe room` and `deluxe king bed`, both live in this database. A
        guest saying "deluxe" has named neither.
        """
        found = one("deluxe chahiye", LOWERCASE)
        assert found.room_type_id is None
        assert found.ask

    def test_a_generic_hotel_still_resolves_its_own_generic_names(self):
        """
        The other direction, and the one a too-cautious resolver gets wrong.
        Where "Deluxe" *is* the whole name and nothing else shares it, the
        guest has named it exactly.
        """
        assert one("deluxe chahiye", GENERIC).room_type_id == "g-deluxe"
        assert one("suite chahiye", GENERIC).room_type_id == "g-suite"

    def test_a_number_that_distinguishes_two_names_is_not_dropped(self):
        assert one("suite 1", NUMBERED).room_type_id == "n-suite1"
        assert one("suite 2", NUMBERED).room_type_id == "n-suite2"

    def test_the_bare_word_without_its_number_is_ambiguous(self):
        found = one("suite chahiye", NUMBERED)
        assert found.room_type_id is None
        assert found.ask

    def test_garden_alone_is_ambiguous_where_two_gardens_exist(self):
        found = one("garden wala", OVERLAPPING)
        assert found.room_type_id is None

    def test_one_partial_match_stays_silent_rather_than_interrogating(self):
        """
        "garden wala" names the only garden room here and still resolves to
        nothing — deliberately. `Garden Standard` needs both its significant
        words, and the partial-match branch only asks when *two or more* rooms
        share the hit, so that "I saw a garden" does not become an
        interrogation about the Garden Standard.

        The cost is a guest who half-named a room getting a conversational
        answer instead of a menu, which is the model's job and was the
        behaviour before the resolver existed. Asserted rather than fixed
        because it is a documented trade-off, not an oversight — and pinning it
        means a future change to the partial rule has to be deliberate.
        """
        found = one("garden wala", DISTINCT)
        assert found.room_type_id is None
        assert not found.ask


# ── the languages a guest actually writes in ────────────────────────────────

@pytest.mark.parametrize("message,rooms,expected", [
    ("गार्डन स्टैंडर्ड चाहिए", DISTINCT, "cm-garden"),   # transliterated and matched
    ("Garden Standard चाहिए", DISTINCT, "cm-garden"),
    ("मुझे Pool Villa चाहिए", DISTINCT, "cm-pool"),
    ("Suite 2 चाहिए", NUMBERED, "n-suite2"),
    ("hume deluxe king bed lena hai", LOWERCASE, "l-king"),
    ("Deluxe Twin ka rate kya hai", OVERLAPPING, "o-twin"),
])
def test_across_scripts(message, rooms, expected):
    """
    Devanagari alongside a Latin room name is the commonest real shape — hotels
    name rooms in English and guests write around them.

    A room name transcribed *into* Devanagari resolves too, and better than
    expected: `_fold` transliterates, so गार्डन स्टैंडर्ड becomes "gaardan
    staindard", which the fuzzy matcher scores against "garden standard" at
    0.89. A guest who writes the room's name in their own script gets the room.
    """
    assert one(message, rooms).room_type_id == expected


# ── nothing is claimed from nothing ─────────────────────────────────────────

@pytest.mark.parametrize("message", ["", "   ", "hi", "ok", "kitna hua?",
                                     "breakfast kitne baje tak?", "👍"])
@pytest.mark.parametrize("hotel", sorted(EVERY_HOTEL))
def test_a_message_that_names_no_room_resolves_nothing(message, hotel):
    assert one(message, EVERY_HOTEL[hotel]).room_type_id is None


def test_a_hotel_with_no_rooms_configured_resolves_nothing():
    assert room_names.resolve("Garden Standard", []).room_type_id is None


def test_a_room_row_missing_a_name_is_skipped_not_crashed():
    rooms = [{"id": "x", "name": None}, {"id": "y", "name": ""},
             {"id": "z", "name": "Garden Standard"}]
    assert one("Garden Standard", rooms).room_type_id == "z"


def test_a_room_row_missing_an_id_is_never_returned():
    assert one("Garden Standard", [{"name": "Garden Standard"}]).room_type_id is None


# ── the property that would have caught a tuned threshold ───────────────────

class TestTheSameSentenceAgainstEveryHotel:
    """
    One sentence, five naming schemes. A resolver tuned to one hotel's names
    passes its own tests and fails here — which is the whole reason this file
    exists.
    """

    @pytest.mark.parametrize("hotel", sorted(EVERY_HOTEL))
    def test_a_confident_sentence_never_produces_two_answers(self, hotel):
        found = one("mujhe deluxe wala room chahiye", EVERY_HOTEL[hotel])
        # Either one room, or a question. Never a silent pick from several.
        if found.room_type_id:
            matching = [r for r in EVERY_HOTEL[hotel]
                        if "deluxe" in r["name"].lower()]
            assert len(matching) <= 1, (
                f"{hotel}: picked {found.room_type_id} from {len(matching)} "
                f"rooms containing 'deluxe'")

    @pytest.mark.parametrize("hotel", sorted(EVERY_HOTEL))
    def test_every_configured_name_resolves_to_itself(self, hotel):
        """
        The floor. Whatever the naming scheme, a guest who types a room's exact
        name gets that room — unless another name contains it, in which case a
        question is the correct answer.
        """
        rooms = EVERY_HOTEL[hotel]
        for room in rooms:
            found = one(room["name"], rooms)
            contained_in_another = any(
                other["id"] != room["id"]
                and room["name"].lower() in other["name"].lower()
                for other in rooms)
            if contained_in_another:
                continue
            assert found.room_type_id == room["id"], (
                f"{hotel}: {room['name']!r} did not resolve to itself")
