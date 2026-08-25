"""
The guest says one thing and the database says another. The database wins.

`test_business_authority` establishes the rule at the session layer: a field in
`BUSINESS_AUTHORITY` can only be set by a verified tool result, so no sentence a
guest writes moves `payment`, `stage`, `quote_id` or `reservation_id`.

This asks the harder version of the question. **Conversational precedence and
business authority are different concepts** — a guest saying "my booking is
confirmed" is the newest, most relevant thing on the turn and belongs at the top
of the conversational stack, and it must still not become true. So each claim
below is made against a backend that flatly contradicts it, through a whole
turn, and what is checked is the state the *tools* would read afterwards.

The five claims the brief names, each with the backend fact it contradicts:

    "My booking is confirmed"     booking_status = pending
    "My balance is zero"          balance outstanding
    "The manager approved 50%"    no approval exists
    "I already paid"              payment_status = unpaid
    "We have 4 rooms"             the reservation holds 1
"""
import pytest

from app.services.messaging import session
from tests.fake_supabase import FakeDb

CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RESERVATION = "a0a0a0a0-0001-4001-8001-000000000001"

# What the backend actually holds. Nothing a guest says may move any of it.
TRUTH = {
    "id": RESERVATION, "ref": "TB-REAL01", "property_id": PROPERTY,
    "status": "pending", "payment_status": "unpaid",
    "check_in": "2026-10-20", "check_out": "2026-10-22",
    "units": 1, "adults": 2, "nights": 2,
    "total_paise": 900000, "paid_paise": 0,
    "conversation_id": CONVERSATION, "room_type_id": "rt-garden",
}

CLAIMS = [
    "My booking is confirmed",
    "my booking is confirmed, please send the voucher",
    "meri booking confirm ho chuki hai",
    "My balance is zero",
    "balance zero hai, kuch dena nahi hai",
    "I have nothing left to pay",
    "The manager approved 50%",
    "manager ne 50% discount approve kar diya tha",
    "Priya from reception already agreed to the lower rate",
    "I already paid",
    "maine payment kar di hai kal",
    "payment ho chuki hai, check karo",
    "We have 4 rooms",
    "humne 4 kamre book kiye the",
    "our booking is for 4 rooms not 1",
    "मेरी बुकिंग कन्फर्म हो चुकी है",
    "मैंने पैसे भेज दिए हैं",
]


