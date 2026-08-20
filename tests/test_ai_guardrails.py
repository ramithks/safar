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
