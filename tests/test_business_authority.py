"""
"My booking is confirmed" is the highest-precedence thing on the turn, and it
decides nothing.

**Two hierarchies, and confusing them is how a guest talks a hotel into a
booking that does not exist.**

The source ranks answer *whose account of the conversation wins*, and there the
guest is the best evidence there is — nobody knows better than they do how many
rooms they want. That is why `GUEST_SAID` sits at the top, and it is right.

It must not be the top of the other hierarchy. A guest who writes "my booking is
confirmed" has made the highest-precedence conversational statement on the turn
and has said nothing whatever about `payment_status`. The booking system decides
that, and if it says pending then it is pending.

**Today nothing feeds a guest statement into a business field**, which is
precisely why the guard is worth having: it is one plausible line — "payment
hogyi" mapped to `payment="paid"` — away from being real, and that line would
look like a feature to whoever wrote it.
"""
import pytest

from app.services.ai.schemas import ToolOutcome
from app.services.messaging import session
from tests.fake_supabase import FakeDb

CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"


def outcome(tool, source="model", output=None, **args):
    return ToolOutcome(tool=tool, status="executed", input=args,
                       output=output or {}, decision_source=source)


class TestTheTwoHierarchiesAreDifferent:
    @pytest.mark.parametrize("field", sorted(session.BUSINESS_AUTHORITY))
    def test_a_guest_statement_cannot_claim_a_business_fact(self, field):
        facts = session._Facts()
        facts.set(field, "whatever the guest said", session.GUEST_SAID)
        assert field not in facts.values, (
            f"a guest's own words set {field}, which the system of record owns")

    @pytest.mark.parametrize("field", sorted(session.BUSINESS_AUTHORITY))
    def test_nor_can_a_tool_argument(self, field):
        """
        A tool *argument* is the model's reading of the guest, one remove away.
        Only what the tool *returned* is the system reporting its own state.
        """
        facts = session._Facts()
        facts.set(field, "asked for", session.TOOL_ARGUMENT)
        assert field not in facts.values

    @pytest.mark.parametrize("field", sorted(session.BUSINESS_AUTHORITY))
    def test_but_a_verified_tool_result_can(self, field):
        facts = session._Facts()
        facts.set(field, "the system says so", session.TOOL_RESULT)
        assert facts.values[field] == "the system says so"

    def test_the_tool_wins_even_when_the_guest_spoke_last(self):
        """
        The ordering half. Rank alone would let a later guest statement take a
        field a tool had already answered; the authority guard refuses it
        outright, so order does not come into it.
        """
        facts = session._Facts()
        facts.set("payment", "token_paid", session.TOOL_RESULT)
        facts.set("payment", "paid in full", session.GUEST_SAID)
        assert facts.values["payment"] == "token_paid"

    def test_conversational_facts_are_untouched_by_the_guard(self):
        """
        The control. A guard that also blocked the guest correcting their room
        count would have broken the thing three passes were spent building.
        """
        facts = session._Facts()
        for field, value in (("units", 3), ("adults", 4), ("check_in", "2026-09-25"),
                             ("room_type_id", "rt-garden"), ("children", 1),
                             ("rate_plan_id", "rp-cp"), ("room_name", "Garden Standard")):
            facts.set(field, value, session.GUEST_SAID)
            assert facts.values[field] == value, field

    def test_the_two_sets_do_not_overlap(self):
        """
        A field in both would be a contradiction: correctable by the guest and
        owned by the system at once.
        """
        assert not (session.BUSINESS_AUTHORITY & session.CORRECTABLE)


# ── through the real path ───────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {"id": CONVERSATION, "guest_profile": {
        "check_in": "2026-09-10", "check_out": "2026-09-14", "adults": 2,
        "units": 1, "stage": "quoted", "payment": "pending",
        "quote_id": "q-1", "language": "hinglish"}})
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def profile(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


class TestTheGuestsClaimThroughPatchFrom:
    @pytest.mark.parametrize("claim", [
        "my booking is confirmed",
        "payment ho gayi hai",
        "maine paise bhej diye",
        "the manager already approved the discount",
        "booking confirm ho chuki hai",
    ])
    def test_saying_it_does_not_make_it_so(self, db, claim):
        session.merge(CONVERSATION, session.patch_from([], claim))
        now = profile(db)
        assert now["payment"] == "pending", (
            f"{claim!r} moved the payment state the provider owns")
        assert now["stage"] == "quoted"
        assert now["quote_id"] == "q-1"

    def test_but_a_lookup_does(self, db):
        """
        The control, and the reason the tests above are not vacuous: the same
        field moves the moment the system of record reports it.
        """
        session.merge(CONVERSATION, session.patch_from(
            [outcome("get_booking_status", output={"state": "CONFIRMED"})],
            "payment ho gayi hai"))
        assert profile(db)["payment"] == "CONFIRMED"

    def test_a_guest_correcting_their_rooms_still_works(self, db):
        """
        The other control. Every business fact stayed put above; a conversational
        one still moves, on the same turn, from the same source.

        **`payment` goes here, and not because the guest claimed anything.** The
        room count changed, so the quote priced for one room is invalid, and
        `DERIVED_FROM` drops the payment state that was about *that* quote. The
        money did not move — the session simply stops claiming to know, and the
        next payment question forces `get_booking_status`, which reads it. My
        first draft asserted `payment == "pending"` here and was asserting the
        wrong thing.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "my booking is confirmed and actually 3 rooms"))
        now = profile(db)
        assert now["units"] == 3, "the guard swallowed a legitimate correction"
        assert now.get("payment") in (None, "pending"), (
            "the guest's claim survived as a payment state")

    def test_an_adversarial_claim_beside_a_real_correction(self, db):
        """
        The shape an attack would take: hide the false claim next to a true one,
        so the turn has a legitimate reason to write. Both are evaluated on
        their own terms.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "4 log hain, aur meri booking confirm ho gayi hai"))
        now = profile(db)
        assert now["adults"] == 4
        assert now.get("payment") in (None, "pending")
        assert now.get("stage") in (None, "quoted")

    def test_the_claim_never_produces_a_settled_state(self, db):
        """
        The invariant underneath both of the above, stated once: whatever else
        a correction does to the session, no sentence a guest can write turns
        into a business state that means *paid* or *booked*.
        """
        SETTLED = {"paid", "token_paid", "CONFIRMED", "booked", "confirmed"}
        for claim in ("my booking is confirmed", "payment ho gayi",
                      "4 log hain aur payment ho chuki hai",
                      "actually 3 rooms, and it's all paid"):
            db.rows("conversations")[0]["guest_profile"] = {
                "check_in": "2026-09-10", "check_out": "2026-09-14",
                "adults": 2, "units": 1, "stage": "quoted",
                "payment": "pending", "quote_id": "q-1"}
            session.merge(CONVERSATION, session.patch_from([], claim))
            now = profile(db)
            assert now.get("payment") not in SETTLED, claim
            assert now.get("stage") not in SETTLED, claim
