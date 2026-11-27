"""
Is this host a StaySuite backend, and is it wired to the right database?

    ./venv/bin/python scripts/staysuite_backend_check.py --base https://api.staysuite.example.com

Run it against a new deployment before pointing the PWA at it, and against the
marketplace host too — it is designed to give the *opposite* answer there, and a
check that cannot fail is not a check.

── why this exists ──────────────────────────────────────────────────────────

The marketplace and the hotel OS are two Supabase projects, and `get_db()`
builds one client from one `SUPABASE_URL`. Every way of getting that wrong
looks like something else:

  wrong project, routes mounted   → 500 from PostgREST, "table not found",
                                    on public routes a guest can reach
  wrong project, routes gated     → clean 404, indistinguishable from a
                                    service that has not finished deploying
  right project, wrong keys       → 401 on every call, reads as "my password
                                    is wrong"

None of that is visible from the environment variables, which is why this
script does not read them. **It proves the database by reading a row out of
it.** A property id is a uuid that exists in exactly one Supabase project; if
this host hands one back for a member who signed in against the hotel-OS
issuer, then the auth keys, the service key and the schema are all the hotel
OS's, and no amount of copied configuration can fake that.

The account is a *credential you already have* — by default the seeded dev
receptionist, which exists only in the hotel-OS project. Override both for a
real one:

    --email deepak@casamandala.in --password ... --property <uuid>
"""
import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request

HOTEL_OS_REF = "rdlozqvuywtgsodunait"
MARKETPLACE_REF = "uabxtdhxbamuhthxnhid"

DEFAULT_EMAIL = "deepak@casamandala.in"
DEFAULT_PASSWORD = "hotel-os-dev"
DEFAULT_PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

PWA_ORIGIN = "https://app.staysuite.example.com"
HOSTILE_ORIGIN = "https://evil.example"

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")
    return ok


def skip(label: str) -> None:
    """Not proven, and said so. A check that quietly vanishes reads as a pass."""
    print(f"  ·    skipped — {label}")


