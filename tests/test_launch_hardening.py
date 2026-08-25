"""
Four things that were wrong on the way to launch, each one now nailed down.

None of these were crashes. Every one of them was the product quietly doing
something reasonable-looking that a hotel owner or a guest would have read as
the product failing:

  · a booking gate that made the AI apologise for the hotel's own safety rule
  · a public chat that could raise a housekeeping job with no room on it
  · a domain box that accepted anything, including our own hostnames
  · an empty model completion that ended a guest's turn as "someone will reply"

They share a shape, which is why they share a file: the code was right and the
*conversation* it produced was wrong.
"""
import asyncio

import pytest

from app.services.ai import registry
from app.services.pms import domains


def run(coro):
    # No pytest-asyncio in this suite; `asyncio.run` per test is the idiom the
    # rest of tests/ uses.
    return asyncio.run(coro)


registry.load_all()


# ── a deferral is a fork, not a failure ──────────────────────────────────────


def test_create_booking_tells_the_model_what_to_do_instead():
    """
    The gate on `create_booking` is unconditional, so *every* guest who says
    "book it" reaches it. Told only that approval was required, the model
    apologised to them for it.
    """
    spec = registry.get("create_booking")
    assert spec.always_requires_approval, "the gate itself must not be relaxed"
    assert spec.when_deferred, "a tool nobody can execute must say what to do instead"

    hint = spec.when_deferred.lower()
    assert "create_booking_payment_link" in hint, "it has to name the road that works"
    assert "not apologise" in hint or "not apologize" in hint
    # And it must not let the model promise a hold that does not exist.
    assert "reserved" in hint and "do not" in hint


def test_the_payment_route_is_open_where_the_booking_route_is_gated():
    """The asymmetry the whole fix rests on. If both were gated, the AI
    employee could not complete a sale at all; if neither were, a model could
    take a room off sale on its own say-so."""
    assert registry.get("create_booking").always_requires_approval
    assert not registry.get("create_booking_payment_link").always_requires_approval


def test_the_guest_agent_can_reach_the_payment_route():
    from app.services.ai.agents import guest, sales

    for agent in (guest.DEFINITION, sales.DEFINITION):
        assert "create_booking_payment_link" in agent.tools, agent.agent_type


def test_every_ungrantable_tool_says_what_to_do_instead():
    """
    A tool that can never execute without a human is a tool the model will meet
    on a live conversation and have to explain. Every one of them gets a
    sentence written for that moment.
    """
    for spec in registry.REGISTRY.values():
        if spec.always_requires_approval:
            assert spec.when_deferred, (
                f"{spec.name} always queues for approval but tells the model nothing "
                "to do while it waits — which is how it ends up apologising"
            )


# ── a ticket nobody can act on ───────────────────────────────────────────────


def test_an_anonymous_visitor_cannot_raise_a_housekeeping_job():
    """
    The hotel's website is open to the internet and its visitors have no
    booking, no room and no phone number. This raised

        #1014  Two extra towels   room: —   guest: —

    for one of them: a job on a housekeeper's screen with no door to knock on,
    and a task queue open to anybody who can type.
    """
    from app.services.ai.errors import OutOfScope
    from app.services.ai.tools import operations

    class Ctx:
        property_id = "47f4cbf8-7cad-4369-a4c9-a4a70a0c29c0"
        partner_id = "ed8b3e94-6f79-42cf-be6d-c46eaf3652ac"
        conversation_id = None
        reservation_id = None
        guest_id = None
        guest_phone = None
        guest_name = None
        channel = "website"
        principal = None

    args = operations.CreateTaskArgs(
        title="Two extra towels", category="housekeeping", priority="normal",
    )
    with pytest.raises(OutOfScope) as exc:
        run(operations.create_task(args, Ctx()))

    # And the guest hears a question, not a refusal.
    said = exc.value.guest_message.lower()
    assert "room" in said
    assert "cannot" not in said and "sorry" not in said


