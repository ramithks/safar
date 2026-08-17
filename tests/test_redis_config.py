"""
Redis is four things behind one URL, and only one of them complains.

`REDIS_URL` is the job store, the rate limiter's storage, the messaging
pipeline's lock — and the Celery broker. A worker pointed at an unreachable
broker consumes nothing and reports nothing, so guest replies and campaigns
stop happening with no error anywhere. The health check is the only surface
that speaks up, which makes what it says load-bearing.

The StayOS production deployment came up with
`Error 111 connecting to localhost:6379. Connection refused.` — which reads
like Redis is down. It meant `REDIS_URL` had never been set and the
development default had survived into a container, where nothing listens on
localhost. These tests are about telling those two apart from the response
alone, because that response is all anyone pastes.
"""

import pytest

from app.config import Settings, redis_is_unconfigured, redis_target


class TestRedaction:
    """A Redis URL carries its password inline. This string goes into logs."""

    def test_password_is_stripped(self):
        out = redis_target("redis://:hunter2@cache.internal:6379/1")
        assert "hunter2" not in out
        assert out == "redis://cache.internal:6379/1"

    def test_username_and_password_are_stripped(self):
        out = redis_target("redis://admin:hunter2@cache.internal:6379/1")
        assert "hunter2" not in out and "admin" not in out

    def test_what_survives_is_what_you_need_to_debug(self):
        assert redis_target("redis://redis:6379/1") == "redis://redis:6379/1"

    def test_tls_scheme_is_kept(self):
        assert redis_target("rediss://:pw@host:6380/2") == "rediss://host:6380/2"

    def test_a_log_line_never_raises(self):
        """Whatever is in the variable, this has to return a string."""
        for junk in ("", "not-a-url", "redis://", ":::::", "redis://[bad"):
            assert isinstance(redis_target(junk), str)


class TestUnconfiguredDetection:
    """
    "Was it provided?", not "does it look like the default?" — the two differ
    and the difference is a false alarm in the boot log.
    """

    def test_absent_is_unconfigured(self, monkeypatch):
        monkeypatch.setattr("app.config._REDIS_URL_WAS_PROVIDED", False)
        assert redis_is_unconfigured()

    def test_supplied_is_configured(self, monkeypatch):
        monkeypatch.setattr("app.config._REDIS_URL_WAS_PROVIDED", True)
        assert not redis_is_unconfigured()

    def test_pydantic_records_provision_from_the_environment(self, monkeypatch):
        """
        The mechanism, checked against pydantic directly rather than trusted.
        `model_fields_set` is what separates "supplied" from "defaulted".
        """
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        assert "redis_url" in Settings().model_fields_set

    def test_set_to_exactly_the_default_still_counts_as_supplied(self, monkeypatch):
        """
        The case a value comparison gets wrong, and the reason this asks
        pydantic instead. `redis://localhost:6379/0` is a legitimate thing to
        set — a compose stack whose Redis really is on localhost — and telling
        someone they have not set a variable they just set is how a warning
        stops being read. It happened once, verifying the compose stack.
        """
        default = Settings.model_fields["redis_url"].default
        monkeypatch.setenv("REDIS_URL", default)
        assert "redis_url" in Settings().model_fields_set

    def test_it_is_captured_once_rather_than_per_call(self):
        """
        The health endpoint calls this on every hit. Recomputing would re-read
        `.env` from disk each time.
        """
        import inspect

        from app import config

        src = inspect.getsource(config.redis_is_unconfigured)
        assert "Settings()" not in src


