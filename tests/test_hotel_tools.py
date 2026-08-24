"""
The agent's side of the hotel OS: what the tools may say, and to whom.

Most of what matters here is not a happy path. It is a set of *properties* of
the tool surface — asserted against the registry itself rather than against one
call, so a tool added later that breaks one of them fails this file rather than
a guest's conversation.
"""
import pytest

from app.services.ai import registry, toolsets
from app.services.ai.agents import guest as guest_agent
from app.services.ai.agents import sales as sales_agent
from app.services.pms import escalation

registry.load_all()

NEW_TOOLS = (
    "get_room_details", "get_checkin_options", "get_meal_info",
    "get_group_rates", "get_cancellation_terms", "get_payment_terms",
    "get_experiences", "negotiate_rate", "escalate_to_team",
)


class TestTheToolSurface:
    def test_every_new_tool_is_registered(self):
        missing = [name for name in NEW_TOOLS if name not in registry.REGISTRY]
        assert not missing, missing

    def test_no_tool_lets_a_model_name_another_hotel(self):
        # The property always comes from the conversation. A tool that took a
        # property id would be one a guest could aim at somebody else's hotel by
        # talking the model into passing a uuid.
        for name in NEW_TOOLS:
            fields = registry.REGISTRY[name].input_model.model_fields
            assert "property_id" not in fields, name
            assert "partner_id" not in fields, name

    def test_the_read_tools_are_reads(self):
        for name in NEW_TOOLS:
            spec = registry.REGISTRY[name]
            if name in ("negotiate_rate", "escalate_to_team"):
                continue
            assert spec.mutating is False, name
            assert spec.autonomy_floor == 0, name

    def test_negotiating_sits_at_the_same_floor_as_quoting(self):
        # It takes a hold when the answer is yes, and a hold is a room somebody
        # else cannot book for fifteen minutes.
        assert registry.REGISTRY["negotiate_rate"].autonomy_floor == \
            registry.REGISTRY["get_quote"].autonomy_floor
        assert registry.REGISTRY["negotiate_rate"].permission == "rates"

    def test_escalating_is_scoped_to_its_own_conversation(self):
        spec = registry.REGISTRY["escalate_to_team"]
        assert spec.scoped_to_conversation is True
        assert spec.permission == "messaging"

    def test_no_tool_input_asks_the_model_for_a_discount_percentage(self):
        # The whole point of negotiate_rate: the model passes on the guest's own
        # rupee figure and does no arithmetic. A percentage field would be an
        # invitation to compute one.
        fields = registry.REGISTRY["negotiate_rate"].input_model.model_fields
        assert "discount_pct" not in fields
        assert "requested_nightly_rupees" in fields

    def test_the_escalation_reason_field_forbids_numbers_in_its_own_description(self):
        # Not enforceable in code — a model can write anything — so the contract
        # is stated where the model reads it, and this pins that it still is.
        described = registry.REGISTRY["escalate_to_team"].input_model.model_fields["reason"].description
        assert "Do NOT include prices" in described


class TestWhoHoldsThem:
    def test_the_guest_agent_can_answer_the_five_common_questions(self):
        held = set(guest_agent.DEFINITION.tools)
        assert {"get_room_details", "get_meal_info", "get_checkin_options",
                "get_cancellation_terms", "get_experiences"} <= held

    def test_the_guest_agent_negotiates_because_it_is_the_one_on_instagram(self):
        # This test used to assert the opposite, on the reasoning that the Guest
        # agent answers people the hotel already knows and a price argument
        # about a stay that does not exist belongs to Sales.
        #
        # No pilot property has ever provisioned Sales. The Guest agent is what
        # answers Instagram, and it already holds `get_quote` and
        # `create_booking_payment_link` — it sells to strangers all day. What it
        # did not hold was the engine, so a guest who wrote "thoda kam karo" met
        # a model with a rate card and no rules. Replayed against production it
        # invented ₹4,204 a night and the reply guardrail caught it.
        #
        # Holding the tool is not deciding the price: `negotiate_rate` sits at
        # autonomy floor 3 like `get_quote`, and the number comes from
        # `negotiation.decide()` either way.
        held = set(guest_agent.DEFINITION.tools)
        assert {"negotiate_rate", "get_group_rates", "escalate_to_team"} <= held
        assert registry.REGISTRY["negotiate_rate"].autonomy_floor == 3
        # Still not this one. A model may ask what the rules allow; it may not
        # take a room off sale on its own say-so.
        assert "create_booking" not in held

    def test_the_sales_agent_holds_the_whole_negotiation_ladder(self):
        held = set(sales_agent.DEFINITION.tools)
        assert {"get_negotiation_policy", "negotiate_rate", "propose_rate_exception",
                "get_group_rates", "escalate_to_team"} <= held

    def test_no_staff_agent_was_given_a_selling_tool_by_accident(self):
        from app.services.ai.agents import frontdesk, marketing, owner
        for definition in (frontdesk.DEFINITION, marketing.DEFINITION, owner.DEFINITION):
            held = set(definition.tools)
            assert "negotiate_rate" not in held, definition.agent_type


