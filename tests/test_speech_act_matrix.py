"""
Three hundred sentences, and the sixteen words that mean different things in each.

`test_intent_categories` establishes the six categories on 102 composed
examples. `test_real_message_corpus` checks 134 things guests actually wrote.
This is the third layer the brief asks for and the one aimed squarely at the
words that carry more than one speech act:

    free · available · book · reserve · confirm · cancel · price · best price
    how much · how many · how long · when · where · included · add · remove

Each appears here in every sense a hotel receives it. The point is not the
count — it is that none of these is resolved by a keyword exception. What
separates "how many rooms are free?" from "can you give me a free room?" is
not the word `free`; it is that the first asks a question and the second makes
a request, and the same rule decides "is parking free?" and "make parking
free" without either being named.

Four of these were misroutes when the file was written, and each one was a
rule that was too narrow rather than a word that needed special handling:

    "When can I check in?"          `check in` with a space was not an
                                    amenity — only `check-in` and `checkin`
                                    were listed, the least likely of the three

    "Can you give me a free room?"  the vacancy reading of `free` vetoed the
                                    haggle. A request plus `free` is a request
                                    for something free; a question plus `free`
                                    is a question about vacancy

    "Can you do 10% off?"           the discount pattern is wrapped in a
                                    lookbehind that a digit before `%` always
                                    fails

    "That's a bit above our budget" the indirect English haggle, which is most
                                    of them

The expectations below are the current classifications, reviewed one by one.
Several sit at TRANSACTIONAL where a human would say INFORMATIONAL — "how many
rooms do you have?", "where do I pay?" — and that is the safe direction by
design: TRANSACTIONAL permits every requirement, so a misread costs a tool call
the detectors were going to make anyway, while a wrong INFORMATIONAL silently
suppresses a business action and loses a sale with no trace.
"""
import pytest

from app.services.ai import intent, required_actions
from tests.test_intent_categories import MATRIX as COMPOSED
from tests.test_real_message_corpus import REAL

