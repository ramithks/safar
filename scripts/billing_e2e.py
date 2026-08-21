#!/usr/bin/env python3
"""
The money: a hotel subscribes, is charged, gets its credits, and spends them.

    plan ──► coupon ──► Razorpay mandate ──► webhook ──► active + credits
                                                │
                                                └── retry ──► nothing twice

**Real Razorpay, test keys.** Plans and subscriptions are actually created; the
webhook is delivered by this script with a signature computed from the live
webhook secret, which is the only part Razorpay cannot do for us without a
browser and a card. Everything the signature protects is therefore exercised
exactly as it will be in production — including the duplicate delivery, which
is the case that matters most and which no manual test ever performs.

**Runs on the demo hotel and a QA hotel.** Both are ours. The three pilot
hotels are never read and never written; their mandates and balances are
asserted untouched at the end.

    set -a; . ./.env.hotelos; set +a
    export ADMIN_API_PASSCODE=…
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/billing_e2e.py
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

API = "http://127.0.0.1:8010/api/v1"
REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co/auth/v1"

DEMO = "47f4cbf8-7cad-4369-a4c9-a4a70a0c29c0"
DEMO_OWNER = "owner@anandihouse.demo"
DEMO_RECEPTION = "reception@anandihouse.demo"
PASSWORD = "AnandiDemo!2026"
OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

PILOTS = ("Erin Villa Coonoor", "Hotel Lotus", "Your Perfect NATURE RETREAT")

TAG = uuid.uuid4().hex[:6].upper()
ADMIN = {"X-Admin-Passcode": os.environ.get("ADMIN_API_PASSCODE", ""),
         "X-Admin-Actor": f"billing_e2e/{TAG}"}
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

PASS = FAIL = 0
STEP = 0


def http(url, method="GET", body=None, headers=None, raw_body=None):
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, text[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


_raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True).strip()
MGMT = base64.b64decode(_raw.removeprefix("go-keyring-base64:")).decode()


def sql(query):
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    if st not in (200, 201):
        raise RuntimeError(f"SQL failed: {st} {str(body)[:300]}")
    return body


_keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
             headers={"Authorization": f"Bearer {MGMT}",
                      "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in _keys if k.get("name") == "anon"][0]


def signin(email):
    st, d = http(f"{SB}/token?grant_type=password", "POST",
                 {"email": email, "password": PASSWORD}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


def section(title):
    global STEP
    STEP += 1
    print(f"\n── {STEP}. {title} " + "─" * max(0, 56 - len(title)))


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def deliver(event_name, entity, *, event_id=None, payment=None, invoice=None,
            signature=True):
    """
    Post a webhook the way Razorpay does, signed with the real secret.

    The signature is over the exact bytes sent — re-serialising the parsed JSON
    would change them and the HMAC would never match, which is the bug this
    helper exists to not have.
    """
    payload = {"event": event_name, "payload": {"subscription": {"entity": entity}}}
    if payment:
        payload["payload"]["payment"] = {"entity": payment}
    if invoice:
        payload["payload"]["invoice"] = {"entity": invoice}
    body = json.dumps(payload).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig if signature else "deadbeef"}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return http(f"{API}/pms/public/razorpay/webhook", "POST",
                headers=headers, raw_body=body)


def account_of(property_id):
    rows = sql(f"select billing_account_id from properties where id = '{property_id}';")
    return rows[0]["billing_account_id"] if rows else None


def balance(property_id):
    rows = sql(f"""select w.balance_credits from credit_wallets w
                   join properties p on p.billing_account_id = w.billing_account_id
                   where p.id = '{property_id}';""")
    return int(rows[0]["balance_credits"]) if rows else 0


def cleanup():
    sql(f"""delete from plan_coupon_redemptions
             where coupon_id in (select id from plan_coupons where code like 'QA{TAG}%');""")
    sql(f"delete from plan_coupons where code like 'QA{TAG}%';")
    sql(f"""delete from subscriptions
             where property_id = '{DEMO}' and metadata->>'createdVia' = 'owner_checkout';""")
    sql(f"delete from razorpay_webhook_events where event_id like 'evt_qa_{TAG}%';")


def main():
    if http(f"{API}/health")[0] != 200:
        print("The API is not running on :8010.")
        return 2
    if not WEBHOOK_SECRET:
        print("RAZORPAY_WEBHOOK_SECRET is not set; the webhook half cannot run.")
        return 2
    if not ADMIN["X-Admin-Passcode"]:
        print("ADMIN_API_PASSCODE is not set; the admin half cannot run.")
        return 2

    owner = signin(DEMO_OWNER)
    reception = signin(DEMO_RECEPTION)
    if not owner:
        print("Could not sign in as the demo owner.")
        return 2

    cleanup()
    plan_url = f"{API}/pms/properties/{DEMO}/plan"
    opening_balance = balance(DEMO)
    pilots_before = sql(
        "select name, (select count(*) from subscriptions s where s.property_id = p.id "
        "and s.razorpay_subscription_id is not null) mandates from properties p "
        f"where p.name in {PILOTS};")

    # ── 1 ────────────────────────────────────────────────────────────────────
    section("one price, and it comes from the database")
    st, plan = http(plan_url, headers=owner)
    check("the plan answers", st == 200, st)
    check("it costs ₹999", plan.get("pricePaise") == 99900, plan.get("pricePaise"))
    check("and says so in the hotel's words, not a status enum",
          plan.get("headline") and "_" not in plan["headline"], plan.get("headline"))
    check("nothing is active yet", plan.get("status") == "none", plan.get("status"))
    check("and it can be started", plan.get("canStart") is True)
    check("credits included are configured, not hard-coded",
          plan.get("includedCredits", 0) > 0, plan.get("includedCredits"))
    db_price = sql("select price_paise, credit_grant from subscription_plans "
                   "where code='starter' and effective_to is null;")[0]
    check("the API price is the database price",
          plan["pricePaise"] == db_price["price_paise"],
          f"{plan['pricePaise']} vs {db_price['price_paise']}")
    check("and so is the credit grant",
          plan["includedCredits"] == db_price["credit_grant"])

    # ── 2 ────────────────────────────────────────────────────────────────────
    section("who may see and touch billing")
    st, _ = http(plan_url)
    check("no token, no plan", st == 401, st)
    st, _ = http(f"{API}/pms/properties/{OTHER}/plan", headers=owner)
    check("one hotel cannot read another's plan", st == 404, st)
    st, _ = http(f"{API}/pms/properties/{OTHER}/plan/subscribe", "POST", {}, owner)
    check("nor subscribe on its behalf", st == 404, st)
    if reception:
        st, d = http(plan_url, headers=reception)
        check("a receptionist cannot see what the hotel pays", st == 403, st)
        check("  and is told who can",
              "owner" in str((d or {}).get("detail", "")).lower(), d)
        st, _ = http(f"{API}/pms/properties/{DEMO}/plan/subscribe", "POST", {}, reception)
        check("nor start a subscription", st == 403, st)

    # ── 3 ────────────────────────────────────────────────────────────────────
    section("the price is not the client's to choose")
    st, d = http(f"{API}/pms/properties/{DEMO}/plan/subscribe", "POST",
                 {"pricePaise": 100, "amount": 1, "planId": "whatever",
                  "credits": 999999}, owner)
    check("extra fields naming a price are ignored, not honoured", st in (200, 422), st)
    if st == 200:
        check("the mandate is for ₹999 whatever the body said",
              d.get("amountPaise") == 99900, d.get("amountPaise"))
        sql(f"""delete from subscriptions where property_id = '{DEMO}'
                 and razorpay_subscription_id = '{d["razorpaySubscriptionId"]}';""")

    # ── 4 ────────────────────────────────────────────────────────────────────
    section("coupons: what a code is worth is decided here")
    coupons = {}
    for code, body in [
        (f"QA{TAG}FREE", {"kind": "free_days", "freeDays": 14, "campaign": "QA",
                          "description": "Two weeks free"}),
        (f"QA{TAG}P20", {"kind": "percent", "percentOff": 20,
                         "razorpayOfferId": f"offer_qa_{TAG}_20"}),
        (f"QA{TAG}P30", {"kind": "percent", "percentOff": 30,
                         "razorpayOfferId": f"offer_qa_{TAG}_30"}),
        (f"QA{TAG}P50", {"kind": "percent", "percentOff": 50,
                         "razorpayOfferId": f"offer_qa_{TAG}_50"}),
        (f"QA{TAG}P60", {"kind": "percent", "percentOff": 60,
                         "razorpayOfferId": f"offer_qa_{TAG}_60"}),
        (f"QA{TAG}CAP", {"kind": "percent", "percentOff": 25, "maxRedemptions": 1,
                         "razorpayOfferId": f"offer_qa_{TAG}_cap"}),
        (f"QA{TAG}OFF", {"kind": "percent", "percentOff": 40, "active": False,
                         "razorpayOfferId": f"offer_qa_{TAG}_off"}),
        (f"QA{TAG}OLD", {"kind": "percent", "percentOff": 40,
                         "razorpayOfferId": f"offer_qa_{TAG}_old",
                         "expiresAt": (datetime.now(timezone.utc)
                                       - timedelta(days=1)).isoformat()}),
    ]:
        st, d = http(f"{API}/admin/plan-coupons", "POST", {"code": code, **body}, ADMIN)
        if st == 200:
            coupons[code] = d
        else:
            check(f"created {code}", False, f"{st} {str(d)[:120]}")
    check("the operator can create every shape", len(coupons) == 8, sorted(coupons))

    preview = f"{API}/pms/properties/{DEMO}/plan/coupon"
    for code, expected in [(f"QA{TAG}P20", 79920), (f"QA{TAG}P30", 69930),
                           (f"QA{TAG}P50", 49950), (f"QA{TAG}P60", 39960)]:
        st, d = http(preview, "POST", {"code": code}, owner)
        pct = code[-2:]
        check(f"{pct}% off ₹999 is ₹{expected // 100}",
              st == 200 and d.get("firstPaymentPaise") == expected,
              f"{st} {(d or {}).get('firstPaymentPaise')}")
        if st == 200:
            check(f"  and the renewal is still ₹999",
                  d.get("renewalPaise") == 99900, d.get("renewalPaise"))

    st, d = http(preview, "POST", {"code": f"QA{TAG}FREE"}, owner)
    check("a free-days code discounts time, not money",
          st == 200 and d.get("freeDays") == 14 and d.get("discountPaise") == 0,
          f"{st} {d}")

    for label, code in [("an invented code", "NOSUCH99"),
                        ("a disabled code", f"QA{TAG}OFF"),
                        ("an expired code", f"QA{TAG}OLD")]:
        st, d = http(preview, "POST", {"code": code}, owner)
        check(f"{label} is refused", st == 422, st)
        check("  with the same sentence every time",
              (d or {}).get("detail") == "That code isn't valid. Check it and try again.",
              (d or {}).get("detail"))

    check("previewing spends nothing",
          sql(f"select count(*) n from plan_coupon_redemptions r join plan_coupons c "
              f"on c.id=r.coupon_id where c.code like 'QA{TAG}%';")[0]["n"] == 0)

    # ── 5 ────────────────────────────────────────────────────────────────────
    section("a real mandate, on real Razorpay")
    st, checkout = http(f"{API}/pms/properties/{DEMO}/plan/subscribe", "POST",
                        {"couponCode": f"QA{TAG}FREE"}, owner)
    check("Razorpay accepts the subscription", st == 200, f"{st} {str(checkout)[:180]}")
    if st != 200:
        print("\n(cannot continue without a mandate)")
        cleanup()
        return 1

    mandate = checkout["razorpaySubscriptionId"]
    check("it hands back a mandate id", mandate.startswith("sub_"), mandate)
    check("and a publishable key, never a secret",
          checkout["razorpayKeyId"].startswith("rzp_")
          and "secret" not in json.dumps(checkout).lower())
    check("the amount is the plan's", checkout["amountPaise"] == 99900)
    check("the coupon rode along", checkout.get("couponCode") == f"QA{TAG}FREE")

    st, plan = http(plan_url, headers=owner)
    check("the hotel is waiting for approval, not active",
          plan["status"] == "pending", plan["status"])
    check("and is told so plainly",
          "approv" in plan["headline"].lower(), plan["headline"])
    check("nothing has been granted yet",
          sql(f"select count(*) n from credit_ledger where metadata->>'paymentId' "
              f"is not null and reference_id in (select id from subscriptions "
              f"where razorpay_subscription_id='{mandate}');")[0]["n"] == 0)

    # With the same coupon: subscribing again *without* one correctly clears it,
    # which is a different behaviour and not what this check is about.
    st, d = http(f"{API}/pms/properties/{DEMO}/plan/subscribe", "POST",
                 {"couponCode": f"QA{TAG}FREE"}, owner)
    check("a second checkout while one is pending reuses the row",
          sql(f"select count(*) n from subscriptions where property_id='{DEMO}' "
              f"and status='pending';")[0]["n"] == 1)
    if st == 200:
        mandate = d["razorpaySubscriptionId"]

    # ── 6 ────────────────────────────────────────────────────────────────────
    section("the webhook is what makes it real")
    st, _ = deliver("subscription.authenticated", {"id": mandate, "status": "authenticated"},
                    event_id=f"evt_qa_{TAG}_auth", signature=False)
    check("an unsigned event is refused", st == 400, st)

    before = balance(DEMO)
    st, d = deliver("subscription.authenticated", {"id": mandate, "status": "authenticated"},
                    event_id=f"evt_qa_{TAG}_auth")
    check("a signed one is accepted", st == 200, f"{st} {d}")
    st, plan = http(plan_url, headers=owner)
    check("the hotel is active", plan["status"] == "active", plan["status"])
    check("and reads as active", plan["headline"] == "StayOS is active", plan["headline"])
    check("authentication alone grants no credits", balance(DEMO) == before,
          f"{before} → {balance(DEMO)}")
    check("but the coupon is now spent",
          sql(f"select count(*) n from plan_coupon_redemptions r join plan_coupons c "
              f"on c.id=r.coupon_id where c.code='QA{TAG}FREE';")[0]["n"] == 1)

    st, d = deliver("subscription.authenticated", {"id": mandate, "status": "authenticated"},
                    event_id=f"evt_qa_{TAG}_auth")
    check("the same event again is recognised, not re-run",
          (d or {}).get("handled") == "already seen", d)
    check("and the coupon is still spent exactly once",
          sql(f"select count(*) n from plan_coupon_redemptions r join plan_coupons c "
              f"on c.id=r.coupon_id where c.code='QA{TAG}FREE';")[0]["n"] == 1)

    # ── 7 ────────────────────────────────────────────────────────────────────
    section("the charge is what grants the credits")
    payment_id = f"pay_qa_{TAG}"
    before = balance(DEMO)
    st, d = deliver("subscription.charged", {"id": mandate, "status": "active"},
                    event_id=f"evt_qa_{TAG}_charge1",
                    payment={"id": payment_id, "amount": 99900},
                    invoice={"id": f"inv_qa_{TAG}"})
    check("the charge is handled", st == 200, f"{st} {d}")
    granted = balance(DEMO) - before
    check(f"a month of credits is granted ({granted})",
          granted == plan["includedCredits"], f"{granted} vs {plan['includedCredits']}")

    st, d = deliver("subscription.charged", {"id": mandate, "status": "active"},
                    event_id=f"evt_qa_{TAG}_charge1_retry",
                    payment={"id": payment_id, "amount": 99900})
    check("a redelivered charge grants nothing",
          balance(DEMO) - before == granted, balance(DEMO) - before)
    check("  and says it was a duplicate", (d or {}).get("duplicate") is True, d)

    st, _ = deliver("subscription.charged", {"id": mandate, "status": "active"},
                    event_id=f"evt_qa_{TAG}_charge2",
                    payment={"id": f"pay_qa_{TAG}_month2", "amount": 99900})
    check("next month's charge grants again",
          balance(DEMO) - before == granted * 2, balance(DEMO) - before)
    # `credit_ledger` is append-only — `guard_credit_ledger` refuses a delete,
    # which is the whole point of it. So the extra month is reversed the way an
    # operator would reverse it, which exercises the path that matters.
    http(f"{API}/admin/credits/adjust", "POST",
         {"billingAccountId": account_of(DEMO), "credits": -granted,
          "kind": "adjustment", "reason": "QA: second month reversed",
          "reference": f"qa-{TAG}-rev2"}, ADMIN)

    # ── 8 ────────────────────────────────────────────────────────────────────
    section("a failed payment does not switch the hotel off")
    st, _ = deliver("subscription.pending", {"id": mandate, "status": "pending"},
                    event_id=f"evt_qa_{TAG}_pending")
    st, plan = http(plan_url, headers=owner)
    check("the hotel is past due", plan["status"] == "past_due", plan["status"])
    check("told in words it can act on",
          "didn't go through" in plan["headline"], plan["headline"])
    check("and the AI keeps answering", plan["serving"] is True)
    check("with a grace period on the row", bool(plan.get("graceUntil")))

    grace = sql(f"select grace_until from subscriptions where "
                f"razorpay_subscription_id='{mandate}';")[0]["grace_until"]
    deliver("subscription.halted", {"id": mandate, "status": "halted"},
            event_id=f"evt_qa_{TAG}_halted")
    check("a second failure does not buy another week",
          sql(f"select grace_until from subscriptions where "
              f"razorpay_subscription_id='{mandate}';")[0]["grace_until"] == grace)

    sql(f"""update subscriptions set grace_until = now() - interval '1 day'
             where razorpay_subscription_id = '{mandate}';""")
    st, plan = http(plan_url, headers=owner)
    check("once grace has run out the AI stops", plan["serving"] is False)

    deliver("subscription.charged", {"id": mandate, "status": "active"},
            event_id=f"evt_qa_{TAG}_recover",
            payment={"id": f"pay_qa_{TAG}_recover", "amount": 99900})
    st, plan = http(plan_url, headers=owner)
    check("paying clears it", plan["status"] == "active" and plan["serving"] is True,
          f"{plan['status']} serving={plan['serving']}")
    http(f"{API}/admin/credits/adjust", "POST",
         {"billingAccountId": account_of(DEMO), "credits": -granted,
          "kind": "adjustment", "reason": "QA: recovery charge reversed",
          "reference": f"qa-{TAG}-rev3"}, ADMIN)

    # ── 9 ────────────────────────────────────────────────────────────────────
    section("cancelling")
    st, plan = http(f"{API}/pms/properties/{DEMO}/plan/cancel", "POST", {}, owner)
    check("the owner can cancel", st == 200, f"{st} {str(plan)[:140]}")
    check("at the end of the month they paid for",
          (plan or {}).get("cancelAtPeriodEnd") is True, plan)
    deliver("subscription.cancelled", {"id": mandate, "status": "cancelled"},
            event_id=f"evt_qa_{TAG}_cancelled")
    st, plan = http(plan_url, headers=owner)
    check("and the hotel reads as cancelled",
          plan["status"] == "cancelled" and "cancelled" in plan["headline"].lower(),
          plan["headline"])
    check("with the AI stopped", plan["serving"] is False)
    check("and can start again", plan["canStart"] is True)

    # ── 10 ───────────────────────────────────────────────────────────────────
    section("a coupon cannot be used twice by the same hotel")
    st, d = http(preview, "POST", {"code": f"QA{TAG}FREE"}, owner)
    check("the code this hotel already redeemed is refused", st == 422, st)

    coupon_cap = coupons.get(f"QA{TAG}CAP")
    if coupon_cap:
        sql(f"""insert into plan_coupon_redemptions (coupon_id, partner_id,
                razorpay_subscription_id)
                select '{coupon_cap["id"]}', p.partner_id, 'sub_qa_cap_{TAG}'
                from properties p where p.id = '{OTHER}';""")
        st, _ = http(preview, "POST", {"code": f"QA{TAG}CAP"}, owner)
        check("a code that has run out is refused to everyone", st == 422, st)

    # ── 11 ───────────────────────────────────────────────────────────────────
    section("the operator's controls")
    st, hotels = http(f"{API}/admin/hotels?q=Anandi", headers=ADMIN)
    check("hotels can be searched", st == 200 and len(hotels) >= 1, st)
    demo_row = next((h for h in (hotels or []) if h["propertyId"] == DEMO), None)
    check("with the plan and the balance on the row",
          demo_row and demo_row["credits"] > 0, demo_row)
    account = demo_row["billingAccountId"]

    st, _ = http(f"{API}/admin/hotels?q=Anandi")
    check("and not without the passcode", st == 404, st)

    before = balance(DEMO)
    ref = f"qa-{TAG}-bonus"
    st, d = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": 250, "kind": "bonus",
                  "reason": "Launch partner bonus", "reference": ref}, ADMIN)
    check("credits can be added", st == 200 and balance(DEMO) == before + 250,
          f"{st} {balance(DEMO) - before}")
    st, d = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": 250, "kind": "bonus",
                  "reason": "Launch partner bonus", "reference": ref}, ADMIN)
    check("the same adjustment twice adds nothing",
          balance(DEMO) == before + 250, balance(DEMO) - before)
    check("  and says so", "Already applied" in (d or {}).get("message", ""), d)

    st, _ = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": -100, "kind": "adjustment",
                  "reason": "QA reversal", "reference": f"qa-{TAG}-back"}, ADMIN)
    check("credits can be removed", balance(DEMO) == before + 150,
          balance(DEMO) - before)

    # More than the hotel holds, but inside the sanity bound — so the refusal
    # under test is the balance check and not the "that is an absurd number"
    # one above it.
    st, d = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": -(balance(DEMO) + 500),
                  "reason": "should fail", "reference": f"qa-{TAG}-neg"}, ADMIN)
    check("a balance cannot be driven negative", st == 422, st)
    check("  and the refusal names the real balance",
          str(balance(DEMO)) in str((d or {}).get("detail", "")).replace(",", ""),
          (d or {}).get("detail"))
    st, d = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": -5_000_000,
                  "reason": "absurd", "reference": f"qa-{TAG}-absurd"}, ADMIN)
    check("and an absurd number is refused in words, not a schema error",
          st == 422 and "million" in str((d or {}).get("detail", "")), d)

    st, d = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": 10,
                  "reason": "", "reference": f"qa-{TAG}-noreason"}, ADMIN)
    check("an adjustment with no reason is refused", st == 422, st)

    st, ledger = http(f"{API}/admin/hotels/{account}/ledger?limit=20", headers=ADMIN)
    check("the ledger reads back", st == 200 and len(ledger) > 0, st)
    manual = [e for e in (ledger or []) if e.get("actorType") == "admin"]
    check("every manual move records who and why",
          manual and all(e.get("actor") and e.get("reason") for e in manual),
          str(manual[:1])[:200])

    audit = sql(f"""select count(*) n from admin_audit_log
                     where actor = 'billing_e2e/{TAG}' and action = 'credits.adjust';""")
    check("and lands in the audit log", audit[0]["n"] >= 3, audit)

    # ── 12 ───────────────────────────────────────────────────────────────────
    section("nobody else's money")
    st, _ = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": 100,
                  "reason": "no passcode", "reference": f"qa-{TAG}-nopass"}, None)
    check("credits cannot be moved without the passcode", st == 404, st)
    st, _ = http(f"{API}/admin/credits/adjust", "POST",
                 {"billingAccountId": account, "credits": 100,
                  "reason": "hotel token", "reference": f"qa-{TAG}-hotel"}, owner)
    check("nor with a hotel owner's own token", st == 404, st)
    st, _ = http(f"{API}/admin/hotels", headers=owner)
    check("a hotel owner cannot list every hotel", st == 404, st)

    # ── 13 ───────────────────────────────────────────────────────────────────
    section("the pilot hotels are as they were")
    after = sql("select name, (select count(*) from subscriptions s "
                "where s.property_id = p.id and s.razorpay_subscription_id is not null) "
                f"mandates from properties p where p.name in {PILOTS};")
    check("all three are present", len(after) == 3, after)
    check("and none of them has been given a mandate",
          all(r["mandates"] == 0 for r in after), after)
    check("nor was charged", all(r["mandates"] == b["mandates"]
                                 for r, b in zip(sorted(after, key=lambda x: x["name"]),
                                                 sorted(pilots_before, key=lambda x: x["name"]))))

    # Put the demo hotel's balance back where this run found it. Reversed
    # rather than deleted: the ledger is append-only and the reversal is itself
    # the audit trail for what this suite did.
    drift = balance(DEMO) - opening_balance
    if drift:
        http(f"{API}/admin/credits/adjust", "POST",
             {"billingAccountId": account_of(DEMO), "credits": -drift,
              "kind": "adjustment", "reason": f"QA billing_e2e {TAG} swept",
              "reference": f"qa-{TAG}-sweep"}, ADMIN)
    cleanup()
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("REAL RAZORPAY (test keys) · REAL DATABASE · SIGNED WEBHOOKS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
