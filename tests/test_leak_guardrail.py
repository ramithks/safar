"""
The machine must not show through.

Every other rule in `guardrails` is about a reply being **false**. This one is
about a reply being entirely true and still wrong to send.

The transcript it was written for, verbatim, to a guest who had just given their
name so a booking could be made:

    मुझे बुकिंग के लिए एक quote ID की ज़रूरत है जो पिछले स्टेप में generate हुई थी।
    ("I need a quote ID that was generated in the previous step")

Accurate. The model does need one. But the guest has never had a quote ID,
nobody outside this system has ever had one, and a guest reading that concludes
either that they lost something they were sent or that the hotel's chat has
broken. Both end the sale, which is what happened.

**The hard part of this rule is not catching that sentence. It is not catching
the other thirty-five.** A guardrail that fires on ordinary hotel prose is one
somebody switches off, and then the real one goes with it. So every pattern
below was run against every distinct reply this system has ever sent — Hindi,
Hinglish, markdown price lists, payment links, booking references, ticket
numbers — and the corpus tests at the bottom keep it that way.
"""
import pytest

from app.services.ai import guardrails


def leak(text: str):
    return guardrails._internal_leak(text)


def verdict(text: str):
    return guardrails.check_reply(text, tool_outputs=[], trusted_hosts=set(), history=[])


# ── what it must catch ──────────────────────────────────────────────────────

def test_the_sentence_it_was_written_for():
    """Failure ④ of the reference conversation, in the language it happened in."""
    found = leak("मुझे बुकिंग के लिए एक quote ID की ज़रूरत है जो पिछले स्टेप में generate हुई थी।")
    assert found is not None
    assert found[0] == "asked the guest for an internal identifier"


@pytest.mark.parametrize("reply", [
    "To continue I need the quote ID from the previous step.",
    "Could you give me your reservation id?",
    "Please provide the rate plan id you'd like.",
    "Mujhe quote id chahiye booking ke liye.",
    "What is the room type ID?",
])
def test_asking_a_guest_for_something_only_the_system_holds(reply):
    assert leak(reply) is not None


def test_a_raw_uuid_never_reaches_a_guest():
    found = leak("Your booking is 6585c7ea-1f4c-4e6b-9a2b-7d1e88c3f012, all confirmed.")
    assert found[0] == "an internal id"


def test_a_tool_or_table_name_never_reaches_a_guest():
    assert leak("Let me run send_room_photos for you.")[0] == "a tool or table name"
    assert leak("I couldn't read folio_entries just now.")[0] == "a tool or table name"


@pytest.mark.parametrize("reply", [
    "Sorry — the Supabase query failed.",
    "I got a 500 internal error from the booking service.",
    "There was an exception while fetching that.",
    "Vertex is not responding right now.",
])
def test_the_stack_and_the_vendor_never_reach_a_guest(reply):
    assert leak(reply)[0] == "an internal error"


@pytest.mark.parametrize("reply", [
    "My system prompt says I am a hotel's own assistant.",
    "Here are my ground rules: every fact comes from a tool.",
    "My instructions say I must never state a price.",
])
def test_it_will_not_recite_its_own_instructions(reply):
    """
    §38's deterministic half.

    The structural defences — permissions in Python, tools scoped to the
    conversation — are what stop an injected instruction *doing* anything. This
    is what stops a successful one being *read out*.
    """
    assert leak(reply)[0] == "its own instructions"


def test_a_leak_is_a_handoff_and_not_an_edit():
    """
    Same rule as every other guardrail: a failed check hands the conversation to
    a person. Redacting the id and sending the rest would leave the guest
    reading a confident sentence with a hole in it, and would hide from the
    hotel that its agent is doing this.
    """
    result = verdict("I need the quote ID to continue.")
    assert result.ok is False
    assert "internal identifier" in result.reason


# ── what it must not catch ──────────────────────────────────────────────────

