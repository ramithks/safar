"""
A test suite must not be able to eat the hotel it runs on.

**What happened.** `room_inventory.units_booked` is owned by two RPCs:
`convert_hold` adds to it when a booking confirms, `release_booking` subtracts
when one goes away. Test teardown wrote

    update public.reservations set status = 'cancelled' where …

which goes past both. No trigger catches it, because until this build the
product had no cancellation path at all — only the suites produced that status.
So the counter moved one way only, every run, for weeks.

It reached **377 room-nights sold to nobody at Casa Mandala**. Deluxe Balcony —
the room the reference conversation is about — had fifteen of its sixteen units
gone across the replay's own dates, so the replay could no longer sell the room
its own script asks for. The model was told the guest's choice was unavailable
and every assertion after the quote failed. Two sessions went looking for the
regression that explained it. There was none.

**Why this file is mostly a grep.** The bug was not in any function; it was in a
*habit*. Nothing was wrong with the code that ran — the wrong thing was the
statement somebody typed in a teardown block six months earlier, in a script no
unit test imports. The only durable defence is a rule that fails on the day
somebody types it again, and that rule has to be checked against the source
because there is nowhere else it exists.

So: two behavioural tests on the helper, and one that reads every script in the
repository and refuses the raw statement.
"""
import pathlib
import re
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import e2e_sweep  # noqa: E402

from tests.fake_supabase import FakeDb  # noqa: E402

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RESERVATION = "a0a0a0a0-0001-4001-8001-000000000001"
ROOM_TYPE = "22222222-2222-4222-8222-222222222222"


def make_db(status: str = "confirmed") -> FakeDb:
    fake = FakeDb()
    fake.seed("reservations", {
        "id": RESERVATION, "status": status, "room_type_id": ROOM_TYPE,
        "check_in": "2026-09-10", "check_out": "2026-09-12", "units": 2,
    })
    return fake


# ── the helper does what the product does ───────────────────────────────────

def test_cancelling_a_test_booking_gives_the_rooms_back():
    db = make_db()
    assert e2e_sweep.cancel_reservation(db, RESERVATION) is True

    released = [c for c in db.rpc_calls if c[0] == "release_booking"]
    assert len(released) == 1
    assert released[0][1] == {
        "p_room_type": ROOM_TYPE, "p_check_in": "2026-09-10",
        "p_check_out": "2026-09-12", "p_units": 2,
    }
    assert db.rows("reservations")[0]["status"] == "cancelled"


def test_the_rooms_come_back_before_the_row_is_marked_dead():
    """
    The order a crash can survive. Stock released against a booking still marked
    confirmed is visible to the night audit and fixable by a later sweep; a
    cancelled booking still holding stock is the invisible failure that took two
    sessions to find.
    """
    db = make_db()
    seen: list[str] = []
    original = db.rpc
    db.rpc = lambda name, params=None: (
        seen.append(db.rows("reservations")[0]["status"]) or original(name, params)
    )

    e2e_sweep.cancel_reservation(db, RESERVATION)
    assert seen == ["confirmed"]


def test_cancelling_twice_releases_once():
    """
    Releasing a second time subtracts twice and puts inventory *above* what the
    hotel has — the same corruption from the other end, and the one that
    manufactures an oversell.
    """
    db = make_db()
    assert e2e_sweep.cancel_reservation(db, RESERVATION) is True
    assert e2e_sweep.cancel_reservation(db, RESERVATION) is False
    assert len([c for c in db.rpc_calls if c[0] == "release_booking"]) == 1


def test_a_booking_that_never_consumed_inventory_releases_nothing():
    """
    A `pending` reservation never called `book_inventory`. Releasing against it
    would hand the hotel stock it never sold.
    """
    db = make_db(status="pending")
    e2e_sweep.cancel_reservation(db, RESERVATION)
    assert [c for c in db.rpc_calls if c[0] == "release_booking"] == []
    assert db.rows("reservations")[0]["status"] == "cancelled"


def test_an_unknown_reservation_is_a_no_op():
    db = make_db()
    assert e2e_sweep.cancel_reservation(db, "nope") is False
    assert db.rpc_calls == []


# ── the SQL the management-API suites use ───────────────────────────────────

def test_the_sql_form_releases_before_it_cancels():
    """
    `b2b_e2e` and `creator_e2e` have no Supabase client and drive everything
    through the management SQL endpoint, so they get the rule as a string. The
    order has to be the same one.
    """
    statement = e2e_sweep.cancel_reservation_sql(RESERVATION)
    assert statement.index("release_booking") < statement.index("update public.reservations")
    assert "status = 'confirmed'" in statement, (
        "without the guard, running it twice releases twice"
    )


