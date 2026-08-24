"""
Saying a booking exists, saying it does not, and saying neither.

**The defect.** `guardrails._CONFIRMED` matches the *phrase* "booking confirm".
Every honest answer to "is my booking confirmed?" contains that phrase, because
it is what the guest asked about — so the rule written to stop the agent
inventing a booking was refusing the sentences that told the truth about one.
Measured against the code as it stood, all four of these were refused:

    payment aate hi aapki booking confirm ho jayegi      it will be, once you pay
    booking confirm karne ke liye ₹38,647 pay kar dijiye  to confirm it, pay
    aapki booking confirm nahi hui hai abhi tak           it is NOT confirmed
    I will email you once your booking is confirmed       when it happens

The third is a denial. The rule existed to stop the agent claiming a booking,
and it was refusing the sentence that says there isn't one.

A refusal is not a soft failure here: `runtime` replaces the reply with
`handoff_reply` and escalates, so each of these hands the guest to a person at
the exact moment they are being asked to pay. The first two were caught by
`scripts/replay_e2e.py` against the live model, mid-sale.

**Why it could not be patched again.** The old shape was three regexes arguing:
the phrase, a hedge anywhere in a ±90-character window, and an infinitive in the
twelve characters after — with the hedge gated on `get_booking_status` having
run this turn. So the same sentence was a lie or not depending on which tools
the model reached for, and each new false refusal was answered with another
alternation. `karni` was the second one this month; `ho jaye` would have been the
third.

**The shape that replaces it is grammatical mood**, and it has three outcomes
rather than two, which is the part a first cut got wrong. A denial is as much a
statement about a stay as an affirmative — an agent that has not looked does not
know it, and telling a guest who *has* paid that they have not sends them to
re-pay. So:

    CLAIMED       needs a tool that secures a stay
    DENIED        needs a lookup that came back unsecured
    HYPOTHETICAL  needs nothing — it makes no claim about the present

Hindi carries mood in the verb and puts the verb after the object, so the tail
immediately following the phrase decides most of it. English carries it in front,
so the clause head is searched for a subordinator — bounded by a clause break
rather than a character count, which is what keeps *"When you arrive, your room
is booked"* a claim while *"once your booking is confirmed"* is not.
"""
import pytest

from app.services.ai import guardrails
from app.services.ai.guardrails import CLAIMED, DENIED, HYPOTHETICAL


class FakeOutcome:
    def __init__(self, tool, output=None, ok=True):
        self.tool, self.output, self.ok = tool, output, ok


NOTHING: list = []
QUOTED = [FakeOutcome("get_quote", {"totalPaise": 3584000})]
LOOKED_AND_FOUND_NOTHING = [FakeOutcome("get_booking_status", {"state": "PENDING"})]
LOOKED_AND_FOUND_ONE = [FakeOutcome("get_booking_status", {"state": "CONFIRMED"})]
BOOKED = [FakeOutcome("create_booking", {"ref": "TB-5CECKV"})]


def allowed(text, outcomes=NOTHING) -> bool:
    return guardrails.check_reply(text, tool_outputs=outcomes).ok


def mood_of(text) -> str:
    """The mood of the first phrase in the text, for the classifier's own tests."""
    match = guardrails._CONFIRMED.search(text)
    assert match, f"no confirmation phrase in {text!r} — the test is wrong"
    return guardrails._mood(text, match)


# ── the four sentences that were refused in production shapes ───────────────

class TestTheHonestSentencesThatWereRefused:
    """Each of these is a real transcript or a direct paraphrase of one."""

    def test_the_sentence_the_live_replay_refused(self):
        """
        Found by `replay_e2e` against the live model. Read it closely: it says
        the payment has *not* arrived and the rooms are *not* booked, and offers
        a link. It was refused as a claim that a stay was secured.
        """
        assert allowed(
            "Sahil ji, maine check kiya hai par abhi tak aapki payment hamare "
            "paas nahi aayi hai aur rooms abhi booked nahi hain. Agar aap "
            "payment karna chahte hain toh main aapko link bhej deta hoon "
            "jisse payment hote hi booking confirm ho jaye."
        )

    def test_it_will_be_confirmed_once_you_pay(self):
        assert allowed("Payment aate hi aapki booking confirm ho jayegi.")

    def test_in_order_to_confirm_it_please_pay(self):
        # The masculine infinitive. This one had already been fixed once.
        assert allowed("Booking confirm karne ke liye abhi payment kar dijiye.")

    def test_do_you_want_to_confirm_the_booking(self):
        # The feminine, which is the commoner form because `booking` is feminine.
        assert allowed("Agar aapko booking confirm karni hai toh batayein.")

    def test_i_will_email_you_once_it_is_confirmed(self):
        assert allowed("I will email you once your booking is confirmed.")


