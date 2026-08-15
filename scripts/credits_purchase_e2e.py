#!/usr/bin/env python3
"""
Buying credits — the commerce layer over a ledger that already existed.

    order ──► Razorpay ──► verify / webhook ──► grant_credits() ──► purchased
                                                                     bucket

The ledger, the bucket walk, `charge_usage` and the wallet have been built and
tested since migration 13. **Nothing here touches any of them.** A purchase
appends a `purchased` bucket through `billing.grant()`, keyed on the Razorpay
order id, and that is the whole of it — no wallet balance is written directly
and `charge_usage` is unchanged.

So what this suite asserts is almost entirely about money happening once:

  * a webhook delivered three times grants once
  * the browser and the webhook racing grant once
  * the ledger and the wallet agree afterwards, to the credit

Razorpay order creation is real against the live account. The card is not: the
signatures are computed with the same HMAC and the same secret the server
verifies with, which exercises the verification rather than bypassing it — a
forged signature is asserted to be refused.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/credits_purchase_e2e.py
"""
import base64
import hashlib
import hmac
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"
PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def http(url, method="GET", data=None, headers=None, raw_body=None):
    body = raw_body if raw_body is not None else (
        json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(
        url, method=method, data=body,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


def sql(query: str):
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    if st not in (200, 201):
        raise RuntimeError(f"SQL failed: {st} {str(body)[:300]}")
    return body


keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
            headers={"Authorization": f"Bearer {MGMT}",
                     "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in keys if k.get("name") == "anon"][0]


def signin(email):
    st, d = http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                 {"email": email, "password": "hotel-os-dev"}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


def payment_signature(order_id: str, payment_id: str) -> str:
    return hmac.new((settings.razorpay_key_secret or "").encode(),
                    f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def webhook_body(order_id: str, payment_id: str) -> tuple[bytes, str]:
    payload = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }).encode()
    return payload, hmac.new((settings.razorpay_webhook_secret or "").encode(),
                             payload, hashlib.sha256).hexdigest()


def wallet():
    rows = sql(f"""
select w.balance_credits as balance, w.id as wallet_id, a.id as account_id
  from public.credit_wallets w
  join public.billing_accounts a on a.id = w.billing_account_id
  join public.properties p on p.billing_account_id = a.id
 where p.id = '{PROP}' limit 1;""")
    return rows[0] if rows else None


def ledger_total(account_id: str) -> int:
    return int(sql(f"""select coalesce(sum(credits), 0)::bigint as t
                        from public.credit_ledger
                       where billing_account_id = '{account_id}';""")[0]["t"])


def main() -> int:
    print("\n══ credit purchase e2e ══\n")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1
    if not (settings.razorpay_key_secret and settings.razorpay_webhook_secret):
        print("  Razorpay is not configured — this suite cannot run")
        return 1

    manager = signin("priya@casamandala.in")     # billing
    reception = signin("deepak@casamandala.in")  # not billing
    if not manager or not reception:
        print("  could not sign in")
        return 1

    before = wallet()
    check("the property has a wallet", before is not None, str(before))
    if not before:
        return 1
    account = before["account_id"]
    opening = int(before["balance"])
    opening_ledger = ledger_total(account)
    check("the wallet balance is the ledger before we start",
          opening == opening_ledger, f"wallet={opening} ledger={opening_ledger}")

    # ── who may buy ──────────────────────────────────────────────────────────
    print("\n── who may spend the hotel's money ──")
    st, _ = http(f"{API}/properties/{PROP}/credits/order", "POST", {"credits": 500}, reception)
    check("a receptionist cannot buy credits", st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/credits/order", "POST", {"credits": 500})
    check("unauthenticated cannot", st == 401, str(st))
    st, _ = http(f"{API}/properties/{PROP}/credits/order", "POST", {"credits": 1}, manager)
    check("a nonsensical bundle is refused", st == 422, str(st))
    st, _ = http(f"{API}/properties/{PROP}/credits/order", "POST",
                 {"credits": 500, "amountPaise": 1}, manager)
    check("an amount in the body is not a field the server reads", st == 200, str(st))

    # ── the order ────────────────────────────────────────────────────────────
    print("\n── the order ──")
    CREDITS = 500
    st, order = http(f"{API}/properties/{PROP}/credits/order", "POST",
                     {"credits": CREDITS}, manager)
    check("a manager starts a top-up", st == 200, f"{st} {str(order)[:200]}")
    if st != 200:
        return 1
    check("the server priced it, not the client",
          order["amountPaise"] == CREDITS * 100,
          f"{order['amountPaise']} for {CREDITS} credits")
    check("a real Razorpay order exists",
          order["razorpayOrderId"].startswith("order_"), str(order.get("razorpayOrderId")))
    check("only the public key reaches the browser",
          order["keyId"] and "secret" not in order["keyId"].lower(),
          "a secret was sent to the client")
    check("nothing has been granted yet",
          int(wallet()["balance"]) == opening, str(wallet()))

    rzp_order = order["razorpayOrderId"]
    payment_id = f"pay_TEST{uuid.uuid4().hex[:14]}"

    st, _ = http(f"{API}/properties/{PROP}/credits/verify", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": "not-a-signature",
    }, manager)
    check("a forged signature grants nothing", st == 400, str(st))
    check("and the wallet has not moved", int(wallet()["balance"]) == opening)

    # ── the grant ────────────────────────────────────────────────────────────
    print("\n── the grant ──")
    signature = payment_signature(rzp_order, payment_id)
    st, granted = http(f"{API}/properties/{PROP}/credits/verify", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": signature,
    }, manager)
    check("a verified payment grants the credits", st == 200, f"{st} {str(granted)[:200]}")
    check("it reports what it granted",
          st == 200 and granted["creditsGranted"] == CREDITS, str(granted)[:200])

    after = wallet()
    check(f"the wallet is {CREDITS} credits richer",
          int(after["balance"]) == opening + CREDITS,
          f"{opening} → {after['balance']}")
    check("the ledger and the wallet still agree",
          ledger_total(account) == int(after["balance"]),
          f"ledger={ledger_total(account)} wallet={after['balance']}")

    buckets = sql(f"""select kind, credits_granted, source_type
                        from public.credit_buckets
                       where wallet_id = '{after["wallet_id"]}'
                         and source_type = 'purchase'
                       order by granted_at desc limit 1;""")
    check("it landed in a purchased bucket",
          buckets and buckets[0]["kind"] == "purchased", str(buckets))
    check("with the right number of credits",
          buckets and int(buckets[0]["credits_granted"]) == CREDITS, str(buckets))

    entries = sql(f"""select entry_type, credits from public.credit_ledger
                       where idempotency_key = 'credit-purchase:{rzp_order}';""")
    check("exactly one ledger entry names this order", len(entries) == 1, str(entries))
    check("and it is a grant, not an adjustment",
          entries and entries[0]["entry_type"] == "grant", str(entries))

    # ── the same money, arriving again ───────────────────────────────────────
    print("\n── the same payment, confirmed again and again ──")
    st, again = http(f"{API}/properties/{PROP}/credits/verify", "POST", {
        "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
        "razorpaySignature": signature,
    }, manager)
    check("a second verification grants nothing",
          st == 200 and again.get("alreadyProcessed") is True, f"{st} {str(again)[:200]}")
    check("and the wallet is unchanged",
          int(wallet()["balance"]) == opening + CREDITS, str(wallet()))

    body, sig = webhook_body(rzp_order, payment_id)
    for i in range(3):
        st, hook = http(f"{API}/public/razorpay/webhook", "POST",
                        raw_body=body, headers={"X-Razorpay-Signature": sig})
        check(f"webhook delivery {i + 1} is a no-op",
              st == 200 and hook.get("handled") == "already settled", f"{st} {hook}")
    check("three webhook deliveries granted nothing further",
          int(wallet()["balance"]) == opening + CREDITS, str(wallet()))
    check("and there is still one ledger entry for this order",
          len(sql(f"""select id from public.credit_ledger
                       where idempotency_key = 'credit-purchase:{rzp_order}';""")) == 1)

    # ── the webhook arriving first ───────────────────────────────────────────
    print("\n── the webhook wins the race ──")
    st, order2 = http(f"{API}/properties/{PROP}/credits/order", "POST",
                      {"credits": 200}, manager)
    if st == 200:
        pay2 = f"pay_TEST{uuid.uuid4().hex[:14]}"
        body2, sig2 = webhook_body(order2["razorpayOrderId"], pay2)
        st, hook2 = http(f"{API}/public/razorpay/webhook", "POST",
                         raw_body=body2, headers={"X-Razorpay-Signature": sig2})
        check("the webhook grants with no browser involved",
              st == 200 and hook2.get("handled") == "credits", f"{st} {hook2}")
        check("the wallet moved by exactly the bundle",
              int(wallet()["balance"]) == opening + CREDITS + 200, str(wallet()))

        st, late = http(f"{API}/properties/{PROP}/credits/verify", "POST", {
            "razorpayOrderId": order2["razorpayOrderId"], "razorpayPaymentId": pay2,
            "razorpaySignature": payment_signature(order2["razorpayOrderId"], pay2),
        }, manager)
        check("the browser arriving late is told it was already done",
              st == 200 and late.get("alreadyProcessed") is True, f"{st} {str(late)[:160]}")
        check("and granted nothing more",
              int(wallet()["balance"]) == opening + CREDITS + 200, str(wallet()))

    # ── a failed payment ─────────────────────────────────────────────────────
    print("\n── a payment that failed ──")
    st, order3 = http(f"{API}/properties/{PROP}/credits/order", "POST",
                      {"credits": 300}, manager)
    if st == 200:
        failed = json.dumps({
            "event": "payment.failed",
            "payload": {"payment": {"entity": {
                "id": f"pay_TEST{uuid.uuid4().hex[:14]}",
                "order_id": order3["razorpayOrderId"],
                "error_description": "card declined",
            }}},
        }).encode()
        fsig = hmac.new((settings.razorpay_webhook_secret or "").encode(),
                        failed, hashlib.sha256).hexdigest()
        st, hook3 = http(f"{API}/public/razorpay/webhook", "POST",
                         raw_body=failed, headers={"X-Razorpay-Signature": fsig})
        check("a failed payment is recorded", st == 200 and hook3.get("handled") == "failed",
              f"{st} {hook3}")
        row = sql(f"""select status, failure_reason from public.payment_orders
                       where razorpay_order_id = '{order3["razorpayOrderId"]}';""")
        check("the order says why", row and row[0]["status"] == "failed"
              and "declined" in (row[0]["failure_reason"] or ""), str(row))
        check("and nothing was granted",
              int(wallet()["balance"]) == opening + CREDITS + 200, str(wallet()))

    # ── zero drift ───────────────────────────────────────────────────────────
    print("\n── the books balance ──")
    final = wallet()
    check("the wallet is the sum of its ledger, to the credit",
          int(final["balance"]) == ledger_total(account),
          f"wallet={final['balance']} ledger={ledger_total(account)}")
    check("the whole run moved the wallet by exactly what was bought",
          int(final["balance"]) - opening == CREDITS + 200,
          f"moved {int(final['balance']) - opening}, bought {CREDITS + 200}")

    st, history = http(f"{API}/properties/{PROP}/payments", headers=manager)
    check("the hotel can see its own orders", st == 200 and history, f"{st}")
    check("including the one that failed",
          st == 200 and any(o["status"] == "failed" for o in history))
    st, _ = http(f"{API}/properties/{PROP}/payments", headers=reception)
    check("a receptionist cannot", st == 403, str(st))

    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL LEDGER · REAL RAZORPAY ORDERS · SIMULATED CARD "
          "(signatures computed with the live secret)\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
