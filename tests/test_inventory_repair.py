"""
Eighteen oversold units, and the two things that were wrong about that number.

The first: **it was twenty-four.** `reconcile_inventory.disagreements` walked
`room_inventory` and compared each row against the live reservations, so it
could only ever find a night the ledger already knew about. Three unswept
fixture stays covered 2026-09-21 to 09-24; the 21st had no `room_inventory` row
at all, and a night that is not in the table cannot disagree with the table.

That missing night is the *worse* shape of the two. `property_availability`
falls back to `room_types.total_units` when there is no row, so it reported
twelve of twelve Garden Standard rooms free while six units sat confirmed and
paid against them. The under-count was exactly the nights nobody had ever
written down.

The second: **the repair depends on who is on the night, and no counter knows.**
Six real guests the ledger was never told about and six fixture rows nobody
swept produce identical arithmetic and opposite repairs — write the ledger up,
or retire the reservations. Getting it backwards either sells a paying guest's
room to somebody else or takes six sellable rooms off a hotel for a month.

So `repair_inventory.classify` decides, on evidence, and refuses when it cannot.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reconcile_inventory import disagreements  # noqa: E402
from repair_inventory import (  # noqa: E402
    CANCELLED_NOT_RELEASED, CORRUPT_STATE, REAL_ACTIVE, TEST_FIXTURE, UNKNOWN,
    classify, repair,
)
from tests.fake_supabase import FakeDb  # noqa: E402

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
GARDEN = "10000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def a_fixture(**over) -> dict:
    """The three rows as production actually holds them."""
    row = {
        "id": "res-fixture-1", "ref": "TB-IBX5FC", "property_id": PROPERTY,
        "room_type_id": GARDEN, "check_in": "2026-09-21", "check_out": "2026-09-25",
        "units": 2, "status": "confirmed", "payment_status": "token_paid",
        "created_by": "import", "guest_name": "Inbox Panel Guest",
        "guest_phone": "+919812345678", "source": "direct",
        "total_paise": 5376000, "paid_paise": 1075200,
        # Every trace of a booking having happened is absent, because none of
        # the paths that set them ran. `public_token` is present — it is a
        # column default and proves nothing.
        "public_token": "tok_abc123", "conversation_id": None, "quote_id": None,
        "booking_id": None, "agent_action_id": None, "guest_id": None,
        "source_ref": None, "b2b_agent_id": None,
    }
    row.update(over)
    return row


def a_real_booking(**over) -> dict:
    row = a_fixture(
        id="res-real-1", ref="TB-9KQ2M1", created_by="agent",
        guest_name="Ananya Rao", guest_phone="+919845512309",
        conversation_id="conv-1", quote_id="q-1", guest_id="guest-1",
        public_token="tok_real",
    )
    row.update(over)
    return row


# ── the night nobody wrote down ─────────────────────────────────────────────

class TestASoldNightWithNoLedgerRow:
    """
    The under-count, pinned. Four nights were oversold and three were reported.
    """

    @pytest.fixture
    def db(self):
        fake = FakeDb()
        fake.seed("room_types", {"id": GARDEN, "property_id": PROPERTY,
                                 "name": "Garden Standard"})
        for r in ("res-1", "res-2", "res-3"):
            fake.seed("reservations", a_fixture(id=r, ref=f"TB-IBX{r[-1]}"))
        # The ledger knows about three of the four nights, and says zero.
        for night in ("2026-09-22", "2026-09-23", "2026-09-24"):
            fake.seed("room_inventory", {
                "id": f"inv-{night}", "property_id": PROPERTY,
                "room_type_id": GARDEN, "date": night, "units_booked": 0})
        return fake

    def test_the_missing_night_is_found(self, db):
        found = disagreements(db, PROPERTY)
        dates = sorted(row["date"][:10] for row, _ in found["wrong"])
        assert "2026-09-21" in dates, (
            "a night sold against no ledger row is still invisible")
        assert dates == ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]

    def test_the_count_is_twenty_four_not_eighteen(self, db):
        assert disagreements(db, PROPERTY)["oversold"] == 24

    def test_the_missing_night_is_reported_as_its_own_thing(self, db):
        """
        Separately from a wrong counter, because the repair differs: one is
        corrected, the other has to be written for the first time.
        """
        found = disagreements(db, PROPERTY)
        assert len(found["unledgered"]) == 1
        row, sold = found["unledgered"][0]
        assert row["date"] == "2026-09-21" and sold == 6
        assert row["id"] is None, "a night that does not exist yet has no id"

    def test_a_ledger_that_agrees_reports_nothing(self, db):
        """
        The control. Without it, a function that returned every night would
        pass every assertion above.
        """
        for night in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"):
            existing = [r for r in db.rows("room_inventory")
                        if r["date"] == night]
            if existing:
                existing[0]["units_booked"] = 6
            else:
                db.seed("room_inventory", {
                    "id": f"inv-{night}", "property_id": PROPERTY,
                    "room_type_id": GARDEN, "date": night, "units_booked": 6})
        found = disagreements(db, PROPERTY)
        assert found["wrong"] == [] and found["oversold"] == 0

    def test_a_night_nobody_has_sold_is_not_invented(self, db):
        """
        `unledgered` is nights with *reservations* against no row — not every
        date in the calendar that has no row. A hotel has no inventory rows for
        2027 and that is not a discrepancy.
        """
        found = disagreements(db, PROPERTY)
        assert all(sold > 0 for _, sold in found["unledgered"])
        assert all(row["date"] < "2026-09-25" for row, _ in found["unledgered"])


# ── who is on the night ─────────────────────────────────────────────────────

class TestClassification:

    def test_the_three_casa_mandala_rows_are_fixtures(self):
        cls, evidence = classify(a_fixture(), property_is_demo=True)
        assert cls == TEST_FIXTURE
        assert all(evidence["signature"].values())

    def test_a_real_booking_is_never_a_fixture(self):
        cls, _ = classify(a_real_booking(), property_is_demo=True)
        assert cls == REAL_ACTIVE

    def test_a_column_default_is_not_evidence_of_a_booking(self):
        """
        **This is the bug that made the classifier useless.**

        `public_token` was on the traces list. It is a column default: all 86
        reservations at Casa Mandala carry one, including the three whose INSERT
        never mentions it. Every row therefore looked like it carried a trace of
        a real booking, nothing could satisfy the signature, and the script
        refused to classify anything — correctly, for a reason entirely its own.

        The test for the list is not "a real booking has this" but "*only* a
        real booking has this".
        """
        assert classify(a_fixture(public_token="tok_zzz"),
                        property_is_demo=True)[0] == TEST_FIXTURE

    def test_a_fixture_ref_on_a_row_with_a_conversation_is_not_repaired(self):
        """
        The dangerous middle. A row wearing some of the costume is where a
        wrong guess is unrecoverable, so it is never repaired automatically.
        """
        cls, _ = classify(a_fixture(conversation_id="conv-real"),
                          property_is_demo=True)
        assert cls == UNKNOWN

    def test_nor_one_a_person_created(self):
        assert classify(a_fixture(created_by="agent"),
                        property_is_demo=True)[0] == UNKNOWN

    def test_nor_one_belonging_to_a_real_guest(self):
        assert classify(a_fixture(guest_phone="+919845512309"),
                        property_is_demo=True)[0] == UNKNOWN

    def test_a_live_property_never_yields_a_fixture(self):
        """
        `is_demo` is the outermost guard. Whatever a row looks like, a hotel
        taking real bookings does not have its reservations retired by a script.
        """
        assert classify(a_fixture(), property_is_demo=False)[0] == UNKNOWN

    def test_a_cancelled_row_the_counter_still_counts(self):
        cls, _ = classify(a_fixture(status="cancelled"), property_is_demo=True)
        assert cls == CANCELLED_NOT_RELEASED

    def test_impossible_dates_are_corrupt_not_fixture(self):
        """
        Checked before the signature, so a row that is both fixture-shaped and
        internally impossible is reported rather than quietly retired.
        """
        cls, _ = classify(a_fixture(check_out="2026-09-21"), property_is_demo=True)
        assert cls == CORRUPT_STATE

    def test_so_are_zero_units(self):
        assert classify(a_fixture(units=0), property_is_demo=True)[0] == CORRUPT_STATE

    def test_so_is_paying_more_than_the_stay_costs(self):
        cls, _ = classify(a_fixture(paid_paise=9999999, total_paise=5376000),
                          property_is_demo=True)
        assert cls == CORRUPT_STATE

    def test_an_unknown_status_is_unknown_not_assumed_live(self):
        assert classify(a_fixture(status="whatever"),
                        property_is_demo=True)[0] == UNKNOWN


# ── what the repair may and may not touch ───────────────────────────────────

class TestTheRepairTouchesOneSideOnly:

    @pytest.fixture
    def db(self):
        fake = FakeDb()
        fake.seed("room_types", {"id": GARDEN, "property_id": PROPERTY,
                                 "name": "Garden Standard"})
        fake.seed("reservations", a_fixture())
        fake.seed("room_inventory", {
            "id": "inv-1", "property_id": PROPERTY, "room_type_id": GARDEN,
            "date": "2026-09-22", "units_booked": 0})
        fake.seed("admin_audit_log", {})
        fake.rows("admin_audit_log").clear()
        return fake

    def _plan(self, db):
        rows = db.rows("reservations")
        return [(classify(r, property_is_demo=True)[0], r,
                 classify(r, property_is_demo=True)[1]) for r in rows]

    def test_a_fixture_is_cancelled_and_never_deleted(self, db):
        repair(db, PROPERTY, self._plan(db), {TEST_FIXTURE}, now=NOW)
        rows = db.rows("reservations")
        assert len(rows) == 1, "the row was deleted; the evidence is gone"
        assert rows[0]["status"] == "cancelled"
        assert "fixture" in (rows[0].get("cancellation_reason") or "")

    def test_the_counter_is_not_touched(self, db):
        """
        These rows never called `convert_hold`, so they never added to
        `units_booked`. Releasing what they never took would subtract six units
        a real guest may be holding.
        """
        before = db.rows("room_inventory")[0]["units_booked"]
        repair(db, PROPERTY, self._plan(db), {TEST_FIXTURE}, now=NOW)
        assert db.rows("room_inventory")[0]["units_booked"] == before

    def test_the_repair_is_audited(self, db):
        repair(db, PROPERTY, self._plan(db), {TEST_FIXTURE}, now=NOW)
        log = db.rows("admin_audit_log")
        assert len(log) == 1
        assert log[0]["action"] == "inventory_repair"
        assert log[0]["entity_id"] == "res-fixture-1"
        assert log[0]["metadata"]["class"] == TEST_FIXTURE
        assert log[0]["metadata"]["inventory_touched"] is False

    def test_an_audit_that_cannot_be_written_stops_the_repair(self, db, monkeypatch):
        """
        Everywhere else in this codebase an audit failure is swallowed so it
        cannot block the work, and that is right — there the work is a hotelier
        saving a form. Here it is an unattended write to production inventory,
        and a repair nobody can reconstruct is worse than one that did not run.
        """
        def explode(*a, **k):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(type(db.table("admin_audit_log")), "insert", explode,
                            raising=False)
        with pytest.raises(SystemExit):
            repair(db, PROPERTY, self._plan(db), {TEST_FIXTURE}, now=NOW)
        assert db.rows("reservations")[0]["status"] == "confirmed", (
            "the reservation was changed after the audit row failed")

    def test_a_class_the_operator_did_not_name_is_not_repaired(self, db):
        repair(db, PROPERTY, self._plan(db), {REAL_ACTIVE} - {REAL_ACTIVE}, now=NOW)
        assert db.rows("reservations")[0]["status"] == "confirmed"

    def test_repairing_a_real_booking_is_refused_outright(self, db):
        db.rows("reservations").clear()
        db.seed("reservations", a_real_booking())
        with pytest.raises(SystemExit, match="conversation with the hotelier"):
            repair(db, PROPERTY, self._plan(db), {REAL_ACTIVE}, now=NOW)

    def test_the_other_classes_have_no_automatic_repair(self, db):
        for cls in (CANCELLED_NOT_RELEASED, CORRUPT_STATE, UNKNOWN):
            with pytest.raises(SystemExit):
                repair(db, PROPERTY, self._plan(db), {cls}, now=NOW)


# ── the failure cannot come back ────────────────────────────────────────────

class TestTheFixtureLifecycleIsClosed:
    """
    The rows existed because `inbox_e2e` swept by the *current run's* ref, so a
    run that died between the insert and the sweep left its reservation behind
    for good and the next run generated a new tag and could not see it.
    """

    def test_the_sweep_is_prefix_scoped(self):
        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "inbox_e2e.py").read_text()
        sweep = source[source.index("def sweep("):source.index("CHECK_IN =")]
        assert "STAY_PREFIX" in sweep
        assert "STAY_REF" not in sweep, (
            "the sweep is scoped to one run again; debris will accumulate")

    def test_every_suite_that_writes_reservations_sweeps_by_prefix(self):
        """
        The asymmetry was the whole bug — every other suite already did this.
        """
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        for name in ("inbox_e2e.py", "checkin_e2e.py", "booking_engine_e2e.py",
                     "owner_e2e.py"):
            source = (scripts / name).read_text()
            if "insert into public.reservations" not in source:
                continue
            assert "ref like" in source, f"{name} writes reservations and never sweeps"

    def test_every_fixture_prefix_is_registered_for_classification(self):
        """
        A suite whose prefix the classifier does not know writes debris that can
        only ever come back UNKNOWN.
        """
        from repair_inventory import FIXTURE_PREFIXES

        scripts = Path(__file__).resolve().parent.parent / "scripts"
        import re

        for path in scripts.glob("*.py"):
            source = path.read_text()
            if "insert into public.reservations" not in source:
                continue
            for found in re.findall(r'PREFIX\s*=\s*"(TB-[A-Z]+)"', source):
                assert found in FIXTURE_PREFIXES, (
                    f"{path.name} writes {found} and repair_inventory does not "
                    f"know it")
