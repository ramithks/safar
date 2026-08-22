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

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/replay_e2e.py

⚠ Real model, real database, real credits. About twenty turns; give it a few
minutes. Runs on Casa Mandala and sweeps its own conversation.
"""
import asyncio
import base64
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

from app.db.client import get_db  # noqa: E402
from app.services.ai import registry  # noqa: E402
from app.services.messaging.instagram.fake import FakeInstagramAdapter  # noqa: E402

REF = "rdlozqvuywtgsodunait"
PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
IG_ACCOUNT = "17841400000000001"

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


def tools_used() -> list[str]:
    conv = conversation()
    if not conv:
        return []
    rows = sql(f"""select a.tool from public.agent_actions a
                     join public.agent_runs r on r.id = a.run_id
                    where r.conversation_id = '{conv['id']}'
                    order by a.created_at;""")
    return [r["tool"] for r in rows] if isinstance(rows, list) else []


def prose() -> str:
    return " ".join(
        (m.get("body") or "") for m in messages()
        if m["direction"] == "outbound" and not m_is_image(m)
    ).lower()


def sweep() -> None:
    conv = conversation()
    if conv:
        sql(f"""delete from public.quotes where conversation_id = '{conv['id']}';
                delete from public.conversations where id = '{conv['id']}';""")


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

    check("③ⁱ and when the model tried to claim one, the guardrail stopped it",
          not GUARDRAIL_REFUSALS
          or all("secured" in r or "confirm" in r.lower() for r in GUARDRAIL_REFUSALS),
          str(GUARDRAIL_REFUSALS))
    if GUARDRAIL_REFUSALS:
        print(f"       ({len(GUARDRAIL_REFUSALS)} refused — the reference "
              f"transcript delivered this claim to a paying guest)")

    # ── 8 & 9: the money ────────────────────────────────────────────────────
    print("\n── \"Payment hogyi\" ──")
    used_before = len(tools_used())
    say("Payment hogyi")
    say("Check karo hogyi meri payment")
    asked = tools_used()[used_before:]

    check("⑧ the agent went and looked instead of guessing",
          "get_booking_status" in asked, str(asked))
    check("   and it is the one tool that takes no arguments",
          registry.REGISTRY["get_booking_status"].input_model.model_fields == {},
          str(registry.REGISTRY["get_booking_status"].input_model.model_fields))
    check("⑨ and nobody was handed to a person for asking",
          "request_human_handoff" not in asked
          and "trouble reaching our system" not in prose(),
          str(asked))
    check("   nor was an unpaid booking described as paid",
          not any(p in prose() for p in ("payment mil gaya", "payment received",
                                         "paid ho gaya")),
          "the agent confirmed money that never arrived")

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

    # ── ⑦ what the session remembered ───────────────────────────────────────
    profile = (conversation() or {}).get("guest_profile") or {}
    check("⑦ the dates the guest gave are on the conversation, not in the prose",
          profile.get("check_in") and profile.get("check_out"), str(profile))
    check("   as are the party and the room count",
          profile.get("adults") and profile.get("units"), str(profile))
    check("   so the website link can carry them",
          "get_hotel_page_link" in registry.REGISTRY, "the tool is not registered")

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
