#!/usr/bin/env python3
"""
The conversation that started all of this, replayed against the build it caused.

On 2026-08-21 a real guest spent an hour and fifty minutes on Instagram trying
to give a hotel ₹7,168, and got there — the money landed at 19:58:52 and the
reservation confirmed. Twenty-six seconds later he asked whether it had gone
through and was told:

    Aapki booking tab confirm ho jaayegi jab payment process ho jaayegi.

It already had. Nothing in the system was going to volunteer that, because
nothing in the system had a reason to speak unprompted, and nothing the agent
could call would have told it either.

That transcript contains nine distinct failures. This replays the guest's own
messages, verbatim, through the real pipeline and the real model, and asserts
that each one is now something else. **Every assertion is about a row or a tool
call, not about wording** — a live model phrases things differently every run,
and a suite that pinned its prose would be measuring the wrong thing. The two
exceptions are both negative and both about what must *never* appear: Devanagari
after a guest asked for Hinglish, and internal vocabulary in front of a guest.

    THE NINE
      1  language asked for, then ignored two turns later
      2  "main aapko rooms ki photos nahi dikha sakta"
      3  a quote described to the guest as a booking
      4  "mujhe booking ke liye ek quote ID ki zarurat hai"
      5  a room offered at 18:46 and refused at 18:49
      6  "quote generate karne mein kuch takleef ho rahi hai"
      7  an hour's gap, and every fact re-asked from scratch
      8  "payment hogyi" answered with a guess
      9  "check karo" answered with a handover

And then the half of the journey he actually completed, which nothing has
replayed since: the questions every hotel is asked before a booking, the
group, the haggle, the money, and the confirmation.

    THE REST OF THE SALE
      · breakfast and an airport pickup, priced from the hotel's own tables
      · five rooms, priced by the group band rather than by the model
      · "thoda kam karo", answered by the engine twice with the same number
      · the floor, which must appear nowhere in the transcript
      · a real Razorpay order, a signed webhook, a confirmed reservation
      · the confirmation nobody had to ask for, and its receipt link
      · "check karo payment hui?" — with, this time, something to find

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010 &     # the webhook is HTTP
    ./venv/bin/python scripts/replay_e2e.py

⚠ Real model, real database, real credits, a real Razorpay order. About thirty
turns; give it a few minutes. Runs on Casa Mandala, seeds two group bands and
sweeps both them and its own conversation.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")
os.environ.setdefault("INSTAGRAM_APP_SECRET", "replay-e2e-app-secret")
os.environ.setdefault("INSTAGRAM_VERIFY_TOKEN", "replay-e2e-verify-token")

import e2e_fixture  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.ai import registry  # noqa: E402
from app.services.pms import negotiation, rates  # noqa: E402
from app.services.messaging.instagram.fake import FakeInstagramAdapter  # noqa: E402

REF = "rdlozqvuywtgsodunait"
PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
IG_ACCOUNT = "17841400000000001"
API = os.environ.get("STAYOS_API_BASE", "http://127.0.0.1:8010").rstrip("/") + "/api/v1/pms"

registry.load_all()
db = get_db()

FIXTURE = e2e_fixture.Fixture(
    label="replay", property_id=PROPERTY, partner_id=PARTNER, prefixes=(),
)
FIXTURE.open()

RUN_TAG = uuid.uuid4().hex[:8]
IGSID = f"replay_{RUN_TAG}"
_seq = 0

# The guest arrived "next month 10th … 4 din k liye". Kept relative so the
# suite does not expire the way a hardcoded September would.
CHECK_IN = date.today() + timedelta(days=20)
CHECK_OUT = CHECK_IN + timedelta(days=4)
WHEN = f"{CHECK_IN.strftime('%d %B')} to {CHECK_OUT.strftime('%d %B')}"

PASS = FAIL = 0
DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Words that belong to the system and not to a guest. The reference transcript
# put four of them in front of a paying customer.
INTERNAL = ("quote id", "quote_id", "rate plan", "rate_plan", "room_type",
            "quote generate", "uuid", "null", "api", "tool")


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def run(coro):
    return asyncio.run(coro)


raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()


def sql(query: str):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        method="POST", data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MGMT}",
                 "User-Agent": "SupabaseCLI/2.0.0"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:300]}


# ── the harness ─────────────────────────────────────────────────────────────

# Every turn that ended somewhere other than a delivered reply, and why. The
# distinction between these is the whole point of reading `agent_runs.error`
# rather than the reply text: they look identical to a guest and mean opposite
# things about the build.
GUARDRAIL_REFUSALS: list[str] = []
PROVIDER_FAILURES: list[str] = []


def last_run() -> dict:
    conv = conversation()
    if not conv:
        return {}
    rows = sql(f"""select status, error from public.agent_runs
                    where conversation_id = '{conv['id']}'
                    order by started_at desc limit 1;""")
    return rows[0] if isinstance(rows, list) and rows else {}


def say(text: str, *, retries: int = 1) -> list[dict]:
    """
    One message from the guest, through the webhook. Returns what came back.

    **Retried once when the *provider* failed**, and only then. A Vertex
    ReadTimeout is Google being slow, not this build being wrong, and a suite
    that goes red on it is a suite somebody mutes. A handoff for any other
    reason is a real failure and is left to fail — that distinction is the
    whole reason `agent_runs.error` is consulted rather than the reply text.
    """
    replies = _say_once(text)
    while True:
        run_row = last_run()
        error = (run_row.get("error") or "")
        upstream = run_row.get("status") == "failed" and any(
            marker in error.lower()
            for marker in ("readtimeout", "timeout", "503", "429", "unavailable",
                           "vertex call failed")
        )
        if error.startswith("guardrail:"):
            GUARDRAIL_REFUSALS.append(error)
            print(f"    🛡  refused: {error[10:120].strip()}")
        elif upstream:
            PROVIDER_FAILURES.append(error)

        # A handoff parks the thread with a person, and every later message is
        # stored unanswered. That is right in production and fatal to a replay,
        # so the thread is handed back — and the fact that it had to be is
        # printed, because a sale that stalls until somebody notices is a
        # finding rather than a detail.
        conv = conversation()
        if conv and conv.get("handler") != "ai":
            from app.services.messaging import service as messaging
            run(messaging.resume_ai(conv["id"], PROPERTY))
            print("    ↩  thread was parked with a human; handed back to the AI")

        if not (upstream and retries > 0):
            return replies
        print(f"    ⟳ upstream said {error!r}; retrying this turn once")
        retries -= 1
        time.sleep(2)
        replies = _say_once(text)


def _say_once(text: str) -> list[dict]:
    global _seq
    _seq += 1
    before = len(messages())
    payload = {
        "object": "instagram",
        "entry": [{
            "id": IG_ACCOUNT,
            "time": int(time.time() * 1000),
            "messaging": [{
                "sender": {"id": IGSID},
                "recipient": {"id": IG_ACCOUNT},
                "timestamp": int(time.time() * 1000),
                "message": {"mid": f"ig_mid.RPL{RUN_TAG}{_seq:03d}", "text": text},
            }],
        }],
    }
    from app.api.v1.messaging import webhooks
    run(webhooks.handle_payload(payload, channel="instagram"))
    fresh = messages()[before:]
    replies = [m for m in fresh if m["direction"] == "outbound"]
    print(f"    guest › {text}")
    for reply in replies:
        kind = "📷" if m_is_image(reply) else "  "
        print(f"    agent {kind}‹ {(reply.get('body') or '(image)')[:150]}")
    return replies


def m_is_image(message: dict) -> bool:
    return message.get("message_type") == "image"


def conversation() -> dict | None:
    rows = (db.table("conversations").select("*")
            .eq("property_id", PROPERTY).eq("guest_handle", IGSID)
            .limit(1).execute().data or [])
    return rows[0] if rows else None


def messages() -> list[dict]:
    conv = conversation()
    if not conv:
        return []
    return (db.table("conversation_messages").select("*")
            .eq("conversation_id", conv["id"]).order("sent_at").execute().data or [])


def actions() -> list[dict]:
    """Every tool call on this thread, in order, with what went in and out."""
    conv = conversation()
    if not conv:
        return []
    rows = sql(f"""select a.tool, a.input, a.output, a.status
                     from public.agent_actions a
                     join public.agent_runs r on r.id = a.run_id
                    where r.conversation_id = '{conv['id']}'
                    order by a.created_at;""")
    return rows if isinstance(rows, list) else []


def tools_used() -> list[str]:
    return [a["tool"] for a in actions()]


# ── did it name a number nobody gave it? ─────────────────────────────────────
#
# The §20 rule stated as an assertion rather than as a prompt line: the backend
# is the source of financial truth, so every rupee figure in front of a guest
# has to be traceable to something a tool returned. A model that is 95% right
# about arithmetic is a model that quotes the wrong price one conversation in
# twenty, and that conversation is a booking somebody has to honour.

RUPEES = re.compile(r"(?:₹|rs\.?|inr)\s*([\d][\d,]*)", re.I)


def figures_in(text: str) -> set[int]:
    """Every rupee amount said out loud, in paise."""
    return {int(m.replace(",", "")) * 100 for m in RUPEES.findall(text)}


def tool_numbers(rows: list[dict]) -> set[int]:
    """
    Every number those calls returned, read both as paise and as rupees.

    Both, because tools return paise and prose says rupees, and which of the
    two a given field was is not worth reconstructing for an assertion whose
    job is to catch invention rather than to audit units.
    """
    found: set[int] = set()

    def walk(value) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            n = int(value)
            found.update({n, n * 100})
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            found.update(figures_in(value))

    for row in rows:
        walk(row.get("output"))
    return found


def unexplained(named: set[int], known: set[int]) -> list[int]:
    """
    Figures the agent said that no tool can account for.

    Multiples and pairwise sums count as accounted for: "₹3,200 a night for
    four nights is ₹12,800" is the agent reading a tool out loud, not deciding
    a price. Anything left over was invented.
    """
    stray = []
    for amount in sorted(named):
        if amount in known:
            continue
        if any(k and amount % k == 0 and 2 <= amount // k <= 20 for k in known):
            continue
        if any((amount - k) in known for k in known):
            continue
        stray.append(amount)
    return stray


def rupees(paise: int) -> str:
    return f"₹{paise // 100:,}"


def http(url, method="GET", data=None, headers=None, raw_body=None):
    body = raw_body if raw_body is not None else (
        json.dumps(data).encode() if data is not None else None
    )
    req = urllib.request.Request(url, method=method, data=body,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


def webhook_body(order_id: str, payment_id: str) -> tuple[bytes, str]:
    payload = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }).encode()
    signature = hmac.new(
        (settings.razorpay_webhook_secret or "").encode(), payload, hashlib.sha256,
    ).hexdigest()
    return payload, signature


def prose() -> str:
    return " ".join(
        (m.get("body") or "") for m in messages()
        if m["direction"] == "outbound" and not m_is_image(m)
    ).lower()


# The two group rows this replay needs and Casa Mandala does not have. Seeded
# rather than assumed, because a suite that silently skips the group journey
# when a demo row is missing is a suite that reports a feature as tested.
GROUP_TAG = "replay group band"


def seed_group_pricing() -> None:
    sql(f"""