# ── the sentence the whole rule exists for ──────────────────────────────────

class TestTheClaimIsStillRefused:
    """
    None of the above may cost us the one thing this guardrail is for.

    The reference sentence went to a real guest on Instagram after a run whose
    only tools were `get_stay_options` and `get_quote`. Nothing was booked.
    """

    REFERENCE = ("Okay, aapke 2 Deluxe King Bed rooms 10 September se 14 September "
                 "tak ke liye book ho gaye hain.")

    def test_the_reference_lie(self):
        assert not allowed(self.REFERENCE, QUOTED)

    def test_ke_liye_in_it_does_not_read_as_a_purpose_clause(self):
        """
        The trap in the reference sentence, and the reason `ke liye` is not on
        the hypothetical list.

        Here it means "for" — a date range. The purposive is *infinitive* + `ke
        liye`, and the infinitive is matched in the tail where a bare `ke liye`
        cannot be confused with it. Putting `ke liye` on the clause-head list
        would have licensed the single worst sentence the product has sent.
        """
        assert mood_of(self.REFERENCE) == CLAIMED

    @pytest.mark.parametrize("text", [
        "Aapki booking confirm ho gayi hai.",
        "Aapki booking confirm hogayi hai.",          # run together, still perfective
        "Aapke rooms book ho gaye hain.",
        "Your rooms are now booked.",
        "Your reservation is confirmed.",
        "The rooms are yours.",
    ])
    def test_the_indicative_affirmative_needs_a_tool_behind_it(self, text):
        assert not allowed(text, QUOTED)

    @pytest.mark.parametrize("text", [
        "Aapki booking confirm ho gayi hai.",
        "Your reservation is confirmed.",
    ])
    def test_and_is_allowed_when_something_secured_one(self, text):
        assert allowed(text, BOOKED)


class TestThePerfectiveTransitive:
    """
    "Maine aapka room confirm kar diya hai" — I have confirmed your room.

    A gap found by testing the mood rule rather than by a transcript: every
    other branch of `_CONFIRMED` needs "booking" or "rooms" adjacent to the
    verb, and this is the shape where the guest's own noun sits in between. It
    was allowed before this work and after the first cut of the mood rule.

    It cannot collide with the infinitive the tail rule allows: `kar diya` has a
    `d` where `karni`, `karna` and `karne` have their ending.
    """

    @pytest.mark.parametrize("text", [
        "Maine aapka room confirm kar diya hai.",
        "Aapka room book kar diya hai.",
        "Booking confirm karwa di hai.",
        "Room confirm kar liya hai.",
    ])
    def test_a_completed_action_is_a_claim(self, text):
        assert not allowed(text, QUOTED)

    @pytest.mark.parametrize("text", [
        "Booking confirm karni hai?",
        "Booking confirm karne ke liye pay kijiye.",
        "Booking confirm karwana hai toh bataiye.",
    ])
    def test_and_the_infinitive_beside_it_is_not(self, text):
        assert allowed(text, QUOTED)


# ── a denial is a claim too ─────────────────────────────────────────────────

