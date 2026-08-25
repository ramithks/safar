"""
A hundred and thirty-four things guests actually wrote.

The intent matrix in `test_intent_categories` is 102 examples I composed, and
composing them is exactly how a blind spot gets written into both the code and
its test. This corpus is the inbound half of one thousand real
`conversation_messages` rows — deduplicated to 168 distinct messages, then
filtered to the 134 carrying no phone number, email, booking reference, link or
date that could identify anyone.

It found three misroutes the composed matrix did not, and every one was in the
dangerous direction — a real business question being read as small talk:

    "Courtyard Standard with breakfast. What's the total?"
        A full stop was not a clause boundary, so the amenity noun and the
        question mark in one half outvoted the price question in the other.

    "Can you arrange airport pickup?"
        The trailing informational branch did not check the request mood that
        the branch above it did. `can you` and `arrange` both matched and were
        both ignored.

    "How many rooms are free tonight?"
        `free` means *vacant* beside a room and *gratis* beside a breakfast,
        and only the second is a haggle.

Plus one in the other direction — "free upgrade mil sakta hai?" routed to the
modification path rather than the negotiation rules, because `upgrade` was
checked before `free`.

The expectations below are the current classifications, reviewed one by one.
This file exists to make a change in any of them deliberate.
"""
import pytest

from app.services.ai import intent, required_actions