def test_reinstating_puts_the_rooms_back_out():
    """
    `creator_e2e` cancels a stay to prove a payout goes to zero and reinstates
    it to prove the payout comes back. Cancelling through `release_booking` and
    reinstating with a bare UPDATE would leave the counter one room short for
    ever.
    """
    statement = e2e_sweep.reinstate_reservation_sql(RESERVATION)
    assert statement.index("book_inventory") < statement.index("update public.reservations")
    assert "'cancelled', 'no_show'" in statement


# ── the rule, enforced against the source ───────────────────────────────────

# `status = 'cancelled'` written straight into an UPDATE. Whitespace is loose
# because the offending statements were formatted four different ways across
# the four scripts that had them.
RAW_CANCEL = re.compile(
    r"update\s+(?:public\.)?reservations[^;]*?\bset\b[^;]*?"
    r"status\s*=\s*'cancelled'",
    re.IGNORECASE | re.DOTALL,
)

# **And the same bug through the other door.**
#
# The rule above only ever looked for the UPDATE form, and `e2e_sweep`'s own
# docstring names the other one in the same breath — "an UPDATE that sets
# `status = 'cancelled'`, *a DELETE from a fixture teardown*". A DELETE goes
# past `release_booking` exactly as the UPDATE does, and it is worse in one
# respect: afterwards there is no row left to compute the release from.
#
# Found by `reconcile_inventory` after a post-deployment run, not by this file:
# five room-nights sold to nobody at Casa Mandala on 2026-12-22, 2026-12-23 and
# 2027-03-12. `booking_engine_e2e` books at +120 and +200 days and had a
# hand-rolled `sweep()` with a bare DELETE in it, so every green run of that
# suite took two more Garden Standards and a Pool Villa out of the hotel.
#
# A guard that catches one spelling of a rule and not the other is how a rule
# survives being broken.
RAW_DELETE = re.compile(
    r"delete\s+from\s+(?:public\.)?reservations",
    re.IGNORECASE,
)

# The two files allowed to contain it, and why.
#
#   e2e_sweep          writes the sanctioned statement; this is where it lives.
#   replay_e2e         releases the stock itself immediately above, in the same
#                      function, and its UPDATE is guarded on not-already-dead.
#                      Asserted below rather than merely trusted.
EXEMPT = {"e2e_sweep.py", "replay_e2e.py", "reconcile_inventory.py"}


def scripts() -> list[pathlib.Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name not in EXEMPT)


# Suites that delete reservations and are *known* not to leak, with the
# evidence rather than the assurance.
#
# **Why an allow-list and not a rule.** Whether a DELETE needs a release
# depends on whether that reservation ever consumed inventory, and only
# `convert_hold` and `book_inventory` move the counter — a fixture row INSERTed
# straight into the table never touched it, and releasing against one of those
# pushes inventory *above* what the hotel has, building an oversell out of a
# cleanup. Which of the two a given suite does is not reliably decidable from
# its source: I tried, and the heuristic flagged four suites that are fine.
#
# So the general backstop is `scripts/reconcile_inventory.py`, which recomputes
# the counter from the reservations that are actually live and is what found
# this in the first place. It reports zero disagreement on every property these
# suites touch. This list records that, so the next person to add a DELETE has
# to say which case they are in.
DELETES_WITHOUT_RELEASING = {
    # Fixture rows written straight to `reservations`; never converted a hold.
    "inbox_e2e.py", "checkin_e2e.py", "owner_e2e.py", "marketing_e2e.py",
    # Delete on their own test properties, which reconcile reports clean.
    "creator_e2e.py", "pms_e2e.py", "pilot_e2e.py", "b2b_e2e.py",
    "domain_e2e.py", "extract_e2e.py", "hotel_os_e2e.py", "frontdesk_e2e.py",
    "onboarding_e2e.py", "billing_journey_e2e.py", "workspace_e2e.py",
    "sales_e2e.py", "golden_e2e.py", "ai_e2e.py", "demo_hotel.py",
    "migration_pricing_e2e.py", "partner_migration_e2e.py",
    "tenant_isolation_e2e.py", "tenant_security_e2e.py", "credits_purchase_e2e.py",
    "billing_e2e.py", "instagram_e2e.py", "celery_ordering_e2e.py",
    # Tears down its own throwaway property entirely — the inventory rows go
    # with it, so there is no counter left to disagree with anything.
    "pilot_fixture.py",
    "negotiation_e2e.py", "payment_status_e2e.py", "whatsapp_e2e.py",
    "creator_e2e.py", "prod_smoke.py", "smoke_test.py", "e2e_fixture.py",
}