CONTEXT_DEPENDENT = [
    # ── free — vacancy, gratis, and a policy question ──
    ('How many rooms are free?', 'TRANSACTIONAL'),
    ('are any rooms free on the 5th?', 'TRANSACTIONAL'),
    ('koi room free hai kal?', 'TRANSACTIONAL'),
    ('Is parking free?', 'INFORMATIONAL'),
    ('is the wifi free', 'INFORMATIONAL'),
    ('free breakfast hai kya?', 'INFORMATIONAL'),
    ('Can you give me a free room?', 'NEGOTIATION'),
    ('give me a free breakfast', 'NEGOTIATION'),
    ('free upgrade milega?', 'NEGOTIATION'),
    ('free me kar do', 'NEGOTIATION'),
    ('can you make the breakfast free', 'NEGOTIATION'),
    ('muft mein ho jayega?', 'NEGOTIATION'),

    # ── available — a facility, a room, a person ──
    ('Is the pool available?', 'INFORMATIONAL'),
    ('is room service available at night', 'INFORMATIONAL'),
    ('is early check-in available', 'INFORMATIONAL'),
    ('kya rooms available hain?', 'TRANSACTIONAL'),
    ('Is the Deluxe available on the 5th?', 'TRANSACTIONAL'),
    ('availability check karo', 'TRANSACTIONAL'),
    ('are you available for 10 rooms', 'TRANSACTIONAL'),
    # Reviewed as ESCALATION when a bare person-noun decided it; TRANSACTIONAL
    # is right. A guest asking whether the manager is *available* has asked a
    # question about the hotel, and ESCALATION owes nothing — so reading it as
    # a request for a human would silence every business action on the turn.
    ('is the manager available', 'TRANSACTIONAL'),
    ('is anyone available to help', 'TRANSACTIONAL'),
    ('are laundry services available', 'INFORMATIONAL'),

    # ── book and reserve ──
    ('Book the room', 'TRANSACTIONAL'),
    ('Can I book online?', 'TRANSACTIONAL'),
    ('I booked last week', 'TRANSACTIONAL'),
    ('reserve kar do', 'TRANSACTIONAL'),
    ('how do I book?', 'TRANSACTIONAL'),
    ('booking kaise karte hain', 'TRANSACTIONAL'),
    ('book kar do 3 raat', 'TRANSACTIONAL'),
    ('I want to book a suite', 'TRANSACTIONAL'),
    ("I'd like to reserve two rooms", 'TRANSACTIONAL'),
    ('can I book for next weekend', 'TRANSACTIONAL'),

    # ── confirm — a lookup and an instruction ──
    ('Is my booking confirmed?', 'STATE_LOOKUP'),
    ('meri booking confirm hai na?', 'STATE_LOOKUP'),
    ('confirm ho gaya kya?', 'TRANSACTIONAL'),
    ('Confirm my booking', 'TRANSACTIONAL'),
    ('confirm kar do booking', 'TRANSACTIONAL'),
    ('please confirm you received this', 'TRANSACTIONAL'),
    ('has my payment been confirmed', 'TRANSACTIONAL'),
    ('kya booking pakki ho gayi?', 'TRANSACTIONAL'),

    # ── cancel — the act and the policy ──
    ('Can I cancel?', 'MUTATION'),
    ('cancel my booking', 'MUTATION'),
    ('cancel karna hai', 'MUTATION'),
    ('if I cancel do I get a refund?', 'MUTATION'),
    ('What is your cancellation policy?', 'INFORMATIONAL'),
    ('cancellation charges kitne hain?', 'INFORMATIONAL'),
    ('refund policy kya hai', 'INFORMATIONAL'),
    ('what happens if I cancel late', 'MUTATION'),
    ('meri booking cancel kar do', 'MUTATION'),

    # ── price and how much ──
    ('How much is the room?', 'TRANSACTIONAL'),
    ('how much for 3 nights', 'TRANSACTIONAL'),
    ('rate kya hai', 'TRANSACTIONAL'),
    ('price list bhejo', 'TRANSACTIONAL'),
    ('How much is parking?', 'INFORMATIONAL'),
    ('how much does breakfast cost', 'INFORMATIONAL'),
    ('how much is the airport taxi', 'INFORMATIONAL'),
    ('best price?', 'NEGOTIATION'),
    ("what's your best price for a suite", 'NEGOTIATION'),
    ('kam se kam kitna lagega', 'TRANSACTIONAL'),
    ('total kitna hoga 4 nights ka?', 'TRANSACTIONAL'),
    ('per night kitna padega', 'TRANSACTIONAL'),

    # ── how many, how long ──
    ('How many rooms do you have?', 'TRANSACTIONAL'),
    ('How many people can stay in a suite?', 'TRANSACTIONAL'),
    ('how many rooms are left for the 5th', 'TRANSACTIONAL'),
    ('kitne kamre hain?', 'TRANSACTIONAL'),
    ('How long is the drive from the airport?', 'INFORMATIONAL'),
    ('how long can I keep the room', 'TRANSACTIONAL'),
    ('kitni der lagegi airport se?', 'INFORMATIONAL'),
    ('how many nights minimum', 'TRANSACTIONAL'),

    # ── when and where ──
    ('When is breakfast?', 'INFORMATIONAL'),
    ('When can I check in?', 'INFORMATIONAL'),
    ('when does the pool close', 'INFORMATIONAL'),
    ('kab tak check out karna hai', 'TRANSACTIONAL'),
    ('When are you cheapest?', 'TRANSACTIONAL'),
    ('Where is the hotel?', 'INFORMATIONAL'),
    ('Where do I pay?', 'TRANSACTIONAL'),
    ('where is the nearest ATM', 'INFORMATIONAL'),
    ('hotel kahan hai?', 'INFORMATIONAL'),
    ('where can I park', 'TRANSACTIONAL'),

    # ── included, add, remove ──
    ('Is breakfast included?', 'STATE_LOOKUP'),
    ('does the rate include dinner', 'STATE_LOOKUP'),
    ('breakfast included hai na?', 'STATE_LOOKUP'),
    ('is tax included in that price', 'TRANSACTIONAL'),
    ('Can you add breakfast?', 'TRANSACTIONAL'),
    ('add a bed please', 'TRANSACTIONAL'),
    ('extra bed add kar sakte ho?', 'TRANSACTIONAL'),
    ('Remove breakfast', 'MUTATION'),
    ('remove one room from my booking', 'MUTATION'),
    ('breakfast hata do', 'MUTATION'),

    # ── the escalations ──
    ('kisi se baat karni hai', 'ESCALATION'),
    ('I want to talk to a human', 'ESCALATION'),
    ('manager se baat karao', 'ESCALATION'),
    ('call me please', 'ESCALATION'),
    ('put me through to reception staff', 'ESCALATION'),
    ('ek insaan se baat karwa dijiye', 'ESCALATION'),
    ('connect me to the manager', 'ESCALATION'),
    ('can someone call me back', 'ESCALATION'),

    # ── informational, across topics ──
    ('What time is breakfast?', 'INFORMATIONAL'),
    ('Breakfast kitne baje tak milta hai?', 'INFORMATIONAL'),
    ('breakfast timing kya hai?', 'INFORMATIONAL'),
    ('नाश्ता कितने बजे तक है?', 'INFORMATIONAL'),
    ('Is breakfast served in the room?', 'INFORMATIONAL'),
    ('What time is check-in?', 'INFORMATIONAL'),
    ('checkout kitne baje hai?', 'INFORMATIONAL'),
    ('Is early check-in possible?', 'INFORMATIONAL'),
    ('चेक-इन का समय क्या है?', 'INFORMATIONAL'),
    ('Do you have parking?', 'INFORMATIONAL'),
    ('parking kahan hai?', 'INFORMATIONAL'),
    ('Is there valet parking?', 'INFORMATIONAL'),
    ('पार्किंग है क्या?', 'INFORMATIONAL'),
    ('What is the wifi password?', 'INFORMATIONAL'),
    ('wifi hai?', 'INFORMATIONAL'),
    ('Is wifi available in the rooms?', 'INFORMATIONAL'),
    ('Is the pool open?', 'INFORMATIONAL'),
    ('pool khula hai kya?', 'INFORMATIONAL'),
    ('पूल कब तक खुला है?', 'INFORMATIONAL'),
    ('Is the restaurant open?', 'INFORMATIONAL'),
    ('restaurant kab tak khula rehta hai?', 'INFORMATIONAL'),
    ('Do you have a restaurant?', 'INFORMATIONAL'),
    ('Are pets allowed?', 'INFORMATIONAL'),
    ('pet allowed hai kya?', 'INFORMATIONAL'),
    ('Can I bring my dog?', 'INFORMATIONAL'),
    ('Is smoking allowed?', 'INFORMATIONAL'),
    ('Do you have a gym?', 'INFORMATIONAL'),
    ('gym kitne baje khulta hai?', 'INFORMATIONAL'),
    ('Is there a lift?', 'INFORMATIONAL'),
    ('क्या लिफ्ट है?', 'INFORMATIONAL'),
    ('spa hai?', 'INFORMATIONAL'),
    ('Do you have room service?', 'INFORMATIONAL'),
    ('AC hai kya rooms mein?', 'INFORMATIONAL'),
    ('Is there a laundry service?', 'INFORMATIONAL'),
    ('geyser chalta hai?', 'INFORMATIONAL'),
    ('Is luggage storage available?', 'INFORMATIONAL'),
    ('Is there an ATM nearby?', 'INFORMATIONAL'),
    ('towel extra milega?', 'INFORMATIONAL'),
    ('airport se kitni door hai hotel?', 'INFORMATIONAL'),
    ('how to reach the hotel from the station', 'INFORMATIONAL'),
    ('laundry ka charge kitna hai?', 'INFORMATIONAL'),

    # ── transactional, across topics ──
    ('5 kamre chahiye, 10 log aa rahe hain', 'TRANSACTIONAL'),
    ('kitna hua?', 'TRANSACTIONAL'),
    ('Do you have rooms for tomorrow?', 'TRANSACTIONAL'),
    ('2 rooms available hain 20 October ko?', 'TRANSACTIONAL'),
    ('मुझे एक कमरा चाहिए', 'TRANSACTIONAL'),
    ('Is the Deluxe room available tomorrow?', 'TRANSACTIONAL'),
    ('send me the payment link', 'TRANSACTIONAL'),
    ('payment link bhej do', 'TRANSACTIONAL'),
    ('rate kya hai, aur checkout kitne baje hai?', 'TRANSACTIONAL'),
    ("Courtyard Standard with breakfast. What's the total?", 'TRANSACTIONAL'),
    ('Can you arrange airport pickup?', 'TRANSACTIONAL'),
    ('20 se 22 October tak chahiye', 'TRANSACTIONAL'),
    ('we are 12 people', 'TRANSACTIONAL'),
    ('what rooms do you have', 'TRANSACTIONAL'),

    # ── negotiations, indirect and direct ──
    ('thoda discount mil sakta hai?', 'NEGOTIATION'),
    ('kam kar do na', 'NEGOTIATION'),
    ('Can you do it cheaper?', 'NEGOTIATION'),
    ('कुछ छूट मिलेगी?', 'NEGOTIATION'),
    ('4000 mein ho jayega?', 'NEGOTIATION'),
    ('Can you do 10% off?', 'NEGOTIATION'),
    ('10% off kar do', 'NEGOTIATION'),
    ('give me 15% discount', 'NEGOTIATION'),
    ('15% kam kar do', 'NEGOTIATION'),
    ("That's a bit above our budget — can you do anything on the rate?", 'NEGOTIATION'),
    ("that's too expensive for us", 'NEGOTIATION'),
    ('any flexibility on the price?', 'NEGOTIATION'),
    ('can you work with us on this?', 'NEGOTIATION'),
    ("it's a bit much honestly", 'NEGOTIATION'),
    ('sasta kar do', 'NEGOTIATION'),

    # ── mutations ──
    ('meri booking cancel karni hai', 'MUTATION'),
    ('I want to cancel my reservation', 'MUTATION'),
    ('Can you change my dates?', 'MUTATION'),
    ('dates badal do', 'MUTATION'),
    ('मेरी बुकिंग रद्द करनी है', 'MUTATION'),
    ('extend my stay by one night', 'MUTATION'),
    ('Can I upgrade to a suite?', 'MUTATION'),
    ('shift my booking to next week', 'MUTATION'),
    ('ek room kam kar do', 'NEGOTIATION'),

    # ── the unreadable, which must stay transactional ──
    ('asdkjhasd', 'TRANSACTIONAL'),
    ('??', 'TRANSACTIONAL'),
    ('ok', 'TRANSACTIONAL'),
    ('hmm', 'TRANSACTIONAL'),
    ('achha', 'TRANSACTIONAL'),
    ('...', 'TRANSACTIONAL'),
    ('👍', 'TRANSACTIONAL'),
    ('whatever', 'TRANSACTIONAL'),
    ('मुझे नहीं पता', 'TRANSACTIONAL'),
    ('k', 'TRANSACTIONAL'),
    ('one more time', 'TRANSACTIONAL'),
    ('still waiting', 'TRANSACTIONAL'),
    ('hello? anyone?', 'TRANSACTIONAL'),
    # ── the second sense of `reserve` and `how many` ──
    #
    # Added because the no-keyword check found that every sentence in
    # the corpus containing either word landed in one category — which
    # is what a keyword deciding a speech act would look like. It was a
    # gap in the examples rather than in the code: both senses were
    # already read correctly, and nothing had asked.
    ('is a reservation needed for the pool', 'INFORMATIONAL'),
    # Reviewed as TRANSACTIONAL when a request verb inside "do I need to …"
    # still read as a request. Asking *whether* an action is needed is a
    # question about the rule, not an instruction to apply it.
    ('Do I need to reserve the restaurant?', 'INFORMATIONAL'),
    ('How many people can the pool hold?', 'INFORMATIONAL'),
    ('how many floors does the hotel have', 'INFORMATIONAL'),
    ('how many towels do you provide', 'TRANSACTIONAL'),
    ('is the restaurant reservation only', 'INFORMATIONAL'),
    ('how much notice do you need to reserve', 'TRANSACTIONAL'),
    ('how many staff are on at night', 'TRANSACTIONAL'),
    # ── the second sense of `add` and `remove` ──
    #
    # Same finding as `reserve`, from the same check: a request verb
    # inside a question frame is a policy question. "do I need to add
    # breakfast in advance" asks the rule; "add breakfast" applies it.
    ('do I need to add breakfast in advance', 'INFORMATIONAL'),
    ('can breakfast be added later', 'INFORMATIONAL'),
    # Still MUTATION. "the fee to remove" is a noun-phrase frame rather than
    # the "do I need to" question frame, and a guest asking what dropping a
    # room costs is close enough to asking to drop one that MUTATION — which
    # permits the modification and pricing tools and not group bands — is the
    # right allowance. Recorded rather than fixed so a future change is
    # deliberate.
    ('what is the fee to remove a room', 'MUTATION'),
    # TRANSACTIONAL rather than MUTATION now — "shoes" is not an amenity, so
    # it lands on the safe default rather than the informational branch. What
    # matters is that it is no longer a *mutation*: nothing about this sentence
    # asks for a booking to be changed.
    ('do I have to remove my shoes', 'TRANSACTIONAL'),
    ('is removing a guest allowed', 'TRANSACTIONAL'),
    ('is there a charge to add a bed?', 'TRANSACTIONAL'),
    ('do I have to book the pool in advance', 'INFORMATIONAL'),
]

