"""
Stay availability — the pre-flight read and the claim/release contract.

Before this existed a stay could be booked for any dates, any number of times.
The tests that matter here are the ones that pin *which* failure mode we chose
when something is uncertain:

  * a listing that tracks no inventory stays bookable (we did not take every
    un-migrated stay offline),
  * a read that fails does not block checkout (the claim is authoritative),
  * a claim that fails IS treated as a loss (a room we might not have is never
    handed out).

The atomic guard itself is proved in SQL, against a real Postgres, by
supabase/tests/race.sh in the website repo — it cannot be proved with a fake DB.
"""
import asyncio

import pytest

from app.services import marketplace_availability as av


def run(coro):
    return asyncio.run(coro)


def night(date, room="rt1", units_left=2, stop_sell=False, min_nights=None):
    return {
        "room_type_id": room,
        "room_name": "Deluxe",
        "date": date,
        "units_left": units_left,
        "units_total": 2,
        "price": 3000,
        "min_nights": min_nights,
        "stop_sell": stop_sell,
        "source": "partner",
    }


# ── sold_out_nights ─────────────────────────────────────────────────────────


class TestSoldOutNights:
    def test_a_night_with_room_to_spare_is_available(self):
        rows = [night("2026-09-01"), night("2026-09-02")]
        assert av.sold_out_nights(rows, "rt1", 1) == []

    def test_a_night_without_enough_units_is_reported(self):
        rows = [night("2026-09-01", units_left=1), night("2026-09-02", units_left=2)]
        assert av.sold_out_nights(rows, "rt1", 2) == ["2026-09-01"]

    def test_asking_for_exactly_what_is_left_still_fits(self):
        assert av.sold_out_nights([night("2026-09-01", units_left=2)], "rt1", 2) == []

    def test_stop_sell_closes_a_night_that_still_has_units(self):
        rows = [night("2026-09-01", units_left=5, stop_sell=True)]
        assert av.sold_out_nights(rows, "rt1", 1) == ["2026-09-01"]

    def test_with_no_room_type_chosen_any_room_that_fits_keeps_the_night_open(self):
        # The traveller has not picked yet. Refusing on the fullest room would
        # hide inventory that is genuinely for sale.
        rows = [
            night("2026-09-01", room="rt1", units_left=0),
            night("2026-09-01", room="rt2", units_left=3),
        ]
        assert av.sold_out_nights(rows, None, 1) == []

    def test_with_no_room_type_a_night_full_across_every_room_is_reported(self):
        rows = [
            night("2026-09-01", room="rt1", units_left=0),
            night("2026-09-01", room="rt2", units_left=0),
        ]
        assert av.sold_out_nights(rows, None, 1) == ["2026-09-01"]

    def test_naming_a_room_type_ignores_the_others(self):
        rows = [
            night("2026-09-01", room="rt1", units_left=0),
            night("2026-09-01", room="rt2", units_left=3),
        ]
        assert av.sold_out_nights(rows, "rt1", 1) == ["2026-09-01"]

    def test_no_rows_means_nothing_to_refuse(self):
        assert av.sold_out_nights([], "rt1", 1) == []

    def test_results_are_sorted_so_the_message_reads_in_order(self):
        rows = [night("2026-09-03", units_left=0), night("2026-09-01", units_left=0)]
        assert av.sold_out_nights(rows, "rt1", 1) == ["2026-09-01", "2026-09-03"]


# ── unmet_min_nights ────────────────────────────────────────────────────────


class TestMinNights:
    def test_a_stay_that_meets_the_rule_passes(self):
        rows = [night("2026-09-01", min_nights=2), night("2026-09-02")]
        assert av.unmet_min_nights(rows, "rt1", 2) is None

    def test_a_stay_shorter_than_the_rule_is_reported(self):
        assert av.unmet_min_nights([night("2026-09-01", min_nights=3)], "rt1", 1) == 3

    def test_only_the_check_in_night_rule_applies(self):
        # Every channel manager works this way. Applying the later night's rule
        # would make a stay spanning it impossible rather than constrained.
        rows = [night("2026-09-01"), night("2026-09-02", min_nights=5)]
        assert av.unmet_min_nights(rows, "rt1", 2) is None

    def test_the_strictest_rule_on_the_check_in_night_wins(self):
        rows = [
            night("2026-09-01", room="rt1", min_nights=2),
            night("2026-09-01", room="rt2", min_nights=4),
        ]
        assert av.unmet_min_nights(rows, None, 1) == 4

    def test_no_rows_means_no_rule(self):
        assert av.unmet_min_nights([], "rt1", 1) is None


# ── check_stay: what we do when we are not sure ─────────────────────────────


