#!/usr/bin/env python3
"""
A hotel points its own address at StayOS.

    claim ──► two DNS records ──► check ──► still pending ──► verified
                                                                │
    a guest's browser on www.hotel.com  ◄── by-host ────────────┘

Runs against the **demo hotel** and a domain nobody owns, so nothing here can
touch a real hotel's live address. The verified state is reached by writing the
row directly — the alternative is owning a domain and waiting on DNS, and what
this suite is proving is the state machine and the host resolution, not that
Cloudflare answers queries.

    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/domain_e2e.py
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:8010/api/v1"
REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co/auth/v1"
HOTEL = "47f4cbf8-7cad-4369-a4c9-a4a70a0c29c0"
SLUG = "anandi-house-udaipur"
OWNER_EMAIL = "owner@anandihouse.demo"
RECEPTION_EMAIL = "reception@anandihouse.demo"
PASSWORD = "AnandiDemo!2026"

# Reserved by RFC 2606 for exactly this: it can never be registered, so no test
# run can collide with a real hotel's address.
TEST_DOMAIN = "bookings.anandi-house-qa.example"

PASS = FAIL = 0
STEP = 0


def http(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()


def sql(query):
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
    st, d = http(f"{SB}/token?grant_type=password", "POST",
                 {"email": email, "password": PASSWORD}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


def section(title):
    global STEP
    STEP += 1
    print(f"\n── {STEP}. {title} " + "─" * max(0, 56 - len(title)))


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def cleanup():
    sql(f"""update hotel_websites set domain=null, domain_token=null,
            domain_status='none', domain_verified_at=null, domain_error=null,
            domain_checked_at=null
            where property_id='{HOTEL}';""")


def main():
    st, _ = http(f"{API}/health")
    if st != 200:
        print("The API is not running on :8010.")
        return 2

    owner = signin(OWNER_EMAIL)
    if not owner:
        print("Could not sign in as the demo owner.")
        return 2

    cleanup()
    base = f"{API}/pms/properties/{HOTEL}/website/domain"

    section("nothing claimed")
    st, d = http(base, headers=owner)
    check("the panel answers", st == 200, st)
    check("with no address", (d or {}).get("status") == "none")
    check("and no records to copy", not (d or {}).get("records"))

    section("what may not be claimed")
    for bad, why in [
        ("partner.travbuddy.in", "a TravBuddy host"),
        ("not-a-domain", "not a hostname"),
        ("", "empty"),
        ("http://", "a scheme and nothing else"),
    ]:
        st, d = http(base, "PUT", {"domain": bad}, owner)
        check(f"{why} is refused", st == 422, st)
        check("  and the refusal is a sentence, not a rule name",
              isinstance((d or {}).get("detail"), str)
              and "regex" not in str(d).lower() and "pattern" not in str(d).lower())

    section("claiming it")
    st, d = http(base, "PUT", {"domain": f"  HTTPS://{TEST_DOMAIN.upper()}/rooms  "}, owner)
    check("a pasted browser address is accepted", st == 200, d)
    check("and stored as a hostname", (d or {}).get("domain") == TEST_DOMAIN, d)
    check("the state is pending, not live", (d or {}).get("status") == "pending")
    records = {r["type"]: r for r in (d or {}).get("records") or []}
    check("two records are handed over", set(records) == {"TXT", "CNAME"}, list(records))
    check("the TXT record proves ownership",
          records.get("TXT", {}).get("name") == f"_travbuddy.{TEST_DOMAIN}")
    check("its value is something we generated",
          str(records.get("TXT", {}).get("value", "")).startswith("travbuddy-verify="))
    check("neither is in DNS yet", not any(r["found"] for r in records.values()))
    token = records["TXT"]["value"]

    section("the token survives a re-save")
    st, d = http(base, "PUT", {"domain": TEST_DOMAIN}, owner)
    same = next(r for r in d["records"] if r["type"] == "TXT")["value"]
    check("re-claiming the same domain keeps the token", same == token,
          "a new token silently invalidates a record the owner already added")

    section("checking before the records exist")
    st, d = http(f"{base}/verify", "POST", {}, owner)
    check("the check answers 200, not an error", st == 200, st)
    check("and honestly says it is still pending", (d or {}).get("status") == "pending", d)
    check("nothing was marked verified", not (d or {}).get("verifiedAt"))

    section("an unverified domain serves nothing")
    st, _ = http(f"{API}/pms/public/sites/by-host?host={TEST_DOMAIN}")
    check("host resolution refuses a merely-claimed domain", st == 404, st)

    section("verified")
    sql(f"""update hotel_websites set domain_status='verified', domain_verified_at=now()
            where property_id='{HOTEL}';""")
    st, d = http(f"{API}/pms/public/sites/by-host?host={TEST_DOMAIN}")
    check("now the hostname resolves to the hotel", st == 200, d)
    check("and names the right one", (d or {}).get("slug") == SLUG, d)
    check("case does not matter to a browser",
          http(f"{API}/pms/public/sites/by-host?host={TEST_DOMAIN.upper()}")[0] == 200)
    st, d = http(f"{API}/pms/properties/{HOTEL}/website", headers=owner)
    check("the builder now prints the hotel's own address",
          (d or {}).get("domain") == TEST_DOMAIN, (d or {}).get("domain"))

    section("an unpublished site takes its address down with it")
    sql(f"update hotel_websites set status='draft' where property_id='{HOTEL}';")
    st, _ = http(f"{API}/pms/public/sites/by-host?host={TEST_DOMAIN}")
    check("an unpublished hotel does not answer on its domain", st == 404, st)
    sql(f"update hotel_websites set status='published' where property_id='{HOTEL}';")

    section("nobody else's domain, and nobody else's hotel")
    other = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    st, _ = http(f"{API}/pms/properties/{other}/website/domain", "PUT",
                 {"domain": "someone-else.example"}, owner)
    check("the demo owner cannot claim a domain for another hotel", st == 404, st)
    st, _ = http(base, headers=None)
    check("and an unauthenticated caller sees nothing", st == 401, st)

    reception = signin(RECEPTION_EMAIL)
    if reception:
        st, d = http(base, "PUT", {"domain": "reception-tried.example"}, reception)
        check("a receptionist cannot change the hotel's address", st == 403, st)
        check("  and is told who can",
              "owner" in str((d or {}).get("detail", "")).lower(), d)

    section("a domain already connected to another hotel")
    sql(f"""update hotel_websites set domain='taken.example', domain_status='verified'
            where property_id='{other}';""")
    st, d = http(base, "PUT", {"domain": "taken.example"}, owner)
    check("is refused", st == 422, st)
    check("  saying so in plain words",
          "another hotel" in str((d or {}).get("detail", "")).lower(), d)
    sql(f"""update hotel_websites set domain=null, domain_status='none'
            where property_id='{other}';""")

    section("disconnecting")
    st, d = http(base, "DELETE", None, owner)
    check("the address is released", (d or {}).get("status") == "none", d)
    st, _ = http(f"{API}/pms/public/sites/by-host?host={TEST_DOMAIN}")
    check("and stops resolving immediately", st == 404, st)
    st, d = http(f"{API}/pms/public/hotels/{SLUG}")
    check("the TravBuddy address never stopped working", st == 200, st)

    cleanup()
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