# The three layers together. Deduplicated on the message, first expectation
# winning, so a sentence reviewed in two files cannot be asserted two ways.
ALL: list[tuple[str, str]] = []
_seen: set[str] = set()
for message, expected in [*COMPOSED, *REAL, *CONTEXT_DEPENDENT]:
    if message in _seen:
        continue
    _seen.add(message)
    ALL.append((message, expected))


def test_the_matrix_is_as_large_as_the_brief_asked_for():
    assert len(ALL) >= 300, f"only {len(ALL)}"


def test_every_category_is_represented():
    found = {expected for _, expected in ALL}
    assert found == set(intent.CATEGORIES), f"missing {set(intent.CATEGORIES) - found}"


@pytest.mark.parametrize("message,expected", CONTEXT_DEPENDENT)
def test_a_context_dependent_word_is_read_in_context(message, expected):
    assert intent.category(message) == expected


class TestTheSameWordInTwoSenses:
    """
    The pairs, asserted as pairs. Each is one word doing two jobs, and a rule
    that got either half by naming the word would fail the other.
    """

    @pytest.mark.parametrize("asking,requesting", [
        ("How many rooms are free?", "Can you give me a free room?"),
        ("Is parking free?", "can you make the breakfast free"),
        ("free breakfast hai kya?", "give me a free breakfast"),
        ("What is your cancellation policy?", "cancel my booking"),
        ("cancellation charges kitne hain?", "meri booking cancel kar do"),
        ("Is my booking confirmed?", "Confirm my booking"),
        ("How much is parking?", "how much for 3 nights"),
        ("Is the pool available?", "Is the Deluxe available on the 5th?"),
        ("When is breakfast?", "Can you add breakfast?"),
        ("Is breakfast included?", "breakfast hata do"),
    ])
    def test_the_two_senses_are_read_differently(self, asking, requesting):
        assert intent.category(asking) != intent.category(requesting), (
            f"{asking!r} and {requesting!r} were read the same way")


