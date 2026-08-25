"""
Seven conversations a hotel actually has, played through end to end.

**What this is for, given everything else that exists.** `replay_e2e` plays one
real transcript against the live model and is the truth; `agent_eval` plays
single turns a hundred times and is the measurement. Neither is a *regression*
suite, because both need Vertex, credits and four minutes. This is the third
thing: the same journeys, scripted, fast enough to run on every commit.

**The model is scripted and that is the point.** What is asserted here is never
what the agent said — it is what survived between turns. A guest who picks the
Deluxe on turn three and changes the dates on turn five must still have the
Deluxe on turn six, and that is a property of `messaging/session.py` and the
required-action layer, not of the model. Scripting the model removes the one
variable that would otherwise make these tests flap, and leaves the ones worth
watching.

The seven, from §19:

    simple booking · group · negotiation · payment · existing room context
    cancellation · escalation

and the guardrail cases, which are the eighth thing and get their own section
because every one of them is a way for the system to lie to a guest.
"""
import asyncio
from datetime import date, timedelta

import pytest

from app.services.ai import registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.providers import EchoProvider
from app.services.messaging import obligations, session
from tests.fake_supabase import FakeDb, install

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
ROOM_TYPE = "22222222-2222-4222-8222-222222222222"
OTHER_ROOM = "33333333-3333-4333-8333-333333333333"
RATE_PLAN = "11111111-1111-4111-8111-111111111111"
OTHER_PLAN = "44444444-4444-4444-8444-444444444444"

CHECK_IN = date.today() + timedelta(days=30)
CHECK_OUT = CHECK_IN + timedelta(days=2)
LATER_IN = CHECK_IN + timedelta(days=10)
LATER_OUT = LATER_IN + timedelta(days=2)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "city": "Anjuna", "state": "Goa", "country": "India",
        "property_type": "boutique", "check_in_time": "14:00",
        "check_out_time": "11:00", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "tax_inclusive": False,
        # `booking._load_context` reads these two and refuses without them.
        "status": "active", "settings": {},
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
        "guest_profile": {},
    })
    fake.seed("group_rate_tiers", {
        "id": "tier-3-5", "property_id": PROPERTY, "partner_id": PARTNER,
        "min_units": 3, "max_units": 5, "discount_pct": 8,
        "fixed_price_paise": None, "negotiable": False, "is_active": True,
        "room_type_id": None,
    })
    fake.seed("group_policies", {
        "id": "gp-1", "property_id": PROPERTY, "partner_id": PARTNER,
        "min_units": 3, "deposit_pct": 30, "payment_due_days": 14,
        "meal_package": None, "blackout_dates": [], "cancellation": {}, "terms": None,
    })

    # **A hotel with rooms it can actually sell.**
    #
    # The first version of this fixture had no room types, so every
    # availability read came back empty, every quote failed, and the
    # session-carryover tests passed for the wrong reason — the room was not
    # preserved because it was never established. A golden conversation against
    # a hotel with nothing to sell is not a golden conversation.
    fake.seed("room_types",
        {"id": ROOM_TYPE, "property_id": PROPERTY, "partner_id": PARTNER,
         "code": "DLX", "name": "Deluxe Balcony", "max_occupancy": 3,
         "total_units": 6, "sort_order": 1, "is_active": True},
        {"id": OTHER_ROOM, "property_id": PROPERTY, "partner_id": PARTNER,
         "code": "STD", "name": "Garden Standard", "max_occupancy": 2,
         "total_units": 4, "sort_order": 2, "is_active": True},
    )
    fake.seed("rate_plans", {
        "id": RATE_PLAN, "property_id": PROPERTY, "partner_id": PARTNER,
        "room_type_id": ROOM_TYPE, "code": "DLX-RO", "name": "Room only",
        "meal_plan": "RO", "audience": "public", "refundable": True,
        "inclusions": None, "base_price_paise": 450000, "min_stay": 1,
        "derived_from_plan_id": None, "derive_discount_pct": None,
        "valid_from": None, "valid_to": None, "days_of_week": None,
        "allocation_units": None, "is_active": True,
        "cancellation_cutoff_hours": 48, "cancellation_policy": None,
    })
    # **The Garden Standard's own plan, and the fixture was wrong without it.**
    #
    # A hotel sells every room it lists, and this fixture listed two room types
    # and priced one — so any scenario where a guest switches rooms could only
    # fail, for a reason that was about the fixture rather than the product. A
    # rate plan belongs to a room type, which is exactly what
    # `session.DERIVED_FROM` encodes.
    fake.seed("rate_plans", {
        "id": OTHER_PLAN, "property_id": PROPERTY, "partner_id": PARTNER,
        "room_type_id": OTHER_ROOM, "code": "STD-RO", "name": "Room only",
        "meal_plan": "RO", "audience": "public", "refundable": True,
        "inclusions": None, "base_price_paise": 320000, "min_stay": 1,
        "derived_from_plan_id": None, "derive_discount_pct": None,
        "valid_from": None, "valid_to": None, "days_of_week": None,
        "allocation_units": None, "is_active": True,
        "cancellation_cutoff_hours": 48, "cancellation_policy": None,
    })

    # The two RPCs `availability._fetch` reads. Callables rather than static
    # rows so the fixture answers for whatever nights a test asks about — the
    # date-change journey asks about two different weeks in one conversation.
    def _availability(params):
        start = date.fromisoformat(params["p_from"])
        end = date.fromisoformat(params["p_to"])
        out = []
        day = start
        while day < end:
            for room, left in ((ROOM_TYPE, 6), (OTHER_ROOM, 4)):
                out.append({"room_type_id": room, "date": day.isoformat(),
                            "units_left": left, "stop_sell": False})
            day += timedelta(days=1)
        return out

    fake.rpcs["property_availability"] = _availability
    fake.rpcs["property_rates"] = lambda params: []
    # Quoting takes a hold, and a hold is an RPC. Answering with an id rather
    # than None is what makes `get_quote` a quote instead of a 409 — and the
    # 409's message ("those dates are no longer available") is a plausible
    # enough sentence that a fixture missing this looks like a sold-out hotel
    # rather than a missing stub.
    fake.rpcs["hold_inventory"] = lambda params: "40000000-0000-4000-8000-000000000001"
    fake.rpcs["release_hold"] = lambda params: True
    # Every module that took its own reference to `get_db`, found rather than
    # named — see `fake_supabase.install`.
    #
    # This was a hand-kept list of fifteen module names, and the comment on it
    # recorded what a missing name costs: `app.services.pms.booking` was absent
    # on the first draft, so `get_quote` looked up its rate plan in production,
    # found nothing, and the session-carryover assertions failed for a reason
    # that had nothing to do with sessions. The list was also still short —
    # `billing.service` was reaching the real project on every turn, which is
    # most of what made this file slow.
    install(monkeypatch, fake)
    return fake