class TestCheckStay:
    def test_a_listing_with_no_room_types_stays_bookable(self, monkeypatch):
        # The migration backfills rooms, but a listing that never described any
        # has nothing to track. Refusing these would take them all offline.
        monkeypatch.setattr(av, "room_types", _async([]))
        out = run(av.check_stay("L1", "2026-09-01", "2026-09-03", 1))
        assert out["ok"] is True
        assert out["tracked"] is False

    def test_a_failed_read_does_not_block_checkout(self, monkeypatch):
        # claim_stay_nights is the authoritative guard; this pass is advisory,
        # so a database blip must not stop someone booking.
        monkeypatch.setattr(av, "room_types", _async([{"id": "rt1"}]))
        monkeypatch.setattr(av, "availability", _async([]))
        out = run(av.check_stay("L1", "2026-09-01", "2026-09-03", 1))
        assert out["ok"] is True
        assert out["tracked"] is True

    def test_a_room_types_read_that_raises_leaves_the_stay_bookable(self, monkeypatch):
        # room_types is the first thing check_stay touches, so an exception
        # here would propagate out of /quote and /checkout and take every stay
        # offline — including the case that matters most, the window where this
        # code is deployed and the migration has not been applied yet.
        def boom():
            raise RuntimeError('relation "listing_room_types" does not exist')

        monkeypatch.setattr(av, "get_db", boom)
        out = run(av.check_stay("L1", "2026-09-01", "2026-09-03", 1))
        assert out["ok"] is True
        assert out["tracked"] is False

    def test_a_sold_out_night_is_refused(self, monkeypatch):
        monkeypatch.setattr(av, "room_types", _async([{"id": "rt1"}]))
        monkeypatch.setattr(
            av,
            "availability",
            _async([night("2026-09-01", units_left=0), night("2026-09-02")]),
        )
        out = run(av.check_stay("L1", "2026-09-01", "2026-09-03", 1, "rt1"))
        assert out["ok"] is False
        assert out["sold_out_nights"] == ["2026-09-01"]

    def test_units_left_is_the_tightest_night(self, monkeypatch):
        monkeypatch.setattr(av, "room_types", _async([{"id": "rt1"}]))
        monkeypatch.setattr(
            av,
            "availability",
            _async([night("2026-09-01", units_left=4), night("2026-09-02", units_left=1)]),
        )
        out = run(av.check_stay("L1", "2026-09-01", "2026-09-03", 1, "rt1"))
        assert out["units_left"] == 1

    def test_reversed_dates_are_not_a_refusal(self, monkeypatch):
        # Pricing rejects these with its own message; availability should not
        # produce a second, more confusing one.
        monkeypatch.setattr(av, "room_types", _async([{"id": "rt1"}]))
        out = run(av.check_stay("L1", "2026-09-05", "2026-09-01", 1))
        assert out["ok"] is True


# ── claim / release ─────────────────────────────────────────────────────────


class FakeRpcDB:
    """Records rpc calls and returns a scripted result."""

    def __init__(self, result=1, raises=False):
        self.result, self.raises = result, raises
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        db = self

        class _R:
            def execute(self_inner):
                if db.raises:
                    raise RuntimeError("connection reset")
                return type("Res", (), {"data": db.result})()

        return _R()


def _async(value):
    async def _fn(*_a, **_k):
        return value

    return _fn


@pytest.fixture
def rpc_db(monkeypatch):
    def _install(db):
        monkeypatch.setattr(av, "get_db", lambda: db)
        return db

    return _install


def stay(**over):
    base = {
        "id": "b1",
        "room_type_id": "rt1",
        "rooms": 2,
        "check_in": "2026-09-01",
        "check_out": "2026-09-04",
    }
    base.update(over)
    return base


class TestClaim:
    def test_a_successful_claim_holds_the_stay(self, rpc_db):
        db = rpc_db(FakeRpcDB(result=1))
        assert run(av.claim(stay())) is True
        name, args = db.calls[0]
        assert name == "claim_stay_nights"
        assert args == {
            "p_room_type": "rt1",
            "p_check_in": "2026-09-01",
            "p_check_out": "2026-09-04",
            "p_units": 2,
        }

    def test_minus_one_is_a_loss(self, rpc_db):
        # The documented "guard lost" return from the RPC.
        rpc_db(FakeRpcDB(result=-1))
        assert run(av.claim(stay())) is False

    def test_zero_units_left_is_still_a_win(self, rpc_db):
        # 0 means "you took the last one", not "you missed it".
        rpc_db(FakeRpcDB(result=0))
        assert run(av.claim(stay())) is True

    def test_a_thrown_error_is_treated_as_a_loss(self, rpc_db):
        # Refunding a traveller we cannot confirm is recoverable; handing out a
        # room we may not have is not.
        rpc_db(FakeRpcDB(raises=True))
        assert run(av.claim(stay())) is False

    def test_a_booking_with_no_room_type_claims_nothing_and_succeeds(self, rpc_db):
        db = rpc_db(FakeRpcDB())
        assert run(av.claim(stay(room_type_id=None))) is True
        assert db.calls == []

    def test_a_range_over_the_cap_is_refused_without_calling_the_rpc(self, rpc_db):
        db = rpc_db(FakeRpcDB())
        assert run(av.claim(stay(check_in="2026-01-01", check_out="2026-06-01"))) is False
        assert db.calls == []

    def test_missing_dates_claim_nothing(self, rpc_db):
        db = rpc_db(FakeRpcDB())
        assert run(av.claim(stay(check_in=None))) is True
        assert db.calls == []

    def test_rooms_defaults_to_one(self, rpc_db):
        db = rpc_db(FakeRpcDB())
        run(av.claim(stay(rooms=None)))
        assert db.calls[0][1]["p_units"] == 1


class TestRelease:
    def test_release_gives_back_what_was_taken(self, rpc_db):
        db = rpc_db(FakeRpcDB())
        run(av.release(stay()))
        name, args = db.calls[0]
        assert name == "release_stay_nights"
        assert args["p_units"] == 2

    def test_release_never_raises(self, rpc_db):
        # It is cleanup on a path that has already decided its outcome.
        rpc_db(FakeRpcDB(raises=True))
        run(av.release(stay()))

    def test_nothing_to_release_is_a_no_op(self, rpc_db):
        db = rpc_db(FakeRpcDB())
        run(av.release(stay(room_type_id=None)))
        assert db.calls == []
