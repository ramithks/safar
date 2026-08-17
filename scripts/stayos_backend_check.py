"""
Is this host a StayOS backend, and is it wired to the right database?

    ./venv/bin/python scripts/stayos_backend_check.py --base https://stayos-backend.travbuddy.in

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
import json
import sys
import urllib.error
import urllib.request

HOTEL_OS_REF = "rdlozqvuywtgsodunait"
MARKETPLACE_REF = "uabxtdhxbamuhthxnhid"

DEFAULT_EMAIL = "deepak@casamandala.in"
DEFAULT_PASSWORD = "hotel-os-dev"
DEFAULT_PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

PARTNER_ORIGIN = "https://partner.travbuddy.in"
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


def http(url, method="GET", body=None, headers=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except json.JSONDecodeError:
                return r.status, raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except json.JSONDecodeError:
            return e.code, raw, dict(e.headers)
    except Exception as e:  # noqa: BLE001 — a dead host is an answer, not a crash
        return 0, str(e), {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="origin of the backend to check")
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--property", default=DEFAULT_PROPERTY,
                    help="a property id that exists in the hotel OS")
    ap.add_argument("--supabase", default=f"https://{HOTEL_OS_REF}.supabase.co",
                    help="the hotel-OS Supabase project, for signing in")
    ap.add_argument("--anon-key", default=None,
                    help="hotel-OS anon key; without it the auth checks are skipped")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    api = f"{base}/api/v1"

    print(f"\n══ StayOS backend check ══\n   {base}\n")

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
              "backend.travbuddy.in\n  and wrong for a StayOS one.\n")
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
        "Origin": PARTNER_ORIGIN, "Access-Control-Request-Method": "GET"})
    allow = headers.get("access-control-allow-origin", "")
    check(f"{PARTNER_ORIGIN} is allowed", st == 200 and allow == PARTNER_ORIGIN,
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