delete from public.group_rate_tiers where property_id = '{PROPERTY}';
insert into public.group_rate_tiers
  (property_id, partner_id, min_units, max_units, discount_pct, negotiable)
values ('{PROPERTY}', '{PARTNER}', 3, 5, 8, false),
       ('{PROPERTY}', '{PARTNER}', 6, null, null, true);
insert into public.group_policies
  (property_id, partner_id, min_units, deposit_pct, payment_due_days)
values ('{PROPERTY}', '{PARTNER}', 3, 30, 14)
on conflict (property_id) do update
   set min_units = 3, deposit_pct = 30, payment_due_days = 14;""")


def sweep() -> None:
    conv = conversation()
    sql(f"delete from public.group_rate_tiers where property_id = '{PROPERTY}';")
    sql(f"delete from public.group_policies where property_id = '{PROPERTY}';")
    if not conv:
        return
    # **The stock goes back before the row is cancelled.**
    #
    # `room_inventory.units_booked` is owned by the RPCs — confirming a booking
    # adds to it, `release_booking` subtracts — and an UPDATE that sets a
    # reservation to 'cancelled' goes past both. No trigger catches it, because
    # the product has no cancellation path at all; only these suites produce
    # that status.
    #
    # So this sweep spent every run quietly removing rooms from Casa Mandala
    # for ever. By the time it was noticed, 377 room-nights across the property
    # were recorded as sold with nobody in them — and Deluxe Balcony, the room
    # the reference guest asks for, had fifteen of its sixteen units gone on
    # the replay's own dates. The suite could no longer sell the room its
    # script is about: the model was told the guest's choice was unavailable,
    # and every assertion after the quote failed. Two sessions were spent
    # looking for the regression that had caused it. There was none.
    #
    # `scripts/reconcile_inventory.py` repairs a property that has already been
    # eaten this way. This is what stops it happening again.
    for row in (sql(
        f"select room_type_id, check_in, check_out, units from public.reservations "
        f" where conversation_id = '{conv['id']}' and status not in ('cancelled','no_show');"
    ) or []):
        if isinstance(row, dict) and row.get("room_type_id"):
            db.rpc("release_booking", {
                "p_room_type": row["room_type_id"],
                "p_check_in": row["check_in"],
                "p_check_out": row["check_out"],
                "p_units": row.get("units") or 1,
            }).execute()

    # One statement per call. A reservation points at its quote and the thread
    # points at its reservation, so a single batch that trips one foreign key
    # rolls back the whole sweep and leaves a paid demo booking behind.
    for statement in (
        f"update public.reservations set status = 'cancelled', conversation_id = null "
        f" where conversation_id = '{conv['id']}';",
        f"update public.conversations set reservation_id = null where id = '{conv['id']}';",
        f"delete from public.payment_orders where conversation_id = '{conv['id']}';",
        f"delete from public.quotes where conversation_id = '{conv['id']}';",
        f"delete from public.conversations where id = '{conv['id']}';",
    ):
        sql(statement)


def main() -> int:
    print("\n══ the reference conversation, replayed ══\n")
    sql(f"""