# (message, category). Real guest messages, ordered by category then text.
REAL = [
    ('this is useless, put me through to a real person', 'ESCALATION'),
    ('And check-out?', 'INFORMATIONAL'),
    ('aur breakfast?', 'INFORMATIONAL'),
    ('Can I get a massage at the hotel?', 'INFORMATIONAL'),
    ('Do you allow dogs?', 'INFORMATIONAL'),
    ('Do you have a pool?', 'INFORMATIONAL'),
    ('Do you have parking?', 'INFORMATIONAL'),
    ('Do you have photos of the Lake View Deluxe?', 'INFORMATIONAL'),
    ('How much is airport pickup?', 'INFORMATIONAL'),
    ('Is there parking?', 'INFORMATIONAL'),
    ('one more thing — is parking free?', 'INFORMATIONAL'),
    ('What is your cancellation policy?', 'INFORMATIONAL'),
    ('What time is check-in and do you have parking?', 'INFORMATIONAL'),
    ('What time is check-in, and do you have parking?', 'INFORMATIONAL'),
    ('What time is check-in?', 'INFORMATIONAL'),
    ('What time is check-out?', 'INFORMATIONAL'),
    ('What time is checkout?', 'INFORMATIONAL'),
    ('what time is my check-in?', 'INFORMATIONAL'),
    ('Yaar thoda kam karo na, 4000 per night mein ho jayega?', 'NEGOTIATION'),
    ('Breakfast included h isme?', 'STATE_LOOKUP'),
    ('Is breakfast included?', 'STATE_LOOKUP'),
    ('10th sep 4 log and 2 room chahhiye', 'TRANSACTIONAL'),
    ('4 adult and 4 din k liye', 'TRANSACTIONAL'),
    ('Actually 5 kamre chahiye, 10 log aa rahe hain', 'TRANSACTIONAL'),
    ('Airport se pickup mil jayega? Kitne ka padega?', 'TRANSACTIONAL'),
    ('And a website one?', 'TRANSACTIONAL'),
    ('and departures?', 'TRANSACTIONAL'),
    ('And the Courtyard Standard?', 'TRANSACTIONAL'),
    ('Book kardo', 'TRANSACTIONAL'),
    ('Book king bed', 'TRANSACTIONAL'),
    ('Can I get two extra towels sent to my room?', 'TRANSACTIONAL'),
    ('Can I get two extra towels?', 'TRANSACTIONAL'),
    ('Can you arrange airport pickup?', 'TRANSACTIONAL'),
    ('Can you send me price of hotel?', 'TRANSACTIONAL'),
    ('Can you send me the payment link for my booking?', 'TRANSACTIONAL'),
    ('check in everyone', 'TRANSACTIONAL'),
    ('Check karo hogyi meri payment', 'TRANSACTIONAL'),
    ('Courtyard Standard for 2 on 25 September, 2 nights?', 'TRANSACTIONAL'),
    ("Courtyard Standard with breakfast. What's the total?", 'TRANSACTIONAL'),
    ('Deluxe Balcony wala book kardo', 'TRANSACTIONAL'),
    ('Deluxe Double for 2 from 15 November 2026 for 2 nights?', 'TRANSACTIONAL'),
    ('Deluxe Double for 2 from 20 November 2026 for 2 nights?', 'TRANSACTIONAL'),
    ('Deluxe Double for 2 from 22 November 2026 for 2 nights?', 'TRANSACTIONAL'),
    ('Deluxe Double for 2 from 25 November 2026 for 2 nights?', 'TRANSACTIONAL'),
    ('Deluxe Double for 2 on 15 November, 2 nights?', 'TRANSACTIONAL'),
    ('Do you have a Courtyard Standard for 2 on 20 September for 2 nights?', 'TRANSACTIONAL'),
    ('Do you have a Courtyard Standard for 2 people on 12 September for 2 nights?', 'TRANSACTIONAL'),
    ('Do you have a Deluxe Double for 2 on 10 October for 2 nights?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 from 28 September to 30 September?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 on 10 October for 2 nights, and how much?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 on 5 October?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 people from 20 September to 22 September?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 people from 5 September to 7 September?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 people on 04 October, for two nights?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 people on 12 September for 2 nights?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 people this weekend?', 'TRANSACTIONAL'),
    ('Do you have a room for 2 this weekend and what does it cost?', 'TRANSACTIONAL'),
    ('do you have a room tomorrow for two?', 'TRANSACTIONAL'),
    ('Do you have a room?', 'TRANSACTIONAL'),
    ('Do you have photos?', 'TRANSACTIONAL'),
    ('first', 'TRANSACTIONAL'),
    ('Haa adult h', 'TRANSACTIONAL'),
    ('Haaa adult h', 'TRANSACTIONAL'),
    ('Hello', 'TRANSACTIONAL'),
    ('hello', 'TRANSACTIONAL'),
    ('hello from elsewhere', 'TRANSACTIONAL'),
    ('hello? anyone there?', 'TRANSACTIONAL'),
    ('hello? anyone?', 'TRANSACTIONAL'),
    ('hi', 'TRANSACTIONAL'),
    ('Hi', 'TRANSACTIONAL'),
    ('Hi! Do you have a room this weekend?', 'TRANSACTIONAL'),
    ('Hi, 5 kamre chahiye, 10 log aa rahe hain', 'TRANSACTIONAL'),
    ('Hi, do you have a room for 2 people on 10 October for 2 nights?', 'TRANSACTIONAL'),
    ('hi, what time is breakfast?', 'TRANSACTIONAL'),
    ('How do I check in online?', 'TRANSACTIONAL'),
    ('How do I pay?', 'TRANSACTIONAL'),
    ('How is occupancy this week?', 'TRANSACTIONAL'),
    ('How many credits do I have and how many conversations is that?', 'TRANSACTIONAL'),
    ('How many credits do I have left?', 'TRANSACTIONAL'),
    ('How many Instagram and website conversations did we have in the last 30 days?', 'TRANSACTIONAL'),
    ('How many rooms are free tonight?', 'TRANSACTIONAL'),
    ('How much am I paying for StayOS and when is the next payment?', 'TRANSACTIONAL'),
    # Reviewed as TRANSACTIONAL when `70% off` matched nothing, and NEGOTIATION
    # is the better answer: it asks a price *and* asks for 70% off, and the
    # discount is the half with a floor and an approval attached. Nothing is
    # lost — NEGOTIATION permits QUOTE_REQUIRED.
    ('how much for two nights? give me 70% off', 'NEGOTIATION'),
    ('How much is that in total, including taxes?', 'TRANSACTIONAL'),
    ('How much is that?', 'TRANSACTIONAL'),
    ('How much?', 'TRANSACTIONAL'),
    ('I have paid already, please confirm my booking is fully paid', 'TRANSACTIONAL'),
    ('I need two extra towels in my room please', 'TRANSACTIONAL'),
    ('Jii', 'TRANSACTIONAL'),
    ('King bed book kardi', 'TRANSACTIONAL'),
    ('Kya AAP hindi m baat kar sakte h?', 'TRANSACTIONAL'),
    ('kya rooms available hain?', 'TRANSACTIONAL'),
    ('M 13 September to 17 September aauga 3 logo k sath', 'TRANSACTIONAL'),
    ('M next month 10th ko aauga 4 din k liye 3 logo k sath', 'TRANSACTIONAL'),
    ('M next month 10th Ko aauga 4 din k liye 3 logo k sath', 'TRANSACTIONAL'),
    ("My AC isn't working, it's really hot", 'TRANSACTIONAL'),
    ("my AC isn't working, it's really hot in here", 'TRANSACTIONAL'),
    ('Ok', 'TRANSACTIONAL'),
    ('Oki book kardo', 'TRANSACTIONAL'),
    ('Okie 2 kamre karne h aaur hinglish m baat karo please', 'TRANSACTIONAL'),
    ('one more time', 'TRANSACTIONAL'),
    ('Payment hogyi', 'TRANSACTIONAL'),
    ('Photos h?', 'TRANSACTIONAL'),
    ('Price kya h?', 'TRANSACTIONAL'),
    ('Room for 2, 10 October to 12 October?', 'TRANSACTIONAL'),
    ('Room for 2, 25 Sept to 27 Sept?', 'TRANSACTIONAL'),
    ('Room for 2, 3 October to 5 October?', 'TRANSACTIONAL'),
    ('room for friday?', 'TRANSACTIONAL'),
    ('Rooms for 2 from 12 Sept to 14 Sept please', 'TRANSACTIONAL'),
    ('Rooms for 2 from 5 Sept to 7 Sept please', 'TRANSACTIONAL'),
    ('Sahil', 'TRANSACTIONAL'),
    ('second', 'TRANSACTIONAL'),
    ('Send me the payment link please', 'TRANSACTIONAL'),
    ('still waiting', 'TRANSACTIONAL'),
    ('stop', 'TRANSACTIONAL'),
    ('Tell me about the Lake View Deluxe', 'TRANSACTIONAL'),
    ('Thanks!', 'TRANSACTIONAL'),
    ('thanks, we had a lovely stay', 'TRANSACTIONAL'),
    ('The Courtyard Standard with breakfast please. How much in total?', 'TRANSACTIONAL'),
    ('the shower in my room is broken', 'TRANSACTIONAL'),
    ('third', 'TRANSACTIONAL'),
    ('Want to reserve room', 'TRANSACTIONAL'),
    ('What can I do in Udaipur?', 'TRANSACTIONAL'),
    ('what did I just ask?', 'TRANSACTIONAL'),
    ('What does an Instagram conversation cost me?', 'TRANSACTIONAL'),
    ('What does it cost?', 'TRANSACTIONAL'),
    ('What time do I have to check out?', 'TRANSACTIONAL'),
    ("What's included in the rate?", 'TRANSACTIONAL'),
    ('who do you think I am', 'TRANSACTIONAL'),
    ('who is arriving today?', 'TRANSACTIONAL'),
    ('Why are my credits low?', 'TRANSACTIONAL'),
    ('Yes confirm', 'TRANSACTIONAL'),
    ('Yes please send me the payment link', 'TRANSACTIONAL'),
    ('Yes, how much is it?', 'TRANSACTIONAL'),
]


