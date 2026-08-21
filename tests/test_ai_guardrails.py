"""
An agent may not invent a payment link, and may not invent a price.

Every case in the first class is a real transcript. Asked to book a room it had
found but not quoted, Gemini wrote:

    A Courtyard Standard for two from 25 to 27 September will be ₹15,120.
    You can confirm and pay using this link: https://pay.hotel.com/f7538a7c…

`get_quote` had not run and `pay.hotel.com` belongs to nobody. Every other check
in the system passed, because each of them asks what the agent *did* and this
was something it only said.

These tests are the reason that reply is now a handoff. They are unit tests on
`check_reply` rather than end-to-end ones on purpose: the property being
protected is "this string is not allowed to reach a guest", and asserting it
against a live model would make the suite depend on the model reproducing its
own bug.
"""
from dataclasses import dataclass

import pytest

from app.services.ai import guardrails


@dataclass(frozen=True)
class FakeOutcome:
    """The shape `check_reply` reads off a ToolOutcome, and nothing more."""

    tool: str
    output: dict


TRUSTED = {"partner.travbuddy.in", "travbuddy.in"}


def check(reply, outcomes, history=None):
    return guardrails.check_reply(
        reply, tool_outputs=outcomes, trusted_hosts=TRUSTED, history=history or []
    )


class TestInventedLinks:
    def test_the_transcript_that_caused_this(self):
        verdict = check(
            "A Courtyard Standard will be ₹15,120. You can pay here: "
            "https://pay.hotel.com/f7538a7c-a496-41ad",
            [FakeOutcome("search_availability", {"roomTypes": [{"name": "Courtyard Standard"}]})],
        )
        assert not verdict.ok
        assert "unverified link" in verdict.reason

    def test_a_link_a_tool_actually_returned_is_allowed(self):
        verdict = check(
            "Your total is ₹7,168. Pay here: https://partner.travbuddy.in/pay/order_TS6",
            [
                FakeOutcome("get_quote", {"totalPaise": 716800}),
                FakeOutcome(
                    "create_booking_payment_link",
                    {"payUrl": "https://partner.travbuddy.in/pay/order_TS6"},
                ),
            ],
        )
        assert verdict.ok

    def test_a_lookalike_domain_does_not_pass_as_a_suffix(self):
        # `endswith("travbuddy.in")` would let this through. That is the bug
        # this assertion exists to prevent somebody reintroducing.
        verdict = check(
            "Pay at https://evil-travbuddy.in/pay/x", [FakeOutcome("get_quote", {"totalPaise": 1})]
        )
        assert not verdict.ok

    def test_a_subdomain_of_a_trusted_host_is_allowed(self):
        verdict = check(
            "Our rooms are at https://anandi.travbuddy.in/rooms",
            [FakeOutcome("get_property_knowledge", {"answer": "ok"})],
        )
        assert verdict.ok

    def test_a_link_the_guest_pasted_is_not_laundered_by_repeating_it(self):
        # The same attack from the other side: get the agent to echo a link the
        # guest supplied, so it arrives carrying the hotel's authority.
        history = [{"role": "user", "content": "can I pay at https://scam.example/x ?"}]
        verdict = check(
            "Yes, please pay at https://scam.example/x",
            [FakeOutcome("get_quote", {"totalPaise": 1})],
            history,
        )
        assert not verdict.ok

    def test_a_link_the_agent_itself_sent_earlier_may_be_repeated(self):
        history = [
            {"role": "assistant", "content": "Pay here: https://partner.travbuddy.in/pay/order_A"}
        ]
        verdict = check(
            "Here's that link again: https://partner.travbuddy.in/pay/order_A",
            [FakeOutcome("get_property_knowledge", {"answer": "ok"})],
            history,
        )
        assert verdict.ok


