"""
Rate limiter.

Keying: requests carrying a token this process has already VERIFIED are keyed
by the JWT `sub` (user id) so limits are per-user; everything else falls back to
client IP. Behind Traefik the IP only works because uvicorn runs with
--proxy-headers (see Dockerfile) — without it every request would share the
proxy's IP and one bucket.

The "already verified" part is load-bearing. This used to decode `sub` without
checking the signature, on the reasoning that forging a sub only moves you to a
different bucket — which is exactly the problem: a script rotating `sub` per
request got a fresh, empty bucket every time, so no endpoint was really limited.
Anything that leans on the rate limit for its security (the booking-ref lookup
leans on it entirely) had no floor under it.

Storage: Redis, so counters survive restarts and are shared across workers.
"""
import os
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.deps.auth import verified_identity


def _user_or_ip(request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        # Only a token whose signature this process has already checked earns a
        # per-user bucket. An unverified one is indistinguishable from a forged
        # one, and a forged one must not be able to escape the IP bucket.
        identity = verified_identity(auth[len("Bearer "):])
        if identity:
            sub, is_anonymous = identity
            # ...and only an account that COST something earns one. An
            # anonymous (guest) user is free and unlimited: a single
            # unauthenticated POST /auth/v1/signup mints a fresh, validly
            # signed token with a brand-new `sub`. Keying those per-user would
            # hand out an empty bucket on demand — the same defeat as the
            # forged-sub bug above, just with real signatures. Guests share the
            # IP bucket, which is what actually caps guest trip generation.
            if not is_anonymous:
                return f"user:{sub}"
    return get_remote_address(request)


# swallow_errors: if Redis is briefly unreachable, fail open (skip limiting)
# instead of 500ing every rate-limited endpoint.
# Safar: when REDIS_URL is unset entirely (free-tier deploy), slowapi still
# runs its header-injection path for decorated routes and crashes on the unset
# request.state.view_rate_limit, so use in-memory storage instead of a
# localhost Redis that will never answer. Per-process limiting: acceptable on
# the single-instance free deployment; set REDIS_URL to restore shared limits.
_limiter_storage = os.environ.get("REDIS_URL") or "memory://"
limiter = Limiter(key_func=_user_or_ip, storage_uri=_limiter_storage, swallow_errors=True)