def ctx_for(db, *, autonomy_level: int = 3, language: str | None = None) -> AgentContext:
    """A context carrying whatever the session has become. Rebuilt per turn."""
    profile = db.rows("conversations")[0].get("guest_profile") or {}
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=autonomy_level,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="config-1", config_version=1, model="",
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        language=language, session=dict(profile),
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def say(db, message: str, script: list, *, language: str | None = None,
        autonomy_level: int = 3):
    """
    One guest turn, and the session write the pipeline would have done after it.

    The session merge is the reason this helper exists rather than a bare
    `run_turn`: the thing under test is what survives *between* turns, and
    without the merge every turn would start from an empty profile and the
    suite would be seven copies of one-turn tests.
    """
    result = run(runtime.run_turn(
        ctx_for(db, autonomy_level=autonomy_level, language=language),
        message, provider=EchoProvider(list(script)),
    ))
    patch = session.patch_from(result.actions)
    patch["obligations"] = obligations.open_from(
        message, result.actions,
        (db.rows("conversations")[0].get("guest_profile") or {}).get("obligations"),
    )
    session.merge(CONVERSATION, patch)
    return result


def profile(db) -> dict:
    return db.rows("conversations")[0].get("guest_profile") or {}


def tools_used(db) -> list[str]:
    return [a["tool"] for a in sorted(db.rows("agent_actions"), key=lambda a: a["id"])]


def stay_options_result() -> dict:
    return {
        "checkIn": str(CHECK_IN), "checkOut": str(CHECK_OUT),
        "options": [{"roomTypeId": ROOM_TYPE, "name": "Deluxe Balcony", "unitsLeft": 6,
                     "ratePlans": [{"ratePlanId": RATE_PLAN, "name": "Room only"}]}],
    }


# ── ① simple booking ────────────────────────────────────────────────────────