class TestInventedPrices:
    def test_a_price_with_no_pricing_tool_on_the_run(self):
        verdict = check(
            "That will be ₹4,500 for two nights.",
            [FakeOutcome("get_property_knowledge", {"answer": "Check-in is 2pm"})],
        )
        assert not verdict.ok
        assert "4500" in verdict.reason

    def test_a_price_a_pricing_tool_did_not_return(self):
        verdict = check(
            "Your total is ₹9,999.", [FakeOutcome("get_quote", {"totalPaise": 716800})]
        )
        assert not verdict.ok

    def test_rupees_derived_from_paise_are_allowed(self):
        # The ordinary case: tools speak paise, agents speak rupees.
        verdict = check(
            "The deposit is ₹1,434.",
            [FakeOutcome("create_booking_payment_link", {"amountPaise": 143360})],
        )
        assert verdict.ok

    def test_restating_a_price_already_in_the_conversation(self):
        history = [{"role": "assistant", "content": "Your total is ₹7,168."}]
        verdict = check(
            "As I mentioned, ₹7,168 in total.",
            [FakeOutcome("get_property_knowledge", {"answer": "ok"})],
            history,
        )
        assert verdict.ok

    def test_small_numbers_are_not_treated_as_money(self):
        verdict = check(
            "We have 2 rooms left, and they sleep 3 people.",
            [FakeOutcome("search_availability", {"unitsLeft": 2})],
        )
        assert verdict.ok

    def test_rupee_written_as_rs_and_as_the_word(self):
        for phrasing in ("Rs. 4,500", "INR 4500", "4,500 rupees"):
            verdict = check(
                f"That will be {phrasing}.",
                [FakeOutcome("get_property_knowledge", {"answer": "ok"})],
            )
            assert not verdict.ok, phrasing


class TestOrdinaryRepliesAreUntouched:
    def test_an_answer_with_no_money_and_no_link(self):
        verdict = check(
            "Check-in is from 2:00 PM and parking is free.",
            [FakeOutcome("get_property_knowledge", {"answer": "2pm"})],
        )
        assert verdict.ok

    def test_an_empty_reply_is_somebody_elses_problem(self):
        # The runtime already treats a missing reply as a failed run. The
        # guardrail must not turn it into a *refused* one and change the reason.
        assert check("", []).ok
        assert check("   ", []).ok


# ── the rule is about evidence, not about which tool produced it ─────────────


def test_the_owner_copilot_may_report_its_own_hotels_revenue():
    """
    The regression that took the Owner Copilot off the air.

    The money rule used to require one of a named set of *pricing* tools on the
    run. Analytics tools were not in it, so an owner asking what the week took
    got a correct answer built from `get_revenue` — and the guardrail threw it
    away and told them somebody would get back to them. About their own hotel.

    A number in a tool result is evidence whichever tool returned it.
    """
    verdict = guardrails.check_reply(
        "You took ₹184,500 last week, and ADR was ₹4,000.",
        tool_outputs=[
            FakeOutcome("get_revenue", {"revenuePaise": 18450000}),
            FakeOutcome("get_occupancy", {"adrPaise": 400000, "occupancy": 0.71}),
        ],
        trusted_hosts=set(),
        history=[],
    )
    assert verdict.ok, verdict.reason


def test_a_number_no_tool_returned_is_still_refused_for_the_owner_too():
    """The widening must not become "staff screens are unchecked"."""
    verdict = guardrails.check_reply(
        "You took ₹250,000 last week.",
        tool_outputs=[FakeOutcome("get_revenue", {"revenuePaise": 18450000})],
        trusted_hosts=set(),
        history=[],
    )
    assert not verdict.ok


def test_a_service_price_from_the_hotels_own_list_is_backed():
    # The upsell path: `list_services` is what the hotel itself charges.
    verdict = guardrails.check_reply(
        "Airport pickup is ₹1,200.",
        tool_outputs=[FakeOutcome("list_services", {"services": [
            {"name": "Airport pickup", "pricePaise": 120000},
        ]})],
        trusted_hosts=set(),
        history=[],
    )
    assert verdict.ok, verdict.reason


# ── a stand-in is not a value ────────────────────────────────────────────────


class TestPlaceholders:
    """
    The reply that produced this rule, to a guest who had just asked to book:

        Please use this link to pay ₹7,168 and confirm your reservation:
        [payment link will be here].

    Nothing in the link rule fired, because there was no link — which is the
    entire failure. The model had been told its tool needed approval and wrote
    the sentence it would have written if the tool had worked.
    """

    def test_the_transcript_that_caused_this(self):
        verdict = check(
            "Please use this link to pay and confirm: [payment link will be here].",
            [FakeOutcome("get_quote", {"totalPaise": 716800})],
        )
        assert not verdict.ok
        assert "placeholder" in verdict.reason

    def test_a_template_variable_never_reaches_a_guest(self):
        assert not check("Hello {{guest_name}}, your room is ready.", []).ok
        assert not check("Send it to <insert link here>", []).ok

    def test_an_ordinary_aside_in_brackets_is_fine(self):
        # Hotels write like this. A rule that catches it is a rule that turns
        # working conversations into handoffs.
        assert check("Breakfast is 7–10 on the rooftop [weather permitting].", []).ok
        assert check("Your room (Deluxe Double) is on the second floor.", []).ok