def test_the_suite_that_actually_leaked_now_gives_its_rooms_back():
    """
    `booking_engine_e2e` pays a real Razorpay order, the webhook converts the
    hold, and its hand-rolled `sweep()` deleted the result with a bare DELETE.
    Every green run took two Garden Standards and a Pool Villa out of Casa
    Mandala for ever — on 2026-12-22 and 2027-03-12, which are exactly its
    +120 and +200 day fixtures. Found by `reconcile_inventory` after a
    post-deployment run, five room-nights sold to nobody.
    """
    source = (SCRIPTS / "booking_engine_e2e.py").read_text()
    found = RAW_DELETE.search(source)
    assert found, "the suite no longer deletes reservations; this test is stale"
    assert "release_booking" in source, (
        "booking_engine_e2e deletes reservations it booked through the payment "
        "path and no longer releases the stock first"
    )
    assert source.index("release_booking") < found.start(), (
        "the release has to happen before the delete — afterwards there is no "
        "row left to compute it from"
    )
    # Only rows that actually consumed inventory.
    release_block = source[source.index("release_booking") - 400:found.start()]
    assert "confirmed" in release_block, (
        "releasing against a reservation that never reached 'confirmed' "
        "subtracts a booking nobody made"
    )


@pytest.mark.parametrize("path", scripts(), ids=lambda p: p.name)
def test_a_new_suite_that_deletes_reservations_has_to_declare_itself(path):
    """
    Not a rule about correctness — a rule about *having thought about it*.

    A suite added next month that deletes reservations is in one of two
    categories, and the difference is a room-night per run either way. This
    fails until somebody puts it in the list or releases the stock, which is
    the smallest possible speed bump in front of a bug that took two sessions
    to find the first time.
    """
    source = path.read_text()
    if not RAW_DELETE.search(source):
        return
    if "release_booking" in source:
        return
    assert path.name in DELETES_WITHOUT_RELEASING, (
        f"{path.name} deletes reservations without releasing inventory. If its "
        f"rows are fixtures written straight to the table, that is correct — "
        f"add it to DELETES_WITHOUT_RELEASING. If it books through the product, "
        f"release the stock first, as booking_engine_e2e now does."
    )


@pytest.mark.parametrize("path", scripts(), ids=lambda p: p.name)
def test_no_suite_cancels_a_reservation_behind_the_inventory_rpcs(path):
    """
    **The regression test §13 asks for.** A suite that types this again fails
    here, on the day it is typed, rather than in six weeks when a hotel's
    availability has quietly become fiction.
    """
    found = RAW_CANCEL.search(path.read_text())
    assert not found, (
        f"{path.name} sets a reservation to 'cancelled' with a raw UPDATE, which "
        f"bypasses release_booking and takes the rooms out of the hotel "
        f"permanently. Use e2e_sweep.cancel_reservation() or "
        f"e2e_sweep.cancel_reservation_sql(). Found: {found.group(0)[:120]!r}"
    )


def test_the_one_exempt_suite_releases_its_own_stock_first():
    """
    `replay_e2e` is exempt because it does the release itself, in the same
    function, a dozen lines above. Exemptions that nobody checks are how a rule
    stops being a rule — so the exemption is verified rather than granted.
    """
    source = (SCRIPTS / "replay_e2e.py").read_text()
    sweep = source[source.index("def sweep()"):source.index("def main()")]

    assert "release_booking" in sweep
    assert sweep.index("release_booking") < sweep.index("status = 'cancelled'")
    assert "status not in ('cancelled','no_show')" in sweep, (
        "the UPDATE must skip rows already dead, or a second sweep releases twice"
    )


def test_the_product_now_has_a_cancellation_path_of_its_own():
    """
    The deeper fix. The reason teardown reached for raw SQL at all was that
    there was nothing else — the audit found no cancellation anywhere in the
    system, so "make this reservation dead" had no supported spelling.

    Now it does, and it goes through the same RPC.
    """
    from app.services.pms import cancellation

    assert hasattr(cancellation, "cancel")
    source = pathlib.Path(cancellation.__file__).read_text()
    assert "release_booking" in source
    assert source.index("release_booking") < source.index('"status": "cancelled"')


# ── the other shared fixture, and the same lesson ───────────────────────────

