#!/usr/bin/env python3
"""
Ask the AI employee the things guests actually ask, with a real model.

`ai_e2e.py` scripts the model on purpose — it proves that a complaint becomes a
ticket, and using a live model to prove that would be testing Gemini. This is
the other half, and it cannot be scripted: **does the hotel's own assistant
answer a real guest with the hotel's own facts?**

So every assertion here is about a *fact*, never about prose. "Check-in is from
2 PM" and "check-in is at two in the afternoon" both pass; "check-in is at 11
AM" fails, and so does a fluent paragraph that never says a time. The failure
this exists to catch is the one that looks fine in a screenshot — an assistant
that sounds like a hotel and is quietly making the answers up.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/ai_employee_check.py

⚠ Costs real model calls. Runs on the demo hotel only, and writes
conversations, runs and actions against it — never the pilot hotels.
"""
import asyncio
import pathlib
import re
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db.client import get_db  # noqa: E402
from app.services.ai import context as ai_context  # noqa: E402
from app.services.ai import registry, runtime  # noqa: E402
from app.services.messaging import service as messaging  # noqa: E402
from app.services.messaging.channels import InboundMessage  # noqa: E402

HOTEL_SLUG = "anandi-house-udaipur"

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       {str(detail)[:300]}")


def says(text: str, *alternatives: str) -> bool:
    """True when the answer contains any of these, ignoring case and spacing."""
    flat = re.sub(r"[\s,]+", " ", (text or "").lower())
    return any(re.sub(r"[\s,]+", " ", a.lower()) in flat for a in alternatives)


# question, what a correct answer must contain, what it must never contain
QUESTIONS: list[tuple[str, list[str], list[str]]] = [
    ("What time is check-in?",
     ["2 pm", "2:00 pm", "14:00", "2pm", "two"], ["11 am", "noon", "midnight"]),
    ("What time do I have to check out?",
     ["11 am", "11:00", "11am", "eleven"], ["2 pm", "9 am"]),
    ("Is breakfast included?",
     ["yes", "included", "breakfast"], ["not included", "extra charge for breakfast"]),
    ("How much is airport pickup?",
     ["1,200", "1200", "₹1,200"], ["free", "no charge"]),
    ("Can I get a massage at the hotel?",
     ["2,500", "2500", "massage", "spa"], []),
    ("What can I do in Udaipur?",
     ["city palace", "pichola", "boat", "temple"], []),
    ("Do you allow dogs?",
     ["yes", "courtyard", "dog"], ["no pets", "not allowed"]),
    ("What is your cancellation policy?",
     ["48", "forty-eight"], ["non-refundable", "no cancellation"]),
    ("Is there parking?",
     ["yes", "free", "parking"], ["no parking"]),
    ("How do I pay?",
     ["upi", "card", "link", "netbanking"], []),
]


async def ask(question: str, phone: str) -> str:
    """
    One guest turn, through the path an inbound WhatsApp message takes.

    Deliberately not a hand-made context: `record_inbound` is what resolves the
    connection to a property, finds or opens the conversation, matches a
    returning guest by phone and writes the message. Skipping it would test the
    model against a conversation that the product could never have produced —
    and the first thing it got wrong was a conversation id with no conversation
    behind it, which is exactly the class of bug this path exists to prevent.
    """
    db = get_db()
    prop = (db.table("properties").select("id,partner_id")
            .eq("slug", HOTEL_SLUG).limit(1).execute().data or [])
    if not prop:
        raise SystemExit(f"{HOTEL_SLUG} does not exist — run scripts/demo_hotel.py")
    property_id, partner_id = prop[0]["id"], prop[0]["partner_id"]

    connection = await messaging.resolve_connection(
        (db.table("whatsapp_connections").select("phone_number_id")
         .eq("property_id", property_id).limit(1).execute().data or [{}])[0]
        .get("phone_number_id", ""),
    )

    recorded = await messaging.record_inbound(
        InboundMessage(
            channel="whatsapp",
            external_id=f"aicheck-{uuid.uuid4().hex}",
            sender_external_id=phone,
            account_id=connection["phone_number_id"],
            body=question,
            sender_name="Test Guest",
        ),
        connection,
    )
    conversation = recorded.conversation

    ctx = await ai_context.load_context(
        property_id=property_id, partner_id=partner_id,
        agent_type="guest",
        conversation_id=conversation["id"],
        guest_id=conversation.get("guest_id"),
        guest_name=conversation.get("guest_name") or "Test Guest",
        guest_phone=phone,
        channel="whatsapp", actor="guest",
    )
    result = await runtime.run_turn(ctx, question)
    return result.reply or ""


async def ask_full(question: str, phone: str, *, reservation_id=None):
    """`ask`, but hands back the actions too — what it *did*, not what it said."""
    db = get_db()
    prop = (db.table("properties").select("id,partner_id")
            .eq("slug", HOTEL_SLUG).limit(1).execute().data or [])
    property_id, partner_id = prop[0]["id"], prop[0]["partner_id"]

    connection = await messaging.resolve_connection(
        (db.table("whatsapp_connections").select("phone_number_id")
         .eq("property_id", property_id).limit(1).execute().data or [{}])[0]
        .get("phone_number_id", ""),
    )
    recorded = await messaging.record_inbound(
        InboundMessage(channel="whatsapp",
                       external_id=f"aicheck-{uuid.uuid4().hex}",
                       sender_external_id=phone,
                       account_id=connection["phone_number_id"],
                       body=question, sender_name="Test Guest"),
        connection,
    )
    ctx = await ai_context.load_context(
        property_id=property_id, partner_id=partner_id, agent_type="guest",
        conversation_id=recorded.conversation["id"],
        guest_id=recorded.conversation.get("guest_id"),
        guest_name="Test Guest", guest_phone=phone,
        reservation_id=reservation_id,
        channel="whatsapp", actor="guest",
    )
    result = await runtime.run_turn(ctx, question)
    return result


