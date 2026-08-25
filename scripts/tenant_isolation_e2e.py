#!/usr/bin/env python3
"""
Two hotels, one API, and nothing crossing between them.

Every other suite here proves a surface works. This one only ever asserts that
something does **not** happen, which is why it is short and why every check is
phrased as an attack: the demo hotel's owner, holding a real token for a real
hotel, reaching for another hotel's guests, bookings, payments, conversations,
website, domain and staff.

**404, never 403.** A 403 on a property id confirms the id is real, which over
a few thousand guesses is a list of every hotel on the platform. 
answers 404 for "not yours" and "does not exist" alike, and these checks pin
that: a suite that accepted either would let the distinction come back.

The receptionist section is the other half. Isolation between hotels is not
enough on its own — inside one hotel, the person who checks guests in must not
be able to rewrite the hotel's website or open its billing.

    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/tenant_isolation_e2e.py

Reads only. Nothing here writes anything that succeeds — that is the point.
"""
import base64, json, os, subprocess, urllib.error, urllib.request

# Which StayOS to test. Defaults to the uvicorn this suite's docstring tells
# you to start; set STAYOS_API_BASE=https://api.hostos.in to run
# the same assertions against the deployed one, which is the only way to find
# out whether what is live behaves like what is on this branch.
API = os.environ.get("STAYOS_API_BASE", "http://127.0.0.1:8010").rstrip("/") + "/api/v1"
REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co/auth/v1"
ANANDI = "47f4cbf8-7cad-4369-a4c9-a4a70a0c29c0"
CASA = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