def test_a_simple_booking_keeps_every_fact_the_guest_gave(db):
    """
    Hi → dates → guests → room → quote. The assertion is not that a booking
    happened; it is that by the last turn nothing has to be asked twice.

    The failure this stands against is the original transcript's ⑦: an hour's
    gap, and every fact re-asked from scratch.
    """
    say(db, "Hi, room chahiye", ["Namaste! Kaunse dates ke liye?"])
    say(db, f"{CHECK_IN} se {CHECK_OUT} tak", [
        [("search_availability", {"check_in": str(CHECK_IN),
                                  "check_out": str(CHECK_OUT), "units": 1, "adults": 2})],
        "Haan, rooms available hain.",
    ])
    say(db, "2 log hain", [
        [("get_stay_options", {"check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
                               "units": 1, "adults": 2})],
        "Deluxe Balcony available hai.",
    ])
    say(db, "Deluxe Balcony chahiye", [
        [("get_quote", {"room_type_id": ROOM_TYPE, "rate_plan_id": RATE_PLAN,
                        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
                        "units": 1, "adults": 2})],
        "Deluxe Balcony ka rate ye raha.",
    ])

    now = profile(db)
    assert now["check_in"] == str(CHECK_IN)
    assert now["check_out"] == str(CHECK_OUT)
    assert now["adults"] == 2
    assert now["room_type_id"] == ROOM_TYPE
    assert now["stage"] in ("quoted", "browsing")
    assert now["last_business_action"] == "get_quote"

    # And the prompt says so, which is the half that was missing: the room was
    # on the session and never in the system prompt, so the agent asked which
    # room a guest had already chosen.
    told = session.describe({"guest_profile": now})
    assert ROOM_TYPE in told
    assert "do not ask which room again" in told


# ── ② group ─────────────────────────────────────────────────────────────────

def test_a_group_request_is_priced_whether_or_not_the_model_asks(db):
    say(db, f"{CHECK_IN} se {CHECK_OUT} tak room chahiye", [
        [("search_availability", {"check_in": str(CHECK_IN),
                                  "check_out": str(CHECK_OUT), "units": 1, "adults": 2})],
        "Haan ji.",
    ])
    # The model does not call the group tool. The application does.
    result = say(db, "Actually 5 kamre chahiye, 10 log aa rahe hain",
                 ["Ji bilkul, main check karti hoon."])

    assert "get_group_rates" in tools_used(db)
    # **Two forced tools now, not one, and the second is the fix.**
    #
    # `get_group_rates` answers "is this a group and what band applies" — a
    # discount percentage and a deposit percentage. It does not and cannot
    # price, because pricing needs a room type it has not been given. A
    # production turn that forced only this one left the model holding two
    # percentages and a room count, and it invented five prices: ₹1,65,600,
    # ₹2,13,600, ₹50,400, ₹59,200, ₹89,600. The guardrail refused the reply and
    # the guest got a person instead of a quote.
    #
    # `get_stay_options` rather than `get_quote` because no room has been
    # chosen: it prices every room type and takes no hold, where a quote would
    # take a fifteen-minute hold on a room this guest has not picked.
    assert result.requirements["forced"] == ["get_group_rates", "get_stay_options"]
    group = [a for a in db.rows("agent_actions") if a["tool"] == "get_group_rates"][-1]
    assert group["decision_source"] == "application"
    assert group["input"]["units"] == 5
    # The hotel's band, not one anybody chose in prose.
    assert group["output"]["appliedBand"]["fromRooms"] == 3
    assert group["output"]["depositPct"] == 30


# ── ③ negotiation ───────────────────────────────────────────────────────────

def test_a_haggle_is_answered_by_the_engine_and_not_by_the_model(db):
    say(db, "Deluxe chahiye", [
        [("get_stay_options", {"check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
                               "units": 1, "adults": 2, "room_type_id": ROOM_TYPE})],
        "Deluxe Balcony available hai.",
    ])
    db.rows("conversations")[0]["guest_profile"].update(
        {"rate_plan_id": RATE_PLAN, "room_type_id": ROOM_TYPE}
    )

    result = say(db, "Yaar thoda kam karo na, 4000 per night mein ho jayega?",
                 ["Main dekhti hoon."])

    negotiations = [a for a in db.rows("agent_actions") if a["tool"] == "negotiate_rate"]
    assert negotiations, "the engine was never asked"
    assert negotiations[-1]["decision_source"] == "application"
    # §20: the guest's own number travels *into* the engine as a question and
    # never out of the model as an answer.
    assert negotiations[-1]["input"]["requested_nightly_rupees"] == 4000
    assert result.requirements["forced"] == ["negotiate_rate"]


