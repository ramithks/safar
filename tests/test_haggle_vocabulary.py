"""
The same business intent, written every way a guest actually writes it.

**Why this suite exists separately from the conversation tests.** Everything the
architecture promises rests on one deterministic step: before the model is
consulted at all, `required_actions.detect` reads the guest's own words and
decides what the turn *owes* — which pricing tool must run, whether a person was
asked for, whether a number was named. A model that behaves perfectly cannot
rescue a turn whose requirement was never detected, because nothing will notice
if it does not. So the vocabulary's coverage is a reliability property in its
own right, and it is measurable without a model, a network or a credit.

**What a miss costs.** A discount ask that is not detected is a turn where the
pricing engine is never asked and the model answers a price question out of its
own head — the exact failure the whole required-action layer was built to close.
An escalation ask that is not detected is a guest who asked for a human and got
a chatbot; §33 asks for 100% of those, and the sweep that produced this file
found the brief's own example missing.

**Written as families, not as cases.** Each family is one business meaning and
the ways guests write it. A vocabulary tested one phrase at a time grows one
phrase at a time; tested as a family, the question becomes "does this pattern
generalise", which is the question §34 asks for. Every entry below is a real
paraphrase — Hinglish, Devanagari, typos, abbreviations, and the shorthand
people actually type on a phone.
"""
import pytest

from app.services.ai import required_actions as ra

# A conversation that has been priced: a room chosen, dates set, a quote given.
# This is the state nearly every haggle happens in.
PRICED = {
    "check_in": "2026-09-10", "check_out": "2026-09-12", "adults": 2,
    "room_type_id": "rt-1", "rate_plan_id": "rp-1", "stage": "quoted",
}


# ── the guest is pushing on the price ───────────────────────────────────────

WANTS_A_DISCOUNT = [
    # English, plain
    "best price?", "best rate?", "best deal?", "what's your best price",
    "final price?", "last price?", "lowest price?", "lowest?", "final?",
    "any discount?", "any deal?", "any offer?", "is there a discount",
    "can you do better?", "can you do better", "do better on the price",
    "too expensive", "that's expensive", "very expensive",
    "cheaper option?", "anything cheaper", "cheapest room",
    "corporate rate?", "long stay discount?", "bulk discount?",
    "give me a concession", "any rebate?", "special rate?", "special price?",
    # Hinglish
    "best rate kya doge", "bhai best rate kya hai", "thoda kam kar do",
    "thoda kam?", "kam karo na", "kam kar dijiye", "kuch kam hoga?",
    "aur kam?", "price kam karo", "rate kam karo", "sasta karo",
    "koi discount?", "discount milega?", "kuch discount karo",
    "bahut mehenga hai", "ye to mehnga hai", "zyada hai bhai",
    "kam se kam kitna", "offer chal raha hai kya?", "koi offer hai?",
    "kuch aur karo", "kuch to karo", "adjust kar do",
    "negotiate kar sakte hain?",
    # a percentage, which is not a word and so could not be listed
    "give me 70% off", "20 percent discount do", "10% kam karo",
    # Devanagari
    "कम कर दीजिए", "कोई छूट है?", "सस्ता कर दो", "बहुत महंगा है",
    "कम से कम कितना", "आखिरी रेट बताइए",
]

NOT_A_DISCOUNT = [
    # Price *questions* are not haggles — they are answered by a quote, and
    # opening a negotiation on one volunteers a discount nobody asked for.
    "what is the price?", "rate kya hai?", "kitna lagega?", "how much per night",
    # Percentages that are facts about the hotel, not asks.
    "18% GST lagega kya?", "70% occupancy hai aaj?",
    # Words that merely contain a haggling word.
    "check out time kya hai final?", "is this the final confirmation?",
    # Nothing to do with money at all.
    "breakfast included hai?", "parking hai?", "kitne baje check in?",
]