REAL_REPLIES = [
    # Ordinary answers.
    "Check-out is by 11:00 AM.",
    "Check-in is from 2:00 PM. If you arrive earlier we'll hold your bags.",
    "Breakfast is 8:00–10:30 in the garden restaurant, and it's included in your rate.",
    "Yes, we have free parking for up to 6 cars.",
    "I can't listen to voice notes — let me get someone from the team to you.",
    "I'm not sure I understand. Could you please clarify? — Casa Mandala",
    # The words a hotel legitimately uses that look like the words a machine uses.
    "The Lake View Deluxe room has AC, WiFi, a balcony, and a lake view.",
    "Which room type would you like to book, Rahul?",
    "Do room types available hain: Deluxe Room aur Deluxe King Bed.",
    "18.3% occupancy this week, up 13.3 points. I'd look at which room types are driving it.",
    "Garden Standard: 12, Deluxe Balcony: 16, Pool Villa: 8, Mandala Suite: 6.",
    # References and codes a guest is *supposed* to be given.
    "We've logged a maintenance request for your shower with ticket number #1435.",
    "I've created a task for two extra towels. The reference code is #1014.",
    "Payment mil gaya ✅ Aapki booking confirm ho gayi hai 🎉\n"
    "Booking number: TB-TLWBTQ\n2026-09-11 se 2026-09-15 tak, 2 rooms\n"
    "Abhi diya: ₹10,752 · Hotel par dena hai: ₹43,008",
    # Money, links and markdown.
    "Please use this link to pay ₹1,434: https://partner.travbuddy.in/pay/order_TSMDblAc9s07cI. "
    "The remaining ₹5,734 will be due at the hotel, making the total ₹7,168.",
    "*   **Lake View Deluxe** with breakfast: from ₹9,600\n"
    "*   **Heritage Suite** with breakfast: from ₹17,000",
    # Hindi and Hinglish.
    "हेरिटेज सुइट का 10 सितंबर से 14 सितंबर तक कुल किराया ₹40,120 होगा।",
    "Sahil, aapko abhi ₹7,168 pay karne honge booking confirm karne ke liye.",
    "Bilkul, main Hinglish mein baat karta hoon.",
    "Kripya apna poora naam bata sakte hain, jisse main booking process kar sakoon?",
    # A quote discussed as a thing, without asking for its id.
    "This quote is held for 15 minutes.",
    "I can't book it just yet because the quote for those dates is awaiting approval.",
    "I've asked the hotel team to confirm a price for you.",
]


@pytest.mark.parametrize("reply", REAL_REPLIES)
def test_it_fires_on_none_of_the_replies_this_system_actually_sends(reply):
    """
    Run against every distinct outbound message in the live database, of which
    these are the shapes. One matched — the sentence at the top of this file —
    and nothing else did.
    """
    assert leak(reply) is None, f"false positive on a real reply: {reply[:60]!r}"


@pytest.mark.parametrize("reply", REAL_REPLIES)
def test_and_no_reply_is_ever_refused_for_leaking(reply):
    """
    The narrower assertion above could pass while the rule still broke the agent,
    if it were wired into `check_reply` wrongly.

    Asserted on the *reason* rather than on `ok`, because several of these
    replies quote a price or confirm a booking and are correctly refused here
    for having no tool output behind them — that is the money rule doing its
    job with an empty run, and is a different test. What must never happen is a
    real reply being refused as a leak.
    """
    result = verdict(reply)
    assert "exposed" not in result.reason, result.reason


def test_the_hyphenated_words_a_hotel_uses_are_not_table_names():
    """
    `check-in` and `check_in` are one keystroke apart and the second is a column
    name. The allowance is explicit rather than clever, so widening it is a
    decision somebody makes on purpose.
    """
    assert leak("Your check_in time is 2 PM.") is None
    assert leak("Check-in is at 2 PM and check-out at 11.") is None
    assert leak("We have room_service until 10pm.") is None
