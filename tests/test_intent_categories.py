"""
Does this guest require a business action, or did they just use a business word?

Every requirement in `required_actions` was answering the second question. The
model matrix caught it on one turn — "Breakfast kitne baje tak milta hai?", the
scenario chosen to require *nothing*, came back owing a quote on 4 of 4 runs on
Gemini 2.5 and 4 of 4 on Gemini 3.x — and identical on both families is what a
system bug looks like when it is being charged to a model.

The noun cannot decide it. A hotel's informational inbox is built almost
entirely out of business nouns: breakfast, check-out, parking, the pool and the
cancellation policy all have prices and rules attached. What separates "what
time is breakfast" from "can you add breakfast" is the verb, and what separates
"is parking free?" from "parking free kar do" is the mood.

This is the matrix the brief asks for: informational against transactional,
across every topic it names, in English, Hindi, Hinglish and Devanagari.

    INFORMATIONAL   owes nothing at all
    STATE_LOOKUP    owes a read of this guest's booking, and nothing else
    TRANSACTIONAL   owes anything the detectors find
    MUTATION        owes the change, never a group band
    NEGOTIATION     owes the rules and the published price
    ESCALATION      owes a person, and is never filtered from any category
"""
import pytest

from app.services.ai import intent, required_actions

# ── the matrix ──────────────────────────────────────────────────────────────
#
# (message, category). Ordered by topic, and every topic the brief names is
# present in at least an informational and a transactional form, because the
# pair is the test — either one alone can be passed by a rule that reads nouns.

INFORMATIONAL = [
    # breakfast
    "What time is breakfast?",
    "Breakfast kitne baje tak milta hai?",
    "breakfast timing kya hai?",
    "नाश्ता कितने बजे तक है?",
    "when does breakfast start",
    "Is breakfast served in the room?",
    "breakfast buffet hai ya a la carte?",
    # check-in / check-out
    "What time is check-in?",
    "checkout kitne baje hai?",
    "check-in kab se hai?",
    "Is early check-in possible?",
    "चेक-इन का समय क्या है?",
    "how does check-in work",
    # parking
    "Do you have parking?",
    "Parking free hai?",
    "parking kahan hai?",
    "Is there valet parking?",
    "पार्किंग है क्या?",
    # wifi
    "What is the wifi password?",
    "wifi hai?",
    "Is wifi available in the rooms?",
    "wifi ka password kya hai?",
    # pool
    "Is the pool open?",
    "Is the pool open tomorrow?",
    "pool khula hai kya?",
    "pool kitne baje tak khula rehta hai?",
    "पूल कब तक खुला है?",
    # restaurant
    "Is the restaurant open?",
    "restaurant kab tak khula rehta hai?",
    "Do you have a restaurant?",
    "restaurant mein veg khana milta hai?",
    # pets
    "Are pets allowed?",
    "pet allowed hai kya?",
    "Can I bring my dog?",
    # policies
    "What is your cancellation policy?",
    "Cancellation policy kya hai?",
    "refund policy kya hai?",
    "रद्द करने का नियम क्या है?",
    "Is smoking allowed?",
    # facilities and place
    "Do you have a gym?",
    "gym kitne baje khulta hai?",
    "Where is the hotel located?",
    "airport se kitni door hai?",
    "how to reach the hotel from the station",
    "Is there a lift?",
    "laundry ka charge kitna hai?",
    "spa hai?",
    "Do you have room service?",
    "AC hai kya rooms mein?",
    "Is there a laundry service?",
    "geyser chalta hai?",
    "Is luggage storage available?",
    "balcony wale rooms hain?",
    "क्या लिफ्ट है?",
    "Is there an ATM nearby?",
    "towel extra milega?",
]

STATE_LOOKUP = [
    "Is breakfast included in my reservation?",
    "Breakfast included hai?",
    "Is parking included in my booking?",
    "meri booking mein breakfast shamil hai?",
    "does my rate include breakfast",
    "Is wifi included?",
    "क्या मेरे बुकिंग में नाश्ता शामिल है?",
]