insert into public.instagram_connections
  (property_id, partner_id, instagram_account_id, username, status, is_sandbox,
   credentials_ref)
values ('{PROPERTY}', '{PARTNER}', '{IG_ACCOUNT}', 'casamandala', 'connected',
        true, 'sandbox')
on conflict (instagram_account_id) do update
   set property_id = excluded.property_id, status = 'connected', is_sandbox = true;""")
    FakeInstagramAdapter.reset()
    sweep()
    seed_group_pricing()

    # The level this replay is about.
    #
    # `agents.autonomy_level` is shared demo state and half the e2e suites set
    # it to 2 for their own assertions without putting it back, so what this
    # replay measures depended on which suite ran last: at 2 every selling tool
    # queues for approval and the sale stops at "the hotel team will confirm".
    # Set explicitly, the way payment_status_e2e does, because a suite about
    # closing a sale on a hotel that cannot quote is measuring nothing.
    sql(f"""update public.agents set autonomy_level = 3
             where property_id = '{PROPERTY}' and agent_type = 'guest';""")
    print("    · guest agent set to autonomy 3 (unattended selling)")

    # ── 1 & 7: language, and a session that outlives the gap ─────────────────
    print("── the opening, as it happened ──")
    say("Hello")
    say("Can you send me price of hotel?")
    say("Kya AAP hindi m baat kar sakte h?")
    say(f"M {WHEN} aauga 3 logo k sath")
    say("Okie 2 kamre karne h aaur hinglish m baat karo please")

    conv = conversation()
    check("① the language the guest asked for is stored, not just answered",
          (conv or {}).get("guest_profile", {}).get("language") == "hinglish",
          str((conv or {}).get("guest_profile")))

    before_photos = len(messages())

    # ── 2: photos ───────────────────────────────────────────────────────────
    print("\n── \"Photos h?\" ──")
    say("Photos h?")
    fresh = messages()[before_photos:]
    images = [m for m in fresh if m_is_image(m)]
    check("② real photographs went out, not an apology",
          len(images) >= 1, f"{len(images)} images among {len(fresh)} messages")
    check("   and they came from this hotel's own rooms",
          all((m.get("media_urls") or []) for m in images),
          str([m.get("media_urls") for m in images]))
    check("   \"main aapko photos nahi dikha sakta\" is not in this transcript",
          "nahi dikha sakta" not in prose() and "unable to show" not in prose(),
          "the sentence this whole build exists to delete came back")

    # ── 3, 4, 5, 6: the sale ────────────────────────────────────────────────
    print("\n── the sale ──")
    say("Price kya h?")
    # The reference guest named a room — "King bed book kardi" — and was told
    # it was booked. Naming one here keeps the replay honest about which
    # failure is being tested.
    say("Deluxe Balcony wala book kardo")
    say("Sahil")

    conv = conversation()
    stays = sql(f"""select count(*) as n from public.reservations
                     where conversation_id = '{conv['id']}';""")
    booked = int(stays[0]["n"]) if isinstance(stays, list) else 0
    check("③ nothing was called a confirmed booking, because nothing was paid",
          booked == 0
          and not any(p in prose() for p in ("book ho gaye", "confirm ho gaya",
                                             "booking confirmed", "is confirmed")),
          f"{booked} reservations; prose claimed a booking")

    said = prose()
    leaked = [word for word in INTERNAL if word in said]
    check("④ no internal vocabulary reached the guest", not leaked, str(leaked))

    # Word-boundary matched, because "₹4,500" contains "500" and a price is not
    # a stack trace. The first version of this check failed on the agent
    # quoting a room correctly, which is the kind of assertion that gets muted.
    narrated = [word for word in
                ("takleef", "traceback", "exception", "internal server",
                 "http 500", "null", "none type")
                if re.search(rf"\b{re.escape(word)}\b", said)]
    # The handoff sentence is excluded here on purpose. It appears for three
    # different reasons — a guardrail refusal, an upstream timeout, and a real
    # internal error — and only the third is what ⑥ is about. Which one it was
    # is on the run row, and is asserted separately below.
    check("⑥ no internal failure was narrated to the guest", not narrated,
          f"{narrated} — an internal failure was read out to a paying customer")

    # Every refusal, classified. The rule this asserts is not "the guardrail
    # never fires" — on a live model it fires, and that is the point of having
    # it — but that everything it stops is one of the four things it exists to
    # stop. A refusal for some fifth reason would mean it is catching honest
    # replies, which is how a guardrail ends up switched off.
    KNOWN = ("secured", "quoted", "exposed", "unverified link", "placeholder")
    unexplained_refusals = [
        r for r in GUARDRAIL_REFUSALS
        if not any(kind in r.lower() for kind in KNOWN)
    ]
    check("③ⁱ everything the guardrail refused was one of its own rules",
          not unexplained_refusals, str(unexplained_refusals))
    if GUARDRAIL_REFUSALS:
        print(f"       ({len(GUARDRAIL_REFUSALS)} refused — the reference "
              f"transcript delivered this claim to a paying guest)")

    # ── what the stay includes ──────────────────────────────────────────────
    #
    # The reference guest never got this far. Breakfast and a car from the
    # airport are the two questions every hotel gets and the two a model will
    # answer from nothing at all — a plausible 8-to-10 buffet and a plausible
    # ₹1,500 transfer, both invented, both quotable back at the desk.
    print("\n── breakfast, and a car from the airport ──")
    mark, spoken = len(actions()), len(messages())
    say("Breakfast included h isme?")
    say("Airport se pickup mil jayega? Kitne ka padega?")
    fresh = actions()[mark:]
    used = [a["tool"] for a in fresh]

    # **Not "get_meal_info was called".** It was, until the tool that shows a
    # guest their options started carrying `mealsIncluded` — derived from the
    # rate's meal plan against what the kitchen actually serves — so answering
    # from the options a guest is already looking at became the right move
    # rather than a shortcut. What must stay true is that the answer came from
    # the hotel's own record and not from a rate plan's wording, and the run
    # that prompted this proved the difference is real: the model read
    # "Breakfast" out of the free-text inclusions and told the guest, without
    # ever asking whether the hotel serves breakfast.
    # **Everything the conversation has looked up, not just this turn's calls.**
    #
    # It was "get_meal_info ran on this turn", which stopped being the right
    # question twice over. First when `get_stay_options` started carrying
    # `mealsIncluded` — derived from the rate's meal plan against what the
    # kitchen actually serves — so the options a guest is already looking at
    # became a legitimate source. Then because a model that read those options
    # two turns ago still has them, and answering from what it was told rather
    # than asking again is the behaviour this whole build is for.
    #
    # What must stay true is that the hotel's own record is behind the answer
    # somewhere, and never a rate plan's wording. The run that prompted the
    # first half of this had the model read "Breakfast" out of the free-text
    # inclusions and tell a guest, without anything having asked whether the
    # hotel serves breakfast at all.
    looked_up = [a["tool"] for a in actions()]
    check("the meal answer came from the hotel's record, not a rate plan's wording",
          any(t in looked_up for t in ("get_meal_info", "get_stay_options", "get_quote")),
          str(looked_up))
    check("and its price list was read before a transfer was priced",
          any(t in used for t in ("list_services", "order_service",
                                  "get_experiences", "get_property_knowledge")),
          str(used))

    said = " ".join((m.get("body") or "") for m in messages()[spoken:]
                    if m["direction"] == "outbound")
    # Every tool call so far, not just this turn's: "the price I quoted you
    # includes breakfast" is the agent referring back to a quote it took two
    # turns ago, which is exactly what it should do.
    stray = unexplained(figures_in(said), tool_numbers(actions()))
    check("every rupee figure it named came out of a tool",
          not stray, f"invented: {[rupees(n) for n in stray]}")

    # ── group pricing ───────────────────────────────────────────────────────
    #
    # Five rooms is inside the 3–5 band seeded above. The assertion is not that
    # the agent got the discount right — it is that the agent never touched the
    # discount: the quote row carries the id of the tier that priced it, which
    # only `create_quote` can have put there.
    print("\n── \"5 kamre chahiye\" ──")
    mark = len(actions())
    say("Actually 5 kamre chahiye, 10 log aa rahe hain")
    fresh = actions()[mark:]
    used = [a["tool"] for a in fresh]

    check("a group was recognised as a group",
          "get_group_rates" in used, str(used))

    tiers = run(rates.tiers_for(PROPERTY))
    band = rates.select_tier(tiers, 5, "")
    check("and the band the engine picks for five rooms is the seeded one",
          band is not None and band.min_units == 3 and band.discount_pct == 8,
          str(band))

    group_tool = next((a for a in fresh if a["tool"] == "get_group_rates"), None)
    if group_tool:
        applied = (group_tool["output"] or {}).get("appliedBand") or {}
        check("   the agent was told the same band, not left to choose one",
              applied.get("fromRooms") == 3, str(group_tool["output"])[:200])
        check("   along with the deposit the hotel actually set",
              (group_tool["output"] or {}).get("depositPct") == 30,
              str(group_tool["output"])[:200])

    conv = conversation()
    priced = sql(f"""select q.units, q.total_paise, q.group_tier_id, t.discount_pct
                       from public.quotes q
                       left join public.group_rate_tiers t on t.id = q.group_tier_id
                      where q.conversation_id = '{conv['id']}' and q.units >= 3
                      order by q.created_at desc limit 1;""")
    priced = priced[0] if isinstance(priced, list) and priced else None
    if priced:
        check("   and the quote records which rule priced it",
              priced["group_tier_id"] is not None and priced["discount_pct"] == 8,
              str(priced))
    else:
        print("       (no multi-room quote was taken this run — nothing to trace)")

    # ── negotiation ─────────────────────────────────────────────────────────
    #
    # §20 asks for this to be deterministic, so it is asserted the only way that
    # means anything: the same question is put to the engine twice, directly,
    # and compared against what the guest was told. The model is a courier here.
    print("\n── \"thoda kam karo\" ──")
    mark, spoken = len(actions()), len(messages())
    say("Yaar thoda kam karo na, 4000 per night mein ho jayega?")
    fresh = actions()[mark:]
    negotiations = [a for a in fresh if a["tool"] == "negotiate_rate"]

    check("it asked the hotel's rules instead of deciding for itself",
          negotiations, str([a["tool"] for a in fresh]))

    if negotiations:
        args, verdict = negotiations[0]["input"], negotiations[0]["output"] or {}
        asked = dict(
            property_id=PROPERTY,
            rate_plan_id=args["rate_plan_id"],
            check_in=date.fromisoformat(args["check_in"]),
            check_out=date.fromisoformat(args["check_out"]),
            units=int(args.get("units") or 1),
            requested_nightly_paise=(
                int(args["requested_nightly_rupees"]) * 100
                if args.get("requested_nightly_rupees") is not None else None
            ),
        )
        first, second = run(negotiation.negotiate(**asked)), run(negotiation.negotiate(**asked))
        check("the same request twice gives the same answer",
              first.as_dict() == second.as_dict(),
              f"{first.as_dict()} vs {second.as_dict()}")

        # **The re-run cannot see the world the agent saw, and that is correct.**
        #
        # A verdict of `allowed` is followed by a quote, and a quote takes a
        # hold: five rooms the guest now has. `negotiate()` asks
        # `stay_options` without `for_conversation` — deliberately, because its
        # job is to know whether the rooms are really there — so asked again a
        # second later it finds three free and answers `not_enough_rooms`.
        #
        # That is the guest's own hold, and treating it as a disagreement made
        # this assertion fail on a run where nothing was wrong. So the rejection
        # is only accepted when the hold that explains it is this conversation's.
        held_here = 0
        if first.verdict == "rejected" and first.reason == "not_enough_rooms":
            conv = conversation()
            held_here = sum(
                q.get("units") or 0
                for q in (db.table("quotes")
                          .select("units,status,hold_id")
                          .eq("conversation_id", conv["id"])
                          .in_("status", ["active", "converted"])
                          .execute().data or [])
                if q.get("hold_id")
            )

        agreed = (
            verdict.get("verdict") == first.verdict
            and verdict.get("finalTotalPaise") == first.final_total_paise
            and verdict.get("counterTotalPaise") == first.counter_total_paise
        )
        check("and the answer the guest got is the engine's, unedited",
              agreed or (verdict.get("verdict") == "allowed"
                         and first.reason == "not_enough_rooms"
                         and held_here >= asked["units"]),
              f"agent {verdict} vs engine {first.as_dict()} (held here: {held_here})")

        # The floor is the number that must never appear. It is computed here
        # only so the transcript can be searched for it — the Decision object
        # deliberately does not carry it, which is why this has to be derived.
        rules = run(negotiation.rules_for(PROPERTY, rate_plan_id=args["rate_plan_id"]))
        floor = negotiation.floor_total_for(
            first.published_total_paise, rules, first.nights, first.units)
        said = " ".join((m.get("body") or "") for m in messages()[spoken:]
                        if m["direction"] == "outbound")
        leaked_floor = []
        for secret in (floor, rules.floor_paise,
                       (floor // max(first.nights, 1) // max(first.units, 1)) if floor else None):
            if not secret:
                continue
            rupee = secret // 100
            if any(form in said for form in (f"{rupee:,}", str(rupee))):
                leaked_floor.append(rupee)
        check("   and the minimum it may go to was never said out loud",
              not leaked_floor, f"the floor appeared in front of the guest: {leaked_floor}")

        if first.verdict == "allowed" and floor:
            check("   the agreed price is at or above that minimum",
                  (first.final_total_paise or 0) >= floor,
                  f"{first.final_total_paise} < {floor}")

        profile = (conversation() or {}).get("guest_profile") or {}
        check("   the outcome is on the thread as state, not as remembered prose",
              profile.get("negotiation") in ("allowed", "counter", "needs_approval",
                                             "refused", verdict.get("verdict")),
              str(profile.get("negotiation")))

    # ── 8 & 9: the money ────────────────────────────────────────────────────
    print("\n── \"Payment hogyi\" ──")
    used_before, spoken = len(tools_used()), len(messages())
    say("Payment hogyi")
    say("Check karo hogyi meri payment")
    asked = tools_used()[used_before:]
    answered = " ".join((m.get("body") or "") for m in messages()[spoken:]
                        if m["direction"] == "outbound").lower()

    check("⑧ the agent went and looked instead of guessing",
          "get_booking_status" in asked, str(asked))
    check("   and it is the one tool that takes no arguments",
          registry.REGISTRY["get_booking_status"].input_model.model_fields == {},
          str(registry.REGISTRY["get_booking_status"].input_model.model_fields))
    check("⑨ and nobody was handed to a person for asking",
          "request_human_handoff" not in asked
          and "trouble reaching our system" not in answered,
          str(asked))
    check("   nor was an unpaid booking described as paid",
          not any(p in prose() for p in ("payment mil gaya", "payment received",
                                         "paid ho gaya")),
          "the agent confirmed money that never arrived")

    # ── ⑦ what the session remembered ───────────────────────────────────────
    #
    # Asserted here rather than at the end, because the end of this replay is
    # now a confirmed booking: `_superseded` clears the stay fields once the
    # conversation moves on, and the dates of a booking that exists live on the
    # reservation. ⑦ is about the hour-long gap in the reference transcript,
    # and that gap is behind us by this line.
    profile = (conversation() or {}).get("guest_profile") or {}
    check("⑦ the dates the guest gave are on the conversation, not in the prose",
          profile.get("check_in") and profile.get("check_out"), str(profile))
    check("   as are the party and the room count",
          profile.get("adults") and profile.get("units"), str(profile))
    check("   so the website link can carry them",
          "get_hotel_page_link" in registry.REGISTRY, "the tool is not registered")

    # ── the money actually arrives ──────────────────────────────────────────
    #
    # Everything above this line is the conversation the reference guest had.
    # This is the half he reached and nobody has replayed since: he paid, the
    # order settled, the reservation confirmed — and he was told his booking
    # would confirm once the payment processed. The webhook is delivered over
    # HTTP because that is how Razorpay delivers it, signature over raw bytes
    # and all; calling the handler in-process would test a different thing.
    print("\n── the payment, and what happens after it ──")
    reachable, _ = http(f"{API}/public/hotels/casa-mandala-anjuna")
    check("the API is up, since the webhook arrives over HTTP",
          reachable == 200,
          f"{API} answered {reachable} — start uvicorn on 8010 with .env.hotelos")

    mark = len(actions())
    say("Theek h, payment link bhej do")
    conv = conversation()

    def latest_order():
        rows = sql(f"""select id, razorpay_order_id, amount_paise, status, quote_id
                         from public.payment_orders
                        where conversation_id = '{conv['id']}'
                        order by created_at desc limit 1;""")
        return rows[0] if isinstance(rows, list) and rows else None

    order = latest_order()
    if order is None:
        # A hotel needs a name on a booking, and Instagram does not always
        # carry one — a new contact arrives as an IGSID and nothing else. So
        # being asked for it once, at the moment money is about to move, is the
        # product working rather than failing. What must not happen is being
        # asked and then still not getting a link.
        print("    (asked for a name first — answering it)")
        say("Sahil Verma")
        order = latest_order()

    check("the agent raised a payment link rather than describing how to pay",
          order is not None,
          "no payment_orders row on this thread")
    check("   and the link is against a quote, so the price is the held one",
          bool(order and order.get("quote_id")), str(order))

    if order is None and reachable == 200:
        # Driven directly so the rest of the journey is still exercised. The
        # check above has already recorded that the agent did not get here on
        # its own, which is the finding; stopping now would hide the four
        # assertions that follow behind one flaky turn.
        quotes = sql(f"""select id from public.quotes
                          where conversation_id = '{conv['id']}'
                          order by created_at desc limit 1;""")
        if isinstance(quotes, list) and quotes:
            from app.services.pms import quote_payment
            run(quote_payment.create_link(
                property_id=PROPERTY, partner_id=PARTNER, quote_id=quotes[0]["id"],
                guest_name="Sahil", guest_phone=None, guest_email=None,
                source="instagram", conversation_id=conv["id"]))
            order = latest_order()
            print("    ↩  raised the link directly so the money path still runs")

    if order:
        body, signature = webhook_body(order["razorpay_order_id"], f"pay_RPL{RUN_TAG}")
        st, _ = http(f"{API}/public/razorpay/webhook", "POST", raw_body=body,
                     headers={"X-Razorpay-Signature": signature})
        check("the signed webhook settles the order", st == 200, str(st))

        stays = sql(f"""select id, ref, status, payment_status, total_paise, paid_paise
                          from public.reservations
                         where conversation_id = '{conv['id']}'
                         order by created_at desc limit 1;""")
        stay = stays[0] if isinstance(stays, list) and stays else None
        check("a reservation exists, created by the money and not by the model",
              stay is not None and stay["status"] == "confirmed", str(stay))

        if stay:
            confirmations = [
                m for m in messages()
                if (m.get("client_reference") or "") == f"booking:{stay['id']}"
            ]
            check("the guest was told without having to ask",
                  len(confirmations) == 1, f"{len(confirmations)} confirmations")
            if confirmations:
                text = confirmations[0].get("body") or ""
                check("   it names the reference they can quote at the desk",
                      stay["ref"] in text, text[:140])
                check("   it carries a link to the booking",
                      "/b/" in text, text[:200])
                check("   and that link is a secret, not the reference",
                      stay["ref"] not in text.split("/b/")[-1], text[:200])
                check("   in the language the guest asked for",
                      not DEVANAGARI.search(text), text[:140])

            # ⑧ again, but this time there is something to find. The reference
            # transcript's twenty-six-second gap between a confirmed booking and
            # "it will confirm once the payment processes" is this assertion.
            print("\n── \"check karo payment hui?\" — with the money in ──")
            mark = len(actions())
            say("Ab check karo payment hui ya nahi?")
            fresh = actions()[mark:]
            looked = [a for a in fresh if a["tool"] == "get_booking_status"]
            check("the agent looked again rather than repeating itself",
                  looked, str([a["tool"] for a in fresh]))
            if looked:
                found = looked[0]["output"] or {}
                check("   and the ledger says confirmed",
                      found.get("state") == "CONFIRMED", str(found)[:200])
                check("   with the reference the reservation actually has",
                      found.get("ref") == stay["ref"], str(found.get("ref")))
            recent = " ".join((m.get("body") or "") for m in messages()[-4:]
                              if m["direction"] == "outbound").lower()
            check("   the guest is told it is confirmed, not that it will be",
                  "confirm" in recent
                  and "ho jaayegi" not in recent and "ho jayegi" not in recent,
                  recent[:200])
            profile = (conversation() or {}).get("guest_profile") or {}
            check("   and the thread carries the state, so the next turn knows",
                  profile.get("payment") == "CONFIRMED", str(profile.get("payment")))

    # ── the language rule, over the whole transcript ────────────────────────
    print("\n── and the rule that was broken twice ──")
    after_request = False
    devanagari_after = []
    for message in messages():
        if message["direction"] == "inbound" and "hinglish" in (message.get("body") or "").lower():
            after_request = True
            continue
        if after_request and message["direction"] == "outbound" and message.get("body"):
            if DEVANAGARI.search(message["body"]):
                devanagari_after.append(message["body"][:60])
    check("① no Devanagari after the guest asked for Hinglish",
          not devanagari_after, str(devanagari_after))

    # ④ over the whole transcript, not one section of it.
    #
    # It was checked only across the three turns of the sale, which is where
    # the reference guest met it. A live run then put "ek naya quote generate
    # karna hoga" in front of the guest fifteen turns later, on the payment
    # turn, and the suite reported ④ green — the machine showing through in a
    # place nobody was looking. Every outbound message, once, at the end.
    everything = " ".join(
        (m.get("body") or "").lower() for m in messages() if m["direction"] == "outbound"
    )
    leaked_anywhere = [word for word in INTERNAL if word in everything]
    check("④ and nowhere in the whole transcript did the machine show through",
          not leaked_anywhere, str(leaked_anywhere))

    print(f"    session at the end: "
          f"{sorted((conversation() or {}).get('guest_profile') or {})}")

    print("\n── how the turns went ──")
    conv = conversation()
    runs = sql(f"""select status, task, model_tier, tokens_in, tokens_out,
                          latency_ms, total_credits
                     from public.agent_runs where conversation_id = '{conv['id']}'
                    order by started_at;""") if conv else []
    if isinstance(runs, list) and runs:
        ok = [r for r in runs if r["status"] == "succeeded"]
        print(f"    {len(runs)} turns · {len(ok)} delivered · "
              f"{len(GUARDRAIL_REFUSALS)} refused by the guardrail · "
              f"{len(PROVIDER_FAILURES)} upstream failures")
        if ok:
            print(f"    tokens in  avg {sum(r['tokens_in'] or 0 for r in ok) // len(ok):,}"
                  f"   latency avg {sum(r['latency_ms'] or 0 for r in ok) // len(ok):,} ms")
        print("    tasks: " + ", ".join(
            f"{t}×{sum(1 for r in runs if r['task'] == t)}"
            for t in dict.fromkeys(r["task"] for r in runs)))
        print("    tiers: " + ", ".join(
            f"{t}×{sum(1 for r in runs if r['model_tier'] == t)}"
            for t in dict.fromkeys(r["model_tier"] for r in runs)))

    print("\n── what it actually called ──")
    for tool in dict.fromkeys(tools_used()):
        print(f"    · {tool}")

    sweep()
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    print("REAL MODEL · REAL DATABASE · REAL PIPELINE · FAKE INSTAGRAM\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
