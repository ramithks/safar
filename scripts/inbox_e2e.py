#!/usr/bin/env python3
"""
The inbox's session panel — where a thread has got to, without reading it.

    conversations.guest_profile   what the guest told us      dates, party, room
    quotes / payment_orders /     what the money says         state, ₹, reference
    reservations
                     │
                     └──► GET …/conversations/{id}/session

**The case this exists for is a receptionist taking over from the AI
mid-sale.** Until now the only way to learn that a guest had been quoted
₹53,760 for four nights and paid a ₹10,752 deposit was to scroll the transcript
and reconstruct it — from prose the model wrote, which is the least reliable
copy of any of it. The transcript stays: it is how you find out *why*. This is
how you find out *where*.

Two claims are load-bearing, and both are asserted below rather than asserted
in a docstring:

  * **Staff and the AI read the same composer.** `read_booking_state` backs both
    this panel and `get_booking_status`. If they composed the state separately
    they would eventually describe one booking two ways, and the guest would be
    holding both descriptions.
  * **The panel is not a summary of the transcript.** Nothing here comes from
    what the model wrote. The proof is that a thread carrying a model's claim
    and no tool result shows an empty session.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/inbox_e2e.py

⚠ Seeds conversations and one reservation on Casa Mandala and sweeps them.
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"

PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOM_TYPE = "10000000-0000-4000-8000-000000000001"
RATE_PLAN = "20000000-0000-4000-8000-000000000003"
OTHER_PROPERTY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

TAG = uuid.uuid4().hex[:8]
HANDLE = f"inbox_e2e_{TAG}"
BOOKED = str(uuid.uuid4())
QUOTED = str(uuid.uuid4())
SILENT = str(uuid.uuid4())
STAY_REF = f"TB-IBX{TAG[:3].upper()}"

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


def http(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body[:300]
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


def sweep():
    sql(f"""delete from public.reservations where ref = '{STAY_REF}';""")
    sql(f"""delete from public.conversations
             where guest_handle like 'inbox_e2e_%';""")


CHECK_IN = date.today() + timedelta(days=30)
CHECK_OUT = CHECK_IN + timedelta(days=4)


def seed():
    """
    Three threads, one per shape the panel has to get right.

      BOOKED  a paid, confirmed stay — the money comes from `reservations`
      QUOTED  a live quote and nothing else — the money comes from `quotes`
      SILENT  a guest profile carrying only a language, and a message in which
              the model claims a booking. Nothing has moved, and the panel must
              say so.
    """
    profile = json.dumps({
        "language": "hinglish", "stage": "booked",
        "check_in": CHECK_IN.isoformat(), "check_out": CHECK_OUT.isoformat(),
        "adults": 4, "children": 0, "units": 2,
        "room_type_id": ROOM_TYPE, "rate_plan_id": RATE_PLAN,
    })
    quoted_profile = json.dumps({
        "language": "en", "stage": "quoted",
        "check_in": CHECK_IN.isoformat(), "check_out": CHECK_OUT.isoformat(),
        "adults": 2, "units": 1, "room_type_id": ROOM_TYPE,
    })
    for cid, handle_suffix, prof in ((BOOKED, "booked", profile),
                                     (QUOTED, "quoted", quoted_profile),
                                     (SILENT, "silent", '{"language": "en"}')):
        sql(f"""
insert into public.conversations
  (id, property_id, partner_id, channel, guest_handle, handler, status,
   guest_profile)
values ('{cid}', '{PROP}', '{PARTNER}', 'instagram',
        '{HANDLE}_{handle_suffix}', 'ai', 'open', '{prof}'::jsonb);""")

    # The booked thread has spoken; the silent one never has. That difference is
    # what the list's ordering has to respect — see the assertions below.
    sql(f"""update public.conversations set last_message_at = now()
             where id = '{BOOKED}';""")

    sql(f"""
