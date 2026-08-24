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

# The two files allowed to contain it, and why.
#
#   e2e_sweep          writes the sanctioned statement; this is where it lives.
#   replay_e2e         releases the stock itself immediately above, in the same
#                      function, and its UPDATE is guarded on not-already-dead.
#                      Asserted below rather than merely trusted.
EXEMPT = {"e2e_sweep.py", "replay_e2e.py", "reconcile_inventory.py"}


def scripts() -> list[pathlib.Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name not in EXEMPT)


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
