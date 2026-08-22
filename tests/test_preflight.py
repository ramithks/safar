"""
A degradation nobody is told about lasts a day.

That is not hypothetical. `REVALIDATE_SECRET` was never set on the StayOS
backend. `seo_ping.hotel_changed` handled it exactly right — skip, log at
`debug`, never fail a hotelier's publish — and so every hotel that published sat
stale on travbuddy.in with nothing anywhere saying why. It was found by noticing
that a re-publish moved the backend's `updatedAt` and not the website's sitemap.

The behaviour was right. The volume was wrong. These tests are about the volume.
"""
import logging

import pytest

from app.services.pms import preflight


def run(monkeypatch, missing=(), caplog=None):
    """Pretend everything is configured except the named variables."""
    for requirement in preflight.REQUIREMENTS:
        monkeypatch.setattr(
            preflight.settings, requirement.attr,
            None if requirement.env in missing else "set", raising=False,
        )
    return preflight.check()


def test_a_fully_configured_box_says_so_once_and_quietly(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        assert run(monkeypatch) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_variable_that_cost_a_day_is_named_at_warning(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        missing = run(monkeypatch, {"REVALIDATE_SECRET"})

    assert [r.env for r in missing] == ["REVALIDATE_SECRET"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("REVALIDATE_SECRET" in m for m in warnings)
    # And it says what stops working, not "missing config" — which tells
    # whoever is deploying nothing they can act on.
    assert any("stale" in m for m in warnings)


def test_every_requirement_explains_what_breaks_without_it():
    """
    The rule that keeps this list useful as it grows. A line that names a
    variable and not a consequence is a line somebody scrolls past.
    """
    for requirement in preflight.REQUIREMENTS:
        assert len(requirement.consequence) > 40, requirement.env
        assert requirement.consequence.endswith("."), requirement.env


def test_every_requirement_names_a_setting_that_exists():
    """
    A typo here fails open — `getattr(..., None)` reads as "not configured" —
    so a mis-named attribute would warn about a variable that *is* set, for
    ever, and train people to ignore the warning.
    """
    from app.config import settings

    for requirement in preflight.REQUIREMENTS:
        assert hasattr(settings, requirement.attr), requirement.env


def test_no_database_stops_the_boot_rather_than_degrading(monkeypatch, caplog):
    """
    The line between the two. A hotel with no photo storage is a hotel that
    cannot upload photos; a hotel with no database is not a hotel.
    """
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RuntimeError) as raised:
            run(monkeypatch, {"SUPABASE_URL"})

    assert "SUPABASE_URL" in str(raised.value)


def test_a_fatal_gap_is_reported_before_it_raises(monkeypatch, caplog):
    """
    The exception reaches a process supervisor; the log line reaches a person.
    """
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RuntimeError):
            run(monkeypatch, {"SUPABASE_SERVICE_KEY"})

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_several_gaps_are_all_reported_not_just_the_first(monkeypatch, caplog):
    """
    A deploy should need one round trip, not one per variable.
    """
    absent = {"REVALIDATE_SECRET", "GCS_BUCKET_NAME", "INSTAGRAM_APP_SECRET"}
    with caplog.at_level(logging.WARNING):
        missing = run(monkeypatch, absent)

    assert {r.env for r in missing} == absent
    summary = caplog.records[-1].getMessage()
    assert "3 of" in summary


def test_nothing_is_ever_logged_that_a_secret_could_be_read_out_of(monkeypatch, caplog):
    """
    It says whether something is set, never what it is set to. A preflight that
    prints a webhook secret is worse than no preflight.
    """
    for requirement in preflight.REQUIREMENTS:
        monkeypatch.setattr(preflight.settings, requirement.attr,
                            "whsec_do_not_log_me", raising=False)

    with caplog.at_level(logging.DEBUG):
        preflight.check()

    assert "whsec_do_not_log_me" not in "\n".join(
        r.getMessage() for r in caplog.records
    )
