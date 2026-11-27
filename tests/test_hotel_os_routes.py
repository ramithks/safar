"""
The hotel OS is only mounted where its database is.

`/api/v1/pms/*` reads `properties`, `reservations`, `hotel_websites` — tables
that exist in the hotel-OS Supabase project and in no other. The marketplace
runs from a different project, and `get_db()` builds one client from one
`SUPABASE_URL`, so a marketplace process with these routes mounted answers 500
to every one of them. That is not hypothetical: it is what
`backend.safar.example.com` did the moment StaySuite first reached production, on the
public hotel page a guest can reach without logging in.

The flag is therefore off by default, and these tests exist to keep it that
way. A change that flips the default is a change that puts 80 routes over the
wrong database on the marketplace, and nothing else in the suite would notice.
"""

import importlib

import pytest

from app.config import Settings


def _mounted(monkeypatch, *, enabled: bool) -> list[str]:
    """Rebuild the v1 router with the flag set, and list what it carries."""
    import app.api.v1.router as router_module

    monkeypatch.setattr(router_module.settings, "enable_hotel_os", enabled)
    reloaded = importlib.reload(router_module)
    try:
        return [route.path for route in reloaded.router.routes]
    finally:
        # Leave the module as the rest of the session expects to find it.
        monkeypatch.undo()
        importlib.reload(router_module)


class TestTheDefault:
    def test_off_unless_asked_for(self, monkeypatch):
        """
        A fresh Settings() is a marketplace deployment. The default has to be
        the safe one because the marketplace's environment is the one nobody
        edits — it must be correct with no variable set at all.

        **The variable is cleared first, and that is the whole point.** This
        used to call `Settings()` and read whatever was in the ambient
        environment, so it asserted "the default is off" while actually
        measuring the developer's shell. On a clean shell it passed; on any
        machine with `.env.hotelos` sourced — which is every machine StaySuite is
        worked on — it failed, for a reason that had nothing to do with the
        code. A test that fails on the developer's machine and passes in CI is
        a test people learn to ignore, and this one guards the marketplace
        against mounting an entire product's routes by accident.
        """
        monkeypatch.delenv("ENABLE_HOTEL_OS", raising=False)
        assert Settings(_env_file=None).enable_hotel_os is False

    def test_and_on_when_it_is_asked_for(self, monkeypatch):
        """The other half: setting it must actually reach the setting."""
        monkeypatch.setenv("ENABLE_HOTEL_OS", "true")
        assert Settings(_env_file=None).enable_hotel_os is True


class TestWhatIsMounted:
    def test_disabled_mounts_no_pms_routes(self, monkeypatch):
        paths = _mounted(monkeypatch, enabled=False)
        assert [p for p in paths if "/pms/" in p] == []

    def test_disabled_mounts_no_messaging_webhooks(self, monkeypatch):
        paths = _mounted(monkeypatch, enabled=False)
        assert [p for p in paths if "/messaging/" in p] == []

    def test_enabled_mounts_the_pms(self, monkeypatch):
        paths = _mounted(monkeypatch, enabled=True)
        assert len([p for p in paths if "/pms/" in p]) > 50

    def test_the_public_booking_routes_are_gated_too(self, monkeypatch):
        """
        The ones that hurt most. These take no credentials, so on a deployment
        without the schema they hand a 500 to any passer-by rather than to a
        logged-in member of staff who could report it.
        """
        off = _mounted(monkeypatch, enabled=False)
        on = _mounted(monkeypatch, enabled=True)
        public = [p for p in on if "/pms/public/" in p]
        assert public, "expected public booking routes when enabled"
        assert not [p for p in off if "/pms/public/" in p]

    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_marketplace_is_unaffected_either_way(self, monkeypatch, enabled):
        """
        The flag must never be able to change what safar.example.com gets. This is
        the assertion that makes the flag safe to deploy at all.
        """
        paths = _mounted(monkeypatch, enabled=enabled)
        assert len([p for p in paths if "/marketplace/" in p]) == 24
        assert "/api/v1/plan" in paths