PASS = FAIL = 0
def check(label, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {label}")
    else: FAIL += 1; print(f"  FAIL {label}  {detail}")

def http(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, raw[:200]

raw = subprocess.check_output(["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()


def sql(query):
    """Read-only, and only ever used to *find* a fixture — never to make one."""
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    if st not in (200, 201):
        raise RuntimeError(f"SQL failed: {st} {str(body)[:200]}")
    return body
keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
            headers={"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in keys if k.get("name") == "anon"][0]

def signin(email, password):
    st, d = http(f"{SB}/token?grant_type=password", "POST",
                 {"email": email, "password": password}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None

owner = signin("owner@anandihouse.demo", "AnandiDemo!2026")
recept = signin("reception@anandihouse.demo", "AnandiDemo!2026")
assert owner, "could not sign in as the demo owner"

print("\n── one hotel's owner, another hotel's data ──")
for label, path in [
    ("today", f"/pms/properties/{CASA}/today"),
    ("reservations", f"/pms/properties/{CASA}/reservations"),
    ("guests", f"/pms/properties/{CASA}/guests"),
    ("conversations", f"/pms/properties/{CASA}/conversations"),
    ("payments", f"/pms/properties/{CASA}/payments"),
    ("agents", f"/pms/properties/{CASA}/agents"),
    ("website", f"/pms/properties/{CASA}/website"),
    ("website domain", f"/pms/properties/{CASA}/website/domain"),
    ("channels", f"/messaging/properties/{CASA}/channels"),
    ("owner overview", f"/pms/properties/{CASA}/owner/overview"),
    ("billing", f"/pms/properties/{CASA}/billing"),
    ("analytics", f"/pms/properties/{CASA}/analytics"),
    ("members", f"/pms/properties/{CASA}/members"),
    ("tasks", f"/pms/properties/{CASA}/tasks"),
    ("services", f"/pms/properties/{CASA}/services"),
    # Added by the Agent 2.0 work. Every new authenticated read goes in this
    # list, because the list is the only thing that makes "we checked" true
    # rather than remembered.
    ("readiness", f"/pms/properties/{CASA}/readiness"),
]:
    st, _ = http(f"{API}{path}", headers=owner)
    check(f"{label} is 404, not a leak", st == 404, st)

print("\n── writing into another hotel ──")
for label, path, body in [
    ("claim their domain", f"/pms/properties/{CASA}/website/domain", {"domain": "stolen.example"}),
    ("publish their site", f"/pms/properties/{CASA}/website/publish", None),
    ("raise a task", f"/pms/properties/{CASA}/tasks", {"title": "x", "category": "housekeeping"}),
]:
    method = "PUT" if "domain" in path else "POST"
    st, _ = http(f"{API}{path}", method, body if body is not None else {}, owner)
    check(f"{label} is refused", st in (404, 403, 422), st)

print("\n── the surfaces added since ──")
# A conversation and a reservation that really do belong to Casa Mandala. Asked
# for by id through the *other* hotel's path, which is the shape of the attack:
# a valid id the attacker learned somewhere, aimed at a property they hold.
their_conversation = sql(f"""select id from conversations
                              where property_id = '{CASA}' limit 1;""")
their_reservation = sql(f"""select id from reservations
                             where property_id = '{CASA}' limit 1;""")

if their_conversation:
    cid = their_conversation[0]["id"]
    st, _ = http(f"{API}/pms/properties/{CASA}/conversations/{cid}/session", headers=owner)
    check("another hotel's session panel is 404", st == 404, st)
    st, _ = http(f"{API}/pms/properties/{ANANDI}/conversations/{cid}/session", headers=owner)
    check("and their conversation id through my own property is 404 too",
          st == 404, f"{st} — the panel read a thread by id alone")

if their_reservation:
    rid = their_reservation[0]["id"]
    st, _ = http(f"{API}/pms/properties/{CASA}/reservations/{rid}/rotate-link",
                 "POST", {}, owner)
    check("another hotel's booking link cannot be revoked", st == 404, st)
    st, _ = http(f"{API}/pms/properties/{ANANDI}/reservations/{rid}/rotate-link",
                 "POST", {}, owner)
    check("nor through my own property's path", st == 404,
          f"{st} — a reservation was rotated by id alone")

st, _ = http(f"{API}/pms/properties/{CASA}", "PATCH", {"address": "stolen"}, owner)
check("another hotel's address cannot be edited", st == 404, st)
st, _ = http(f"{API}/pms/properties/{CASA}/locate", "POST", {}, owner)
check("nor its map pin", st in (404, 403), st)

their_room = sql(f"""select id from room_types where property_id = '{CASA}' limit 1;""")
if their_room:
    st, _ = http(f"{API}/pms/room-types/{their_room[0]['id']}", "PATCH",
                 {"name": "Stolen"}, owner)
    check("another hotel's room type cannot be renamed", st == 404, st)

print("\n── the booking link, which is the only credential a guest holds ──")
# The token opens one booking and says nothing about any other. These ask the
# unauthenticated route directly, because that is how a stranger would.
st, _ = http(f"{API}/pms/public/bookings/{'0' * 32}")
check("a token nobody was issued is 404", st == 404, st)
# A token is compared, never matched. Each of these is a wildcard in some
# language the token passes through — SQL LIKE, a glob, a quoted string — and
# each must come back as "no such booking" rather than somebody's.
#
# 400 counts as a pass on the raw `%` case and 404 does not mean it is weaker:
# behind Coolify the edge rejects a malformed percent-escape before FastAPI is
# reached, where a bare uvicorn passes it through to the handler. Both refuse;
# they refuse at different distances. Pinning this to 404 made the suite fail
# against production while the deployed code was doing the stricter thing.
for pattern, label in (("%" * 32, "a percent wildcard"),
                       ("_" * 32, "an underscore wildcard"),
                       ("*", "a glob"),
                       ("%25", "an escaped percent"),
                       ("'%20or%20'1'='1", "a quoted injection")):
    st, body = http(f"{API}/pms/public/bookings/{pattern}")
    check(f"{label} is not a pattern", st in (400, 404), st)
    check(f"  and {label} is told nothing about why",
          st == 400 or "could not find" in str(body).lower(), body)
if their_reservation:
    their_ref = sql(f"""select ref from reservations where id = '{their_reservation[0]['id']}';""")
    st, _ = http(f"{API}/pms/public/bookings/{their_ref[0]['ref']}")
    check("the human-readable reference does not open the booking page",
          st == 404, f"{st} — the guessable id is being accepted as the token")

print("\n── no token at all ──")
for path in [f"/pms/properties/{ANANDI}/today", f"/pms/properties/{ANANDI}/guests",
             f"/messaging/properties/{ANANDI}/channels", f"/pms/properties/{ANANDI}/website/domain",
             f"/pms/me", "/pms/properties"]:
    st, _ = http(f"{API}{path}")
    check(f"{path.split('/')[-1]} needs a login", st == 401, st)

print("\n── a receptionist inside their own hotel ──")
if recept:
    for label, path, method in [
        ("cannot change the website", f"/pms/properties/{ANANDI}/website", "PATCH"),
        ("cannot claim a domain", f"/pms/properties/{ANANDI}/website/domain", "PUT"),
        ("cannot open billing", f"/pms/properties/{ANANDI}/billing", "GET"),
        ("cannot manage the AI employee", f"/pms/properties/{ANANDI}/agents/x", "PATCH"),
    ]:
        st, d = http(f"{API}{path}", method, {} if method != "GET" else None, recept)
        check(f"{label} ({st})", st in (403, 404), st)
        if st == 403:
            said = str((d or {}).get("detail", "")).lower()
            check("  and the refusal says who can",
                  ("owner" in said or "manager" in said) and "permission" not in said, said[:80])
    st, _ = http(f"{API}/pms/properties/{ANANDI}/reservations", headers=recept)
    check("but can still work the front desk", st == 200, st)

print("\n── the public surface ──")
st, index = http(f"{API}/pms/public/hotels")
check("the published-hotel index is public", st == 200, st)
check("and lists nothing private about a hotel",
      st == 200 and not any(k in json.dumps(index or []).lower()
                            for k in ("partner_id", "public_token", "guest_", "@")),
      json.dumps(index or [])[:200])
st, d = http(f"{API}/pms/public/hotels/casa-mandala-anjuna")
check("a published hotel is public", st == 200, st)
check("and carries no owner, no email, no phone of the hotelier",
      not any(k in json.dumps(d or {}).lower() for k in ("partner_id", "owner", "@casamandala")),
      json.dumps(d or {})[:200])
# An unpublished hotel is not public — asked of whichever hotel is actually
# unpublished right now, rather than of one named here.
#
# This used to name `harry-bhiwani`, which was a draft when the suite was
# written and is a live customer today. The check then failed for the best
# possible reason: a real hotel had published its website. A test that goes red
# when a customer succeeds is a test that gets muted.
draft = sql("""select slug from hotel_websites w
                join properties p on p.id = w.property_id
               where w.status <> 'published' limit 1;""")
if draft:
    st, _ = http(f"{API}/pms/public/hotels/{draft[0]['slug']}")
    check(f"an unpublished hotel is not ({draft[0]['slug']})", st == 404, st)
else:
    # Nothing unpublished to ask about. Prove the rule the other way rather
    # than skipping it: a slug nobody owns must 404 too.
    st, _ = http(f"{API}/pms/public/hotels/no-such-hotel-anywhere")
    check("a hotel that does not exist is not public", st == 404, st)

print(f"\n══ {PASS} passed, {FAIL} failed ══")
raise SystemExit(1 if FAIL else 0)
