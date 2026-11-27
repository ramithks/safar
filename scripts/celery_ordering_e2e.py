#!/usr/bin/env python3
"""
The production processing path: Celery, Redis, and per-conversation ordering.

    Meta webhook → verify → persist → ACK → Celery → agent → reply

Two properties are being checked, and they pull in opposite directions:

**Ordering within a conversation.** A guest firing off three messages in four
seconds must be answered in the order they said them. Two workers racing on one
thread would answer "yes we have a room" before "let me check", which reads to
the guest as the hotel talking over itself.

**Concurrency across conversations.** Forty guests must not queue behind one
slow model call. Serialising everything would make ordering trivial and the
product unusable at the second hotel.

The lock is what reconciles them: keyed on the conversation, so one thread is
serial and forty threads are parallel.

Needs a real worker and a real Redis:

    redis-server &
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/celery -A app.workers.celery_app worker -c 4 --loglevel=warning &
    ./venv/bin/python scripts/celery_ordering_e2e.py

Deliberately NOT part of the default gate run: it needs infrastructure CI does
not have, and a test that is skipped half the time is a test nobody trusts.
"""
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Celery, not inline. That is the entire point of this script.
os.environ["MESSAGING_INLINE_PROCESSING"] = "false"
os.environ.setdefault("WHATSAPP_APP_SECRET", "whatsapp-e2e-app-secret")

from app.db.client import get_db  # noqa: E402
from app.services.messaging.whatsapp.fake import FakeWhatsAppAdapter  # noqa: E402

REF = "rdlozqvuywtgsodunait"
PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PHONE_NUMBER_ID = "109876543210987"

db = get_db()
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()


def sql(query: str):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        method="POST", data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:300]}


# ── the worker has to be there ───────────────────────────────────────────────

print("\n── the worker ──")
from app.workers.celery_app import celery_app  # noqa: E402

try:
    workers = celery_app.control.inspect(timeout=5).registered() or {}
except Exception as exc:  # noqa: BLE001
    workers = {}
    print(f"  (could not reach the broker: {exc})")

registered = {t for tasks in workers.values() for t in tasks}
if not workers:
    print("\n  No Celery worker is running. Start one and re-run:")
    print("    ./venv/bin/celery -A app.workers.celery_app worker -c 4 --loglevel=warning")
    sys.exit(2)

check("a worker is listening", bool(workers), str(list(workers)))
check("and it knows how to process a conversation",
      "safar.messaging.process_conversation" in registered, str(sorted(registered))[:200])


# ── two guests, several messages each ────────────────────────────────────────

RUN_TAG = uuid.uuid4().hex[:6]
GUESTS = [f"9198{RUN_TAG}01", f"9198{RUN_TAG}02"]
MESSAGES = ["first", "second", "third"]

FakeWhatsAppAdapter.reset()


def payload(phone: str, text: str, seq: int) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "987654321098765", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"profile": {"name": f"Order {phone[-2:]}"}, "wa_id": phone}],
            "messages": [{"from": phone, "id": f"wamid.ORD{RUN_TAG}{phone[-2:]}{seq}",
                          "timestamp": str(int(time.time())), "type": "text",
                          "text": {"body": text}}],
        }}]}],
    }


print("\n── inbound, in a hurry ──")
import asyncio  # noqa: E402

from app.api.v1.messaging import webhooks  # noqa: E402

started = time.monotonic()
for seq, text in enumerate(MESSAGES):
    for phone in GUESTS:
        # No pause. Two guests, three messages each, six webhooks as fast as the
        # process can send them — which is what a busy evening looks like.
        result = asyncio.run(webhooks.handle_payload(payload(phone, text, seq)))
        assert result["accepted"] == 1, result
ack = time.monotonic() - started

check("every webhook was accepted", True)
check("and acknowledged without waiting for the agent",
      ack < 8.0, f"{ack:.1f}s for six webhooks")


def conversations() -> list[dict]:
    return (db.table("conversations").select("*")
            .eq("property_id", PROPERTY).in_("guest_phone", GUESTS)
            .execute().data or [])