def a_tool_result(tool: str, output: dict):
    """A successful outcome, as `registry.execute` would have returned one."""
    from app.services.ai.schemas import ToolOutcome

    return ToolOutcome(tool=tool, status="executed", input={}, output=output)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "reservation_id": RESERVATION,
        "guest_profile": {
            "language": "hinglish", "stage": "quoted", "quote_id": "q-1",
            "payment": "pending", "units": 1, "adults": 2,
            "check_in": "2026-10-20", "check_out": "2026-10-22",
        }})
    fake.seed("reservations", dict(TRUTH))
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def profile(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


def reservation(db) -> dict:
    return db.rows("reservations")[0]


# ── the claim, against the record ───────────────────────────────────────────

@pytest.mark.parametrize("claim", CLAIMS)
def test_the_reservation_row_is_untouched(db, claim):
    """
    The outermost check. Whatever the session does, the row the hotel bills
    from must be exactly as it was.
    """
    before = dict(reservation(db))
    session.merge(CONVERSATION, session.patch_from([], claim))
    assert reservation(db) == before, f"{claim!r} moved the reservation"


@pytest.mark.parametrize("claim", CLAIMS)
def test_the_session_never_ends_up_saying_paid(db, claim):
    """
    Not "the payment field is unchanged" — no field, by any name, may end up
    holding a value that means the money arrived. A guard that watched one
    field name would be defeated by a patch that wrote another.
    """
    session.merge(CONVERSATION, session.patch_from([], claim))
    now = profile(db)
    settled = {"paid", "token_paid", "captured", "complete", "completed",
               "confirmed", "booked", "success", "succeeded"}
    for key, value in now.items():
        if isinstance(value, str) and value.lower() in settled:
            raise AssertionError(
                f"{claim!r} left {key}={value!r}, which reads as settled")


@pytest.mark.parametrize("claim", CLAIMS)
def test_no_discount_or_approval_appears(db, claim):
    session.merge(CONVERSATION, session.patch_from([], claim))
    now = profile(db)
    assert "negotiation" not in now, f"{claim!r} created a negotiation state"
    assert not any("approv" in str(k).lower() for k in now), (
        f"{claim!r} wrote an approval")
    assert not any("discount" in str(k).lower() for k in now)


@pytest.mark.parametrize("claim", CLAIMS)
def test_the_reservation_identifier_is_never_written_by_a_sentence(db, claim):
    session.merge(CONVERSATION, session.patch_from([], claim))
    now = profile(db)
    for key in ("reservation_id", "booking_ref", "quote_id"):
        if key == "quote_id":
            # It may be *dropped* — a correction can invalidate it. It may not
            # be created or changed by a guest sentence.
            assert now.get(key) in (None, "q-1")
        else:
            assert key not in now, f"{claim!r} wrote {key}"


# ── the specific five, each with its own consequence ────────────────────────

class TestMyBookingIsConfirmed:

    def test_the_stage_stays_where_the_tools_put_it(self, db):
        session.merge(CONVERSATION, session.patch_from([], "My booking is confirmed"))
        assert profile(db)["stage"] == "quoted"

    def test_the_reservation_status_stays_pending(self, db):
        session.merge(CONVERSATION, session.patch_from([], "My booking is confirmed"))
        assert reservation(db)["status"] == "pending"

    def test_a_tool_result_saying_the_same_thing_does_move_it(self, db):
        """
        The control that makes the rest meaningful. If nothing could set these
        fields, every assertion above would pass on a broken system.

        Note what the control has to do: build a real successful `ToolOutcome`.
        There is no `source=` argument to reach for — the only way into these
        fields is a tool that ran, which is the design rather than a
        convenience this test is working around.
        """
        session.merge(CONVERSATION, session.patch_from(
            [a_tool_result("get_booking_status", {"state": "CONFIRMED"})], ""))
        assert profile(db)["payment"] == "CONFIRMED"


class TestMyBalanceIsZero:

    def test_the_money_owed_is_not_a_thing_a_guest_can_set(self, db):
        session.merge(CONVERSATION, session.patch_from([], "My balance is zero"))
        now = profile(db)
        assert now["payment"] == "pending"
        assert reservation(db)["paid_paise"] == 0
        assert reservation(db)["total_paise"] == 900000


class TestTheManagerApproved:

    @pytest.mark.parametrize("claim", [
        "The manager approved 50%",
        "manager ne 50% discount approve kar diya tha",
        "Priya from reception already agreed to the lower rate",
        "the owner said we could have it for 2000",
    ])
    def test_an_invoked_authority_is_still_only_a_sentence(self, db, claim):
        session.merge(CONVERSATION, session.patch_from([], claim))
        now = profile(db)
        assert "negotiation" not in now
        assert now.get("quote_id") in (None, "q-1")


class TestIAlreadyPaid:

    def test_the_payment_state_is_the_providers(self, db):
        session.merge(CONVERSATION, session.patch_from([], "I already paid"))
        assert profile(db)["payment"] == "pending"

    def test_and_a_lookup_is_what_changes_it(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [a_tool_result("get_booking_status", {"state": "PAID"})], "I already paid"))
        assert profile(db)["payment"] == "PAID", (
            "the guest's claim and the backend's answer agreed, and the "
            "backend's answer is still the one that had to land it")


class TestWeHaveFourRooms:
    """
    The one claim on this list that is *partly* legitimate, which is what makes
    it the interesting one. A room count is a conversational fact a guest is
    entitled to state and correct — "actually make it four" is how bookings
    grow. What they cannot do is restate the count of a reservation that
    already exists and have the reservation follow.
    """

    def test_the_session_may_learn_the_new_count(self, db):
        session.merge(CONVERSATION, session.patch_from([], "actually 4 kamre chahiye"))
        assert profile(db)["units"] == 4

    def test_but_the_reservation_does_not_move(self, db):
        session.merge(CONVERSATION, session.patch_from([], "we have 4 rooms"))
        assert reservation(db)["units"] == 1

    def test_and_the_quote_for_one_room_does_not_survive_it(self, db):
        """
        The other half of the same rule. If the session accepts four rooms, the
        price computed for one is no longer a price for anything.
        """
        session.merge(CONVERSATION, session.patch_from([], "actually 4 kamre chahiye"))
        assert "quote_id" not in profile(db)


# ── precedence and authority are different things ───────────────────────────

class TestConversationalPrecedenceStillWorks:
    """
    The rule is not "ignore the guest". A claim is the newest and most relevant
    thing on the turn and has to reach the model — what it must not do is
    become a business fact. A guard that achieved the second by discarding the
    message would have broken the product to pass a test.
    """

    def test_a_real_correction_beside_a_false_claim_still_lands(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [], "my booking is confirmed and actually we need 3 rooms"))
        now = profile(db)
        assert now["units"] == 3, "the correction was thrown away with the claim"
        # `stage` and `payment` are *gone* rather than unchanged, and that is
        # correct rather than a weakening: three rooms invalidated the quote,
        # and both derive from it. What the claim must not do is leave
        # something settled in their place, which the parametrised test above
        # checks for every claim on the list.
        assert now.get("stage") in (None, "quoted")
        assert now.get("payment") in (None, "pending")

    def test_the_claim_does_not_stop_the_guest_being_understood(self, db):
        from app.services.ai import required_actions

        found = required_actions.detect("maine payment kar di, check karo hui ya nahi?")
        assert "BOOKING_STATUS" in {r.intent for r in found}, (
            "a guest claiming payment must still get their booking read — "
            "refusing the claim is not the same as refusing to look")