class TestADenialIsAlsoAStatement:
    """
    The guarantee a two-mood model silently dropped.

    "Your booking is not confirmed" is a statement about a booking. An agent
    that has not looked does not know it, and saying it to a guest who *has*
    paid sends them to re-pay or to panic. The way out is `get_booking_status`,
    not a confident negative.
    """

    def test_a_denial_with_no_lookup_behind_it_is_refused(self):
        assert not allowed("Aapki booking confirm nahi hui hai.", QUOTED)

    def test_and_is_allowed_once_the_agent_has_looked(self):
        assert allowed("Aapki booking confirm nahi hui hai.", LOOKED_AND_FOUND_NOTHING)

    def test_a_denial_against_a_lookup_that_found_one_is_refused(self):
        """
        The other direction, and the reason the licence is `state not in
        SECURED` rather than "a status tool ran". The agent looked, found a
        confirmed stay, and told the guest there wasn't one.
        """
        assert not allowed("Aapki booking confirm nahi hui hai.", LOOKED_AND_FOUND_ONE)

    def test_the_refusal_says_which_of_the_two_it_was(self):
        """§20 — a failure that cannot be classified cannot be counted."""
        verdict = guardrails.check_reply("Aapki booking confirm nahi hui hai.",
                                         tool_outputs=QUOTED)
        assert "not secured" in verdict.reason and "without looking" in verdict.reason

    def test_a_conditional_denial_is_not_a_denial(self):
        """
        `agar` governs the clause, and the head is read before the tail for
        exactly this: it is a question about what to do, not a report.
        """
        assert mood_of("Agar aapki booking confirm nahi hui hai to bataiye.") == HYPOTHETICAL
        assert allowed("Agar aapki booking confirm nahi hui hai to bataiye.", QUOTED)


# ── the classifier itself ───────────────────────────────────────────────────

class TestMood:
    @pytest.mark.parametrize("text,expected", [
        # the prospective, in the spellings guests and models actually write
        ("Booking confirm ho jayegi.", HYPOTHETICAL),
        ("Booking confirm ho jaayegi.", HYPOTHETICAL),
        ("Booking confirm ho jaegi.", HYPOTHETICAL),
        ("Booking confirm ho jaye.", HYPOTHETICAL),
        ("Booking confirm ho jayenge.", HYPOTHETICAL),
        ("Booking confirm hoga.", HYPOTHETICAL),
        # the infinitive, all three genders, plain and causative
        ("Booking confirm karna hai.", HYPOTHETICAL),
        ("Booking confirm karni hai.", HYPOTHETICAL),
        ("Booking confirm karne ke liye.", HYPOTHETICAL),
        ("Booking confirm karwani hai.", HYPOTHETICAL),
        # the perfective, which is the claim
        ("Booking confirm ho gayi hai.", CLAIMED),
        ("Booking confirm ho gaya hai.", CLAIMED),
        ("Booking confirm hogayi hai.", CLAIMED),
        # the denial
        ("Booking confirm nahi hui hai.", DENIED),
        ("Booking confirm nahin hui.", DENIED),
    ])
    def test_hinglish_mood_lives_in_the_tail(self, text, expected):
        assert mood_of(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("Once your booking is confirmed we will email you.", HYPOTHETICAL),
        ("If your booking is confirmed you will get a link.", HYPOTHETICAL),
        ("As soon as your booking is confirmed we will call.", HYPOTHETICAL),
        ("So that your booking is confirmed, please pay now.", HYPOTHETICAL),
        ("Your booking is confirmed.", CLAIMED),
    ])
    def test_english_mood_lives_in_the_clause_head(self, text, expected):
        assert mood_of(text) == expected

    def test_hoga_and_ho_gaya_are_one_space_apart_and_opposite(self):
        """
        The pair the tail rule has to get right. `hoga` is "will be"; `ho gaya`
        is "has become". Nothing else distinguishes them.
        """
        assert mood_of("Booking confirm hoga.") == HYPOTHETICAL
        assert mood_of("Booking confirm ho gaya.") == CLAIMED

    def test_a_subordinator_in_another_clause_does_not_reach_this_one(self):
        """
        The reason the head is bounded by a clause break and not by a character
        count. `when` here governs the arrival, not the booking.
        """
        assert mood_of("When you arrive, your room is booked and ready.") == CLAIMED

    def test_the_first_real_claim_is_the_one_reported(self):
        """
        A reply can hedge and then claim. Scanning must not stop at the hedge.
        """
        text = ("Booking confirm karne ke liye pay kijiye. "
                "Waise aapke rooms book ho gaye hain.")
        assert not allowed(text, QUOTED)
        assert "book ho gaye" in guardrails.check_reply(text, tool_outputs=QUOTED).reason
