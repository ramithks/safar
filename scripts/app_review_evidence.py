#!/usr/bin/env python3
"""
The two things Meta's App Review wants to watch you do.

Advanced access to `whatsapp_business_messaging` and `whatsapp_business_management`
is granted on the evidence of a **video**, not of a working system. Meta asks
for two, and they are graded on what is visible on screen:

    --messaging    your app sends a message, and the same message arrives on a
                   WhatsApp client that is also on screen
    --management   your app creates a message template through the API

So this script is written to be *watched*. Every step prints what it is about to
do, the actual request, and Meta's actual answer, with a pause between them long
enough to read. Nothing is hidden behind a spinner and nothing succeeds
silently — a reviewer who cannot see the API call cannot grant the permission.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/app_review_evidence.py --management
    ./venv/bin/python scripts/app_review_evidence.py --messaging

`docs/app_review_shotlist.md` says what to have on screen while each one runs.

── what this does to real systems ───────────────────────────────────────────

**It sends a real WhatsApp message** to `WHATSAPP_TEST_RECIPIENT` — the launch
team's own handset, the same single recipient `meta_smoke.py` uses, and there
is deliberately no way to pass a different number. **It creates a real template**
on the WABA named by `--waba`, which then exists on that account until deleted.
Both are the point: a video of a fake is not evidence.

It never touches a pilot hotel, and it never writes to `whatsapp_connections`.

── secrets ──────────────────────────────────────────────────────────────────

Tokens are never echoed, not even truncated. The recipient is masked. Message
ids and template ids appear in full, because they are not secret and they are
the whole reason a reviewer believes the call happened.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.messaging.whatsapp import template_api  # noqa: E402
from app.services.messaging.whatsapp.onboarding import OnboardingError  # noqa: E402

# The template the management video creates. Suffixed with the date so a second
# recording does not collide with the first — Meta locks a template name for 30
# days after deletion, so reusing one name makes the video unrepeatable.
TEMPLATE_PREFIX = "staysuite_review_demo"

BEAT = 2.5  # seconds between steps, so a viewer can read each one


def say(step: int, what: str) -> None:
    print(f"\n\033[1m── {step}. {what}\033[0m", flush=True)


def show(label: str, value) -> None:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, indent=2)
    print(f"   {label}:\n{_indent(str(value))}", flush=True)


def _indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


def beat() -> None:
    time.sleep(BEAT)


def mask(number: str) -> str:
    """Enough to recognise your own handset, not enough to redial it."""
    digits = "".join(c for c in number if c.isdigit())
    return f"…{digits[-4:]}" if len(digits) > 4 else "…"


def token() -> str:
    value = settings.whatsapp_access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not value:
        sys.exit("WHATSAPP_ACCESS_TOKEN is not set. Run: set -a; . ./.env.hotelos; set +a")
    return value


def waba_id(argument: str | None) -> str:
    """
    The WhatsApp Business Account to act on.

    **Not discoverable from the token.** A system-user token cannot enumerate
    the businesses it belongs to — `me/businesses` and
    `assigned_whatsapp_business_accounts` both come back empty — so this has to
    be supplied. It is on the Meta App Dashboard under WhatsApp → API Setup,
    labelled "WhatsApp Business Account ID", directly under the phone number id.
    """
    value = argument or settings.whatsapp_waba_id or os.environ.get("WHATSAPP_WABA_ID")
    if not value:
        sys.exit(
            "No WABA id.\n\n"
            "  Pass --waba <id>, or set WHATSAPP_WABA_ID in .env.hotelos.\n\n"
            "  Find it at: Meta App Dashboard → WhatsApp → API Setup →\n"
            "  'WhatsApp Business Account ID', just under the phone number id.\n"
        )
    return value


# ── whatsapp_business_management ─────────────────────────────────────────────


async def management(waba: str) -> int:
    """
    Create a message template through the API, on camera.

    This is the *whole* permission being demonstrated. A template that already
    exists proves nothing; the reviewer has to watch one come into being, which
    is why the name carries today's date and why the listing before and after
    are both shown.
    """
    access = token()
    name = f"{TEMPLATE_PREFIX}_{time.strftime('%Y%m%d_%H%M%S')}"

    print("\n\033[1mwhatsapp_business_management — creating a message template\033[0m")
    print(f"   WhatsApp Business Account: {waba}")
    print(f"   Graph API: {settings.whatsapp_api_base}")
    beat()

    say(1, "What templates exist on this account right now")
    try:
        before = await template_api.listing(waba_id=waba, token=access)
    except OnboardingError as exc:
        print(f"\n   \033[31mMeta refused the read: {exc}\033[0m")
        return 1
    show("GET", f"{settings.whatsapp_api_base}/{waba}/message_templates")
    show("templates", [f"{t['name']} ({t['status']})" for t in before] or "none yet")
    beat()

    say(2, "The template we are about to create")
    components = [{
        "type": "BODY",
        "text": ("Hello {{1}}, your booking at {{2}} is confirmed. "
                 "We look forward to welcoming you."),
        "example": {"body_text": [["Ravi", "Anandi House"]]},
    }]
    body = {"name": name, "language": "en", "category": "UTILITY",
            "components": components}
    show("POST", f"{settings.whatsapp_api_base}/{waba}/message_templates")
    show("body", body)
    beat()

    say(3, "Sending it to Meta")
    try:
        created = await template_api.create(
            waba_id=waba, token=access, name=name,
            language="en", category="utility", components=components,
        )
    except OnboardingError as exc:
        print(f"\n   \033[31mMeta refused: {exc}\033[0m")
        return 1
    show("Meta replied", created)
    print(f"\n   \033[32m✓ template created — id {created.get('id')}\033[0m")
    beat()

    say(4, "Reading it back, so it is visibly on the account")
    found = await template_api.find(waba_id=waba, token=access, name=name)
    if not found:
        print("\n   \033[31mThe template was created but does not list. "
              "Meta is sometimes a few seconds behind — try the read again.\033[0m")
        return 1
    show("GET", f"{settings.whatsapp_api_base}/{waba}/message_templates")
    show(found["name"], {"status": found.get("status"),
                         "category": found.get("category"),
                         "language": found.get("language")})

    print(f"\n\033[1;32m══ whatsapp_business_management demonstrated ══\033[0m")
    print(f"   Created {name} on WABA {waba}.")
    print("   Status will be PENDING — Meta reviews templates, and the video "
          "only has to show the call succeeding.\n")
    return 0


# ── whatsapp_business_messaging ──────────────────────────────────────────────


async def messaging(wait: int) -> int:
    """
    Send a real WhatsApp message, on camera, to a handset that is also on camera.

    Meta's instruction is explicit that both halves must be visible: the app
    sending, and WhatsApp receiving. This half is ours; the shot list covers
    the other.
    """
    access = token()
    recipient = settings.whatsapp_test_recipient
    phone_number_id = (settings.whatsapp_phone_number_id
                       or settings.whatsapp_test_phone_number_id)

    if not recipient:
        sys.exit("WHATSAPP_TEST_RECIPIENT is not set — there is no number to message.")
    if not phone_number_id:
        sys.exit("WHATSAPP_PHONE_NUMBER_ID is not set — there is nothing to send from.")

    print("\n\033[1mwhatsapp_business_messaging — sending a message\033[0m")
    print(f"   From phone number id: {phone_number_id}")
    print(f"   To: {mask(recipient)}")
    beat()

    stamp = time.strftime("%H:%M:%S")
    text = (f"StaySuite App Review demonstration — {stamp}. "
            "This message was sent by our application through the WhatsApp Cloud API.")

    say(1, "The message we are about to send")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    show("POST", f"{settings.whatsapp_api_base}/{phone_number_id}/messages")
    show("body", {**payload, "to": mask(recipient)})
    beat()

    say(2, "Sending it")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.whatsapp_api_base.rstrip('/')}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access}"},
            json=payload,
        )
    try:
        answer = resp.json()
    except Exception:  # noqa: BLE001
        answer = {"raw": resp.text[:400]}

    if resp.status_code >= 400:
        print(f"\n   \033[31mMeta returned {resp.status_code}\033[0m")
        show("error", answer)
        code = ((answer.get("error") or {}).get("code"))
        if code == 131030:
            print("\n   131030 — the recipient is not on this number's allowed list.\n"
                  "   Add it at Meta → WhatsApp → API Setup → 'To'.")
        return 1

    # Meta echoes the recipient back in `contacts`. Masked here for the same
    # reason it is masked above — this output is recorded and submitted.
    shown = dict(answer)
    if shown.get("contacts"):
        shown["contacts"] = [
            {**c, "input": mask(str(c.get("input", ""))),
             "wa_id": mask(str(c.get("wa_id", "")))}
            for c in shown["contacts"]
        ]
    show("Meta replied", shown)
    message_id = ((answer.get("messages") or [{}])[0]).get("id")
    print(f"\n   \033[32m✓ accepted by Meta — message id {message_id}\033[0m")
    print("\n   \033[1mThe message is on its way. Show the handset now.\033[0m")
    beat()

    if wait <= 0:
        print("\n\033[1;32m══ whatsapp_business_messaging demonstrated ══\033[0m\n")
        return 0

    say(3, f"Waiting up to {wait}s for Meta's delivery receipt on our webhook")
    print("   This is the inbound half — proof the same message reached the "
          "handset, recorded by our own system rather than claimed.")

    db = get_db()
    deadline = time.time() + wait
    seen = None
    while time.time() < deadline:
        rows = (
            db.table("conversation_messages")
            .select("status,external_id,sent_at,delivered_at")
            .eq("external_id", message_id).limit(1).execute().data or []
        )
        if rows and rows[0].get("status") in ("delivered", "read"):
            seen = rows[0]
            break
        await asyncio.sleep(3)

    if seen:
        show("our record", seen)
        print(f"\n   \033[32m✓ delivery confirmed by webhook — status "
              f"{seen['status']}\033[0m")
    else:
        # Not a failure of the permission being demonstrated. The message was
        # accepted by Meta and that is what the video needs; the receipt only
        # arrives if this deployment's webhook is the one Meta is calling.
        print("\n   \033[33mNo delivery receipt arrived in time.\033[0m")
        print("   The send succeeded — this half only works when Meta's webhook "
              "points at this deployment. It is not part of what the video needs.")

    print("\n\033[1;32m══ whatsapp_business_messaging demonstrated ══\033[0m\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--messaging", action="store_true",
                      help="send a real message (whatsapp_business_messaging)")
    mode.add_argument("--management", action="store_true",
                      help="create a real template (whatsapp_business_management)")
    ap.add_argument("--waba", help="WhatsApp Business Account id, for --management")
    ap.add_argument("--wait", type=int, default=60,
                    help="seconds to wait for a delivery receipt (0 to skip)")
    ap.add_argument("--fast", action="store_true",
                    help="no pauses between steps — for a dry run, not for the video")
    args = ap.parse_args()

    global BEAT
    if args.fast:
        BEAT = 0

    if args.management:
        return asyncio.run(management(waba_id(args.waba)))
    return asyncio.run(messaging(args.wait))


if __name__ == "__main__":
    sys.exit(main())