class TestAskingForALowerPrice:
    @pytest.mark.parametrize("message", WANTS_A_DISCOUNT)
    def test_every_way_a_guest_pushes_on_price_is_one_intent(self, message):
        assert ra.wants_discount(message), f"not read as a haggle: {message!r}"

    @pytest.mark.parametrize("message", NOT_A_DISCOUNT)
    def test_and_a_price_question_is_not_one(self, message):
        assert not ra.wants_discount(message), f"read as a haggle: {message!r}"

    @pytest.mark.parametrize("message", WANTS_A_DISCOUNT)
    def test_a_priced_conversation_owes_the_engine_a_call(self, message):
        """
        The point of detecting it. Every one of these must reach
        `negotiate_rate` — by the model's choice or the application's — because
        the alternative is the model naming a price itself.
        """
        tools = {r.tool for r in ra.detect(message, PRICED)}
        assert "negotiate_rate" in tools, f"no negotiation required for {message!r}"


# ── the guest named a figure ────────────────────────────────────────────────

NAMES_A_PRICE = [
    ("4000 mein ho jayega?", 4000),
    ("₹4,000 mein?", 4000),
    ("₹4000 possible?", 4000),
    ("Rs 4000?", 4000),
    ("rs. 4000 mein kar do", 4000),
    ("4,000 rupees?", 4000),
    ("4000 rupaye tak", 4000),
    ("4k?", 4000),
    ("4k mein ho jayega", 4000),
    ("make it 3500", 3500),
    ("3500 kar do", 3500),
    ("kar do 3500", 3500),
    ("can you do 4000", 4000),
    ("how about 4500", 4500),
    ("at 4000?", 4000),
    ("4000 tak ho sakta hai?", 4000),
    ("4000 per night?", 4000),
    ("budget 5000 hai", 5000),
    ("2500 ke andar chahiye", 2500),
]

NOT_A_PRICE = [
    # A year, which is written exactly like a budget.
    "2026 mein aa rahe hain",
    "September 2026 mein aana hai",
    # A room, a phone number, a pincode.
    "room 204 mein hain",
    "my phone is 9876543210",
    "pincode 403509 hai",
    # A count.
    "2 rooms chahiye",
]


class TestNamingAFigure:
    @pytest.mark.parametrize("message,expected", NAMES_A_PRICE)
    def test_the_figure_is_read_out_whole(self, message, expected):
        """
        The guest's number is the one thing about a haggle that must travel
        exactly: it goes *into* `negotiate_rate` as a question and never comes
        out of the model as an answer.
        """
        assert ra.offered_price(message) == expected

    @pytest.mark.parametrize("message", NOT_A_PRICE)
    def test_a_number_that_is_not_money_is_not_an_offer(self, message):
        """
        Passing one of these to the pricing engine produces a verdict about a
        stay nobody proposed. "2026 mein aa rahe hain" read as an offer of
        ₹2,026 before this suite existed.
        """
        assert ra.offered_price(message) is None, f"{message!r} read as money"

    @pytest.mark.parametrize("message,expected", NAMES_A_PRICE)
    def test_naming_a_figure_is_itself_a_haggle(self, message, expected):
        assert ra.wants_discount(message)


# ── the guest asked for a person ────────────────────────────────────────────

ASKS_FOR_A_PERSON = [
    # The brief's own example, which this vocabulary did not contain.
    "human se baat karni hai",
    "Human se baat karni hai.",
    "human se baat karna hai",
    "human se baat karwao",
    "manager se baat karwao",
    "manager se baat karni hai",
    "owner se baat karao",
    "kisi se baat karao",
    "kisi insaan se baat karni hai",
    "insaan se baat karao",
    "staff se baat karni hai",
    "reception se baat karao",
    "reception ko bulao",
    "manager ko bulao",
    "hotel wale se baat karao",
    "koi se baat karao",
    # English
    "talk to a human",
    "I want to talk to a person",
    "speak to the manager",
    "can I speak to someone",
    "let me talk to staff",
    "connect me to a person",
    "transfer me to someone",
    "put me through to the team",
    "I want a real person",
    "give me a live agent",
    # A request for a call is a request for a person.
    "someone call me",
    "call me please",
    "please call back",
    "phone kar do",
]

NOT_ASKING_FOR_A_PERSON = [
    "best price?",
    "can you discount?",
    "what's breakfast?",
    "I need a room",
    "2 log hain",
    "koi room hai?",
    "kisi bhi din aa sakte hain",
]


