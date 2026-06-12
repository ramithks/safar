#!/usr/bin/env python3
"""
Smoke test for every TravBuddy API endpoint.

Checks that each route is reachable and behaves as expected:
  - public routes return 200
  - protected routes return 401/403 without a token (proves auth is wired),
    or 200 with one
  - nothing returns a router-level 404 ("Not Found") or a 5xx

Usage:
  python scripts/smoke_test.py                          # against production
  python scripts/smoke_test.py --base http://localhost:8000
  AUTH_TOKEN=<supabase-jwt> python scripts/smoke_test.py   # authenticated run

Get a token (anon key + test user):
  curl -s "$SUPABASE_URL/auth/v1/token?grant_type=password" \
    -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
    -d '{"email":"...","password":"..."}' | jq -r .access_token

Exit code 0 = all pass, 1 = at least one failure.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "https://backend.travbuddy.in"

# Placeholder IDs: valid-looking UUIDs that won't exist in the DB.
# A 404 whose body is NOT FastAPI's bare {"detail": "Not Found"} means the
# route matched and the handler looked up the resource — that's a pass.
UUID = "00000000-0000-4000-8000-000000000000"

# (method, path, expects) — expects:
#   "public"    -> must be 200
#   "auth"      -> 401/403 without token; 2xx/404-resource/422 with token
#   "auth-post" -> 401/403 without token; with token we still only check
#                  reachability (422 missing-body is fine, avoids real work)
ENDPOINTS = [
    ("GET",    "/", "public"),
    ("GET",    "/api/v1/health", "public"),

    # discover
    ("GET",    "/api/v1/discover/vibe-trips", "auth"),
    ("GET",    "/api/v1/discover/weekend-plans", "auth"),

    # plan + onboarding + search (expensive AI/live searches: reachability only)
    ("POST",   "/api/v1/plan", "auth-post"),
    ("POST",   "/api/v1/onboarding/generate-feed", "auth-post"),
    ("POST",   "/api/v1/search/all", "auth-post"),
    ("POST",   "/api/v1/search/flights", "auth-post"),
    ("POST",   "/api/v1/search/trains", "auth-post"),
    ("POST",   "/api/v1/search/buses", "auth-post"),
    ("POST",   "/api/v1/search/hotels", "auth-post"),

    # groups
    ("POST",   "/api/v1/groups", "auth-post"),
    ("GET",    f"/api/v1/groups/by-trip/{UUID}", "auth"),
    ("POST",   "/api/v1/groups/join/TESTCODE", "auth-post"),
    ("GET",    f"/api/v1/groups/{UUID}", "auth"),
    ("POST",   f"/api/v1/groups/{UUID}/ai", "auth-post"),
    ("GET",    f"/api/v1/groups/{UUID}/expenses", "auth"),
    ("POST",   f"/api/v1/groups/{UUID}/expenses", "auth-post"),
    ("DELETE", f"/api/v1/groups/{UUID}/expenses/{UUID}", "auth"),
    ("GET",    f"/api/v1/groups/{UUID}/messages", "auth"),
    ("POST",   f"/api/v1/groups/{UUID}/messages", "auth-post"),
    ("POST",   f"/api/v1/groups/{UUID}/messages/{UUID}/vote", "auth-post"),

    # images (public proxy to Google Places photos; invalid ref -> 400)
    ("GET",    "/api/v1/images/invalid-photo-ref", "open"),

    # reels
    ("GET",    "/api/v1/reels/developing", "auth"),
    ("POST",   "/api/v1/reels/save", "auth-post"),
    ("GET",    "/api/v1/reels/saved", "auth"),
    ("POST",   f"/api/v1/reels/{UUID}/like", "auth-post"),

    # trips: bookings
    ("GET",    f"/api/v1/trips/{UUID}/bookings", "auth"),
    ("POST",   f"/api/v1/trips/{UUID}/bookings", "auth-post"),
    ("DELETE", f"/api/v1/trips/{UUID}/bookings/{UUID}", "auth"),
    ("PATCH",  f"/api/v1/trips/{UUID}/bookings/{UUID}", "auth-post"),
    ("POST",   f"/api/v1/trips/{UUID}/bookings/{UUID}/confirm-file", "auth-post"),
    ("POST",   f"/api/v1/trips/{UUID}/bookings/{UUID}/upload-url", "auth-post"),

    # trips: documents
    ("GET",    f"/api/v1/trips/{UUID}/documents", "auth"),
    ("POST",   f"/api/v1/trips/{UUID}/documents/create", "auth-post"),
    ("POST",   f"/api/v1/trips/{UUID}/documents/init", "auth-post"),
    ("DELETE", f"/api/v1/trips/{UUID}/documents/{UUID}", "auth"),
    ("POST",   f"/api/v1/trips/{UUID}/documents/{UUID}/confirm", "auth-post"),

    # trips: files + reels
    ("POST",   f"/api/v1/trips/{UUID}/files/upload", "auth-post"),
    ("GET",    f"/api/v1/trips/{UUID}/reels", "auth"),
    ("POST",   f"/api/v1/trips/{UUID}/reels/init", "auth-post"),
    ("DELETE", f"/api/v1/trips/{UUID}/reels/{UUID}", "auth"),
    ("POST",   f"/api/v1/trips/{UUID}/reels/{UUID}/confirm", "auth-post"),

    # users: folders
    ("GET",    "/api/v1/users/folders", "auth"),
    ("POST",   "/api/v1/users/folders", "auth-post"),
    ("DELETE", f"/api/v1/users/folders/{UUID}", "auth"),
    ("GET",    f"/api/v1/users/folders/{UUID}/items", "auth"),
    ("POST",   f"/api/v1/users/folders/{UUID}/items", "auth-post"),
    ("DELETE", f"/api/v1/users/folders/{UUID}/items/{UUID}", "auth"),

    # users: stats + trips
    ("GET",    "/api/v1/users/stats", "auth"),
    ("GET",    "/api/v1/users/trips", "auth"),
    ("GET",    f"/api/v1/users/trips/{UUID}", "auth"),
    ("PUT",    f"/api/v1/users/trips/{UUID}", "auth-post"),
    ("DELETE", f"/api/v1/users/trips/{UUID}", "auth"),
    ("GET",    f"/api/v1/users/trips/{UUID}/journey", "auth"),
]


def call(base: str, method: str, path: str, token: str | None, timeout: int):
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = b"{}" if method in ("POST", "PUT", "PATCH") else None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # cert may be self-signed before DNS flip
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout, context=ctx) as r:
            return r.status, r.read(4096)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)
    except Exception as e:
        return 0, str(e).encode()


def is_router_404(body: bytes) -> bool:
    """FastAPI's bare 404 means no route matched — a real failure."""
    try:
        return json.loads(body).get("detail") == "Not Found"
    except Exception:
        return True  # non-JSON 404 (proxy-level) is also a failure