class TestTheMenus:
    def test_every_new_tool_appears_on_at_least_one_task(self):
        named = set(toolsets.CORE)
        for tools in toolsets.BY_TASK.values():
            named |= set(tools)
        # A tool nobody's menu names is one the classifier can never surface.
        assert set(NEW_TOOLS) <= named

    def test_the_negotiation_task_carries_the_negotiation_engine(self):
        assert "negotiate_rate" in toolsets.BY_TASK["complex_negotiation"]

    def test_narrowing_still_cannot_widen_a_grant(self):
        # The invariant the whole module rests on, re-asserted with the new
        # names in the table.
        granted = frozenset({"get_room_details", "send_message"})
        for task in toolsets.BY_TASK:
            assert toolsets.for_task(task, granted) <= granted


# ── escalation ───────────────────────────────────────────────────────────────


class TestDedupe:
    FACTS = {"room_type_id": "rt", "rate_plan_id": "rp", "check_in": "2026-09-10",
             "check_out": "2026-09-14", "units": 10, "requested_paise": 400_000}

    def test_the_same_argument_rephrased_is_the_same_escalation(self):
        # The key is computed from the substance, never from the wording — a
        # guest who says it differently must not raise a second ticket.
        a = escalation.dedupe_key("discount_request", self.FACTS)
        b = escalation.dedupe_key("discount_request", dict(self.FACTS))
        assert a == b

    def test_a_different_asking_price_is_a_different_escalation(self):
        a = escalation.dedupe_key("discount_request", self.FACTS)
        b = escalation.dedupe_key("discount_request", {**self.FACTS, "requested_paise": 380_000})
        assert a != b

    def test_a_different_kind_about_the_same_stay_is_a_different_escalation(self):
        assert escalation.dedupe_key("discount_request", self.FACTS) != \
            escalation.dedupe_key("group_inquiry", self.FACTS)

    def test_missing_facts_do_not_raise(self):
        assert escalation.dedupe_key("other", {})

    def test_the_key_fits_the_column(self):
        assert len(escalation.dedupe_key("other", self.FACTS)) == 32


class TestTheInternalMessage:
    FACTS = {
        "guest_name": "Sahil", "channel": "instagram",
        "check_in": "2026-09-10", "check_out": "2026-09-14",
        "units": 10, "guests": 20, "room_name": "Lake View Deluxe",
        "published_paise": 500_000, "requested_paise": 400_000,
        "floor_paise": 430_000, "total_paise": 20_000_000,
    }

    def test_it_reads_like_the_briefs_own_example(self):
        text = escalation.compose(
            "group_inquiry", hotel="Neelkanth Haveli", facts=self.FACTS,
            reason="Twenty of us for a wedding, asking for a better rate.",
        )
        assert "Group booking enquiry" in text
        assert "Guest: Sahil (instagram)" in text
        assert "Dates: 2026-09-10 to 2026-09-14" in text
        assert "Rooms: 10" in text
        assert "Guest asked: ₹4,000 per night" in text

    def test_the_internal_message_MAY_name_the_floor(self):
        # This is the asymmetry the whole module exists for. A manager deciding
        # on an exception needs to know what they are making an exception to;
        # the guest never sees this text.
        text = escalation.compose("discount_request", hotel="H", facts=self.FACTS, reason=None)
        assert "Lowest allowed: ₹4,300" in text

    def test_the_floor_line_is_absent_when_there_is_no_floor(self):
        facts = {k: v for k, v in self.FACTS.items() if k != "floor_paise"}
        text = escalation.compose("discount_request", hotel="H", facts=facts, reason=None)
        assert "Lowest allowed" not in text

    def test_a_missing_fact_omits_its_line_rather_than_printing_a_dash(self):
        text = escalation.compose("other", hotel="H", facts={"guest_name": "A"}, reason=None)
        assert "Dates:" not in text and "Rooms:" not in text

    def test_the_reason_is_truncated_rather_than_trusted(self):
        text = escalation.compose("other", hotel="H", facts={}, reason="x" * 5000)
        assert len(text) < 1000

    def test_every_kind_ends_with_something_to_do(self):
        for kind in escalation.DEFAULT_ROLES:
            text = escalation.compose(kind, hotel="H", facts={}, reason=None)
            assert text.rstrip().split("\n")[-1].startswith("Reply")