class TestAutonomyIsAlsoSharedState:
    """
    Inventory was not the only demo-wide thing a suite could quietly move.

    `agents.autonomy_level` is shared exactly as `room_inventory` is: several
    e2e suites set the demo hotel's guest agent to 2 for their own assertions,
    and none of them put it back. That is defensible on its own — until
    something else reads it and reports a number.

    **What it cost.** A live evaluation run reported negotiation accuracy of
    7% and a handoff rate of 90%, which reads as a catastrophic model
    regression and sent me looking for one. There was none. An earlier suite
    had left the agent at level 2, `negotiate_rate` and `get_quote` both sit at
    autonomy floor 3, and below their floor they queue for approval rather than
    executing — so the agent handed off, correctly, thirteen times. Every one
    of those "failures" was the autonomy ladder doing its job.

    The failure mode is identical to the 377 phantom room-nights: a shared
    fixture mutated by one suite and read as truth by another. The defence is
    the same shape too — the reader states what it needs and sets it, rather
    than hoping.
    """

    def test_the_selling_tools_really_do_need_level_three(self):
        """
        The premise. If these floors ever drop, the eval's `REQUIRED_AUTONOMY`
        is over-privileging the agent it measures and should drop with them.
        """
        from app.services.ai import registry

        registry.load_all()
        for name in ("get_quote", "negotiate_rate", "cancel_booking"):
            assert registry.REGISTRY[name].autonomy_floor == 3, name

    def test_the_evaluator_states_the_level_it_needs(self):
        import scripts.agent_eval as ev  # noqa: PLC0415

        assert ev.REQUIRED_AUTONOMY == 3
        assert hasattr(ev, "prepare_property")

    def test_the_evaluator_refuses_to_touch_a_real_hotel(self):
        """
        Raising a customer's autonomy because a script wanted a cleaner number
        would be changing their configuration to make a report look better.
        The guard is `is_demo`, and it is asserted rather than trusted.
        """
        import inspect

        import scripts.agent_eval as ev  # noqa: PLC0415

        source = inspect.getsource(ev.prepare_property)
        assert "is_demo" in source
        assert "SystemExit" in source

    def test_the_measurement_records_the_level_it_was_taken_at(self):
        """
        A number produced at the wrong autonomy is not a worse number, it is a
        different measurement — and one that cannot be told apart from the
        right one after the fact unless the level travels with it.
        """
        import inspect

        import scripts.agent_eval as ev  # noqa: PLC0415

        source = inspect.getsource(ev.main)
        assert '"autonomy_level"' in source
        assert '"autonomy_was"' in source

# ── the runtime check §10 asked for ──────────────────────────────────────────
#
# Everything above this line is static: a regex over suite source, and a list of
# files that have declared which case they are in. That is a speed bump, and it
# is the right speed bump — but §10 is explicit that it is not enough, and it is
# right for a reason worth writing down.
#
# **A static reading cannot answer the question.** Whether a DELETE needs a
# release depends on whether that reservation ever consumed inventory, which is
# a fact about what ran, not about how the statement is spelled. A fixture row
# INSERTed straight into `reservations` never moved the counter; releasing one
# subtracts a booking nobody made and builds an *oversell* out of a cleanup —
# the dangerous direction. I tried to decide it from source and the heuristic
# flagged four suites that were fine, which is how a guard gets muted.
#
# So the decision moves to runtime, per row, and the invariant is checked by
# recomputing rather than by reasoning about syntax.

class TestTheSanctionedCleanup:
    """
    One way to delete a reservation, and it decides per row what to give back.
    """

    def _db(self):
        """Rows shaped the way `cancel_reservation` reads them."""
        db = FakeDb()
        return db

    @staticmethod
    def _reservation(id_: str, status: str) -> dict:
        return {"id": id_, "status": status, "room_type_id": ROOM_TYPE,
                "check_in": "2026-09-10", "check_out": "2026-09-12", "units": 2}

    def test_a_live_reservation_gives_its_stock_back_before_it_goes(self):
        db = self._db()
        db.seed("reservations", self._reservation("r-1", "confirmed"))
        result = e2e_sweep.delete_reservations(db, ["r-1"])

        released = [c for c in db.rpc_calls if c[0] == "release_booking"]
        assert len(released) == 1
        assert released[0][1]["p_units"] == 2
        assert result == {"deleted": 1, "released": 1, "already_dead": 0}
        assert db.rows("reservations") == []

    @pytest.mark.parametrize("status", ["cancelled", "no_show"])
    def test_a_reservation_that_already_gave_its_stock_back_does_not_give_it_twice(
        self, status,
    ):
        """
        The direction that builds an oversell. A cancelled reservation released
        its rooms when it was cancelled; releasing again pushes `units_booked`
        below the truth, and the hotel sells a room it does not have.
        """
        db = self._db()
        db.seed("reservations", self._reservation("r-1", status))
        result = e2e_sweep.delete_reservations(db, ["r-1"])

        assert [c for c in db.rpc_calls if c[0] == "release_booking"] == []
        assert result["already_dead"] == 1
        assert result["released"] == 0

    def test_every_row_is_accounted_for_one_way_or_the_other(self):
        db = self._db()
        db.seed("reservations",
                self._reservation("r-1", "confirmed"),
                self._reservation("r-2", "cancelled"),
                self._reservation("r-3", "confirmed"))
        result = e2e_sweep.delete_reservations(db, ["r-1", "r-2", "r-3"])

        assert result["released"] + result["already_dead"] == result["deleted"] == 3

    def test_a_release_that_fails_does_not_leave_the_row_behind(self):
        """
        Called from teardown, where raising loses the suite's own result. A row
        left behind is debris somebody can see; a suite that died in cleanup is
        a red run nobody can read.
        """
        db = self._db()
        db.seed("reservations", self._reservation("r-1", "confirmed"))

        def _explode(args):
            raise RuntimeError("rpc unavailable")

        db.rpcs["release_booking"] = _explode

        result = e2e_sweep.delete_reservations(db, ["r-1"])
        assert result["deleted"] == 1
        assert db.rows("reservations") == []

    def test_nothing_to_delete_touches_nothing(self):
        db = self._db()
        assert e2e_sweep.delete_reservations(db, []) == {
            "deleted": 0, "released": 0, "already_dead": 0}