def _headers(msg):
    """Case-insensitive header dict.

    `dict(HTTPMessage)` keeps whatever casing the server used and loses the
    case-insensitive lookup, so over HTTP/1.1 — where the proxy title-cases
    them — every `headers.get("access-control-allow-origin")` below silently
    read `None`. That made the CORS section blind: it reported the PWA origin
    as refused, and would have reported a *hostile* origin as refused too,
    whatever the server actually sent.
    """
    return {k.lower(): v for k, v in msg.items()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the 3xx instead of following it.

    The OAuth callback answers 303 to the PWA. Followed, that check would read
    whatever app.staysuite.example.com returned and prove nothing about the backend.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def http(url, method="GET", body=None, headers=None, timeout=25, *,
         raw=None, follow=True):
    """`raw` sends exact bytes — a signed body must be hashed as it is sent."""
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None
    )
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.urlopen if follow else _NO_REDIRECT.open
    try:
        with opener(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw), _headers(r.headers)
            except json.JSONDecodeError:
                return r.status, raw, _headers(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), _headers(e.headers)
        except json.JSONDecodeError:
            return e.code, raw, _headers(e.headers)
    except urllib.error.URLError as e:
        return 0, str(e), {}
    except Exception as e:  # noqa: BLE001 — a dead host is an answer, not a crash
        return 0, str(e), {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="origin of the backend to check")
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--property", default=DEFAULT_PROPERTY,
                    help="a property id that exists in the hotel OS")
    ap.add_argument("--whatsapp-verify-token")
    ap.add_argument("--whatsapp-app-secret")
    ap.add_argument("--instagram-verify-token")
    ap.add_argument("--instagram-app-secret")
    ap.add_argument("--supabase", default=f"https://{HOTEL_OS_REF}.supabase.co",
                    help="the hotel-OS Supabase project, for signing in")
    ap.add_argument("--anon-key", default=None,
                    help="hotel-OS anon key; without it the auth checks are skipped")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    api = f"{base}/api/v1"

    print(f"\n══ StaySuite backend check ══\n   {base}\n")

    # ── 1. is anything there ──────────────────────────────────────────────────
    print("── 1. the service answers ──")
    st, body, _ = http(f"{api}/health")
    check("health responds", st == 200, f"{st} {str(body)[:120]}")
    deps = (body or {}).get("dependencies", {}) if isinstance(body, dict) else {}
    check("its database dependency is up", deps.get("supabase") == "ok", str(deps))
    check("its redis dependency is up", deps.get("redis") == "ok", str(deps))

    # ── 2. is the hotel OS mounted at all ─────────────────────────────────────
    print("\n── 2. the hotel OS is mounted ──")
    st, body, _ = http(f"{api}/pms/properties")
    mounted = st != 404 or (isinstance(body, dict) and body.get("detail") != "Not Found")
    check("PMS routes exist on this host", mounted,
          "every /api/v1/pms route 404s — ENABLE_HOTEL_OS is false here")
    if not mounted:
        print("\n  This is a marketplace deployment. That is correct for "
              "backend.safar.example.com\n  and wrong for a StaySuite one.\n")
        return 1

    # ── 3. unauthenticated ────────────────────────────────────────────────────
    print("\n── 3. credentials are required ──")
    check("no token is 401, not 422 and not 200", st == 401, f"{st} {str(body)[:120]}")
    st, body, _ = http(f"{api}/pms/properties",
                       headers={"Authorization": "Bearer not-a-token"})
    check("a junk token is refused too", st == 401, f"{st} {str(body)[:120]}")

    # ── 4. CORS ───────────────────────────────────────────────────────────────
    print("\n── 4. only the PWA's origin may call it from a browser ──")
    st, _, headers = http(f"{api}/health", method="OPTIONS", headers={
        "Origin": PWA_ORIGIN, "Access-Control-Request-Method": "GET"})
    allow = headers.get("access-control-allow-origin", "")
    check(f"{PWA_ORIGIN} is allowed", st == 200 and allow == PWA_ORIGIN,
          f"{st} {allow!r}")
    check("and not by a wildcard", allow != "*", f"{allow!r}")
    st, _, headers = http(f"{api}/health", method="OPTIONS", headers={
        "Origin": HOSTILE_ORIGIN, "Access-Control-Request-Method": "GET"})
    check(f"{HOSTILE_ORIGIN} is refused",
          st == 400 or not headers.get("access-control-allow-origin"),
          f"{st} {headers.get('access-control-allow-origin')!r}")

    # ── 5. the proof ──────────────────────────────────────────────────────────
    #
    # Everything above would pass on a host pointed at the wrong project. This
    # is the part that cannot.
    print("\n── 5. which database is behind it ──")
    if not args.anon_key:
        print("  ·  skipped — pass --anon-key to prove the database by reading it")
        print("     (the anon key is publishable; it is in the PWA bundle already)")
    else:
        st, tok, _ = http(f"{args.supabase}/auth/v1/token?grant_type=password", "POST",
                          {"email": args.email, "password": args.password},
                          {"apikey": args.anon_key})
        signed_in = check("a hotel-OS account can sign in", st == 200,
                          f"{st} {str(tok)[:140]}")
        if signed_in:
            auth = {"Authorization": "Bearer " + tok["access_token"]}

            # A token minted by the hotel-OS issuer. A backend holding the
            # marketplace's JWT secret cannot verify it and answers 401 — so
            # a 200 here already says the auth keys are the hotel OS's.
            st, props, _ = http(f"{api}/pms/properties", headers=auth)
            check("and this backend accepts its token — same auth issuer",
                  st == 200, f"{st} {str(props)[:140]}")

            ids = {p["id"] for p in props} if isinstance(props, list) else set()
            check("it returns properties, so it read a table it has",
                  bool(ids), str(props)[:140])
            check(f"including {args.property[:8]}… — a row that exists in "
                  f"{HOTEL_OS_REF} and nowhere else",
                  args.property in ids, f"got {sorted(i[:8] for i in ids)}")

            # ── 6. one hotel's data stays one hotel's ─────────────────────────
            print("\n── 6. the boundaries hold ──")
            st, body, _ = http(
                f"{api}/pms/properties/00000000-0000-4000-8000-000000000000/today",
                headers=auth)
            check("a property that does not exist is 404", st == 404,
                  f"{st} {str(body)[:120]}")

            other = next((i for i in _KNOWN_OTHER if i not in ids), None)
            if other:
                st, body, _ = http(f"{api}/pms/properties/{other}/today", headers=auth)
                check("a real property this member has no membership of is 404, "
                      "not 403 — no enumeration oracle",
                      st == 404, f"{st} {str(body)[:120]}")

            if ids:
                mine = args.property if args.property in ids else min(ids)
                st, today, _ = http(f"{api}/pms/properties/{mine}/today", headers=auth)
                check("their own property answers", st == 200, f"{st} {str(today)[:120]}")

    # ── 7. nothing leaks ──────────────────────────────────────────────────────
    print("\n── 7. the service key stays server-side ──")
    st, body, _ = http(f"{api}/health")
    blob = json.dumps(body) if not isinstance(body, str) else body
    for secret in ("service_role", "SUPABASE_SERVICE_KEY", "eyJhbGciOi"):
        check(f"health does not echo {secret}", secret not in blob, blob[:120])
    st, _, _ = http(f"{base}/docs")
    check("the OpenAPI docs are not public", st == 404, str(st))

    # ── 8. messaging is wired to Meta ─────────────────────────────────────────
    #
    # Everything here is provable from outside, which is the point: the
    # credentials live in Coolify where nobody can read them back, so "did the
    # paste actually land" has to be answered by asking the running service.
    #
    # The signature probes send a body with an EMPTY entry list. That passes
    # the signature check, stores nothing and wakes no agent — a probe that
    # injected a message would answer a real guest to prove a secret.
    print("\n── 8. messaging is wired to Meta ──")

    for channel, verify_token in (("whatsapp", args.whatsapp_verify_token),
                                  ("instagram", args.instagram_verify_token)):
        hook = f"{api}/messaging/{channel}/webhook"

        st, _, _ = http(hook)
        check(f"the {channel} webhook is mounted", st in (403, 422), str(st))

        st, body, _ = http(
            f"{hook}?hub.mode=subscribe&hub.verify_token=definitely-not-it&hub.challenge=X"
        )
        check(f"{channel} refuses a wrong verify token", st == 403, str(st))

        if verify_token:
            st, body, _ = http(
                f"{hook}?hub.mode=subscribe&hub.verify_token={verify_token}"
                f"&hub.challenge=PROBE_42"
            )
            # Meta reads the body, not the status: a 200 with the wrong body
            # fails the subscription with no explanation on Meta's side.
            check(f"{channel} echoes Meta's challenge verbatim",
                  st == 200 and str(body).strip() == "PROBE_42",
                  f"{st} {str(body)[:60]}")
        else:
            skip(f"{channel} handshake — pass --{channel}-verify-token to prove it")

    for channel, secret, obj in (
        ("whatsapp", args.whatsapp_app_secret, "whatsapp_business_account"),
        ("instagram", args.instagram_app_secret, "instagram"),
    ):
        if not secret:
            skip(f"{channel} signature — pass --{channel}-app-secret to prove it")
            continue
        payload = json.dumps({"object": obj, "entry": []}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        st, body, _ = http(f"{api}/messaging/{channel}/webhook", method="POST",
                           raw=payload,
                           headers={"X-Hub-Signature-256": sig})
        check(f"this host verifies inbound {channel} with the app secret we hold",
              st == 200,
              f"{st} — the deployed {channel.upper()}_APP_SECRET is missing or "
              f"is a different value, so every inbound webhook Meta sends is "
              f"being rejected with 403")

        st, _, _ = http(f"{api}/messaging/{channel}/webhook", method="POST",
                        raw=payload,
                        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
        check(f"and refuses a {channel} body signed with the wrong key", st == 403, str(st))

    # The Instagram consent flow. A 303 back to the PWA is the contract: the
    # person at the far end is a hotel owner in a browser, and a JSON error at
    # the end of an OAuth round trip is a dead end.
    st, _, headers = http(f"{api}/messaging/instagram/oauth/callback"
                          "?error=access_denied&error_reason=user_denied",
                          follow=False)
    location = (headers or {}).get("location", "")
    check("a declined Instagram consent redirects the owner back to the app",
          st == 303 and location.startswith("https://"),
          f"{st} → {location or 'no location header'}")
    check("and it lands on the PWA, not localhost",
          "localhost" not in location and "127.0.0.1" not in location,
          location or "-")

    print(f"\n╔═ {passed} passed, {failed} failed")
    print(f"╚═ {base}\n")
    return 1 if failed else 0


# Properties from other partners in the hotel OS, used only to prove that
# membership is checked rather than assumed. Reading one must 404.
_KNOWN_OTHER = [
    "dddddddd-dddd-4ddd-8ddd-dddddddddddd",  # Somewhere Else
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",  # Owner Test Hotel
]


if __name__ == "__main__":
    sys.exit(main())