def test_a_repeated_haggle_asks_the_engine_again_rather_than_relenting(db):
    db.rows("conversations")[0]["guest_profile"].update({
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
        "rate_plan_id": RATE_PLAN, "room_type_id": ROOM_TYPE,
        "units": 1, "adults": 2,
    })
    say(db, "thoda kam karo", ["Main dekhti hoon."])
    first = len([a for a in db.rows("agent_actions") if a["tool"] == "negotiate_rate"])
    say(db, "aur kam nahi ho sakta?", ["Ye best hai."])
    second = len([a for a in db.rows("agent_actions") if a["tool"] == "negotiate_rate"])

    assert second > first, (
        "the second push was answered from the model's memory of the first, "
        "which is how a discount grows one message at a time"
    )


# ── ④ payment ───────────────────────────────────────────────────────────────

def test_a_question_about_money_is_answered_by_looking(db):
    """
    The original transcript's ⑧, and the one that started all of this: the money
    had landed and the guest was told it would confirm once the payment
    processed. A guest saying they paid is not the money arriving.
    """
    result = say(db, "Maine payment kar di, check karo hui ya nahi?",
                 ["Main check karti hoon."])

    assert "get_booking_status" in tools_used(db)
    status = [a for a in db.rows("agent_actions") if a["tool"] == "get_booking_status"][-1]
    assert status["decision_source"] == "application"
    assert status["intent"] == "BOOKING_STATUS"
    assert result.requirements["forced"] == ["get_booking_status"]


# ── ⑤ the room they already chose ───────────────────────────────────────────

def test_a_room_chosen_once_survives_a_change_of_room_count(db):
    """§19: guest chooses Deluxe → changes room count → Deluxe stays selected."""
    db.rows("conversations")[0]["guest_profile"].update({
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
        "room_type_id": ROOM_TYPE, "room_name": "Deluxe Balcony",
        "rate_plan_id": RATE_PLAN, "units": 1, "adults": 2,
    })
    say(db, "3 kamre kar do", ["Teen kamre — dekh leti hoon."])

    now = profile(db)
    assert now["room_type_id"] == ROOM_TYPE, "the room they chose was thrown away"
    # …and the group tool was asked about *their* room, not the property.
    group = [a for a in db.rows("agent_actions") if a["tool"] == "get_group_rates"][-1]
    assert group["input"]["room_type_id"] == ROOM_TYPE


def test_a_room_chosen_once_survives_a_change_of_dates(db):
    """
    §19 and §9: the room stays, the price does not, and the agent is told to
    check availability again before promising it.
    """
    db.rows("conversations")[0]["guest_profile"].update({
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
        "room_type_id": ROOM_TYPE, "room_name": "Deluxe Balcony",
        "rate_plan_id": RATE_PLAN, "quote_id": "q-old", "units": 1, "adults": 2,
    })
    say(db, f"Dates change karni hain — {LATER_IN} se {LATER_OUT}", [
        [("search_availability", {"check_in": str(LATER_IN), "check_out": str(LATER_OUT),
                                  "units": 1, "adults": 2})],
        "Un dates par bhi available hai.",
    ])

    now = profile(db)
    assert now["check_in"] == str(LATER_IN)
    assert now["room_type_id"] == ROOM_TYPE, "a date change deleted the room choice"
    assert "quote_id" not in now, "last week's price survived a change of nights"


def test_a_room_that_sells_out_is_taken_off_the_session(db):
    """
    The other half, and the one that would embarrass a hotel: an agent whose
    prompt insists the guest has settled on a room that sold an hour ago.
    """
    db.rows("conversations")[0]["guest_profile"].update({
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
        "room_type_id": ROOM_TYPE, "room_name": "Deluxe Balcony",
        "rate_plan_id": RATE_PLAN, "units": 1, "adults": 2,
    })
    # The Deluxe sells out between one turn and the next.
    db.rpcs["property_availability"] = lambda params: [
        {"room_type_id": OTHER_ROOM, "date": str(CHECK_IN), "units_left": 4,
         "stop_sell": False},
        {"room_type_id": OTHER_ROOM, "date": str(CHECK_IN + timedelta(days=1)),
         "units_left": 4, "stop_sell": False},
    ]
    say(db, "abhi bhi available hai?", [
        [("search_availability", {"check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
                                  "units": 1, "adults": 2})],
        "Dekh leti hoon.",
    ])
    now = profile(db)
    assert "room_type_id" not in now
    assert now.get("room_unavailable") is True
    assert "no longer free" in session.describe({"guest_profile": now})


# ── ⑥ cancellation ──────────────────────────────────────────────────────────