class TestTheInvariantItself:
    """
    *Tests must never leave production-style inventory inconsistent* — checked
    by recomputing the counter from the reservations, which is the only thing
    that can tell debris from an oversell.
    """

    def _seeded(self, units_booked: int):
        db = FakeDb()
        db.seed("room_types", {"id": "rt-1", "property_id": PROPERTY,
                               "name": "Garden Standard"})
        db.seed("reservations", {
            "id": "r-1", "property_id": PROPERTY, "ref": "TB-TEST",
            "status": "confirmed", "check_in": "2026-12-22",
            "check_out": "2026-12-23", "units": 1, "room_type_id": "rt-1"})
        db.seed("room_inventory", {
            "id": "i-1", "property_id": PROPERTY, "room_type_id": "rt-1",
            "date": "2026-12-22", "units_booked": units_booked})
        return db

    def test_a_property_that_agrees_passes_silently(self):
        e2e_sweep.assert_inventory_agrees(self._seeded(1), PROPERTY)

    def test_the_exact_failure_that_was_found_in_production(self):
        """
        `booking_engine_e2e`'s +120 day fixture: a reservation deleted with a
        bare DELETE, its room-night still on the counter. Casa Mandala,
        2026-12-22, one Garden Standard sold to nobody.
        """
        db = self._seeded(1)
        db.table("reservations").delete().eq("id", "r-1").execute()

        with pytest.raises(AssertionError) as caught:
            e2e_sweep.assert_inventory_agrees(db, PROPERTY, label="booking_engine_e2e")

        message = str(caught.value)
        assert "1 room-nights stranded" in message
        assert "2026-12-22" in message
        assert "booking_engine_e2e" in message
        # And it says what to do about it, because a failure nobody can act on
        # is a failure somebody deletes.
        assert "reconcile_inventory.py" in message
        assert "delete_reservations" in message

    def test_the_dangerous_direction_is_reported_as_such(self):
        """
        A counter *below* the live reservations is not debris — it is rooms
        sold that nothing is counting, and it must not be quietly tidied.
        """
        db = self._seeded(0)
        with pytest.raises(AssertionError) as caught:
            e2e_sweep.assert_inventory_agrees(db, PROPERTY)
        assert "1 oversold" in str(caught.value)

    def test_the_sanctioned_delete_leaves_the_invariant_intact(self):
        """
        The two halves closing the loop: the API releases, and the invariant
        agrees afterwards. This is the test that would have failed on the run
        that stranded five room-nights.
        """
        db = self._seeded(1)
        db.rpcs["release_booking"] = lambda args: db.table("room_inventory") \
            .update({"units_booked": 0}).eq("id", "i-1").execute()

        e2e_sweep.delete_reservations(db, ["r-1"])
        e2e_sweep.assert_inventory_agrees(db, PROPERTY)

    def test_the_suite_and_the_repair_tool_share_one_definition(self):
        """
        `assert_inventory_agrees` imports `reconcile_inventory.disagreements`
        rather than recomputing. Two definitions of "consistent" is how a suite
        passes against a hotel the repair tool would still change.
        """
        import inspect
        source = inspect.getsource(e2e_sweep.inventory_disagreements)
        assert "from reconcile_inventory import disagreements" in source
