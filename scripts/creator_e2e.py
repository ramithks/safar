#!/usr/bin/env python3
"""
The creator programme, end to end, against a real database.

    creator link ──► click ──► touch ──┐
                                       │  …weeks…
    WhatsApp message ──► conversation ─┤
                                       │
    booking (website / desk / agent) ──┴──► resolve ──► payout ──► approval

**What is real here.** The property, the rate plans, RLS, `stay_options`,
`create_quote`, the inventory hold, `create_reservation`, the folio, the JWTs,
the whole of migration 23, and the WhatsApp pipeline's inbound path. The one
place M5 had to draw a line — a card nobody can type into a script — is drawn
in the same place and for the same reason: the website booking below goes
through the real `/public/…/order` and `/public/…/confirm` routes with a
signature computed using the same secret the server verifies with, which
exercises the check rather than skipping it.

**What is deliberately not here.** No creator has been paid. `approved` is the
terminal state this milestone reaches and no payment provider is integrated, so
nothing in this suite asserts that money moved — see the closing banner.

The assertions that matter are the ones a spreadsheet cannot make:

  * **the WhatsApp detour** — a click on the website, a booking through a
    thread that has no memory of it, and the creator still attributed. This is
    PRD §16's common path and the one naive last-click attribution loses.
  * **money cannot be duplicated** — one reservation, one payout, under
    concurrent resolution; one approval, under concurrent approval.
  * **evidence cannot be rewritten** — the chain refuses to be edited or
    deleted, which is what makes the payout defensible three months later.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/creator_e2e.py

⚠ Creates a creator, a campaign and real reservations against Casa Mandala.
Everything is swept on the way out by campaign, which is strictly more precise
than a ref prefix: every row this suite writes hangs off one campaign id and
nothing else on the property does.
"""
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# The one way a suite may kill a reservation. See `cancel_reservation_sql`:
# a bare UPDATE to 'cancelled' bypasses the inventory RPCs and takes the room
# out of the hotel permanently.
import e2e_sweep  # noqa: E402

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"

PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_PROP = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SLUG = "casa-mandala-anjuna"

DLX = "10000000-0000-4000-8000-000000000002"
PUBLIC_PLAN = "20000000-0000-4000-8000-000000000003"   # DLX-CP