def test_a_rupee_sign_does_not_swallow_the_number_behind_it():
    """
    The bug that made every payment link with a balance hand off.

    `json.dumps` escapes ₹ to `\\u20b9`. The `9` at the end of that escape is a
    digit, so the scanner read `\\u20b95,734` as "20" and "95734" — and the one
    number actually in the tool result, 5734, was nowhere in the allowed set.
    Every amount in this product is written with a rupee sign in front of it,
    so this affected all of them.
    """
    verdict = check(
        "₹1,434 to confirm now, and the balance of ₹5,734 at the hotel — total ₹7,168.",
        [FakeOutcome("create_booking_payment_link", {
            "payNowText": "₹1,434",
            "dueAtHotelText": "₹5,734",
            "totalText": "₹7,168",
        })],
    )
    assert verdict.ok, verdict.reason


# ── a booking nobody made ────────────────────────────────────────────────────


class TestInventedConfirmations:
    """
    The most expensive sentence this product can send.

    A real guest on Instagram, after a run whose only tools were
    `get_stay_options` and `get_quote`:

        Okay, aapke 2 Deluxe King Bed rooms 10 September se 14 September tak ke
        liye book ho gaye hain.          ("your 2 rooms have been booked")

    Nothing was booked. A minute later the same agent said those rooms were not
    available. A guest who stops reading at the first message arrives at a hotel
    with no reservation — worse than any invented price, because they act on it
    by travelling.
    """

    QUOTED = [FakeOutcome("get_quote", {"totalPaise": 3584000})]
    PAID = [FakeOutcome("create_booking_payment_link", {"payNowPaise": 143360})]

    def test_the_transcript_that_caused_this(self):
        verdict = check(
            "Okay, aapke 2 Deluxe King Bed rooms 10 September se 14 September "
            "tak ke liye book ho gaye hain.",
            self.QUOTED,
        )
        assert not verdict.ok
        assert "secured" in verdict.reason

    @pytest.mark.parametrize("claim", [
        "Your rooms are now booked.",
        "Your reservation is confirmed.",
        "Your booking is complete.",
        "The rooms are yours.",
        "Your rooms are held for you.",
        "आपके कमरे बुक हो गए हैं।",
        "Aapka room confirm ho gaya hai.",
    ])
    def test_no_wording_of_it_gets_through(self, claim):
        """
        Hindi and Hinglish are here because the transcript was. An English-only
        rule would have caught nothing on the conversation it was written for.
        """
        assert not check(claim, self.QUOTED).ok, claim

    @pytest.mark.parametrize("fine", [
        "Your booking will be confirmed once the payment goes through.",
        "I can hold that for you if you'd like to book.",
        "Two rooms are available for those dates.",
        "Booking a room is easy — just tell me your dates.",
        "Would you like me to book it?",
        "Shall I send you a payment link to confirm the booking?",
    ])
    def test_talking_about_booking_is_not_claiming_one(self, fine):
        """
        The rule has to leave the whole sales conversation intact. A guardrail
        that fires on "would you like to book?" would hand every enquiry to a
        human and be switched off within a day.
        """
        assert check(fine, self.QUOTED).ok, fine

    def test_a_real_confirmation_is_allowed(self):
        """Once something has actually secured the stay, saying so is the job."""
        assert check("Your booking is confirmed.", self.PAID).ok

    def test_check_in_also_licenses_it(self):
        assert check("You're all checked in.",
                     [FakeOutcome("check_in", {"room": "204"})]).ok

    def test_the_tools_that_license_it_are_the_ones_that_secure_a_stay(self):
        """
        Asserted as a set rather than left implicit: adding a tool here is
        granting permission to tell a guest they have a room, and that should
        be a deliberate line in a diff.
        """
        assert guardrails.CONFIRMING_TOOLS == {
            "create_booking",
            "create_booking_payment_link",
            "create_payment_link",
            "check_in",
        }
