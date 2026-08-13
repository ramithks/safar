"""
The property clock — the midnight cases.

The bug these exist for: "today" was the server's date, which is UTC. A hotel in
Goa opening the front desk at 00:15 IST was shown yesterday's arrivals, and the
folio it posted was dated yesterday too.

Every assertion below pins a specific instant. A midnight test that depends on
when it runs passes in CI and fails at 19:00 UTC, which is worse than no test.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.pms import clock

UTC = ZoneInfo("UTC")


# ── the conversion itself ────────────────────────────────────────────────────

def test_ist_after_midnight_is_the_new_day():
    # 19:00 UTC on the 14th is 00:30 IST on the 15th. This is the exact window
    # the old code got wrong, every single night.
    at = datetime(2026, 8, 14, 19, 0, tzinfo=UTC)
    assert clock.business_date_in("Asia/Kolkata", at) == date(2026, 8, 15)
    assert at.date() == date(2026, 8, 14)  # what the server would have said


def test_ist_before_midnight_is_still_today():
    # 18:00 UTC is 23:30 IST — one hour earlier, still the 14th.
    at = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
    assert clock.business_date_in("Asia/Kolkata", at) == date(2026, 8, 14)


def test_a_western_property_is_still_on_yesterday():
    # 02:00 UTC on the 15th is 19:00 on the 14th in New York. A UTC server would
    # have rolled that property's day over five hours early.
    at = datetime(2026, 8, 15, 2, 0, tzinfo=UTC)
    assert clock.business_date_in("America/New_York", at) == date(2026, 8, 14)


def test_a_half_hour_offset_is_respected():
    # Nepal is +05:45. 18:20 UTC is 00:05 NPT the next day; 18:10 is 23:55 the
    # same day. Whole-hour arithmetic gets both of these wrong.
    assert clock.business_date_in(
        "Asia/Kathmandu", datetime(2026, 8, 14, 18, 20, tzinfo=UTC)
    ) == date(2026, 8, 15)
    assert clock.business_date_in(
        "Asia/Kathmandu", datetime(2026, 8, 14, 18, 10, tzinfo=UTC)
    ) == date(2026, 8, 14)


def test_two_properties_one_instant_two_dates():
    # The case a single `current_date` cannot represent. Kiritimati is +14 and
    # Niue is −11: twenty-five hours apart, so they never agree.
    at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    east = clock.business_date_in("Pacific/Kiritimati", at)
    west = clock.business_date_in("Pacific/Niue", at)
    assert east > west
    assert (east - west).days == 1


def test_dst_transition_does_not_lose_a_day():
    # London springs forward at 01:00 UTC on 2026-03-29. The date is unchanged
    # either side of it; only the offset moves.
    before = clock.business_date_in("Europe/London", datetime(2026, 3, 29, 0, 30, tzinfo=UTC))
    after = clock.business_date_in("Europe/London", datetime(2026, 3, 29, 1, 30, tzinfo=UTC))
    assert before == after == date(2026, 3, 29)


def test_an_unknown_zone_degrades_rather_than_raising():
    # Migration 9 refuses these at write time, so this is only reachable when
    # Postgres knows a zone this machine's tzdata does not. A front desk must
    # not 500 over it.
    at = datetime(2026, 8, 14, 19, 0, tzinfo=UTC)
    assert clock.business_date_in("Mars/Olympus_Mons", at) == date(2026, 8, 15)


# ── the cache, and what the rest of the module reads through it ──────────────

@pytest.fixture
def stub_timezone(monkeypatch):
    calls = {"n": 0}

    def _set(tz: str):
        clock.clear_cache()
        calls["n"] = 0

        def _load(_property_id: str) -> str:
            calls["n"] += 1
            return tz

        monkeypatch.setattr(clock, "_load_timezone", _load)

    _set.calls = calls  # type: ignore[attr-defined]
    yield _set
    clock.clear_cache()


def test_timezone_is_read_once_per_property(stub_timezone):
    stub_timezone("Asia/Kolkata")
    assert clock.timezone_for("p1") == "Asia/Kolkata"
    assert clock.timezone_for("p1") == "Asia/Kolkata"
    assert clock.timezone_for("p1") == "Asia/Kolkata"
    # Three calls, one database read. This sits on the arrivals path.
    assert stub_timezone.calls["n"] == 1


def test_a_failed_lookup_serves_the_last_known_value(stub_timezone, monkeypatch):
    stub_timezone("Pacific/Auckland")
    assert clock.timezone_for("p1") == "Pacific/Auckland"

    def _boom(_property_id: str) -> str:
        raise RuntimeError("database down")

    monkeypatch.setattr(clock, "_load_timezone", _boom)
    monkeypatch.setattr(clock, "_TTL_SECONDS", -1)  # force the refresh

    # The hotel does not stop being able to check guests in because a lookup
    # failed. Stale beats wrong, and wrong here means a whole day out.
    assert clock.timezone_for("p1") == "Pacific/Auckland"


def test_a_cold_failure_falls_back_to_the_default(stub_timezone, monkeypatch):
    clock.clear_cache()

    def _boom(_property_id: str) -> str:
        raise RuntimeError("database down")

    monkeypatch.setattr(clock, "_load_timezone", _boom)
    assert clock.timezone_for("never-seen") == clock.DEFAULT_TIMEZONE


def test_audit_date_is_the_night_that_just_ended(stub_timezone):
    stub_timezone("Asia/Kolkata")
    # Whatever "today" is at the property, the audit closes the day before it.
    assert clock.audit_date_for("p1") == clock.business_date("p1") - timedelta(days=1)


def test_business_date_iso_is_a_plain_date_string(stub_timezone):
    stub_timezone("Asia/Kolkata")
    value = clock.business_date_iso("p1")
    assert len(value) == 10 and value.count("-") == 2
    assert date.fromisoformat(value) == clock.business_date("p1")