# This suite's own creator handle. Distinct from the seeded @mayaroams so a
# sweep can never take the seed's rows with it.
HANDLE = "@e2ecreator"
PLATFORM = "instagram"

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
    req = urllib.request.Request(
        url, method=method,
        data=raw_body if raw_body is not None
             else (json.dumps(data).encode() if data is not None else None),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
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


# ── the signature Razorpay would send ────────────────────────────────────────
#
# Computed with the same secret the server verifies with. This does not weaken
# the check — the suite asserts a forged one is refused two lines later.

def payment_signature(order_id: str, payment_id: str) -> str:
    from app.config import settings
    return hmac.new(
        (settings.razorpay_key_secret or "").encode(),
        f"{order_id}|{payment_id}".encode(), hashlib.sha256,
    ).hexdigest()


def sweep():
    """
    Give the property back.

    By campaign, not by a ref prefix. Every row this suite writes hangs off one
    campaign — the touches, the participations, the payouts and their trails all
    cascade from it — and the reservations are found through the payouts, which
    is the only link a booking made through the public engine carries.
    """
    sql(f"""
delete from public.reservations r
 using public.creator_payouts p
 where p.reservation_id = r.id
   and p.campaign_id in (select id from public.creator_campaigns
                          where name like 'E2E creator%');
delete from public.reservations
 where creator_code like 'E2ECREATOR-%';
delete from public.conversations where external_id like 'creator-e2e-%';
delete from public.creator_campaigns where name like 'E2E creator%';
delete from public.creators where handle = '{HANDLE}';""")


def one(rows, key, default=None):
    return rows[0][key] if rows else default


def main() -> int:  # noqa: C901 — a linear script; splitting it hides the order
    print("\n══ creator programme e2e ══\n")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    owner = signin("sahil@casamandala.in")
    manager = signin("priya@casamandala.in")
    reception = signin("deepak@casamandala.in")
    outsider = signin("nobody@example.com")
    if not (owner and manager and reception and outsider):
        print("  could not sign in — has bootstrap/01_seed_identities.sql been applied?")
        return 1

    sweep()
    print(f"── creator ────────────────────────────────────────────────")
    print(f"   property         Casa Mandala  {PROP[:8]}…")
    print(f"   owns             the campaign named 'E2E creator…' and everything under it")
    print()

    today = date.today()

    # ── 1. a hotel puts a creator on a campaign ──────────────────────────────
    print("── 1. the campaign ──")

    st, created = http(f"{API}/properties/{PROP}/creator-campaigns", "POST", {
        "creatorName": "E2E Creator", "creatorHandle": HANDLE,
        "platform": PLATFORM, "followers": 42000, "city": "Goa",
        "campaignName": "E2E creator programme",
        "startDate": str(today - timedelta(days=5)),
        "endDate": str(today + timedelta(days=90)),
        "commissionPct": 10, "guestDiscountPct": 5,
        "attributionWindowDays": 30, "minNights": 2,
    }, manager)
    check("a manager can put a creator on a campaign", st == 201, f"{st} {str(created)[:250]}")
    if st != 201:
        print("\n  cannot continue without a campaign")
        return 1

    part_id = created["id"]
    campaign_id = created["campaignId"]
    code = created["code"]

    check("it starts as a draft, so nothing is being credited yet",
          created["status"] == "draft", created["status"])
    check("with a code derived from the handle and a random tail",
          code.startswith("E2ECREATOR-") and len(code) > 11, code)
    check("and no internal id anywhere in what a creator would share",
          campaign_id not in code and created["creatorId"] not in code, code)
    check("the terms are the hotel's, read back",
          created["commissionPct"] == 10 and created["minNights"] == 2
          and created["attributionWindowDays"] == 30, str(created)[:200])
    check("and it has produced nothing yet",
          created["clicks"] == 0 and created["bookings"] == 0
          and created["payoutPaise"] == 0, str(created)[:200])

    st, again = http(f"{API}/properties/{PROP}/creator-campaigns", "POST", {
        "creatorName": "E2E Creator", "creatorHandle": HANDLE, "platform": PLATFORM,
        "campaignId": campaign_id,
    }, manager)
    check("the same creator cannot be added to the same campaign twice",
          st == 409, f"{st} {str(again)[:160]}")

    st, _ = http(f"{API}/properties/{PROP}/creator-campaigns", "POST", {
        "creatorName": "E2E Creator", "creatorHandle": HANDLE, "platform": PLATFORM,
        "campaignName": "E2E creator second", "startDate": str(today),
        "endDate": str(today - timedelta(days=1)),
    }, manager)
    check("a campaign that ends before it starts is refused", st == 400, str(st))

    st, _ = http(f"{API}/properties/{PROP}/creator-campaigns", "POST", {
        "creatorName": "Nope", "creatorHandle": "@nope", "campaignName": "E2E creator no",
        "startDate": str(today), "endDate": str(today + timedelta(days=5)),
    }, reception)
    check("a receptionist cannot start a creator campaign", st == 403, str(st))

    # ── 2. a click on a draft campaign is worth nothing ──────────────────────
    print("\n── 2. the link, before it is live ──")

    st, cold = http(f"{API}/public/hotels/{SLUG}/attribution", "POST", {"code": code})
    check("the public page still answers", st == 200, f"{st} {str(cold)[:160]}")
    check("but a draft campaign attributes nobody", cold["attributed"] is False, str(cold))
    check("and records no touch at all",
          int(one(sql(f"""select count(*) as n from public.attribution_touches
                           where campaign_id = '{campaign_id}';"""), "n")) == 0)

    st, live = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/status",
                    "POST", {"status": "live"}, manager)
    check("the hotel takes the campaign live", st == 200 and live["status"] == "live",
          f"{st} {str(live)[:160]}")

    st, bad = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/status",
                   "POST", {"status": "draft"}, manager)
    check("and cannot un-launch it — a campaign does not go back to draft",
          st == 422, f"{st} {str(bad)[:160]}")

    # ── 3. the click ─────────────────────────────────────────────────────────
    print("\n── 3. the click ──")

    st, touch = http(f"{API}/public/hotels/{SLUG}/attribution", "POST", {"code": code})
    check("a live campaign attributes the click", st == 200 and touch["attributed"],
          f"{st} {str(touch)[:200]}")
    visitor = touch["visitorRef"]
    touch_ref = touch["touchRef"]
    check("the guest is told whose link they followed",
          touch["creatorHandle"] == HANDLE and touch["campaignName"] == "E2E creator programme",
          str(touch)[:200])
    check("and what it is worth to them", touch["guestDiscountPct"] == 5,
          str(touch.get("guestDiscountPct")))
    check("the browser is given a ref to carry", bool(visitor), str(visitor))
    check("and a token to hand back on another channel",
          bool(touch_ref) and touch_ref.startswith("CR-"), str(touch_ref))

    st, dup = http(f"{API}/public/hotels/{SLUG}/attribution", "POST",
                   {"code": code, "visitorRef": visitor})
    check("the same visitor clicking again is the same visitor",
          st == 200 and dup["visitorRef"] == visitor, f"{st} {str(dup)[:160]}")
    clicks = int(one(sql(f"""select count(*) as n from public.attribution_touches
                             where campaign_id = '{campaign_id}' and kind = 'click';"""), "n"))
    check("and one touch, not two — a duplicate click is not duplicate money",
          clicks == 1, f"{clicks} clicks")

    st, wrong = http(f"{API}/public/hotels/{SLUG}/attribution", "POST",
                     {"code": "NOBODY-XXXX"})
    check("a code nobody issued attributes nobody",
          st == 200 and wrong["attributed"] is False, f"{st} {str(wrong)[:160]}")

    st, _ = http(f"{API}/public/hotels/not-a-hotel/attribution", "POST", {"code": code})
    check("and a slug nobody published is a 404 before any campaign is read",
          st == 404, str(st))

    # ── 4. the detour ────────────────────────────────────────────────────────
    #
    # The guest leaves the website and messages the hotel. Nothing about that
    # message names a creator; what it carries is the token this server minted.
    print("\n── 4. the WhatsApp detour ──")

    conversation_id = str(uuid.uuid4())
    guest_phone = "+919810" + str(int(datetime.now().timestamp()))[-6:]
    sql(f"""insert into public.conversations
             (id, property_id, partner_id, channel, external_id, guest_phone,
              guest_name, handler)
           values ('{conversation_id}', '{PROP}', '{PARTNER}', 'whatsapp',
                   'creator-e2e-{conversation_id[:8]}', '{guest_phone}',
                   'Creator Guest', 'ai');""")

    from app.services.pms import creators as creators_service
    import asyncio

    bound = asyncio.run(creators_service.bind_conversation(
        PROP, conversation_id,
        f"Hi! Saw the reel, is a room free in December? (ref: {touch_ref})",
    ))
    check("the hotel's own token is read back out of the guest's message",
          bound == touch_ref, f"{bound} vs {touch_ref}")

    chain = sql(f"""select kind, conversation_id from public.attribution_touches
                     where campaign_id = '{campaign_id}' order by occurred_at;""")
    check("the click is now bound to the thread",
          any(r["kind"] == "click" and r["conversation_id"] == conversation_id for r in chain),
          str(chain)[:250])
    check("and the chain records the channel change as its own link",
          any(r["kind"] == "conversation" for r in chain), str(chain)[:250])

    again = asyncio.run(creators_service.bind_conversation(
        PROP, conversation_id, f"still thinking — {touch_ref}"))
    depth = int(one(sql(f"""select count(*) as n from public.attribution_touches
                            where campaign_id = '{campaign_id}';"""), "n"))
    check("re-sending the token binds nothing twice", depth == 2, f"{depth} touches")

    noise = asyncio.run(creators_service.bind_conversation(
        PROP, conversation_id, "no token in this one at all"))
    check("and an ordinary message costs one regex and changes nothing",
          noise is None, str(noise))

    # ── 5. the booking, weeks later, on a channel with no memory ─────────────
    print("\n── 5. the booking ──")

    check_in = today + timedelta(days=210)
    check_out = check_in + timedelta(days=3)

    st, quote = http(f"{API}/properties/{PROP}/quotes", "POST", {
        "roomTypeId": DLX, "ratePlanId": PUBLIC_PLAN, "checkIn": str(check_in), "checkOut": str(check_out),
        "units": 1, "adults": 2, "children": 0,
    }, reception)
    check("the desk prices the stay through the same engine as everyone else",
          st == 200, f"{st} {str(quote)[:200]}")

    st, booking = http(f"{API}/properties/{PROP}/reservations", "POST", {
        "quoteId": quote["id"], "guestName": "Creator Guest",
        "guestPhone": guest_phone, "source": "whatsapp",
        "tokenPaise": 0, "paymentMethod": None,
    }, reception)
    check("and books it", st == 200, f"{st} {str(booking)[:250]}")
    reservation_id = booking["id"]
    ref = booking["ref"]

    sql(f"""update public.conversations set reservation_id = '{reservation_id}'
             where id = '{conversation_id}';""")
    asyncio.run(creators_service.resolve_for_reservation({"id": reservation_id}))

    payout = sql(f"""select * from public.creator_payouts
                      where reservation_id = '{reservation_id}';""")
    check("a booking on a channel that never saw the link is still the creator's",
          len(payout) == 1, str(payout)[:250])
    if not payout:
        sweep()
        print("\n  attribution did not resolve — the rest depends on it")
        print(f"\n══ {PASS} passed, {FAIL} failed ══")
        return 1
    payout_id = payout[0]["id"]

    stamped = sql(f"""select creator_code from public.reservations
                       where id = '{reservation_id}';""")
    check("the reservation carries the code as a convenience",
          one(stamped, "creator_code") == code, str(stamped))

    receipt = sql(f"""select count(*) as n from public.attribution_touches
                       where reservation_id = '{reservation_id}' and kind = 'booking';""")
    check("and the chain gains a receipt saying it converted",
          int(one(receipt, "n")) == 1, str(receipt))

    check("the terms are snapshotted onto the payout, not read from the campaign later",
          float(payout[0]["commission_pct"]) == 10 and int(payout[0]["terms_version"]) == 1,
          str(payout[0])[:200])
    check("nothing is payable while the guest has not stayed",
          payout[0]["status"] == "accruing" and payout[0]["eligible"] is False,
          str(payout[0])[:200])
    check("and the reason is written down, because a creator will ask",
          bool(payout[0]["ineligible_reason"]), str(payout[0].get("ineligible_reason")))

    # ── 6. resolving twice, at once ──────────────────────────────────────────
    print("\n── 6. one booking, one payout ──")

    def resolve_once(_):
        return asyncio.run(creators_service.resolve(reservation_id))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(resolve_once, range(4)))

    n = int(one(sql(f"""select count(*) as n from public.creator_payouts
                         where reservation_id = '{reservation_id}';"""), "n"))
    check("four simultaneous resolutions produce one payout", n == 1, f"{n} payouts")

    st, forced = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                      {"query": f"""insert into public.creator_payouts
        (property_id, partner_id, campaign_id, creator_id, campaign_creator_id,
         reservation_id, attribution_touch_id, commission_pct, terms_version)
        select property_id, partner_id, campaign_id, creator_id, campaign_creator_id,
               reservation_id, attribution_touch_id, commission_pct, terms_version
          from public.creator_payouts where id = '{payout_id}';"""},
                      {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    check("and not even a direct insert can make a second — the constraint decides",
          st >= 400, f"{st} {str(forced)[:160]}")

    # ── 7. evidence that stays evidence ──────────────────────────────────────
    print("\n── 7. the chain cannot be rewritten ──")

    st, _ = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                 {"query": f"""delete from public.attribution_touches
                                where campaign_id = '{campaign_id}';"""},
                 {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    check("a touch cannot be deleted", st >= 400, str(st))

    st, _ = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                 {"query": f"""update public.attribution_touches
                                  set occurred_at = now() - interval '400 days'
                                where campaign_id = '{campaign_id}';"""},
                 {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    check("nor backdated to survive a window it missed", st >= 400, str(st))

    # ── 8. what it is worth, and when ────────────────────────────────────────
    print("\n── 8. eligibility ──")

    def recalc():
        asyncio.run(creators_service.recalculate_for_reservation(reservation_id))
        return sql(f"select * from public.creator_payouts where id = '{payout_id}';")[0]

    sql(f"""update public.reservations set status = 'checked_out'
             where id = '{reservation_id}';""")
    row = recalc()
    check("a stay that is still unpaid is not payable",
          row["eligible"] is False and "unpaid" in (row["ineligible_reason"] or ""),
          str(row)[:220])

    total = int(booking["totalPaise"])
    sql(f"""update public.reservations set paid_paise = {total}
             where id = '{reservation_id}';""")
    row = recalc()
    check("a finished, fully paid stay is", row["status"] == "pending_approval"
          and row["eligible"] is True, str(row)[:220])

    gross = int(booking["grossPaise"])
    discount = int(booking.get("discountPaise") or 0)
    expected = round((gross - discount) * 10 / 100)
    check("the commission is 10% of room revenue before tax",
          int(row["net_revenue_paise"]) == gross - discount
          and int(row["amount_paise"]) == expected,
          f"net {row['net_revenue_paise']} amount {row['amount_paise']} expected {expected}")
    check("and the room nights are the stay's, not a guess",
          int(row["room_nights"]) == 3, str(row["room_nights"]))

    # Stock back first. A bare UPDATE to 'cancelled' bypasses the inventory
    # RPCs and leaves the night sold to nobody — see e2e_sweep.
    sql(e2e_sweep.cancel_reservation_sql(reservation_id, "creator_e2e"))
    row = recalc()
    check("a cancellation takes it back to nothing",
          int(row["amount_paise"]) == 0 and row["eligible"] is False, str(row)[:200])

    # …and the room goes back out when the stay is reinstated, or the counter
    # ends this test one room short for ever.
    sql(e2e_sweep.reinstate_reservation_sql(reservation_id, "checked_out"))
    row = recalc()
    check("and reinstating the stay reinstates it", row["status"] == "pending_approval",
          str(row)[:200])

    # ── 9. the evidence chain, as an owner reads it ──────────────────────────
    print("\n── 9. why this payout exists ──")

    st, touches = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/touches",
                       headers=manager)
    check("the chain is readable", st == 200 and len(touches) == 3,
          f"{st} {len(touches) if st == 200 else str(touches)[:160]}")
    kinds = [t["kind"] for t in touches] if st == 200 else []
    check("and reads click → conversation → booking",
          set(kinds) == {"click", "conversation", "booking"}, str(kinds))
    booking_touch = next((t for t in touches if t["kind"] == "booking"), {}) if st == 200 else {}
    check("with the booking reference on it, not a uuid",
          booking_touch.get("reservationRef") == ref, str(booking_touch)[:200])
    check("and what the stay was worth",
          booking_touch.get("valuePaise") == total and booking_touch.get("nights") == 3,
          str(booking_touch)[:200])

    st, payouts = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/payouts",
                       headers=manager)
    check("the payout reads as a stay, not a row", st == 200 and len(payouts) == 1,
          f"{st} {str(payouts)[:200]}")
    if st == 200 and payouts:
        p = payouts[0]
        check("naming the guest, the dates and the reference",
              p["reservationRef"] == ref and p["guestName"] == "Creator Guest",
              str(p)[:220])
        check("the commission it was computed under", p["commissionPct"] == 10,
              str(p.get("commissionPct")))
        check("and what is owed", p["amountPaise"] == expected, str(p.get("amountPaise")))

    st, cards = http(f"{API}/properties/{PROP}/creator-campaigns", headers=manager)
    card = next((c for c in cards if c["id"] == part_id), {}) if st == 200 else {}
    check("the card rolls the chain up", st == 200 and card.get("clicks") == 1
          and card.get("bookings") == 1 and card.get("roomNights") == 3,
          str(card)[:250])
    check("and shows what is waiting for somebody to press approve",
          card.get("payoutPaise") == expected
          and card.get("payoutStatus") == "pending_approval", str(card)[:250])

    # ── 10. approval ─────────────────────────────────────────────────────────
    print("\n── 10. approval ──")

    st, refused = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                       "POST", {}, manager)
    check("a manager runs the programme and cannot sign off its cost",
          st == 403, f"{st} {str(refused)[:160]}")

    st, refused = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                       "POST", {}, reception)
    check("nor can a receptionist", st == 403, str(st))

    st, refused = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                       "POST", {}, outsider)
    check("and a stranger gets a 404, not a 403", st == 404, str(st))

    st, approved = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                        "POST", {"note": "August cycle"}, owner)
    check("an owner approves it", st == 200 and approved["approved"] == 1,
          f"{st} {str(approved)[:200]}")
    check("for the amount the server computed, not one anybody sent",
          approved.get("amountPaise") == expected, str(approved))

    st, twice = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                     "POST", {}, owner)
    check("pressing approve again approves nothing", st == 200 and twice["approved"] == 0,
          f"{st} {str(twice)[:160]}")

    trail = sql(f"""select * from public.creator_payout_events
                     where payout_id = '{payout_id}' order by at;""")
    check("the trail has exactly one entry", len(trail) == 1, str(trail)[:250])
    if trail:
        check("naming who, from what, to what and for how much",
              trail[0]["from_status"] == "pending_approval"
              and trail[0]["to_status"] == "approved"
              and int(trail[0]["amount_paise"]) == expected
              and bool(trail[0]["actor_name"]), str(trail[0])[:250])
        check("and carrying the note the owner left", trail[0]["note"] == "August cycle",
              str(trail[0].get("note")))

    # ── 11. a snapshot is a promise ──────────────────────────────────────────
    print("\n── 11. history does not move ──")

    sql(f"""update public.creator_campaigns set commission_pct = 30
             where id = '{campaign_id}';""")
    row = recalc()
    check("renegotiating in September does not restate August",
          int(row["amount_paise"]) == expected and float(row["commission_pct"]) == 10,
          str(row)[:200])

    version = sql(f"""select terms_version from public.creator_campaigns
                       where id = '{campaign_id}';""")
    check("though the campaign's own terms version moves, so the change is visible",
          int(one(version, "terms_version")) == 2, str(version))

    sql(e2e_sweep.cancel_reservation_sql(reservation_id, "creator_e2e"))
    row = recalc()
    check("and nothing recomputes a payout an owner has already signed off",
          row["status"] == "approved" and int(row["amount_paise"]) == expected,
          str(row)[:200])
    sql(e2e_sweep.reinstate_reservation_sql(reservation_id, "checked_out"))
    sql(f"""update public.creator_campaigns set commission_pct = 10
             where id = '{campaign_id}';""")

    # ── 12. two owners, one button ───────────────────────────────────────────
    print("\n── 12. approving under concurrency ──")

    second_res = _second_booking(reception, manager, code, part_id, campaign_id,
                                 today, PUBLIC_PLAN)
    if second_res:
        def approve_once(_):
            return http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                        "POST", {}, owner)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(approve_once, range(4)))

        wins = sum(1 for st, body in results
                   if st == 200 and isinstance(body, dict) and body.get("approved") == 1)
        total_approved = sum(body.get("approved", 0) for st, body in results
                             if st == 200 and isinstance(body, dict))
        check("four simultaneous approvals approve the payout exactly once",
              total_approved == 1 and wins == 1, f"{[b for _, b in results]}")

        events = int(one(sql(f"""select count(*) as n from public.creator_payout_events e
                                  join public.creator_payouts p on p.id = e.payout_id
                                 where p.reservation_id = '{second_res}';"""), "n"))
        check("and the trail gains exactly one entry", events == 1, f"{events} entries")

    # ── 13. the real website path, end to end ────────────────────────────────
    #
    # Everything above books through the desk. This is the path a creator's
    # audience actually takes: the link, the public availability, a Razorpay
    # order, and the confirmation. The visitor ref rides on the order rather
    # than on the booking request, because the webhook has no browser to ask.
    print("\n── 13. the website, the way a guest uses it ──")

    st, web_touch = http(f"{API}/public/hotels/{SLUG}/attribution", "POST", {"code": code})
    web_visitor = web_touch.get("visitorRef") if st == 200 else None
    check("a guest follows the link", st == 200 and web_touch.get("attributed"),
          f"{st} {str(web_touch)[:160]}")

    web_in = today + timedelta(days=270)
    st, avail = http(f"{API}/public/hotels/{SLUG}/availability"
                     f"?from={web_in}&to={web_in + timedelta(days=3)}&units=1&adults=2")
    plan_id = None
    if st == 200 and avail.get("rooms"):
        for room in avail["rooms"]:
            if room["ratePlans"]:
                plan_id = room["ratePlans"][0]["ratePlanId"]
                break
    check("the public page offers a rate", bool(plan_id), f"{st} {str(avail)[:200]}")

    web_ref = None
    if plan_id:
        st, web_quote = http(f"{API}/public/hotels/{SLUG}/quote", "POST", {
            "ratePlanId": plan_id, "checkIn": str(web_in),
            "checkOut": str(web_in + timedelta(days=3)),
            "units": 1, "adults": 2, "children": 0,
        })
        check("and prices it through the same engine", st == 200,
              f"{st} {str(web_quote)[:200]}")

        if st == 200:
            st, order = http(
                f"{API}/public/hotels/{SLUG}/quotes/{web_quote['quoteId']}/order",
                "POST", {
                    "guest": {"fullName": "Website Guest", "email": "web@example.com",
                              "phone": "+919812340000"},
                    "addons": [], "source": "creator",
                    # The whole point of this section.
                    "visitorRef": web_visitor,
                })
            check("a real Razorpay order is created in test mode",
                  st == 200 and str(order.get("razorpayOrderId", "")).startswith("order_"),
                  f"{st} {str(order)[:200]}")

            if st == 200:
                rzp_order = order["razorpayOrderId"]
                payment_id = "pay_creator_e2e_" + uuid.uuid4().hex[:12]

                st, _ = http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
                    "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
                    "razorpaySignature": "deadbeef",
                })
                check("a forged payment signature is refused", st == 400, str(st))

                st, booked = http(f"{API}/public/hotels/{SLUG}/confirm", "POST", {
                    "razorpayOrderId": rzp_order, "razorpayPaymentId": payment_id,
                    "razorpaySignature": payment_signature(rzp_order, payment_id),
                })
                check("a valid one books the stay", st == 200, f"{st} {str(booked)[:220]}")
                web_ref = booked.get("ref") if st == 200 else None

    if web_ref:
        attributed = sql(f"""select p.status, p.campaign_id, r.creator_code
                               from public.creator_payouts p
                               join public.reservations r on r.id = p.reservation_id
                              where r.ref = '{web_ref}';""")
        check("and the website booking is the creator's, with no creator in the request",
              len(attributed) == 1 and attributed[0]["campaign_id"] == campaign_id,
              str(attributed)[:220])
        check("the code lands on the reservation as the derived convenience",
              one(attributed, "creator_code") == code, str(attributed)[:200])

    # ── 14. Instagram ────────────────────────────────────────────────────────
    #
    # The same chain on the third channel. A creator posts on Instagram, the
    # guest opens the link, then DMs the hotel — so the touch is a website
    # click and the conversation is Instagram, which is exactly how it happens.
    print("\n── 14. the same chain on Instagram ──")

    st, ig_touch = http(f"{API}/public/hotels/{SLUG}/attribution", "POST", {"code": code})
    ig_ref_token = ig_touch.get("touchRef") if st == 200 else None
    ig_conversation = str(uuid.uuid4())
    sql(f"""insert into public.conversations
             (id, property_id, partner_id, channel, external_id, guest_handle,
              guest_name, handler)
           values ('{ig_conversation}', '{PROP}', '{PARTNER}', 'instagram',
                   'creator-e2e-ig-{ig_conversation[:8]}', '@somebody',
                   'Instagram Guest', 'ai');""")

    bound_ig = asyncio.run(creators_service.bind_conversation(
        PROP, ig_conversation, f"loved the reel — {ig_ref_token}"))
    check("an Instagram thread binds to the chain the same way",
          bound_ig == ig_ref_token, f"{bound_ig} vs {ig_ref_token}")

    ig_in = today + timedelta(days=290)
    st, ig_quote = http(f"{API}/properties/{PROP}/quotes", "POST", {
        "roomTypeId": DLX, "ratePlanId": PUBLIC_PLAN, "checkIn": str(ig_in),
        "checkOut": str(ig_in + timedelta(days=2)), "units": 1,
        "adults": 2, "children": 0,
    }, reception)
    ig_res = None
    if st == 200:
        st, ig_booking = http(f"{API}/properties/{PROP}/reservations", "POST", {
            "quoteId": ig_quote["id"], "guestName": "Instagram Guest",
            "source": "instagram", "tokenPaise": 0, "paymentMethod": None,
        }, reception)
        if st == 200:
            ig_res = ig_booking["id"]
            asyncio.run(creators_service.resolve(
                ig_res, conversation_id=ig_conversation))

    ig_payout = sql(f"""select p.*, t.source from public.creator_payouts p
                          join public.attribution_touches t
                            on t.reservation_id = p.reservation_id and t.kind = 'booking'
                         where p.reservation_id = '{ig_res}';""") if ig_res else []
    check("and an Instagram booking attributes to the creator",
          len(ig_payout) == 1, str(ig_payout)[:220])
    check("with the channel recorded on the receipt, not guessed later",
          one(ig_payout, "source") == "instagram", str(ig_payout)[:200])

    # ── 15. a creator cannot approve their own payout ────────────────────────
    #
    # Checked in the database rather than only in the API, because the creator's
    # profile is on the `creators` row and comparing it to the actor is one line
    # no caller can forget. Proved here by making the hotel's own owner the
    # creator, which is the shape of the abuse: the person who signs the cheque
    # is also the person it is written to.
    print("\n── 15. the creator and the cheque ──")

    # Make the Instagram stay payable, so there is something to refuse and then
    # allow. Without it the assertion below would pass for the wrong reason —
    # nothing pending approves as zero whether the actor is the creator or not.
    if ig_res:
        sql(f"""update public.reservations
                   set status = 'checked_out', paid_paise = total_paise
                 where id = '{ig_res}';""")
        asyncio.run(creators_service.recalculate_for_reservation(ig_res))
        pending = one(sql(f"""select status from public.creator_payouts
                               where reservation_id = '{ig_res}';"""), "status")
        check("a settled Instagram stay becomes payable",
              pending == "pending_approval", str(pending))

    owner_profile = one(sql("""select id from public.profiles
                                where email = 'sahil@casamandala.in';"""), "id")
    sql(f"""update public.creators set profile_id = '{owner_profile}'
             where handle = '{HANDLE}';""")
    st, self_approve = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                            "POST", {}, owner)
    check("an owner who is the creator cannot approve their own payout",
          st == 403, f"{st} {str(self_approve)[:200]}")
    sql(f"""update public.creators set profile_id = null where handle = '{HANDLE}';""")

    st, now_ok = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                      "POST", {}, owner)
    check("and can once they are not", st == 200 and now_ok["approved"] >= 1,
          f"{st} {str(now_ok)[:200]}")

    # ── 16. the cases that must not pay ──────────────────────────────────────
    print("\n── 16. what does not attribute ──")

    stale_res = _stale_touch_booking(reception, campaign_id, part_id, today, PUBLIC_PLAN)
    check("a click whose window closed before the booking attributes nothing",
          stale_res is True, str(stale_res))

    st, paused = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/status",
                      "POST", {"status": "paused"}, manager)
    check("the hotel pauses the creator", st == 200 and paused["status"] == "paused",
          f"{st} {str(paused)[:160]}")

    st, cold = http(f"{API}/public/hotels/{SLUG}/attribution", "POST", {"code": code})
    check("a paused campaign attributes nobody", st == 200 and cold["attributed"] is False,
          f"{st} {str(cold)[:160]}")

    http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/status",
         "POST", {"status": "live"}, manager)

    # ── 14. scope ────────────────────────────────────────────────────────────
    print("\n── 17. who may see any of it ──")

    st, _ = http(f"{API}/properties/{PROP}/creator-campaigns", headers=reception)
    check("a receptionist cannot read the hotel's commission rates", st == 403, str(st))

    st, _ = http(f"{API}/properties/{PROP}/creator-campaigns", headers=outsider)
    check("another partner gets a 404, not a 403", st == 404, str(st))

    st, _ = http(f"{API}/properties/{OTHER_PROP}/creator-campaigns/{part_id}/touches",
                 headers=owner)
    check("a campaign id from one hotel 404s at another", st == 404, str(st))

    st, _ = http(f"{API}/properties/{OTHER_PROP}/creator-campaigns/{part_id}/approve",
                 "POST", {}, owner)
    check("and cannot be approved through the wrong property either", st == 404, str(st))

    st, _ = http(f"{API}/properties/{PROP}/creator-campaigns")
    check("an unauthenticated caller reads nothing", st in (401, 403), str(st))

    st, _ = http(f"{API}/properties/{PROP}/creator-campaigns/{uuid.uuid4()}/touches",
                 headers=owner)
    check("and a campaign that does not exist 404s", st == 404, str(st))

    # ── 15. the browser decides nothing ──────────────────────────────────────
    print("\n── 18. what a client cannot send ──")

    st, body = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/approve",
                    "POST", {"amountPaise": 99999999, "approved": 50}, owner)
    check("an amount in the approval request is ignored, not honoured",
          st == 200 and body.get("amountPaise", 0) < 99999999, f"{st} {str(body)[:160]}")

    st, body = http(f"{API}/properties/{PROP}/creator-campaigns/{part_id}/status",
                    "POST", {"status": "cancelled"}, manager)
    check("a status outside the lifecycle is a 422, not a 500", st == 422, str(st))

    st, body = http(f"{API}/public/hotels/{SLUG}/attribution", "POST",
                    {"code": code, "creatorId": str(uuid.uuid4()),
                     "commissionPct": 90})
    check("and a public caller naming a creator or a commission is answered "
          "as if they had not", st == 200 and body["attributed"]
          and body.get("guestDiscountPct") == 5, f"{st} {str(body)[:200]}")

    sweep()
    print("\n  · swept the campaign, its chain, its payouts and every stay under it")
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("\nREAL DATABASE · REAL PMS SERVICES · REAL JWTs · REAL ATTRIBUTION")
    print("NO CREATOR WAS PAID: 'approved' is the terminal state, and no payment")
    print("provider is integrated on this path.\n")
    return 1 if FAIL else 0