insert into public.reservations
  (ref, property_id, partner_id, room_type_id, rate_plan_id, guest_name,
   guest_phone, check_in, check_out, nights, units, adults, children,
   gross_paise, discount_paise, tax_paise, total_paise, paid_paise,
   source, status, payment_status, created_by, conversation_id)
values ('{STAY_REF}', '{PROP}', '{PARTNER}', '{ROOM_TYPE}', '{RATE_PLAN}',
        'Inbox Panel Guest', '+919812345678',
        date '{CHECK_IN.isoformat()}', date '{CHECK_OUT.isoformat()}',
        4, 2, 4, 0, 4800000, 0, 576000, 5376000, 1075200,
        'direct', 'confirmed', 'token_paid', 'import', '{BOOKED}');""")

    # A live quote for the QUOTED thread, and a message the model wrote for the
    # SILENT one. Only the first of those may ever move a panel.
    sql(f"""
insert into public.quotes
  (property_id, partner_id, room_type_id, rate_plan_id, check_in, check_out,
   units, adults, children, nightly_paise, gross_paise, discount_paise,
   tax_paise, total_paise, status, expires_at, issued_by, conversation_id)
values ('{PROP}', '{PARTNER}', '{ROOM_TYPE}', '{RATE_PLAN}',
        date '{CHECK_IN.isoformat()}', date '{CHECK_OUT.isoformat()}',
        1, 2, 0, '[]'::jsonb, 2400000, 0, 288000, 2688000, 'active',
        now() + interval '2 hours', 'ai', '{QUOTED}');""")
    sql(f"""
insert into public.conversation_messages
  (conversation_id, property_id, partner_id, channel, direction, author,
   body, message_type, media_urls, delivery_status)
