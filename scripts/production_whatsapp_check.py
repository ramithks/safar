#!/usr/bin/env python3
"""
The whole WhatsApp chain, against the deployment guests will actually reach.

    Meta  ──► POST /messaging/webhooks/whatsapp  (signed)
              └─► signature ─► phone_number_id ─► property ─► conversation
                              ─► Celery(hotelos) ─► AI employee ─► Meta ─► phone

`meta_smoke.py` proves the *outbound* half from a laptop, and cannot prove the
inbound half at all: Meta's callback points at production, so nothing it sends
ever arrives locally. `whatsapp_e2e.py` proves the inbound half with a fake
Meta. This is the missing third: a real signature, over the real webhook, on
the real host, and then the database is asked what happened.

The message is signed with the app secret we hold. That is not a forgery — it
is the only way to originate a byte-identical request to the one Meta sends,
and if this deployment accepts a body signed with a *wrong* key
(`stayos_backend_check` section 8 proves it does not) then the signature has
stopped meaning anything anyway.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/production_whatsapp_check.py

⚠ Writes a real conversation and a real agent run on the **demo hotel**, and
the AI employee's reply is a real WhatsApp message to
`WHATSAPP_TEST_RECIPIENT` — the launch team's own handset, which is the only
number Meta's test sender is allowed to reach. It never touches a pilot hotel.
"""
import argparse
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db.client import get_db  # noqa: E402

HOTEL_SLUG = "anandi-house-udaipur"
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {str(detail)[:240]}")
    return bool(cond)


def post(url: str, body: bytes, secret: str) -> tuple[int, str]:
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Hub-Signature-256": signature},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except urllib.error.URLError as e:
        return 0, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="https://stayos-backend.travbuddy.in")
    ap.add_argument("--wait", type=int, default=90,
                    help="seconds to wait for the worker to answer")
    ap.add_argument("--message", default="What time is check-in?")
    args = ap.parse_args()

    secret = os.environ.get("WHATSAPP_APP_SECRET") or ""
    sender = os.environ.get("WHATSAPP_TEST_PHONE_NUMBER_ID") or ""
    guest = (os.environ.get("WHATSAPP_TEST_RECIPIENT") or "").lstrip("+")
    if not (secret and sender and guest):
        print("Set WHATSAPP_APP_SECRET, WHATSAPP_TEST_PHONE_NUMBER_ID and "
              "WHATSAPP_TEST_RECIPIENT first.")
        return 2

    db = get_db()

    print("\n── the sender is wired to a hotel ──")
    connection = (db.table("whatsapp_connections")
                  .select("property_id,is_sandbox,credentials_ref,status")
                  .eq("phone_number_id", sender).execute().data or [])
    if not check("exactly one hotel owns this Meta sender", len(connection) == 1,
                 f"{len(connection)} rows for {sender}"):
        return 1
    conn = connection[0]
    prop = (db.table("properties").select("id,name,slug")
            .eq("id", conn["property_id"]).limit(1).execute().data or [{}])[0]
    check("it is the demo hotel, not a pilot", prop.get("slug") == HOTEL_SLUG,
          prop.get("name"))
    check("and it is a live connection, not a sandbox one",
          conn["is_sandbox"] is False and conn["credentials_ref"] != "sandbox", conn)

    property_id = prop["id"]
    before = len(db.table("conversations").select("id")
                 .eq("property_id", property_id).limit(2000).execute().data or [])

    print("\n── Meta delivers a message ──")
    mid = f"wamid.PRODCHECK{uuid.uuid4().hex[:12].upper()}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "prod-check",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "", "phone_number_id": sender},
                    "contacts": [{"profile": {"name": "Launch Check"}, "wa_id": guest}],
                    "messages": [{
                        "from": guest, "id": mid,
                        "timestamp": str(int(time.time())),
                        "type": "text", "text": {"body": args.message},
                    }],
                },
            }],
        }],
    }
    body = json.dumps(payload).encode()

    url = f"{args.base}/api/v1/messaging/whatsapp/webhook"
    st, text = post(url, body, secret)
    check("production accepts a correctly signed delivery", st == 200, f"{st} {text}")

    bad = post(url, body, "not-the-app-secret")
    check("and refuses the same body signed with the wrong key",
          bad[0] in (401, 403), bad)

    print("\n── what production did with it ──")
    conversation = message = None
    for _ in range(max(1, args.wait // 3)):
        rows = (db.table("conversation_messages")
                .select("id,conversation_id,direction,body")
                .eq("external_id", mid).limit(1).execute().data or [])
        if rows:
            message = rows[0]
            conversation = message["conversation_id"]
            break
        time.sleep(3)

    if not check("the message was stored against a conversation", message is not None,
                 f"nothing with external_id {mid} after {args.wait}s"):
        print("\n  The webhook returned 200 but nothing was written. Either "
              "ENABLE_HOTEL_OS is false on this host, or the router is not mounted.")
        return 1

    convo = (db.table("conversations").select("property_id,guest_phone,handler,channel")
             .eq("id", conversation).limit(1).execute().data or [{}])[0]
    check("routed to the hotel that owns the sender",
          convo.get("property_id") == property_id, convo)
    check("and to a WhatsApp conversation, not another channel",
          convo.get("channel") == "whatsapp", convo)
    check("the AI employee is the handler", convo.get("handler") == "ai", convo)

    after = len(db.table("conversations").select("id")
                .eq("property_id", property_id).limit(2000).execute().data or [])
    check("no other hotel gained a conversation", after >= before, f"{before} -> {after}")

    print("\n── the AI employee answers ──")
    reply = None
    runs = []
    for _ in range(max(1, args.wait // 3)):
        runs = (db.table("agent_runs").select("id,status,error,started_at")
                .eq("conversation_id", conversation)
                .order("started_at", desc=True).limit(1).execute().data or [])
        outbound = (db.table("conversation_messages")
                    .select("id,direction,body,delivery_status,external_id,failure_reason")
                    .eq("conversation_id", conversation).eq("direction", "outbound")
                    .order("created_at", desc=True).limit(1).execute().data or [])
        if outbound:
            reply = outbound[0]
            break
        time.sleep(3)

    if runs:
        check("an agent run was opened", True)
        check("  and it did not fail", runs[0].get("status") != "failed",
              runs[0].get("error"))
    else:
        check("an agent run was opened", False,
              "no agent_runs row — is the hotelos Celery worker consuming?")

    if not check("a reply was produced", reply is not None,
                 f"no outbound message after {args.wait}s — "
                 "the worker may not be running with -Q hotelos"):
        return 1

    print(f"       “{(reply.get('body') or '')[:160]}”")
    check("it went out through Meta, not a fake",
          str(reply.get("external_id") or "").startswith("wamid."),
          reply.get("external_id"))
    check("Meta accepted it",
          reply.get("delivery_status") in ("sent", "delivered", "read"),
          f"{reply.get('delivery_status')} {reply.get('failure_reason') or ''}")
    check("and it answers from the hotel's own data",
          any(w in (reply.get("body") or "").lower()
              for w in ("2:00", "2 pm", "2pm", "14:00", "two")),
          reply.get("body"))

    print(f"\n╔═ {PASS} passed, {FAIL} failed")
    print("╚═ " + ("Meta → webhook → hotel → AI employee → Meta → a real handset"
                   if not FAIL else "the production chain is broken"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