# ── the two scenarios that need their own booking ────────────────────────────

def _second_booking(reception, manager, code, part_id, campaign_id, today, plan):
    """A second attributed, payable stay — the one four owners race to approve."""
    import asyncio

    from app.services.pms import creators as creators_service

    st, touch = http(f"{API}/public/hotels/{SLUG}/attribution", "POST", {"code": code})
    if st != 200 or not touch.get("attributed"):
        check("a second click for the concurrency test", False, str(touch)[:160])
        return None
    visitor = touch["visitorRef"]

    check_in = today + timedelta(days=230)
    st, quote = http(f"{API}/properties/{PROP}/quotes", "POST", {
        "roomTypeId": DLX, "ratePlanId": plan, "checkIn": str(check_in),
        "checkOut": str(check_in + timedelta(days=2)), "units": 1,
        "adults": 2, "children": 0,
    }, reception)
    if st != 200:
        check("a second stay for the concurrency test", False, str(quote)[:160])
        return None

    st, booking = http(f"{API}/properties/{PROP}/reservations", "POST", {
        "quoteId": quote["id"], "guestName": "Second Guest", "source": "direct",
        "tokenPaise": 0, "paymentMethod": None,
    }, reception)
    if st != 200:
        check("a second booking for the concurrency test", False, str(booking)[:160])
        return None

    asyncio.run(creators_service.resolve(booking["id"], visitor_ref=visitor))
    sql(f"""update public.reservations
               set status = 'checked_out', paid_paise = {booking['totalPaise']}
             where id = '{booking['id']}';""")
    asyncio.run(creators_service.recalculate_for_reservation(booking["id"]))

    ready = sql(f"""select status from public.creator_payouts
                     where reservation_id = '{booking['id']}';""")
    check("a website click and a direct booking attribute too",
          one(ready, "status") == "pending_approval", str(ready)[:200])
    return booking["id"]