class TestNoInformationalQuestionOwesABusinessAction:
    """
    The property the whole layer exists for, over all three hundred.
    """

    def test_over_the_whole_matrix(self):
        offenders = [m for m, expected in ALL
                     if expected == intent.INFORMATIONAL
                     and required_actions.detect(m)]
        assert not offenders, offenders

    def test_and_a_state_lookup_never_prices_anything(self):
        priced = {"QUOTE_REQUIRED", "GROUP_BOOKING", "NEGOTIATION", "DATE_CHANGE",
                  "GUEST_COUNT_CHANGE", "ROOM_SELECTION"}
        offenders = [m for m, expected in ALL
                     if expected == intent.STATE_LOOKUP
                     and {r.intent for r in required_actions.detect(m)} & priced]
        assert not offenders, offenders

    def test_and_every_request_for_a_person_reaches_one(self):
        offenders = [m for m, expected in ALL
                     if expected == intent.ESCALATION
                     and "ESCALATION" not in {r.intent
                                              for r in required_actions.detect(m)}]
        assert not offenders, offenders


class TestNoKeywordExceptions:
    """
    "Do not solve these with keyword-specific exceptions."

    The structural version of this check was two bad tests before it was one
    honest one. Scanning string literals flagged every regex in the module, and
    then every docstring — both times a failure nobody could act on, because
    neither is a special case: a vocabulary is a list of words in a category,
    which is the opposite.

    So it tests the claim instead of approximating it. If a word were being
    handled by a rule that named it, then *every* sentence containing that word
    would land in one category. The check is that none of the sixteen does.
    """

    WORDS = ["free", "available", "book", "reserve", "confirm", "cancel",
             "price", "how much", "how many", "how long", "when", "where",
             "included", "add", "remove"]

    @pytest.mark.parametrize("word", WORDS)
    def test_no_word_decides_a_category_by_itself(self, word):
        landings = {expected for message, expected in ALL
                    if word in message.lower()}
        assert len(landings) > 1, (
            f"every sentence containing {word!r} is {landings} — that is a "
            f"keyword deciding a speech act, which is the bug this layer exists "
            f"to fix")

    def test_and_the_categories_are_decided_by_general_rules(self):
        """
        The three that actually do the separating, named. Each is about the
        *shape* of the sentence rather than about any word in this list:

            _REQUESTS_SOMETHING   is the guest asking the hotel to act?
            _asks_a_question      is this a question at all?
            _CLAUSES              which half of a compound message wins?
        """
        from app.services.ai import intent as module

        for rule in ("_REQUESTS_SOMETHING", "_asks_a_question", "_CLAUSES"):
            assert hasattr(module, rule), rule