class TestRouting:
    def test_every_kind_has_a_fallback_role(self):
        # A hotel that has configured no routing must still be reachable.
        # Escalating to nobody is the failure this module exists to close.
        for kind in escalation.DEFAULT_ROLES:
            assert escalation.DEFAULT_ROLES[kind]

    def test_every_kind_maps_to_a_publishable_event(self):
        from app.api.v1.pms.hotel_os_schemas import NOTIFICATION_EVENTS
        for kind, event in escalation.EVENT_FOR_KIND.items():
            assert event in NOTIFICATION_EVENTS, (kind, event)

    def test_the_two_vocabularies_cover_the_same_kinds(self):
        # A kind with a default role and no event would route to a person and
        # ignore the hotel's own rules; the reverse would do the opposite.
        assert set(escalation.DEFAULT_ROLES) == set(escalation.EVENT_FOR_KIND)

    def test_the_tool_accepts_only_kinds_the_service_can_route(self):
        """
        A subset, not an equality, and the difference is one word.

        `unanswered` is routed by the service and is *not* offered to the agent,
        because it is not something an agent asks for: it is the system noticing
        that a guest has been waiting. Nothing raises it today — the sweep that
        would is not built — so it is a routing rule with no producer yet, which
        is a gap worth naming rather than a vocabulary mismatch worth
        "fixing" by deleting the rule.
        """
        from app.services.ai.tools.hotel import _ESCALATION_KINDS
        assert _ESCALATION_KINDS < set(escalation.DEFAULT_ROLES)
        assert set(escalation.DEFAULT_ROLES) - _ESCALATION_KINDS == {"unanswered"}


@pytest.mark.parametrize("kind", sorted(escalation.DEFAULT_ROLES))
def test_a_sales_role_is_first_for_anything_about_price(kind):
    if kind in ("discount_request", "group_inquiry"):
        assert escalation.DEFAULT_ROLES[kind][0] == "sales"


# ── a toolset has to be able to use what it offers ──────────────────────────

# Which tools cannot run without another tool's result, and what they need.
#
# Not a general dependency graph — only the cases where a tool takes an id that
# nothing else can produce. `create_booking_payment_link` takes a `quote_id`,
# and a quote id exists only because `get_quote` made one.
# The rule for being on this list is narrow and deliberate: the tool takes a
# **required** argument that is an id, and exactly one family of tools in the
# catalogue can produce that id. An optional id is not a prerequisite — a
# `get_meal_info` with no rate plan answers about room-only, which is a worse
# answer than the one it could have given but not an impossible call. A required
# id with no producer on the menu is a tool the model can be offered and cannot
# use, which is what §6 is about.
#
# `needs` is a set: `get_quote` wants a `rate_plan_id`, and both
# `get_stay_options` and `negotiate_rate` can hand it one. Any of them closes it.
PREREQUISITES = {
    # A quote id exists only because get_quote made one.
    "create_booking_payment_link": {"get_quote", "negotiate_rate"},
    "create_booking": {"get_quote", "negotiate_rate"},
    # A rate plan id is not something a model may derive from a room's name —
    # that is how it once invented `rate_plan_id = "breakfast_included"`.
    "get_quote": {"get_stay_options"},
    "negotiate_rate": {"get_stay_options"},
    "propose_rate_exception": {"get_stay_options"},
    # A hold id comes back from the quote that took it.
    "release_hold": {"get_quote", "negotiate_rate"},
    # A task cannot be assigned, read or closed before it has been raised or
    # found. `list_guest_tasks` counts because a guest asking "kya hua us
    # complaint ka" is answered by finding their own tickets, not by being
    # asked for an id no guest has ever seen.
    "assign_task": {"create_task", "get_task", "list_guest_tasks", "get_task_status"},
    "get_task": {"create_task", "list_guest_tasks"},
    "get_task_status": {"create_task", "list_guest_tasks"},
    "update_task_status": {"create_task", "list_guest_tasks", "get_task"},
    # A service id, from the hotel's own price list.
    "order_service": {"list_services"},
    # A room type id, from whatever established the room.
    "send_room_photos": {"search_availability", "get_stay_options", "get_room_types"},
    "get_room_details": {"search_availability", "get_stay_options", "get_room_types"},
}


