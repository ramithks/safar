#!/usr/bin/env python3
"""
The §44 conversation, against the real engine and the real database.

    Guest: "₹5,000 ka room hai, 4 rooms chahiye, best price kya doge?"

    1. understand it is a group
    2. retrieve group pricing
    3. retrieve negotiation rules
    4. compute the allowed offer through the backend
    5. answer naturally
    6. never reveal the floor
    7. escalate if approval is needed
    8. resume when the hotel replies

Steps 1, 5 and 8 belong to the model and to the pipeline, and are exercised by
`sales_e2e` and `instagram_e2e`. **This suite is steps 2, 3, 4, 6 and 7** — the
deterministic half, which is the half that must not be able to go wrong.

It drives the PMS services directly rather than over HTTP, because what is under
test is the *engine*: given a hotel's own rules and a guest's own number, what
comes back and what is in it. Over HTTP that answer passes through a projection
this suite also has to check, so it checks the projection separately and the
engine at source.

⚠ Creates one hotel under `@negotiation.stayos.test`, with rooms, rates, tiers
and rules, and sweeps it at each end. Nothing outside that namespace is touched.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/negotiation_e2e.py
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

# Run from anywhere: this file imports the services it tests, and `scripts/` is
# not the package root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.pms import negotiation, policies  # noqa: E402
from app.services.pms.escalation import compose, dedupe_key

REF = "rdlozqvuywtgsodunait"
TAG = uuid.uuid4().hex[:8]
EMAIL = f"owner-{TAG}@negotiation.stayos.test"

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")
    return ok


_raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(_raw.removeprefix("go-keyring-base64:")).decode()


def sql(query):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {MGMT}", "Content-Type": "application/json",
                 "User-Agent": "SupabaseCLI/2.0.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"SQL failed: {e.code} {e.read().decode()[:400]}") from e


_OURS = ("select p.id from public.properties p "
         "join public.partners pt on pt.id = p.partner_id "
         "where pt.slug like 'negotiation-test-%'")


def sweep():
    sql(f"""
delete from public.negotiation_rules where property_id in ({_OURS});
delete from public.group_rate_tiers where property_id in ({_OURS});
delete from public.group_policies where property_id in ({_OURS});
delete from public.inventory_holds where property_id in ({_OURS});
delete from public.quotes where property_id in ({_OURS});
delete from public.rate_plan_rates where rate_plan_id in (
  select id from public.rate_plans where property_id in ({_OURS}));
delete from public.rate_plans where property_id in ({_OURS});
delete from public.room_inventory where room_type_id in (
  select id from public.room_types where property_id in ({_OURS}));
delete from public.room_types where property_id in ({_OURS});
delete from public.properties where partner_id in (
  select id from public.partners where slug like 'negotiation-test-%');
delete from public.partners where slug like 'negotiation-test-%';
delete from auth.users where email like '%@negotiation.stayos.test';
""")


def build():
    """
    A hotel priced exactly like the brief's example.

    ₹5,000 published, a 3–5 room band at 5% and a 6–9 band at 10%, a floor of
    ₹4,300 and an agent allowance of 10% with approval to 20%.
    """
    rows = sql(f"""