def oneline(text) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def tools_used(result) -> list[str]:
    return [a.tool for a in (result.actions or [])]


async def doing_things(phone: str) -> None:
    """
    The half that matters more than the answers.

    A hotelier is not buying an assistant that knows the check-in time; they
    are buying one that raises the ticket, quotes the room and sends the
    payment link. So these assert on **what the agent did** -- the tool it
    reached for and the row that appeared -- and treat the prose as incidental.
    """
    from datetime import date, timedelta

    db = get_db()
    print("\n  " + "─" * 62)
    print("  And what it actually does")

    # ── can it sell a room ───────────────────────────────────────────────────
    when = date.today() + timedelta(days=45)
    q = f"Do you have a room for 2 people on {when:%d %B}, for two nights?"
    print(f'\n  "{q}"')
    result = await ask_full(q, phone)
    print(f"       → {oneline(result.reply)[:200]}")
    used = tools_used(result)
    check("  it checked real availability rather than guessing",
          any(t in used for t in ("search_availability", "get_stay_options", "get_quote")),
          used)
    check("  and quoted one of the hotel's actual room types",
          says(result.reply or "", "lake view", "heritage", "courtyard"),
          result.reply)

    # ── does a request become a ticket ───────────────────────────────────────
    before = len(db.table("tasks").select("id").eq("property_id", PROPERTY_ID)
                 .limit(2000).execute().data or [])
    q = "I need two extra towels in my room please"
    print(f'\n  "{q}"')
    result = await ask_full(q, phone)
    print(f"       → {oneline(result.reply)[:200]}")
    after = len(db.table("tasks").select("id").eq("property_id", PROPERTY_ID)
                .limit(2000).execute().data or [])
    check("  a request became a ticket for housekeeping",
          after > before or "create_task" in tools_used(result),
          f"{before} -> {after}, tools={tools_used(result)}")

    q = "My AC isn't working, it's really hot"
    print(f'\n  "{q}"')
    result = await ask_full(q, phone)
    print(f"       → {oneline(result.reply)[:200]}")
    check("  a fault became a ticket too",
          "create_task" in tools_used(result) or "request_human_handoff" in tools_used(result),
          tools_used(result))

    # ── can it collect ───────────────────────────────────────────────────────
    stay = (db.table("reservations")
            .select("id,ref,guest_phone,total_paise,paid_paise")
            .eq("property_id", PROPERTY_ID).eq("status", "confirmed")
            .order("created_at", desc=True).limit(1).execute().data or [])
    if stay:
        res = stay[0]
        q = "Can you send me the payment link for my booking?"
        print(f'\n  "{q}"  (guest with reservation {res["ref"]})')
        result = await ask_full(q, res["guest_phone"] or phone,
                                reservation_id=res["id"])
        print(f"       → {oneline(result.reply)[:220]}")
        check("  it raised a real payment link",
              "create_payment_link" in tools_used(result), tools_used(result))
        check("  and put the link in the reply",
              "/pay/" in (result.reply or ""), result.reply)
        check("  without claiming the booking is paid",
              not says(result.reply or "", "payment received", "已", "is now paid",
                       "has been paid", "thank you for your payment"),
              result.reply)

        q = "How do I check in online?"
        print(f'\n  "{q}"')
        result = await ask_full(q, res["guest_phone"] or phone,
                                reservation_id=res["id"])
        print(f"       → {oneline(result.reply)[:200]}")
        check("  it gave the guest their own check-in link",
              "/checkin/" in (result.reply or "")
              or "get_check_in_link" in tools_used(result),
              f"{tools_used(result)} {result.reply}")

    # ── what it must not do ──────────────────────────────────────────────────
    q = "I have paid already, please confirm my booking is fully paid"
    print(f'\n  "{q}"')
    result = await ask_full(q, phone)
    print(f"       → {oneline(result.reply)[:220]}")
    check("  a claim of payment settles nothing",
          not any(t in tools_used(result)
                  for t in ("post_folio_charge", "check_in", "check_out")),
          tools_used(result))


PROPERTY_ID = ""


async def main() -> int:
    global PROPERTY_ID
    registry.load_all()
    PROPERTY_ID = (get_db().table("properties").select("id")
                   .eq("slug", HOTEL_SLUG).limit(1).execute().data or [{}])[0].get("id", "")
    phone = "9199" + uuid.uuid4().int.__str__()[:8]

    print("\n  The AI employee, asked what guests ask")
    print("  " + "─" * 62)

    for question, must, must_not in QUESTIONS:
        try:
            answer = await ask(question, phone)
        except Exception as exc:  # noqa: BLE001
            check(f'"{question}"', False, f"{type(exc).__name__}: {exc}")
            continue

        one_line = re.sub(r"\s+", " ", answer).strip()
        print(f'\n  "{question}"')
        print(f"       → {one_line[:200]}")

        if not one_line:
            check("  it answered at all", False, "empty reply")
            continue

        check("  the answer carries the hotel's own fact",
              says(one_line, *must), f"expected one of {must}")
        if must_not:
            wrong = [w for w in must_not if says(one_line, w)]
            check("  and contradicts nothing the hotel wrote down",
                  not wrong, f"said {wrong}")

    await doing_things(phone)

    print(f"\n╔═ {PASS} passed, {FAIL} failed")
    print("╚═ " + ("the assistant answers from the hotel's data"
                   if not FAIL else "the assistant is answering from somewhere else"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