TRANSACTIONAL = [
    # acquiring
    "Can you add breakfast?",
    "breakfast add kar do",
    "I want to book a room",
    "5 kamre chahiye, 10 log aa rahe hain",
    "Do you have rooms for tomorrow?",
    "2 rooms available hain 20 October ko?",
    "kitna hua?",
    "What's the rate for a deluxe room?",
    "per night kitna padega?",
    "मुझे एक कमरा चाहिए",
    "book kar do 3 raat ke liye",
    "Is the Deluxe room available tomorrow?",
    "total kitna hoga 4 nights ka?",
    "send me the payment link",
    "payment link bhej do",
    "I'd like to reserve a suite",
    "rate kya hai, aur checkout kitne baje hai?",
]

MUTATION = [
    "Remove breakfast from my booking",
    "meri booking cancel karni hai",
    "cancel karna hai",
    "I want to cancel my reservation",
    "Can you change my dates?",
    "dates badal do",
    "मेरी बुकिंग रद्द करनी है",
    "extend my stay by one night",
    "Can I upgrade to a suite?",
    "remove one room from my booking",
]

NEGOTIATION = [
    "Can you give breakfast free?",
    "thoda discount mil sakta hai?",
    "best price kya hai?",
    "kam kar do na",
    "Can you do it cheaper?",
    "breakfast free kar do",
    "कुछ छूट मिलेगी?",
    "4000 mein ho jayega?",
]

ESCALATION_ASKS = [
    "kisi se baat karni hai",
    "I want to talk to a human",
    "manager se baat karao",
    "call me please",
    "put me through to reception staff",
    "ek insaan se baat karwa dijiye",
    "connect me to the manager",
]

MATRIX = (
    [(m, intent.INFORMATIONAL) for m in INFORMATIONAL]
    + [(m, intent.STATE_LOOKUP) for m in STATE_LOOKUP]
    + [(m, intent.TRANSACTIONAL) for m in TRANSACTIONAL]
    + [(m, intent.MUTATION) for m in MUTATION]
    + [(m, intent.NEGOTIATION) for m in NEGOTIATION]
    + [(m, intent.ESCALATION) for m in ESCALATION_ASKS]
)


def test_the_matrix_is_as_large_as_the_brief_asked_for():
    assert len(MATRIX) >= 100, f"only {len(MATRIX)} examples"


@pytest.mark.parametrize("message,expected", MATRIX)
def test_category(message, expected):
    assert intent.category(message) == expected, (
        f"{message!r} read as {intent.category(message)}")


# ── the consequence, which is the actual point ──────────────────────────────

@pytest.mark.parametrize("message", INFORMATIONAL)
def test_an_informational_question_owes_nothing(message):
    """
    Not "owes fewer things" — owes nothing. A question about the pool must not
    price a stay, check availability, quote a group band, take a hold, or read
    a booking.
    """
    assert required_actions.detect(message) == [], (
        f"{message!r} required "
        f"{[r.intent for r in required_actions.detect(message)]}")


@pytest.mark.parametrize("message", STATE_LOOKUP)
def test_a_state_lookup_never_prices_anything(message):
    """
    A guest asking what their booking contains gets their booking read. They do
    not get a quote, an availability search or a group band — those price a
    stay that is already sold.
    """
    priced = {"QUOTE_REQUIRED", "GROUP_BOOKING", "NEGOTIATION", "DATE_CHANGE",
              "GUEST_COUNT_CHANGE", "ROOM_SELECTION"}
    got = {r.intent for r in required_actions.detect(message)}
    assert not (got & priced), f"{message!r} priced something: {got & priced}"


@pytest.mark.parametrize("message", ESCALATION_ASKS)
def test_a_guest_asking_for_a_person_is_never_filtered(message):
    """
    ESCALATION is exempt from every category. It is also *forced*, so a filter
    able to remove it would be a filter able to override a forced stop.
    """
    assert "ESCALATION" in {r.intent for r in required_actions.detect(message)}