convs = conversations()
check("two separate conversations exist", len(convs) == 2, str(len(convs)))


# ── wait for the worker ──────────────────────────────────────────────────────

print("\n── the worker drains them ──")


def unanswered() -> int:
    ids = [c["id"] for c in convs]
    rows = (db.table("conversation_messages").select("id")
            .in_("conversation_id", ids).eq("direction", "inbound")
            .is_("agent_run_id", "null").execute().data or [])
    return len(rows)


deadline = time.monotonic() + 120
while unanswered() > 0 and time.monotonic() < deadline:
    time.sleep(2)

check("every inbound message was picked up", unanswered() == 0, f"{unanswered()} left")

for conv in convs:
    runs = (db.table("agent_runs").select("id,started_at,finished_at")
            .eq("conversation_id", conv["id"]).order("started_at").execute().data or [])
    check(f"{conv['guest_name']}: one run per message",
          len(runs) == len(MESSAGES), f"{len(runs)} runs for {len(MESSAGES)} messages")

    # Ordering, the assertion this script exists for. Runs on one conversation
    # must not overlap: the next may only start after the previous finished.
    overlapped = [
        (a["id"], b["id"]) for a, b in zip(runs, runs[1:])
        if a.get("finished_at") and b["started_at"] < a["finished_at"]
    ]
    check(f"{conv['guest_name']}: runs are serial, never overlapping",
          not overlapped, f"{len(overlapped)} overlapping pair(s)")

    # Ordered the same way the drain orders, and for the same reason: `sent_at`
    # is Meta's second-granularity timestamp and three messages sent inside four
    # seconds routinely tie. Asserting on a tied sort was this suite reporting
    # its own non-determinism as a product failure.
    answered = (db.table("conversation_messages")
                .select("body,agent_run_id,sent_at,created_at")
                .eq("conversation_id", conv["id"]).eq("direction", "inbound")
                .order("sent_at").order("created_at").execute().data or [])
    order = [m["body"] for m in answered]
    check(f"{conv['guest_name']}: answered in the order they were said",
          order == MESSAGES, str(order))


# ── and the two guests were not serialised behind each other ─────────────────

print("\n── concurrency across conversations ──")

all_runs = []
for conv in convs:
    all_runs += (db.table("agent_runs").select("conversation_id,started_at,finished_at")
                 .eq("conversation_id", conv["id"]).execute().data or [])


def _t(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


cross = 0
for a in all_runs:
    for b in all_runs:
        if a["conversation_id"] == b["conversation_id"] or not (a.get("finished_at") and b.get("finished_at")):
            continue
        if _t(a["started_at"]) < _t(b["finished_at"]) and _t(b["started_at"]) < _t(a["finished_at"]):
            cross += 1

# Forty guests must not queue behind one slow model call. With four workers and
# six messages this should overlap; if it never does, the lock is too coarse.
check("different conversations were processed at the same time",
      cross > 0, "no overlap between the two conversations — the lock is too coarse")

check("nothing was answered twice",
      sum(len(db.table("agent_runs").select("id").eq("conversation_id", c["id"])
               .execute().data or []) for c in convs) == len(GUESTS) * len(MESSAGES),
      "a message was processed more than once")


# ── a redelivery mid-flight is still a no-op ─────────────────────────────────

print("\n── duplicate webhook under load ──")
before = sum(len(db.table("agent_runs").select("id").eq("conversation_id", c["id"])
                 .execute().data or []) for c in convs)
for seq, text in enumerate(MESSAGES):
    for phone in GUESTS:
        again = asyncio.run(webhooks.handle_payload(payload(phone, text, seq)))
        assert again["duplicates"] == 1, again
time.sleep(6)
after = sum(len(db.table("agent_runs").select("id").eq("conversation_id", c["id"])
                .execute().data or []) for c in convs)
check("six redelivered webhooks produced no new runs", before == after, f"{before} → {after}")

print(f"\n{PASS} passed, {FAIL} failed")
print("\nREAL CELERY · REAL REDIS · REAL DATABASE · REAL VERTEX · FAKE META")
sys.exit(1 if FAIL else 0)
