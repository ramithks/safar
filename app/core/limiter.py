"""
Rate limiter.

Keying: authenticated requests are keyed by the JWT `sub` (user id) so limits
are per-user; unauthenticated requests fall back to client IP. Behind Traefik
the IP only works because uvicorn runs with --proxy-headers (see Dockerfile) —
without it every request would share the proxy's IP and one bucket.

Storage: Redis, so counters survive restarts and are shared across workers.
"""
import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def _user_or_ip(request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            # Signature is NOT verified here — that's require_user's job.
            # For rate-limit bucketing an unverified sub is fine: forging a
            # different sub only ever puts the attacker in a different bucket.
            payload = jwt.decode(auth[len("Bearer "):], options={"verify_signature": False})
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except jwt.InvalidTokenError:
            pass
    return get_remote_address(request)


# swallow_errors: if Redis is briefly unreachable, fail open (skip limiting)
# instead of 500ing every rate-limited endpoint.
limiter = Limiter(key_func=_user_or_ip, storage_uri=settings.redis_url, swallow_errors=True)
