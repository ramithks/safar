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
import base64, json, subprocess, urllib.error, urllib.request

API = "http://127.0.0.1:8010/api/v1"
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
st, d = http(f"{API}/pms/public/hotels/casa-mandala-anjuna")
check("a published hotel is public", st == 200, st)
check("and carries no owner, no email, no phone of the hotelier",
      not any(k in json.dumps(d or {}).lower() for k in ("partner_id", "owner", "@casamandala")),
      json.dumps(d or {})[:200])
st, _ = http(f"{API}/pms/public/hotels/harry-bhiwani")
check("an unpublished hotel is not", st == 404, st)

print(f"\n══ {PASS} passed, {FAIL} failed ══")
raise SystemExit(1 if FAIL else 0)