def _stale_touch_booking(reception, campaign_id, part_id, today, plan):
    """
    A click whose window closed. Inserted with a past `expires_at` directly,
    because there is no way to make `now()` be six weeks ago and a suite that
    waited six weeks would not be a suite.
    """
    import asyncio

    from app.services.pms import creators as creators_service

    stale_visitor = "vr_stale_" + uuid.uuid4().hex[:10]
    row = sql(f"""select property_id, partner_id, creator_id from public.campaign_creators
                   where id = '{part_id}';""")[0]
    sql(f"""insert into public.attribution_touches
             (property_id, partner_id, campaign_id, creator_id, campaign_creator_id,
              kind, source, occurred_at, expires_at, visitor_ref, event_key)
           values ('{row['property_id']}', '{row['partner_id']}', '{campaign_id}',
                   '{row['creator_id']}', '{part_id}', 'click', 'website',
                   now() - interval '90 days', now() - interval '60 days',
                   '{stale_visitor}', 'click:stale:{stale_visitor}');""")

    check_in = today + timedelta(days=250)
    st, quote = http(f"{API}/properties/{PROP}/quotes", "POST", {
        "roomTypeId": DLX, "ratePlanId": plan, "checkIn": str(check_in),
        "checkOut": str(check_in + timedelta(days=2)), "units": 1,
        "adults": 2, "children": 0,
    }, reception)
    if st != 200:
        return f"quote failed: {str(quote)[:160]}"

    st, booking = http(f"{API}/properties/{PROP}/reservations", "POST", {
        "quoteId": quote["id"], "guestName": "Too Late", "source": "direct",
        "tokenPaise": 0, "paymentMethod": None,
    }, reception)
    if st != 200:
        return f"booking failed: {str(booking)[:160]}"

    verdict = asyncio.run(creators_service.resolve(
        booking["id"], visitor_ref=stale_visitor))
    sql(f"delete from public.reservations where id = '{booking['id']}';")
    return verdict.get("reason") == "no_touch" or verdict


if __name__ == "__main__":
    os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")
    sys.exit(main())