def test_an_instagram_guest_we_have_never_met_still_gets_a_ticket():
    """
    The line is reachability, not identity.

    A complaint arriving by Instagram DM comes from a real account a member of
    staff can reply to, so a ticket with no room on it is still a job somebody
    can pick up and ask about. Refusing it would have broken the one channel
    this product launches on besides the website — which is how it was caught.
    """
    from app.services.ai.tools import operations

    seen = {}

    async def fake_create(property_id, **kw):
        seen.update(kw)
        return {"id": kw["task_id"], "code": "#1", "title": kw["title"],
                "category": kw["category"], "priority": kw["priority"],
                "status": "open", "roomNumber": kw.get("room_number")}

    class Ctx:
        property_id = "p"
        partner_id = "pa"
        conversation_id = "c"
        reservation_id = None
        guest_id = None
        guest_phone = None
        guest_name = None
        channel = "instagram"
        principal = None

    import app.services.pms.frontdesk as frontdesk_service
    original = frontdesk_service.create_task
    frontdesk_service.create_task = fake_create
    try:
        args = operations.CreateTaskArgs(
            title="Air conditioning not cooling", category="maintenance", priority="high",
        )
        run(operations.create_task(args, Ctx()))
    finally:
        frontdesk_service.create_task = original

    assert seen["title"] == "Air conditioning not cooling"
    # And no room was invented for somebody we cannot place.
    assert not seen.get("room_number")


def test_a_named_room_is_enough_to_raise_one():
    """A walk-in reporting a broken light in the lobby still gets a ticket."""
    from app.services.ai.tools import operations

    seen = {}

    async def fake_create(property_id, **kw):
        seen.update(kw)
        return {"id": kw["task_id"], "code": "#1", "title": kw["title"],
                "category": kw["category"], "priority": kw["priority"],
                "status": "open", "roomNumber": kw.get("room_number")}

    class Ctx:
        property_id = "p"
        partner_id = "pa"
        conversation_id = None
        reservation_id = None
        guest_id = None
        guest_phone = None
        guest_name = None
        channel = "website"
        principal = None

    import app.services.pms.frontdesk as frontdesk_service
    original = frontdesk_service.create_task
    frontdesk_service.create_task = fake_create
    try:
        args = operations.CreateTaskArgs(
            title="Light out in the lobby", category="maintenance",
            priority="normal", room_number="LOBBY",
        )
        run(operations.create_task(args, Ctx()))
    finally:
        frontdesk_service.create_task = original

    assert seen["room_number"] == "LOBBY"


# ── the hotel's own address ──────────────────────────────────────────────────


@pytest.mark.parametrize("typed,expected", [
    ("grandhotel.com", "grandhotel.com"),
    ("  GrandHotel.COM  ", "grandhotel.com"),
    ("https://www.grandhotel.com/rooms?x=1", "www.grandhotel.com"),
    ("http://grandhotel.com:8080/", "grandhotel.com"),
    ("grandhotel.com.", "grandhotel.com"),
])
def test_what_people_paste_is_accepted_as_a_hostname(typed, expected):
    # Owners paste what is in their browser bar. Refusing it would be correct
    # and would read, to them, as the box being broken.
    assert domains.validate(typed) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "localhost", "hotel", "-hotel.com", "hotel..com",
    "hostos.in", "app.hostos.in", "api.hostos.in",
    "partner.travbuddy.in", "travbuddy.in", "anything.vercel.app",
])
def test_what_may_not_be_claimed(bad):
    with pytest.raises(domains.DomainError):
        domains.validate(bad)


def test_our_own_hostnames_are_reserved_by_name_not_by_luck():
    """
    A hotel that claimed `app.hostos.in` would be claiming the staff app
    itself, and host resolution would then serve a booking page to everyone who
    tried to log in.

    `travbuddy.in` is still on the list after the rename because
    `partner.travbuddy.in` still resolves to this app — a name we redirect is
    still a name we own, and still one no hotel may claim.
    """
    assert "hostos.in" in domains.RESERVED_SUFFIXES
    assert "travbuddy.in" in domains.RESERVED_SUFFIXES
    with pytest.raises(domains.DomainError):
        domains.validate("staff.hostos.in")
    with pytest.raises(domains.DomainError):
        domains.validate("staff.travbuddy.in")


def test_a_refusal_is_a_sentence_a_hotel_owner_can_act_on():
    for bad in ("not-a-domain", "app.hostos.in", "partner.travbuddy.in", ""):
        try:
            domains.validate(bad)
        except domains.DomainError as exc:
            said = str(exc.detail).lower()
            for word in ("regex", "pattern", "hostname must", "validationerror", "none"):
                assert word not in said, f"{bad!r} → {exc.detail}"
            assert said.endswith(".") or said.endswith("address.")


def test_the_ownership_record_is_the_security_control():
    """
    Without it, this endpoint takes any string a hotelier typed and starts
    serving their booking page on it. The domain's real owner may never have
    heard of us.
    """
    assert domains.TXT_NAME == "_travbuddy"
    first = domains._new_token()
    assert first.startswith("travbuddy-verify=")
    assert first != domains._new_token(), "a guessable token proves nothing"
    assert len(first) > 30