with u as (
  insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                          email_confirmed_at, created_at, updated_at)
  values (gen_random_uuid(), '00000000-0000-0000-0000-000000000000', 'authenticated',
          'authenticated', '{EMAIL}', '', now(), now(), now())
  returning id
), pt as (
  insert into public.partners (owner_id, slug, brand_name, whatsapp, lists_stays)
  select id, 'negotiation-test-{TAG}', 'Negotiation Test', '919000000000', true from u
  returning id
), p as (
  insert into public.properties (partner_id, slug, name, city, status, settings)
  select id, 'negotiation-test-hotel-{TAG}', 'Negotiation Test Hotel', 'Udaipur', 'active',
         '{{"max_discount_pct": 10, "max_approved_discount_pct": 20, "token_pct": 20}}'::jsonb
  from pt returning id, partner_id
), rt as (
  insert into public.room_types (property_id, partner_id, code, name, total_units,
                                 max_occupancy, standard_occupancy)
  select id, partner_id, 'STD', 'Lake View', 10, 3, 2 from p
  returning id, property_id, partner_id
), rp as (
  insert into public.rate_plans (property_id, partner_id, room_type_id, code, name,
                                 meal_plan, audience, base_price_paise)
  select property_id, partner_id, id, 'STD-CP', 'With breakfast', 'CP', 'public', 500000
  from rt returning id, property_id, room_type_id
), tiers as (
  insert into public.group_rate_tiers (property_id, partner_id, min_units, max_units,
                                       discount_pct, negotiable)
  select rt.property_id, rt.partner_id, v.lo, v.hi, v.pct, v.neg
  from rt, (values (3,5,5,false), (6,9,10,false), (10,null,null,true)) as v(lo,hi,pct,neg)
  returning id
), rules as (
  insert into public.negotiation_rules (property_id, partner_id, scope, floor_paise,
                                        target_paise, auto_pct, approval_pct)
  select property_id, partner_id, 'property', 430000, 470000, 10, 20 from rt
  returning id
)
select rp.id as plan, rp.property_id as prop, rp.room_type_id as room from rp;
""")
    row = rows[0] if isinstance(rows, list) else rows
    return row["prop"], row["plan"], row["room"]


async def main():
    print(f"\n══ negotiation: the §44 conversation, deterministically ══\n   {EMAIL}\n")
    sweep()
    property_id, plan_id, room_id = build()

    check_in = date.today() + timedelta(days=45)
    check_out = check_in + timedelta(days=1)

    async def ask(rupees, units=1, ci=check_in, co=check_out):
        return await negotiation.negotiate(
            property_id=property_id, rate_plan_id=plan_id,
            check_in=ci, check_out=co, units=units,
            requested_nightly_paise=(rupees * 100) if rupees is not None else None,
        )

    # ── 1. the group is recognised, and priced ───────────────────────────────
    print("── 1. four rooms is a different price from one ──")
    one = await ask(None, units=1)
    four = await ask(None, units=4)
    six = await ask(None, units=6)
    check("one room is published at ₹5,000",
          one.published_nightly_paise == 500_000, str(one.published_nightly_paise))
    check("four rooms fall in the 3–5 band and are published at ₹4,750",
          four.published_nightly_paise == 475_000, str(four.published_nightly_paise))
    check("six rooms fall in the 6–9 band and are published at ₹4,500",
          six.published_nightly_paise == 450_000, str(six.published_nightly_paise))
    check("the group price is per room, so four rooms total ₹19,000",
          four.published_total_paise == 1_900_000, str(four.published_total_paise))

    # ── 2. the negotiation runs on top of the group price ────────────────────
    print("\n── 2. and the negotiation starts from there, not the rack rate ──")
    best_four = await ask(None, units=4)
    check("'best price' on four rooms answers with a counter",
          best_four.verdict == "counter", best_four.verdict)
    # 10% off the 3–5 band's ₹4,750 is ₹4,275 — *below* the ₹4,300 floor — so the
    # floor binds and the counter has to come back above it.
    #
    # **It comes back at ₹4,322.50, not at ₹4,300, and that is correct.** The
    # first version of this assertion expected the floor exactly and was wrong
    # about how the engine works: `price_stay` takes an integer percentage, and
    # ₹17,200 (the floor across four rooms) is 9.47% off ₹19,000. Nine per cent
    # gives ₹17,290 and ten gives ₹17,100 — one above the floor, one under it —
    # so `_pct_for` steps back to nine. The guest is quoted a price the pricing
    # engine can actually produce, in the direction that protects the hotel.
    # Asserting equality with the floor would have been asserting a number no
    # quote could ever charge.
    check("the counter clears the ₹4,300 floor",
          best_four.counter_nightly_paise >= 430_000,
          str(best_four.counter_nightly_paise))
    check("and lands within one percentage point of it, not a whole band above",
          best_four.counter_nightly_paise <= 430_000 * 1.02,
          str(best_four.counter_nightly_paise))
    check("the hotel's own group discount was NOT applied twice",
          best_four.counter_nightly_paise > 400_000, str(best_four.counter_nightly_paise))

    # The number the quote will genuinely compute — which is the whole reason
    # the engine returns a percentage alongside the price.
    gross = best_four.published_total_paise
    pct = best_four.counter_discount_pct
    check("the counter is exactly what an integer discount produces",
          best_four.counter_total_paise == gross - round(gross * pct / 100),
          f"{best_four.counter_total_paise} vs {gross} @ {pct}%")

    # ── 3. the four verdicts ─────────────────────────────────────────────────
    print("\n── 3. the four answers ──")
    at_4800 = await ask(4800)
    check("₹4,800 on one room is agreed on the spot", at_4800.verdict == "allowed",
          f"{at_4800.verdict} {at_4800.reason}")
    at_4500 = await ask(4500)
    check("₹4,500 — exactly the agent's 10% — is agreed too",
          at_4500.verdict == "allowed", f"{at_4500.verdict} {at_4500.reason}")
    at_4400 = await ask(4400)
    check("₹4,400 needs a human, because it is under the agent's limit and above the floor",
          at_4400.verdict == "needs_approval", f"{at_4400.verdict} {at_4400.reason}")
    at_4000 = await ask(4000)
    check("₹4,000 is countered rather than refused", at_4000.verdict == "counter",
          f"{at_4000.verdict} {at_4000.reason}")
    check("and the counter is what the agent may give alone — ₹4,500",
          at_4000.counter_nightly_paise == 450_000, str(at_4000.counter_nightly_paise))
    check("with an offer to ask the hotel, because approval headroom exists",
          at_4000.approval_available is True, str(at_4000.approval_available))

    # ── 4. the floor holds, over every asking price ──────────────────────────
    print("\n── 4. nothing prices below the floor, at any asking price ──")
    breaches = []
    for rupees in range(0, 6000, 137):
        d = await ask(rupees)
        for price in (d.final_nightly_paise, d.counter_nightly_paise):
            if price is not None and price < 430_000:
                breaches.append((rupees, price))
    check(f"{len(range(0, 6000, 137))} asking prices, none under ₹4,300",
          not breaches, str(breaches[:3]))

    # ── 5. what the guest may never be told ──────────────────────────────────
    print("\n── 5. the floor never leaves the server ──")
    projections = [(await ask(r)).as_dict() for r in (4000, 4400, 4800, 1)]
    leaky_keys = sorted({
        k for p in projections for k in p
        if any(w in k.lower() for w in ("floor", "minimum", "target", "ceiling", "auto"))
    })
    check("no field in any projection is named after an internal number",
          not leaky_keys, str(leaky_keys))
    check("the internal target of ₹4,700 appears in no projection",
          not any(470_000 in [v for v in p.values() if isinstance(v, int)] for p in projections),
          "₹4,700 leaked")

    # ── 6. the guardrails ────────────────────────────────────────────────────
    print("\n── 6. §10, one refusal at a time ──")
    sql(f"""update public.negotiation_rules
            set blackout_dates = array['{check_in.isoformat()}']::date[]
            where property_id = '{property_id}';""")
    blacked = await ask(4000)
    check("a blackout night refuses rather than counters",
          blacked.verdict == "rejected" and blacked.reason == "blackout_dates",
          f"{blacked.verdict} {blacked.reason}")

    sql(f"""update public.negotiation_rules
            set blackout_dates = '{{}}'::date[], min_units = 3
            where property_id = '{property_id}';""")
    too_few = await ask(4000, units=1)
    check("one room refuses when the hotel negotiates only on three or more",
          too_few.reason == "below_minimum_rooms", too_few.reason)
    enough = await ask(4000, units=4)
    check("and four rooms still negotiates", enough.verdict in ("counter", "needs_approval", "allowed"),
          enough.verdict)

    sql(f"""update public.negotiation_rules set min_units = 1, enabled = false
            where property_id = '{property_id}';""")
    off = await ask(4000)
    check("negotiation switched off refuses with its own reason",
          off.verdict == "rejected" and off.reason == "negotiation_closed",
          f"{off.verdict} {off.reason}")

    sql(f"""update public.negotiation_rules set enabled = true
            where property_id = '{property_id}';""")
    sold_out = await ask(4000, units=50)
    check("more rooms than exist refuses on availability, not on price",
          sold_out.reason == "not_enough_rooms", sold_out.reason)

    # ── 7. what the hotel's own team is told ─────────────────────────────────
    print("\n── 7. the escalation a manager reads ──")
    facts = {
        "guest_name": "Sahil", "channel": "instagram",
        "check_in": check_in.isoformat(), "check_out": check_out.isoformat(),
        "units": 10, "guests": 20, "room_name": "Lake View",
        "published_paise": 450_000, "requested_paise": 400_000, "floor_paise": 430_000,
    }
    message = compose("group_inquiry", hotel="Negotiation Test Hotel", facts=facts,
                      reason="Twenty of us for a wedding.")
    check("it names the guest, the dates and the rooms",
          all(s in message for s in ("Sahil", check_in.isoformat(), "Rooms: 10")),
          message[:120])
    check("it tells the manager what was asked and what is allowed",
          "Guest asked: ₹4,000" in message and "Lowest allowed: ₹4,300" in message,
          message)
    check("and it ends with a decision to make",
          message.rstrip().endswith("for this group."), message.rstrip()[-60:])
    check("the same request twice is the same escalation",
          dedupe_key("group_inquiry", facts) == dedupe_key("group_inquiry", dict(facts)))

    # ── 8. the money, end to end ─────────────────────────────────────────────
    print("\n── 8. what the hotel is actually paid ──")
    from app.services.pms import fees
    agreed = await ask(4500, units=4)
    check("a four-room stay at ₹4,500 agrees", agreed.verdict == "allowed", agreed.verdict)
    total = agreed.final_total_paise or 0
    split = fees.breakdown(total, 400)
    check("StayOS keeps 4%", split.fee_paise == round(total * 0.04), str(split.fee_paise))
    check("and the two halves sum back to the booking",
          split.fee_paise + split.payout_paise == total, str(split))

    deposit = policies.deposit_for(total, token_pct=20)
    check("the deposit is 20% and the balance is the rest",
          deposit["depositPaise"] + deposit["balancePaise"] == total, str(deposit))

    print(f"\n══ {passed} passed, {failed} failed ══\n")
    sweep()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        sweep()
        raise
