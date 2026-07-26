"""
Event bus + admin passcode tests.

The properties that matter: a broken subscriber can never fail an admin action
that already committed, and the passcode gate fails closed.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.services import events
from app.services.events import emit, on, reset_for_tests, subscribers_for


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_for_tests()
    yield
    reset_for_tests()


class TestEventBus:
    def test_delivers_to_a_subscriber(self):
        seen = []

        @on("listing.approved")
        async def handler(name, payload):
            seen.append((name, payload["id"]))

        asyncio.run(emit("listing.approved", {"id": "abc"}))
        assert seen == [("listing.approved", "abc")]

    def test_delivers_to_every_subscriber_of_an_event(self):
        calls = []
        for n in range(3):
            @on("x")
            async def handler(_name, _payload, n=n):
                calls.append(n)

        asyncio.run(emit("x", {}))
        assert sorted(calls) == [0, 1, 2]

    def test_wildcard_sees_everything(self):
        seen = []

        @on(events.WILDCARD)
        async def firehose(name, _payload):
            seen.append(name)

        asyncio.run(emit("a.b", {}))
        asyncio.run(emit("c.d", {}))
        assert seen == ["a.b", "c.d"]

    def test_sync_handlers_are_supported(self):
        seen = []

        @on("sync")
        def handler(_name, payload):
            seen.append(payload["v"])

        asyncio.run(emit("sync", {"v": 1}))
        assert seen == [1]

    def test_one_failing_subscriber_does_not_stop_the_others(self):
        """The action already committed — a broken handler must not cascade."""
        survived = []

        @on("boom")
        async def bad(_name, _payload):
            raise RuntimeError("subscriber exploded")

        @on("boom")
        async def good(_name, _payload):
            survived.append(True)

        asyncio.run(emit("boom", {}))
        assert survived == [True]

    def test_emit_never_raises_even_when_every_subscriber_fails(self):
        @on("all-bad")
        async def bad(_name, _payload):
            raise RuntimeError("nope")

        asyncio.run(emit("all-bad", {}))  # must not raise

    def test_emit_with_no_subscribers_is_a_no_op(self):
        asyncio.run(emit("nobody.listening", {"a": 1}))

    def test_registration_is_visible(self):
        @on("q")
        async def handler(_n, _p):
            ...

        assert len(subscribers_for("q")) == 1


class TestAdminPasscode:
    """The gate must fail closed, and must not confirm the endpoint exists."""

    def _call(self, configured, supplied):
        from app.config import settings
        from app.deps.admin import require_admin

        original = settings.admin_api_passcode
        settings.admin_api_passcode = configured
        try:
            class Req:
                headers = {}
                client = None

            return asyncio.run(require_admin(Req(), supplied, "Sahil"))
        finally:
            settings.admin_api_passcode = original

    def test_unset_passcode_refuses_everything(self):
        with pytest.raises(HTTPException) as e:
            self._call(None, "anything")
        assert e.value.status_code == 503

    def test_wrong_passcode_is_404_not_403(self):
        with pytest.raises(HTTPException) as e:
            self._call("correct-horse", "wrong")
        assert e.value.status_code == 404

    def test_missing_header_is_rejected(self):
        with pytest.raises(HTTPException) as e:
            self._call("correct-horse", None)
        assert e.value.status_code == 404

    def test_correct_passcode_returns_the_actor(self):
        identity = self._call("correct-horse", "correct-horse")
        assert identity.actor == "Sahil"

    def test_actor_defaults_when_not_supplied(self):
        from app.config import settings
        from app.deps.admin import require_admin

        original = settings.admin_api_passcode
        settings.admin_api_passcode = "pass"
        try:
            class Req:
                headers = {}
                client = None

            identity = asyncio.run(require_admin(Req(), "pass", None))
            assert identity.actor == "admin"
        finally:
            settings.admin_api_passcode = original