def is_missing_auth_422(body: bytes) -> bool:
    """The auth dependency is a required header param, so a missing token
    surfaces as 422 with loc ["header","authorization"] — that's auth working."""
    try:
        return any(
            e.get("type") == "missing" and "authorization" in e.get("loc", [])
            for e in json.loads(body).get("detail", [])
        )
    except Exception:
        return False


def evaluate(expects: str, status: int, body: bytes, has_token: bool) -> tuple[bool, str]:
    if status == 0:
        return False, "connection failed"
    if status >= 500:
        return False, "server error"
    if status == 404 and is_router_404(body):
        return False, "route not found"
    if expects == "public":
        return status == 200, f"expected 200"
    if expects == "open":
        return status < 500, "expected non-5xx"
    if not has_token:
        ok = status in (401, 403) or (status == 422 and is_missing_auth_422(body))
        return ok, "expected auth rejection without token"
    # with token: 2xx, resource-404, or 422 (placeholder ids / empty body) all
    # prove the route is wired past auth
    return status in (200, 201, 204, 404, 409, 422), "unexpected status with token"


def main():
    ap = argparse.ArgumentParser(description="TravBuddy API smoke test")
    ap.add_argument("--base", default=os.environ.get("BASE_URL", DEFAULT_BASE))
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()
    token = os.environ.get("AUTH_TOKEN")

    print(f"Base URL : {args.base}")
    print(f"Auth     : {'token provided' if token else 'no token (reachability mode)'}")
    print("-" * 72)

    failures = 0
    for method, path, expects in ENDPOINTS:
        status, body = call(args.base, method, path, token, args.timeout)
        ok, why = evaluate(expects, status, body, bool(token))
        mark = "PASS" if ok else "FAIL"
        line = f"{mark}  {status:>3}  {method:<6} {path}"
        if not ok:
            failures += 1
            line += f"   <- {why}: {body[:120]!r}"
        print(line)

    print("-" * 72)
    total = len(ENDPOINTS)
    print(f"{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