class TestTheFilterIsExhaustive:

    def test_every_category_has_an_allowance(self):
        for name in intent.CATEGORIES:
            assert name in intent.ALLOWS, f"{name} would fall through to _ALL"

    def test_every_allowed_name_is_a_real_intent(self):
        """
        `intent` holds intent names as strings so the import runs one way. This
        is what a symbolic import would have bought.
        """
        real = set(required_actions.INTENTS)
        for name, allowed in intent.ALLOWS.items():
            assert allowed <= real, f"{name} allows {allowed - real}, which do not exist"
        assert intent.ALWAYS_PERMITTED <= real

    def test_informational_allows_nothing(self):
        assert intent.ALLOWS[intent.INFORMATIONAL] == frozenset()

    def test_transactional_allows_everything(self):
        assert intent.ALLOWS[intent.TRANSACTIONAL] == set(required_actions.INTENTS) - {
            "ESCALATION"} | {"ESCALATION"}

    def test_escalation_survives_the_strictest_category(self):
        assert intent.permits(intent.INFORMATIONAL, "ESCALATION")

    def test_nothing_else_survives_it(self):
        for name in required_actions.INTENTS:
            if name == "ESCALATION":
                continue
            assert not intent.permits(intent.INFORMATIONAL, name)

    def test_an_unknown_category_permits_everything(self):
        """
        The safe direction. A category this table has not heard of must let the
        detectors through, not silence them.
        """
        assert intent.permits("SOMETHING_NEW", "QUOTE_REQUIRED")


class TestTheDefaultIsTheSafeOne:
    """
    A message the reader cannot place is TRANSACTIONAL, not INFORMATIONAL.
    Guessing informational silences a real business action and loses a sale
    with no trace; guessing transactional costs a tool call the detectors were
    going to make anyway.
    """

    @pytest.mark.parametrize("message", [
        "asdkjhasd", "??", "ok", "hmm", "achha", "...", "👍",
        "मुझे नहीं पता", "whatever",
    ])
    def test_an_unreadable_message_does_not_suppress(self, message):
        assert intent.category(message) == intent.TRANSACTIONAL

    def test_an_empty_message_is_informational(self):
        """The one exception, and it owes nothing because there is nothing."""
        assert intent.category("") == intent.INFORMATIONAL
        assert intent.category(None) == intent.INFORMATIONAL


class TestTwoQuestionsInOneMessage:
    """
    The shape a guest writes more than any other, and the one that broke first.
    Each clause is categorised on its own and the most consequential wins.
    """

    def test_a_price_question_with_a_clock_question_attached(self):
        assert intent.category(
            "rate kya hai, aur checkout kitne baje hai?") == intent.TRANSACTIONAL

    def test_and_it_still_owes_the_price(self):
        got = {r.intent for r in required_actions.detect(
            "rate kya hai, aur checkout kitne baje hai?")}
        assert "QUOTE_REQUIRED" in got

    def test_a_haggle_with_a_timing_question_attached(self):
        assert intent.category(
            "breakfast free kar do aur timing bhi bata dena") == intent.NEGOTIATION

    def test_two_informational_questions_stay_informational(self):
        assert intent.category(
            "breakfast kitne baje tak hai aur pool khula hai kya?"
        ) == intent.INFORMATIONAL

    def test_a_person_asked_for_alongside_anything_else_wins(self):
        assert intent.category(
            "rate kya hai? aur kisi se baat karni hai") == intent.ESCALATION


# ── the audit: every detector, probed with its own keywords ─────────────────