@pytest.mark.parametrize("tool,needs", sorted(PREREQUISITES.items()))
def test_a_toolset_can_use_every_tool_it_offers(tool, needs):
    """
    The defect this is for cost a sale at the last step.

    "Theek h, payment link bhej do" classifies as `payment_status` — it is a
    question about payment — and that toolset held `create_booking_payment_link`
    and no `get_quote`. So the agent was handed the tool that takes the money
    and nothing that produces what that tool takes. It reported the problem
    accurately and uselessly — *"payment link generate karne ke liye mujhe ek
    naya quote banana hoga. Abhi main woh nahi kar pa rahi hoon"* — and then
    handed a guest who had just agreed to pay to a person.

    Narrowing the tools per task is right: it is what keeps the prompt small
    and the model's choice easy. The rule it has to obey is that a task offering
    a tool must also offer what that tool depends on, or the narrowing has
    removed a capability rather than the noise around it.
    """
    for task, tools in toolsets.BY_TASK.items():
        available = set(tools) | set(toolsets.CORE)
        if tool in available:
            assert available & set(needs), (
                f"{task!r} offers {tool} and none of {sorted(needs)}, so the model "
                f"can be handed {tool} and have no way to produce what it takes"
            )


def test_every_prerequisite_names_tools_that_exist():
    """
    A typo here would silently switch the rule off for the tool it meant to
    protect — the loop above only fires on names it finds in a toolset.
    """
    for tool, needs in PREREQUISITES.items():
        assert tool in registry.REGISTRY, tool
        for producer in needs:
            assert producer in registry.REGISTRY, producer


def test_every_tool_with_a_required_id_argument_is_accounted_for():
    """
    The list above is hand-written, which means the failure mode is *omission* —
    a tool added next month that takes a required id and never gets a line here.

    So the catalogue is walked instead of trusted: any tool with a required
    argument whose name ends in `_id` must either be listed above, or be
    deliberately exempted below with the reason it needs no producer. A new
    tool that is neither fails this test on the day it is written, which is the
    only day the answer is cheap.
    """
    # Ids that come from the *conversation*, not from another tool. `ctx`
    # supplies these and the model is not trusted with them anyway — see
    # `scoped_to_conversation` and AgentContext.allowed_reservations.
    FROM_CONTEXT = {
        "reservation_id", "conversation_id", "guest_id", "member_id",
        "property_id", "folio_id", "room_id", "campaign_id", "creative_id",
        "ad_account_id", "page_id", "recommendation_id", "escalation_id",
        "channel_account_id", "booking_id",
    }
    unaccounted = []
    for name, spec in registry.REGISTRY.items():
        if name in PREREQUISITES:
            continue
        for field, info in spec.input_model.model_fields.items():
            if not field.endswith("_id") or field in FROM_CONTEXT:
                continue
            if info.is_required():
                unaccounted.append(f"{name}.{field}")

    assert not unaccounted, (
        "these tools take a required id and nothing says which tool produces "
        f"it: {sorted(unaccounted)} — add them to PREREQUISITES or to "
        "FROM_CONTEXT with a reason"
    )