class TestHealthMessage:
    """What the endpoint actually returns, which is what gets pasted."""

    @staticmethod
    def _health_with(monkeypatch, url, *, ping_fails=True):
        monkeypatch.setattr("app.config.settings.redis_url", url)
        if ping_fails:
            class Dead:
                def ping(self):
                    raise ConnectionError(
                        "Error 111 connecting to localhost:6379. Connection refused.")
            monkeypatch.setattr("app.workers.job_store._get_redis", lambda: Dead())
        else:
            class Live:
                def ping(self):
                    return True
            monkeypatch.setattr("app.workers.job_store._get_redis", lambda: Live())
        from app.api.v1.health import _check_redis
        return _check_redis()

    def test_unset_says_so_by_name(self, monkeypatch):
        """
        The whole point. Production hit this and the message blamed Redis.
        """
        monkeypatch.setattr("app.config._REDIS_URL_WAS_PROVIDED", False)
        default = Settings.model_fields["redis_url"].default
        out = self._health_with(monkeypatch, default)
        assert "REDIS_URL is not set" in out
        assert "fell back" in out

    def test_configured_but_unreachable_does_not_blame_the_variable(self, monkeypatch):
        monkeypatch.setattr("app.config._REDIS_URL_WAS_PROVIDED", True)
        out = self._health_with(monkeypatch, "redis://redis:6379/1")
        assert "REDIS_URL is not set" not in out
        assert "redis://redis:6379/1" in out

    def test_the_failure_names_the_target(self, monkeypatch):
        out = self._health_with(monkeypatch, "redis://cache.internal:6379/1")
        assert "redis://cache.internal:6379/1" in out

    def test_no_password_reaches_the_response(self, monkeypatch):
        out = self._health_with(monkeypatch, "redis://:hunter2@cache.internal:6379/1")
        assert "hunter2" not in out

    def test_healthy_says_only_ok(self, monkeypatch):
        """
        Nothing about the target when it works — a healthy response is public
        surface and should describe no infrastructure at all.
        """
        out = self._health_with(monkeypatch, "redis://redis:6379/1", ping_fails=False)
        assert out == "ok"

    def test_a_missing_password_stays_readable(self, monkeypatch):
        """
        Coolify's managed Redis sets `requirepass`, so a URL copied without its
        credentials connects and is refused with `Authentication required.` —
        which sounds like a permissions bug in the app rather than a missing
        password in a URL. The message has to keep the host visible so the
        reader can see the URL they supplied and notice what is not in it.

        This is the case that actually happened: the generated env file guessed
        a passwordless URL from a snapshot taken before the password existed.
        """
        monkeypatch.setattr("app.config.settings.redis_url", "redis://redis:6379/1")

        class NoAuth:
            def ping(self):
                raise ConnectionError("Authentication required.")

        monkeypatch.setattr("app.workers.job_store._get_redis", lambda: NoAuth())
        from app.api.v1.health import _check_redis
        out = _check_redis()
        assert "Authentication required" in out
        assert "redis://redis:6379/1" in out
        assert "REDIS_URL is not set" not in out

    def test_a_wrong_password_is_not_confused_with_a_missing_one(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.redis_url", "redis://:nope@redis:6379/1")

        class BadAuth:
            def ping(self):
                raise ConnectionError(
                    "invalid username-password pair or user is disabled.")

        monkeypatch.setattr("app.workers.job_store._get_redis", lambda: BadAuth())
        from app.api.v1.health import _check_redis
        out = _check_redis()
        assert "invalid username-password" in out
        assert "nope" not in out, "the rejected password must not be echoed"


class TestTheTwoDeployments:
    def test_the_dev_default_is_database_zero(self):
        """
        StayOS production belongs on database 1. If the default ever moves to a
        non-zero database this comment and DEPLOYING_STAYOS.md both need
        revisiting, so pin it.
        """
        assert Settings.model_fields["redis_url"].default.endswith("/0")

    @pytest.mark.parametrize("url,db", [
        ("redis://redis:6379/0", "/0"),
        ("redis://redis:6379/1", "/1"),
    ])
    def test_the_database_number_survives_redaction(self, url, db):
        """Which database is the whole point of the separation; never elide it."""
        assert redis_target(url).endswith(db)