def test_a_cancellation_asks_what_it_costs_before_it_costs_anything(db):
    """
    §14 and §19. The required action is the *quote*, never the cancellation:
    cancelling a stay because a guest typed the word "cancel" is exactly what a
    deterministic layer must never do. The guest hears the refund, says yes, and
    only then does the model call the tool that acts.
    """
    result = say(db, "Mujhe booking cancel karni hai, paisa wapas milega?",
                 ["Main dekh leti hoon."])

    assert "BOOKING_CANCELLATION" in result.requirements["intents"]
    assert result.requirements["required"] == ["get_cancellation_quote"]
    # It cannot succeed in this fixture — there is no reservation on the thread —
    # but what must be true is that `cancel_booking` was never the thing forced.
    assert "cancel_booking" not in result.requirements["forced"]


# ── ⑦ escalation ────────────────────────────────────────────────────────────

def test_a_handoff_stops_the_agent_rather_than_continuing_past_it(db):
    result = say(db, "5 kamre chahiye aur manager se baat karwao", [
        [("request_human_handoff", {"reason": "guest_asked"})],
        "Ek minute, team aapse baat karegi.",
    ])

    assert result.handoff is True
    assert db.rows("conversations")[0]["handler"] == "human_requested"
    # Not a single tool after the exit — including the group tool the message
    # would otherwise have required.
    assert "get_group_rates" not in tools_used(db)


# ── ⑧ the guardrail, which is the eighth journey ────────────────────────────

@pytest.mark.parametrize("reply,why", [
    # A payment link nothing produced.
    ("Yahan pay kariye: https://pay-casamandala.example.com/x9 — booking pakki.",
     "an unbacked payment link"),
    # An internal identifier asked of a guest.
    ("Booking aage badhane ke liye mujhe ek quote ID chahiye.",
     "an internal identifier"),
    # A raw uuid.
    (f"Aapka room {ROOM_TYPE} confirm ho gaya hai.", "a uuid"),
    # A price nothing computed.
    ("Aapke liye special rate ₹3,150 per night kar diya hai.", "an invented price"),
])
def test_a_reply_the_system_cannot_stand_behind_never_reaches_the_guest(db, reply, why):
    """
    §20, four ways. Each of these is true-sounding, none of them is backed by a
    tool, and every one of them is something a hotel would have to honour or
    apologise for. The guardrail replaces the reply and hands over — the guest
    gets a person rather than a lie.
    """
    result = say(db, "payment link bhejo", [reply])

    assert result.reply != reply, f"{why} reached the guest"
    assert result.handoff is True
    assert (result.error or "").startswith("guardrail:")


def test_the_guardrail_does_not_fire_on_an_honest_reply(db):
    """
    The control, and the more important half. A guardrail that refuses ordinary
    prose is one somebody switches off — and a switched-off guardrail is worse
    than no guardrail, because everybody still believes it is there.
    """
    honest = "Breakfast 8 se 10:30 tak garden restaurant mein milta hai."
    result = say(db, "breakfast kab tak milta hai?", [honest])

    assert result.reply == honest
    assert result.handoff is False
    assert result.error is None


def test_a_booking_reference_may_be_said_out_loud(db):
    """
    `TB-5CECKV` is what a guest is *supposed* to be given, and a guardrail that
    treated it like a uuid would break the confirmation message it exists to
    protect.
    """
    # A real confirmed booking on the thread, so `get_booking_status` has
    # something true to return. Backing it matters: the same sentence with
    # nothing behind it is a fabricated confirmation, and the parametrised test
    # above asserts that case is refused.
    db.seed("reservations", {
        "id": "a0a0a0a0-0001-4001-8001-000000000001", "ref": "TB-5CECKV",
        "property_id": PROPERTY, "partner_id": PARTNER, "status": "confirmed",
        "payment_status": "paid", "check_in": str(CHECK_IN),
        "check_out": str(CHECK_OUT), "nights": 2, "units": 1,
        "total_paise": 900000, "paid_paise": 900000,
        "guest_name": "Nisha Kapoor", "conversation_id": CONVERSATION,
        "room_type_id": ROOM_TYPE, "public_token": "tok-5cec",
    })
    db.rows("conversations")[0]["reservation_id"] = "a0a0a0a0-0001-4001-8001-000000000001"

    honest = "Aapki booking confirm ho gayi hai — reference TB-5CECKV."
    result = say(db, "confirm hui?", [
        [("get_booking_status", {})],
        honest,
    ])

    assert result.reply == honest, (
        "a booking reference is what a guest is *supposed* to be given; a "
        "guardrail that treats TB-5CECKV like a uuid breaks the confirmation "
        "message it exists to protect"
    )
    assert result.handoff is False
