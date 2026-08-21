"""
Shared setup for the unit suite.

**The tests must not share rate-limit counters with a running application.**

`app/core/limiter.py` stores its counters in Redis so they survive restarts and
are shared across workers — correct for production, and the reason four tests
in `test_admin_impersonation.py` began failing with 429 on any machine where
the E2E suites had been run. The counters were real, the limit was real, and
the tests were measuring the developer's afternoon.

That is the same shape of bug as the one in `test_hotel_os_routes.py`: a test
that passes in CI and fails locally teaches people to ignore the suite, and
these particular tests guard the impersonation endpoint — which mints a
sign-in link into somebody else's hotel.

So the limiter is switched off for the unit suite. Not stubbed, not given its
own Redis: `slowapi` has a supported off switch, and the thing under test in
this suite is never the rate limit itself. Where a limit genuinely matters —
that an unauthenticated caller cannot enumerate booking references — it is the
E2E suites that check it, against a real server with real Redis.
"""
import pytest

from app.core.limiter import limiter


@pytest.fixture(autouse=True, scope="session")
def _no_rate_limits():
    """Off for the whole session, and restored after."""
    was_enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was_enabled
