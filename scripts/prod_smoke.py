#!/usr/bin/env python3
"""
Is the hardening actually live?

    ./venv/bin/python scripts/prod_smoke.py

Every assertion here is about a *behaviour that changed* in the production
hardening pass, checked against the real hosts over HTTP. Nothing is mocked and
nothing is read from an environment variable — the point is to prove what the
deployed code does, and configuration that says the right thing while the box
serves the old bundle is the failure this exists to catch.

**It is designed to fail before the deploy.** Run it now and it tells you which
of the three services is still on the old code; run it after and it should be
silent. A smoke test that passes against both is testing nothing.

Read-only. It creates no bookings, sends no messages and spends no credits, so
it is safe to run against production as often as you like.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

BACKEND = "https://stayos-backend.travbuddy.in"
WEBSITE = "https://travbuddy.in"
PARTNER = "https://partner.travbuddy.in"

# A hotel that is real, and one that is seeded. The first must stay visible and
# the second must disappear from everything a crawler reads.
REAL_HOTEL = "anandi-house-udaipur"
DEMO_HOTEL = "casa-mandala-anjuna"

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"\n         {str(detail)[:220]}" if detail else ""))


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "stayos-smoke/1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc), {}


def post(url, payload, headers=None, timeout=30):
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=BACKEND)
    ap.add_argument("--website", default=WEBSITE)
    ap.add_argument("--partner", default=PARTNER)
    args = ap.parse_args()

    print("══ StayOS production smoke ══\n")

    # ── the services answer at all ──────────────────────────────────────────
    print("── reachable ──")
    for name, url in (("backend", f"{args.backend}/api/v1/pms/public/hotels"),
                      ("website", f"{args.website}/book"),
                      ("partner", f"{args.partner}/")):
        status, _, _ = get(url)
        check(f"{name} answers", status == 200, f"{url} → {status}")

    # ── §36 demo data ───────────────────────────────────────────────────────
    print("\n── demo hotels are not production signals ──")
    status, body, _ = get(f"{args.backend}/api/v1/pms/public/hotels")
    hotels = json.loads(body) if status == 200 else []
    slugs = [h.get("slug") for h in hotels]
    check("the hotel index — what the sitemap enumerates — excludes the seeded showcase",
          DEMO_HOTEL not in slugs, f"listed: {slugs}")
    check("and excludes the QA fixture",
          "qa-launch-house-jaipur" not in slugs, f"listed: {slugs}")
    check("while a real hotel is still listed",
          REAL_HOTEL in slugs, f"listed: {slugs}")

    status, body, _ = get(f"{args.backend}/api/v1/pms/public/hotels/{DEMO_HOTEL}")
    check("the demo hotel's own page still serves — it is hidden from crawlers, not from the product",
          status == 200, status)
    if status == 200:
        check("and says so, so the template can refuse to be indexed",
              json.loads(body).get("isDemo") is True, body[:160])

    status, page, _ = get(f"{args.website}/book/{DEMO_HOTEL}")
    if status == 200:
        robots = re.search(r'<meta name="robots" content="([^"]+)"', page)
        check("the demo hotel's website page is noindex",
              bool(robots) and "noindex" in robots.group(1),
              robots.group(1) if robots else "no robots tag found")

    status, sitemap, _ = get(f"{args.website}/sitemaps/stayos-hotels.xml")
    check("the StayOS hotel sitemap answers", status == 200, status)
    if status == 200:
        check("and no longer carries the fictional hotel",
              DEMO_HOTEL not in sitemap, sitemap[:200])

    # ── §7 the public booking token ─────────────────────────────────────────
    print("\n── the public booking page ──")
    status, body, _ = get(f"{args.backend}/api/v1/pms/public/bookings/deadbeefdeadbeef")
    check("an unknown booking token is 404, not an error that confirms the space",
          status == 404, status)
    status, _, _ = get(f"{args.backend}/api/v1/pms/public/stays/TB-XXXXXX")
    check("an unknown booking reference is 404", status == 404, status)

    # ── §15 webhooks ────────────────────────────────────────────────────────
    print("\n── webhooks refuse what is not signed ──")
    status, body = post(f"{args.backend}/api/v1/pms/public/razorpay/webhook",
                        {"event": "payment.captured"},
                        {"X-Razorpay-Signature": "not-a-signature",
                         "X-Razorpay-Event-Id": "evt_smoke_test"})
    check("Razorpay refuses a bad signature", status in (400, 503), f"{status} {body[:120]}")
    check("and the refusal says nothing about what a good one looks like",
          "secret" not in body.lower() and "hmac" not in body.lower(), body[:160])

    status, body = post(f"{args.backend}/api/v1/messaging/instagram/webhook",
                        {"entry": []}, {"X-Hub-Signature-256": "sha256=00"})
    check("Instagram refuses a bad signature", status == 403, f"{status} {body[:120]}")

    status, _, _ = get(f"{args.backend}/api/v1/messaging/instagram/webhook"
                       "?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=x")
    check("and refuses the handshake with a wrong verify token", status == 403, status)

    # ── §25 revalidation ────────────────────────────────────────────────────
    print("\n── revalidation ──")
    status, body = post(f"{args.website}/api/revalidate",
                        {"kind": "stayos_hotel", "slug": REAL_HOTEL},
                        {"x-revalidate-secret": "definitely-not-the-secret"})
    check("the website refuses a wrong revalidation secret", status == 403, f"{status} {body[:120]}")
    check("and 503 would mean it is not configured at all", status != 503, body[:120])

    # ── §29 observability ───────────────────────────────────────────────────
    print("\n── observability ──")
    status, _, headers = get(f"{args.backend}/api/v1/pms/public/hotels")
    request_id = headers.get("X-Request-Id") or headers.get("x-request-id")
    check("every response carries a correlation id", bool(request_id), dict(headers))
    if request_id:
        status, _, headers = get(f"{args.backend}/api/v1/pms/public/hotels",
                                 {"X-Request-Id": "smoke-test-1234"})
        echoed = headers.get("X-Request-Id") or headers.get("x-request-id")
        check("and honours one supplied by the caller", echoed == "smoke-test-1234", echoed)

    # ── §33 public API surface ──────────────────────────────────────────────
    print("\n── the public API says only what it means to ──")
    status, body, _ = get(f"{args.backend}/api/v1/pms/public/hotels/{REAL_HOTEL}")
    if status == 200:
        for leak in ("partner_id", "partnerId", "owner_id", "ownerEmail",
                     "service_role", "razorpay_key", "token_pct"):
            check(f"a public hotel carries no {leak}", leak not in body, "")

    status, body, _ = get(f"{args.backend}/api/v1/pms/properties")
    check("a hotel-scoped route needs a session", status in (401, 403, 404, 405),
          f"{status} {body[:120]}")

    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("REAL HOSTS · READ ONLY · NO BOOKINGS CREATED")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