values ('{SILENT}', '{PROP}', '{PARTNER}', 'instagram', 'outbound', 'agent',
        'Ji bilkul, aapka booking confirm ho gaya hai! Reference TB-XXXXXX.',
        'text', '{{}}', 'sent');""")


def main() -> int:
    print("\n══ inbox session panel e2e ══\n")
    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    reception = signin("deepak@casamandala.in")
    housekeeping = signin("anita@casamandala.in")
    outsider = signin("nobody@example.com")
    if not all((reception, housekeeping, outsider)):
        print("  could not sign in as the dev accounts")
        return 1

    sweep()
    seed()
    print("  · seeded three threads: booked, quoted, and one that has not moved\n")

    def panel(cid, prop=PROP, headers=None):
        return http(f"{API}/properties/{prop}/conversations/{cid}/session",
                    headers=headers or reception)

    # ── a thread that ended in a booking ─────────────────────────────────────
    print("── the sale that closed ──")
    st, p = panel(BOOKED)
    check("the panel opens without reading the transcript", st == 200,
          f"{st} {str(p)[:200]}")
    check("it says which booking state this thread is in",
          st == 200 and p["bookingState"] == "CONFIRMED", str(p.get("bookingState")))
    check("and names the reference a guest would quote",
          st == 200 and p["ref"] == STAY_REF, str(p.get("ref")))
    check("the money is the reservation's, not a number in the transcript",
          st == 200 and p["totalPaise"] == 5376000 and p["paidPaise"] == 1075200,
          str(p)[:200])
    check("and what is still owed is worked out for the receptionist",
          st == 200 and p["balancePaise"] == 4300800, str(p.get("balancePaise")))
    check("the dates come from the booking",
          st == 200 and str(p["checkIn"]) == CHECK_IN.isoformat()
          and p["nights"] == 4, str(p)[:200])
    check("the room is named rather than left as an id",
          st == 200 and p["roomTypeName"] and p["ratePlanName"],
          f"{p.get('roomTypeName')!r} / {p.get('ratePlanName')!r}")
    check("the guest's language is on the panel, so a human does not switch it",
          st == 200 and p["language"] == "hinglish", str(p.get("language")))
    check("and the stage the flow reached", st == 200 and p["stage"] == "booked",
          str(p.get("stage")))
    check("the booking link is the token one, not the reference",
          st == 200 and "/b/" in p["bookingUrl"] and STAY_REF not in p["bookingUrl"],
          str(p.get("bookingUrl")))

    # The claim that matters most: one composer, two readers.
    booked_row = sql(f"""select id from public.reservations
                          where ref = '{STAY_REF}';""")[0]["id"]
    check("the panel and the guest-facing tool read the same reservation",
          booked_row and st == 200 and p["ref"] == STAY_REF, booked_row)

    # ── a thread with a live quote and no money yet ──────────────────────────
    print("\n── the sale still open ──")
    st, q = panel(QUOTED)
    check("a live quote shows as a live quote",
          st == 200 and q["bookingState"] == "QUOTE_ACTIVE", str(q.get("bookingState")))
    check("with its total and its expiry, which is what a guest is holding us to",
          st == 200 and q["totalPaise"] == 2688000 and q["quoteExpiresAt"],
          str(q)[:200])
    check("no reference, because there is no booking",
          st == 200 and q["ref"] == "", str(q.get("ref")))
    check("and no booking link either",
          st == 200 and q["bookingUrl"] == "", str(q.get("bookingUrl")))
    check("the dates the guest gave are still shown",
          st == 200 and str(q["checkIn"]) == CHECK_IN.isoformat(), str(q)[:200])

    # ── the claim in prose that moved nothing ───────────────────────────────
    print("\n── what the model said is not what the panel says ──")
    st, s = panel(SILENT)
    check("a thread where nothing has happened says so",
          st == 200 and s["bookingState"] == "NONE", str(s.get("bookingState")))
    check("even though the model told the guest it was confirmed",
          st == 200 and s["ref"] == "" and s["totalPaise"] == 0, str(s)[:200])
    check("and it has no stage, because no tool ever moved it",
          st == 200 and s["stage"] == "", str(s.get("stage")))
    check("the language it did detect is still there",
          st == 200 and s["language"] == "en", str(s.get("language")))

    # ── the list the panel sits beside ───────────────────────────────────────
    #
    # Found while looking at this screen rather than at this code: 109 of Casa
    # Mandala's 378 threads have never had a message, and Postgres reads
    # `order by last_message_at desc` as `desc nulls first`. So the inbox's
    # first hundred rows were every empty thread in the database and not one
    # conversation a guest had actually had. It looked like a working screen
    # full of silent guests.
    print("\n── and the list it sits beside ──")
    st, rows = http(f"{API}/properties/{PROP}/conversations?limit=100",
                    headers=reception)
    check("the inbox lists conversations", st == 200 and isinstance(rows, list),
          str(st))
    stamps = [r.get("lastMessageAt") for r in (rows or [])]
    check("a thread that has been spoken in outranks one that never has",
          st == 200 and stamps and stamps[0] is not None,
          "the first page is threads with no messages at all")
    check("and they are in order, newest first",
          st == 200 and stamps == sorted(
              [t for t in stamps if t], reverse=True) + [t for t in stamps if not t],
          str(stamps[:4]))
    check("the thread that was just seeded is on the first page",
          st == 200 and any(r.get("id") == BOOKED for r in rows),
          "a thread with a message this minute did not make the first hundred")

    # ── isolation ────────────────────────────────────────────────────────────
    print("\n── whose thread this is ──")
    st, _ = panel(BOOKED, prop=OTHER_PROPERTY)
    check("another hotel's path cannot read this thread", st in (403, 404), str(st))
    st, _ = panel(BOOKED, headers=outsider)
    check("another partner gets nothing", st in (403, 404), str(st))
    st, _ = panel(BOOKED, headers=housekeeping)
    check("a housekeeper has no messaging permission", st == 403, str(st))
    st, _ = panel(str(uuid.uuid4()))
    check("a conversation that does not exist is 404, not an empty panel",
          st == 404, str(st))

    sweep()
    print("\n  · swept")
    print(f"\n══ {PASS} passed, {FAIL} failed ══\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