class TestNoDetectorFiresOnAKeywordAlone:
    """
    The brief asks for the whole required_action system to be audited for the
    failure the breakfast turn exposed. This is that audit, kept as a test:
    each detector is given messages containing exactly the words it looks for,
    used in a sense that requires no business action.

    Five of thirty-four probes survived the first pass. Three were real and are
    fixed. Two are deliberately left, and are asserted here as they stand so
    that a future change to either is a decision rather than an accident.
    """

    @pytest.mark.parametrize("message", [
        # escalation words, no request for a person
        "the manager was lovely",
        "reception staff were helpful",
        "I'll call you later",
        # cancellation words, no cancellation
        "what is your cancellation policy?",
        "refund policy kya hai?",
        "I cancelled my flight, not the hotel",
        "cancellation charges kitne hote hain generally?",
        # status words, no booking question
        "payment counter kahan hai?",
        # group words, no group booking
        "10 log ka restaurant mein table milega?",
        "5 log breakfast ke liye aayenge",
        # negotiation words, no haggle
        "free wifi hai?",
        "is parking free?",
        "complimentary breakfast hai?",
        # payment words, no payment
        "kya card payment accept karte ho?",
        "payment ke liye kya options hain?",
        # date words, no stay
        "is the pool open tomorrow?",
        "kal breakfast milega?",
        "tomorrow ka weather kaisa hai?",
        # headcount words, no party
        "2 log restaurant mein baith sakte hain?",
        # price words, no stay to price
        "laundry ka charge kitna hai?",
        "airport kitni door hai?",
    ])
    def test_a_keyword_in_an_innocent_sentence_requires_nothing(self, message):
        found = required_actions.detect(message)
        assert found == [], f"{message!r} required {[r.intent for r in found]}"

    def test_a_headcount_beside_a_facility_is_not_a_group_booking(self):
        """
        The rule `room_number` established, from the other end. "5 log
        breakfast ke liye aayenge" is five people coming *to breakfast*, and it
        raised a group booking for three rooms plus a quote to price them.

        Only the *inferred* leap is blocked. A guest who names rooms has named
        rooms, and a guest who says "group" has said what they mean.
        """
        assert "GROUP_BOOKING" not in {
            r.intent for r in required_actions.detect("5 log breakfast ke liye aayenge")}
        for still_a_group in ("5 kamre chahiye, 10 log aa rahe hain",
                              "we need rooms for 10 people next month",
                              "group booking ke liye baat karni hai",
                              "10 people staying, breakfast included?",
                              "हमें 5 कमरे चाहिए, 10 लोग आ रहे हैं"):
            assert "GROUP_BOOKING" in {
                r.intent for r in required_actions.detect(still_a_group)}, still_a_group


class TestTheTwoLeftAlone:
    """
    Two probes still raise a requirement and both are judgement calls rather
    than defects. Asserted as they are so a change to either is deliberate.
    """

    def test_an_unattached_group_of_ten_still_offers_group_rates(self):
        """
        "we are a group of 10 sightseeing tomorrow" raises GROUP_BOOKING.

        Strictly it is a statement, not a request. Left because the cost is
        asymmetric in the useful direction: `get_group_rates` is a read that
        takes no hold and moves no stock, and a hotel told that ten people are
        coming would rather its agent knew the band than not. The failure it
        risks is an unprompted offer; the failure it prevents is missing the
        largest booking of the week.
        """
        assert "GROUP_BOOKING" in {r.intent for r in required_actions.detect(
            "we are a group of 10 sightseeing tomorrow")}

    def test_asking_the_fee_to_change_rooms_still_prices_the_stay(self):
        """
        "room change karne ka charge?" raises QUOTE_REQUIRED.

        The guest asked for a change fee and what they will be handed is a stay
        price, which is not the same number. Left because the category is
        MUTATION and a modification does genuinely need re-pricing — the tool
        that answers a change fee properly does not exist yet, and forcing the
        nearest read is better than forcing nothing while the model invents a
        figure. Worth revisiting when there is a fee tool to route it to.
        """
        assert "QUOTE_REQUIRED" in {r.intent for r in required_actions.detect(
            "room change karne ka charge?")}
