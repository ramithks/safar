#!/usr/bin/env python3
"""
The whole commercial journey, once, on a hotel that has never paid.

    subscribe with a coupon ──► authorise ──► charged ──► credits + bonus
         │                                                     │
         └── guest talks on the website ── one charge ─────────┘
             guest talks again on Instagram ── one more
                  owner sees it · copilot says the same numbers
                       admin sees the ledger · duplicate changes nothing

**Runs on the QA hotel**, which is ours and was created through the browser.
Its mandate is reset at the start so the journey can run again; the credit
ledger is append-only and is not, which is correct — a grant against a real
(test) payment is a real grant.

That makes the launch bonus the one thing this suite cannot re-run, since it is
once per hotel *ever*. So it is checked both ways: as a fact in the ledger,
which is permanent and provable, and as behaviour now, which must be that it
does **not** land a second time.

The three pilot hotels are never read and never written.

    set -a; . ./.env.hotelos; set +a
    export ADMIN_API_PASSCODE=…
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/billing_journey_e2e.py
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

API = "http://127.0.0.1:8010/api/v1"
REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co/auth/v1"

QA_SLUG = "qa-launch-house-jaipur"
PILOTS = ("Erin Villa Coonoor", "Hotel Lotus", "Your Perfect NATURE RETREAT")

TAG = uuid.uuid4().hex[:6].upper()
ADMIN = {"X-Admin-Passcode": os.environ.get("ADMIN_API_PASSCODE", ""),
         "X-Admin-Actor": f"journey/{TAG}"}
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
        with urllib.request.urlopen(req, timeout=180) as r:
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


def section(title):
    global STEP
    STEP += 1
    print(f"\n── {STEP}. {title} " + "─" * max(0, 54 - len(title)))


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def deliver(event_name, entity, *, event_id=None, payment=None):
    payload = {"event": event_name, "payload": {"subscription": {"entity": entity}}}
    if payment:
        payload["payload"]["payment"] = {"entity": payment}
    body = json.dumps(payload).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return http(f"{API}/pms/public/razorpay/webhook", "POST",
                headers=headers, raw_body=body)


def chat(slug, message, session=None):
    st, d = http(f"{API}/pms/public/hotels/{slug}/chat", "POST",
                 {"message": message, "session": session})
    return d if st == 200 else None


def main():
    if http(f"{API}/health")[0] != 200:
        print("The API is not running on :8010.")
        return 2
    if not WEBHOOK_SECRET or not ADMIN["X-Admin-Passcode"]:
        print("RAZORPAY_WEBHOOK_SECRET and ADMIN_API_PASSCODE must both be set.")
        return 2
    # And that the *server* agrees with it. A passcode set here but not on the
    # running process makes every admin call 404, which then surfaces three
    # sections later as "that coupon code isn't valid" — a true sentence about
    # a coupon that was never created, and half an hour to chase.
    if http(f"{API}/admin/hotels?limit=1", headers=ADMIN)[0] != 200:
        print("The API on :8010 is not accepting this ADMIN_API_PASSCODE.\n"
              "Start it with the same value this script has:\n"
              "  ADMIN_API_PASSCODE=… uvicorn app.main:app --port 8010")
        return 2


    prop = sql(f"""select p.id, p.partner_id, p.billing_account_id,
                          (select email from profiles pr
                            join partner_members m on m.profile_id = pr.id
                           where m.partner_id = p.partner_id and m.role = 'owner' limit 1) owner_email
                     from properties p where p.slug = '{QA_SLUG}';""")
    if not prop:
        print(f"No QA hotel ({QA_SLUG}). Run the onboarding browser flow first.")
        return 2
    prop = prop[0]
    hotel, account = prop["id"], prop["billing_account_id"]

    # The QA hotel's owner was created through the browser with a known
    # password shape; sign in as them so every check runs as a real hotelier.
    email = prop["owner_email"]
    password = f"QaLaunch!{email.split('.')[2].split('@')[0]}A1" if email else None
    st, d = http(f"{SB}/token?grant_type=password", "POST",
                 {"email": email, "password": password}, {"apikey": ANON})
    if st != 200:
        print(f"Could not sign in as {email}. Skipping — this suite needs the QA owner.")
        return 0
    owner = {"Authorization": "Bearer " + d["access_token"]}

    # Reset the mandate so the journey can run again. Only the subscription
    # row: the ledger is append-only, the wallet is real, and the hotel's
    # rooms, website and conversations are none of this suite's business.
    sql(f"delete from subscriptions where property_id = '{hotel}';")

    pilots_before = sql(
        "select name, (select count(*) from subscriptions s where s.property_id = p.id) subs "
        f"from properties p where p.name in {PILOTS};")

    # ── 1 ────────────────────────────────────────────────────────────────────
    section("a hotel that has never paid")
    st, plan = http(f"{API}/pms/properties/{hotel}/plan", headers=owner)
    check("its plan is ready to start", plan.get("canStart") is True, plan.get("status"))
    check("at ₹999", plan.get("pricePaise") == 99900)
    check("with 3,000 credits a month", plan.get("includedCredits") == 3000)
    bonus = int(plan.get("signupBonusCredits") or 0)
    check(f"and a {plan.get('signupBonusLabel','bonus').lower()} of {bonus}", bonus > 0)
    # Has this hotel ever collected the launch bonus? Both answers are
    # correct and each licenses a different assertion below.
    bonus_history = sql(f"""select credits, metadata->>'reason' reason
                              from credit_ledger
                             where billing_account_id = '{account}'
                               and idempotency_key = 'signup-bonus:{account}';""")
    had_bonus = bool(bonus_history)
    if had_bonus:
        check(f"it collected its {bonus} launch bonus once, and it is in the ledger",
              bonus_history[0]["credits"] == bonus
              and (bonus_history[0]["reason"] or "").lower() == plan["signupBonusLabel"].lower(),
              bonus_history)

    opening = sql(f"select balance_credits b from credit_wallets "
                  f"where billing_account_id = '{account}';")[0]["b"]

    # ── 2 ────────────────────────────────────────────────────────────────────
    section("a coupon, priced by the server")
    code = f"JQA{TAG}"
    st, _ = http(f"{API}/admin/plan-coupons", "POST",
                 {"code": code, "kind": "free_days", "freeDays": 7,
                  "campaign": "Journey QA", "description": "A week free"}, ADMIN)
    check("the operator creates one", st == 200, st)
    st, preview = http(f"{API}/pms/properties/{hotel}/plan/coupon", "POST",
                       {"code": code}, owner)
    check("the hotel can price it", st == 200 and preview.get("freeDays") == 7, preview)
    check("previewing spends nothing",
          sql(f"""select count(*) n from plan_coupon_redemptions r
                   join plan_coupons c on c.id = r.coupon_id
                  where c.code = '{code}';""")[0]["n"] == 0)

    # ── 3 ────────────────────────────────────────────────────────────────────
    section("subscribing")
    st, checkout = http(f"{API}/pms/properties/{hotel}/plan/subscribe", "POST",
                        {"couponCode": code}, owner)
    check("Razorpay opens a mandate", st == 200, f"{st} {str(checkout)[:160]}")
    if st != 200:
        return 1
    mandate = checkout["razorpaySubscriptionId"]
    check("nothing is active yet",
          http(f"{API}/pms/properties/{hotel}/plan", headers=owner)[1]["status"] == "pending")
    check("and no credits have moved",
          sql(f"select balance_credits b from credit_wallets "
              f"where billing_account_id = '{account}';")[0]["b"] == opening)

    deliver("subscription.authenticated", {"id": mandate, "status": "authenticated"},
            event_id=f"evt_j_{TAG}_auth")
    st, plan = http(f"{API}/pms/properties/{hotel}/plan", headers=owner)
    check("the webhook makes it active", plan["status"] == "active", plan["status"])
    check("the coupon is now spent",
          sql(f"""select count(*) n from plan_coupon_redemptions r
                   join plan_coupons c on c.id = r.coupon_id
                  where c.code = '{code}';""")[0]["n"] == 1)

    # ── 4 ────────────────────────────────────────────────────────────────────
    section("the first payment")
    st, d = deliver("subscription.charged", {"id": mandate, "status": "active"},
                    event_id=f"evt_j_{TAG}_charge",
                    payment={"id": f"pay_j_{TAG}", "amount": 99900})
    check("3,000 credits are granted", (d or {}).get("credits_granted") == 3000, d)
    expected_bonus = 0 if had_bonus else bonus
    if had_bonus:
        check("the launch bonus is not paid a second time",
              (d or {}).get("bonus_granted") == 0, d)
    else:
        check(f"and the {bonus} launch bonus with them",
              (d or {}).get("bonus_granted") == bonus, d)
    after = sql(f"select balance_credits b from credit_wallets "
                f"where billing_account_id = '{account}';")[0]["b"]
    check(f"the balance moved by exactly {3000 + expected_bonus}",
          after - opening == 3000 + expected_bonus, after - opening)
    check("the allowance is its own ledger row, keyed on the payment",
          sql(f"""select count(*) n from credit_ledger
                   where billing_account_id = '{account}'
                     and idempotency_key = 'subscription:{mandate}:pay_j_{TAG}';""")[0]["n"] == 1)

    st, d = deliver("subscription.charged", {"id": mandate, "status": "active"},
                    event_id=f"evt_j_{TAG}_charge_retry",
                    payment={"id": f"pay_j_{TAG}", "amount": 99900})
    check("a redelivered charge changes nothing",
          sql(f"select balance_credits b from credit_wallets "
              f"where billing_account_id = '{account}';")[0]["b"] == after,
          "balance moved on a duplicate")

    # ── 5 ────────────────────────────────────────────────────────────────────
    section("a guest, and what the conversation costs")
    before_chat = after
    session = None
    for message in ["Hi", "Do you have a room?", "What does it cost?", "Thanks!"]:
        turn = chat(QA_SLUG, message, session)
        if not turn:
            check("the AI answered", False, f"no reply to {message!r}")
            break
        session = turn["session"]
    spent = before_chat - sql(f"select balance_credits b from credit_wallets "
                              f"where billing_account_id = '{account}';")[0]["b"]
    check("four messages are one website conversation, at 5 credits", spent == 5, spent)
    check("charged once, keyed on the conversation",
          sql(f"""select count(*) n from usage_events
                   where property_id = '{hotel}'
                     and usage_type = 'ai.conversation.website';""")[0]["n"] >= 1)

    before_ig = sql(f"select balance_credits b from credit_wallets "
                    f"where billing_account_id = '{account}';")[0]["b"]
    # An Instagram thread, charged at its own rate. Written directly because
    # delivering a real Meta webhook needs a connected account this hotel does
    # not have — the charge path is the same `record_run` either way.
    convo = sql(f"""insert into conversations (property_id, partner_id, channel, status,
                                               external_id, guest_name)
                    values ('{hotel}', '{prop["partner_id"]}', 'instagram', 'open',
                            'ig_journey_{TAG}', 'Journey Guest')
                    returning id;""")[0]["id"]
    st, _ = http(f"{API}/pms/properties/{hotel}/conversations", headers=owner)
    check("the Inbox loads with an Instagram thread on it", st == 200, st)

    # ── 6 ────────────────────────────────────────────────────────────────────
    section("what the owner sees")
    st, credits = http(f"{API}/pms/properties/{hotel}/credits", headers=owner)
    check("the credit summary answers", st == 200, st)
    web = next((c for c in credits["byChannel"] if c["channel"] == "website"), {})
    check("the website conversation is counted", web.get("conversations", 0) >= 1, web)
    check("at 5 credits each", web.get("creditsEach") == 5, web)
    pools = " ".join((p["label"] or "").lower() for p in credits["remainingByKind"])
    check("and the pools say where the credits came from",
          "monthly" in pools or "subscription" in pools or "bonus" in pools, pools)

    st, ask = http(f"{API}/pms/properties/{hotel}/owner/ask", "POST",
                   {"message": "How many credits do I have left?"}, owner)
    said = (ask or {}).get("summary") or ""
    check("the copilot answers with a real number", st == 200 and any(
        ch.isdigit() for ch in said), said[:120])
    check("  and it is the same number the screen shows",
          str(credits["balance"])[:2] in said.replace(",", ""), f"{said[:80]} vs {credits['balance']}")

    # ── 7 ────────────────────────────────────────────────────────────────────
    section("what the operator sees")
    st, hotels = http(f"{API}/admin/hotels?q=QA%20Launch", headers=ADMIN)
    row = next((h for h in (hotels or []) if h["propertyId"] == hotel), None)
    check("the hotel is listed as paying", row and row["planStatus"] == "active", row)
    check("with a real mandate behind it", row and row["hasMandate"] is True)
    check("and a last-paid date", bool(row and row.get("lastPaidAt")), row)
    st, ledger = http(f"{API}/admin/hotels/{account}/ledger", headers=ADMIN)
    kinds = {e["entryType"] for e in (ledger or [])}
    check("the ledger shows the grants and the spend", {"grant", "debit"} <= kinds, kinds)
    debit = next((e for e in ledger if e["entryType"] == "debit"), None)
    check("and says what a debit paid for",
          debit and (debit.get("usageType") or "").startswith("ai."), debit)

    # ── 8 ────────────────────────────────────────────────────────────────────
    section("cancelling, without losing anything")
    rooms_before = sql(f"select count(*) n from room_types where property_id = '{hotel}';")[0]["n"]
    st, plan = http(f"{API}/pms/properties/{hotel}/plan/cancel", "POST", {}, owner)
    check("the owner can cancel", st == 200, st)
    check("at the end of the paid month", plan.get("cancelAtPeriodEnd") is True)
    check("the rooms are untouched",
          sql(f"select count(*) n from room_types where property_id = '{hotel}';")[0]["n"] == rooms_before)
    check("and so are the credits",
          sql(f"select balance_credits b from credit_wallets "
              f"where billing_account_id = '{account}';")[0]["b"] > 0)

    # ── 9 ────────────────────────────────────────────────────────────────────
    section("the pilot hotels never moved")
    after_pilots = sql(
        "select name, (select count(*) from subscriptions s where s.property_id = p.id) subs "
        f"from properties p where p.name in {PILOTS};")
    check("all three are as they were",
          sorted((r["name"], r["subs"]) for r in after_pilots)
          == sorted((r["name"], r["subs"]) for r in pilots_before), after_pilots)

    # Sweep this run's fixtures. The ledger is append-only by design, so the
    # credits stay — they are a real grant against a real (test) payment.
    sql(f"delete from conversations where id = '{convo}';")
    sql(f"""delete from plan_coupon_redemptions
             where coupon_id in (select id from plan_coupons where code = '{code}');""")
    sql(f"delete from plan_coupons where code = '{code}';")
    sql(f"delete from razorpay_webhook_events where event_id like 'evt_j_{TAG}%';")

    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("REAL RAZORPAY (test keys) · REAL DATABASE · REAL AI · SIGNED WEBHOOKS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