class TestAskingForAHuman:
    @pytest.mark.parametrize("message", ASKS_FOR_A_PERSON)
    def test_an_explicit_request_for_a_person_is_always_recognised(self, message):
        """
        §33 asks for 100% of these in the tested corpus. It is the one number
        with no acceptable shortfall: the guest said the quiet part out loud and
        was answered by a machine.
        """
        assert ra._ESCALATION_ASK.search(message), f"missed: {message!r}"

    @pytest.mark.parametrize("message", ASKS_FOR_A_PERSON)
    def test_and_the_turn_owes_a_handoff(self, message):
        tools = {r.tool for r in ra.detect(message, PRICED)}
        assert "request_human_handoff" in tools, f"no handoff required for {message!r}"

    @pytest.mark.parametrize("message", NOT_ASKING_FOR_A_PERSON)
    def test_and_an_ordinary_question_does_not(self, message):
        """
        The other half of §33: zero unjustified guest-requested handoffs. A
        vocabulary loose enough to catch every ask is a vocabulary that ends
        sales.
        """
        assert not ra._ESCALATION_ASK.search(message), f"false positive: {message!r}"


# ── the guest wants more than one room ──────────────────────────────────────

COUNTS_PEOPLE = [
    ("we are 10 people", 10), ("10 log hain", 10), ("4 adults", 4),
    # "there are 12 of us" — the commonest way an English group says its size,
    # and it names no noun at all, so both of the patterns built around a word
    # for people missed it. A twelve-person enquiry raised no requirement and
    # reached no group pricing. Found by a long journey: the guest opened with
    # "we're 10 people" (detected) and corrected to "actually 12 of us now"
    # (not), so the band silently stopped following the party.
    ("there are 12 of us", 12), ("actually 12 of us now", 12),
    ("we're 10 of us", 10), ("12 of us are coming", 12), ("2 of us", 2),
]

NOT_A_PARTY_SIZE = [
    # Idiomatic English for "a member of our group". Reading it as a party of
    # one would overwrite a party of twelve on the session with a 1, and a guest
    # who really is alone says "just me" or "1 adult".
    "one of us is vegetarian",
    "1 of us needs a cot",
    "one of us arrives late",
]


class TestCountingPeople:
    @pytest.mark.parametrize("message,expected", COUNTS_PEOPLE)
    def test_the_party_size_is_read(self, message, expected):
        assert ra.people_count(message) == expected, message

    @pytest.mark.parametrize("message", NOT_A_PARTY_SIZE)
    def test_and_an_idiom_is_not_a_count(self, message):
        assert ra.people_count(message) is None, message


WANTS_ROOMS = [
    ("5 rooms chahiye", 5), ("5 kamre chahiye", 5), ("5 room chaye", 5),
    ("need 5 rooms", 5), ("humein 5 rooms chahiye", 5),
    ("5 कमरे चाहिए", 5), ("हमें 5 कमरे चाहिए", 5),
    ("10 rooms ke liye best rate?", 10), ("need two rooms", 2),
    ("3 rooms book karne hain", 3), ("do kamre chahiye", 2),
    ("paanch rooms", 5), ("twenty rooms for a wedding", 20),
]


class TestCountingRooms:
    @pytest.mark.parametrize("message,expected", WANTS_ROOMS)
    def test_the_count_is_read_whatever_script_it_is_in(self, message, expected):
        assert ra.room_count(message) == expected, f"{message!r}"

    @pytest.mark.parametrize("message,expected", WANTS_ROOMS)
    def test_more_than_two_rooms_is_a_group_and_owes_group_pricing(self, message, expected):
        if expected < 3:
            pytest.skip("two rooms is not a group at this property")
        tools = {r.tool for r in ra.detect(message, PRICED)}
        assert "get_group_rates" in tools, f"{message!r} did not require group pricing"


# ── the four scripts this product is written in ─────────────────────────────

class TestTheSameSentenceInFourScripts:
    """
    §23. The business intent must converge whatever alphabet it arrives in —
    English, Roman Hindi, Devanagari, and the mixture that is how most guests
    actually type.
    """

    @pytest.mark.parametrize("family", [
        ("I need 5 rooms", "5 rooms chahiye", "5 kamre chaiye", "5 कमरे चाहिए"),
        ("give me a discount", "discount do", "kam kar do", "कम कर दो"),
    ])
    def test_one_meaning_one_requirement(self, family):
        detected = [tuple(sorted(r.tool for r in ra.detect(m, PRICED))) for m in family]
        assert len(set(detected)) == 1, (
            f"the same request produced different requirements: "
            f"{list(zip(family, detected))}"
        )