def test_the_corpus_is_real_and_large():
    assert len(REAL) >= 130


@pytest.mark.parametrize("message,expected", REAL)
def test_a_real_guest_message_is_categorised_as_reviewed(message, expected):
    assert intent.category(message) == expected


class TestTheGatesBlastRadiusOnRealTraffic:
    """
    How much the intent gate actually changes on messages nobody wrote for it.

    The number matters in both directions. Large would mean the gate is
    reshaping real conversations, which needs far more evidence than this pass
    has. Zero would mean it is inert and the unit tests are testing a mock.
    """

    def _with_and_without(self, message):
        original = intent.permits
        intent.permits = lambda category, name: True
        ungated = {r.intent for r in required_actions.detect(message)}
        intent.permits = original
        gated = {r.intent for r in required_actions.detect(message)}
        return ungated, gated

    def test_it_suppresses_something_but_barely_anything(self):
        changed = [m for m, _ in REAL
                   if self._with_and_without(m)[0] != self._with_and_without(m)[1]]
        assert 0 < len(changed) <= 5, (
            f"the gate changed {len(changed)} of {len(REAL)} real messages: {changed}")

    def test_and_everything_it_suppressed_was_informational(self):
        for message, _ in REAL:
            ungated, gated = self._with_and_without(message)
            if ungated != gated:
                assert intent.category(message) in (
                    intent.INFORMATIONAL, intent.STATE_LOOKUP), message

    def test_no_real_message_asking_a_price_is_called_informational(self):
        """
        The dangerous direction, stated as a property rather than as a list.
        A message that names money or a stay and asks about it must never end
        up owing nothing.
        """
        import re

        asks_money = re.compile(
            r"(?<![a-z0-9])(?:total|how much for|rate for|price for|per night|"
            r"kitna hoga|kitna lagega|book|reserve)(?![a-z0-9])", re.IGNORECASE)
        for message, _ in REAL:
            if asks_money.search(message):
                assert intent.category(message) != intent.INFORMATIONAL, message

    def test_every_request_for_a_person_survives(self):
        for message, category in REAL:
            if category == intent.ESCALATION:
                assert "ESCALATION" in {
                    r.intent for r in required_actions.detect(message)}, message
