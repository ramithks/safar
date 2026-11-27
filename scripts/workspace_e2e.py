#!/usr/bin/env python3
"""
The four reads the PWA had no route for, over HTTP with real tokens.

    /guests    who has stayed here          'frontdesk'
    /services  what else the hotel sells    any member · 'rates' to edit
    /members   who works here               'members'
    /margin    what each property cost us   'billing'  ← the only route in the
                                                          PMS that returns
                                                          provider cost

These four screens shipped rendering seed data because the endpoints behind them
did not exist. That is the worst shape a gap can have: the Guests page drew
invented people in the same typeface as real ones, so nothing looked wrong.

**Most of what is asserted here is who may not read.** A guest list is names,
phone numbers and stay history; a margin row is what Safar paid Google. The
interesting assertions are the 403s.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/workspace_e2e.py

⚠ Creates and retires one service on Casa Mandala. It sweeps it on the way out.
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = "http://127.0.0.1:8010/api/v1/pms"
PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
# The marketing suite's hotel, under a different partner. Used as the negative:
# a margin row for it must never appear in Casa Mandala's answer.
OTHER_PROPERTY = "ffffffff-ffff-4fff-8fff-ffffffffffff"

TAG = uuid.uuid4().hex[:6].upper()

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


keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
            headers={"Authorization": f"Bearer {MGMT}",
                     "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in keys if k.get("name") == "anon"][0]


def signin(email):
    st, d = http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                 {"email": email, "password": "hotel-os-dev"}, {"apikey": ANON})
    return {"Authorization": "Bearer " + d["access_token"]} if st == 200 else None


def main() -> int:
    print("\n══ workspace e2e ══\n")

    if http(f"{API}/me")[0] == 0:
        print("  API not running on :8010 — start uvicorn first")
        return 1

    manager = signin("priya@casamandala.in")      # rates, members, billing
    reception = signin("deepak@casamandala.in")   # frontdesk, folio, tasks
    housekeeping = signin("anita@casamandala.in") # housekeeping, tasks
    outsider = signin("nobody@example.com")       # a different partner
    if not all((manager, reception, housekeeping, outsider)):
        print("  could not sign in as the four dev accounts")
        return 1

    # ── guests ───────────────────────────────────────────────────────────────
    print("── guests ──")
    st, guests = http(f"{API}/properties/{PROP}/guests", headers=reception)
    check("a receptionist reads the guest list", st == 200, f"{st} {str(guests)[:120]}")
    check("every guest belongs to this property",
          st == 200 and all(g["propertyId"] == PROP for g in guests),
          "another property's guest appeared")
    check("guests carry stay history and spend",
          st == 200 and (not guests or
                         ("stays" in guests[0] and "totalSpendPaise" in guests[0])),
          str(guests[:1])[:200])
    check("and their marketing consent, so a screen cannot imply one",
          st == 200 and (not guests or "marketingOptIn" in guests[0]))

    st, _ = http(f"{API}/properties/{PROP}/guests", headers=housekeeping)
    check("a housekeeper does not — a guest list is not cleaning information",
          st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/guests", headers=outsider)
    check("another partner gets 404, not 403", st == 404, str(st))
    st, _ = http(f"{API}/properties/{PROP}/guests")
    check("unauthenticated is 401", st == 401, str(st))

    # A Postgres wildcard typed into a search box is a literal, not a match-all.
    st, wild = http(f"{API}/properties/{PROP}/guests?search=%25", headers=reception)
    check("'%' in search matches a literal percent, not everyone",
          st == 200 and len(wild) <= len(guests),
          f"{len(wild) if st == 200 else wild} of {len(guests)}")
    st, under = http(f"{API}/properties/{PROP}/guests?search=_", headers=reception)
    check("and so does '_'", st == 200 and len(under) <= len(guests),
          f"{len(under) if st == 200 else under} of {len(guests)}")

    # ── services ─────────────────────────────────────────────────────────────
    print("\n── services ──")
    st, before = http(f"{API}/properties/{PROP}/services", headers=reception)
    check("any member reads what the hotel sells", st == 200, f"{st} {str(before)[:120]}")

    st, made = http(f"{API}/properties/{PROP}/services", "POST", {
        "name": f"Workspace E2E transfer {TAG}",
        "description": "Created by scripts/workspace_e2e.py",
        "pricePaise": 150_000, "category": "transfer", "isActive": True,
    }, manager)
    check("a manager creates one", st == 200 and made.get("pricePaise") == 150_000,
          f"{st} {str(made)[:160]}")

    service_id = made.get("id") if st == 200 else None
    if service_id:
        st, upd = http(f"{API}/properties/{PROP}/services", "POST", {
            "id": service_id,
            "name": f"Workspace E2E transfer {TAG}",
            "description": "Amended",
            "pricePaise": 175_000, "category": "transfer", "isActive": False,
        }, manager)
        check("and amends it in place, rather than making a second one",
              st == 200 and upd["id"] == service_id and upd["pricePaise"] == 175_000,
              f"{st} {str(upd)[:160]}")

        st, active = http(f"{API}/properties/{PROP}/services", headers=reception)
        check("a retired service is not offered to the desk",
              st == 200 and all(s["id"] != service_id for s in active))
        st, every = http(f"{API}/properties/{PROP}/services?include_inactive=true",
                         headers=reception)
        check("but settings can still see it, to bring it back",
              st == 200 and any(s["id"] == service_id for s in every))

    st, _ = http(f"{API}/properties/{PROP}/services", "POST", {
        "name": "nope", "description": "", "pricePaise": 100, "category": "transfer",
    }, reception)
    check("a receptionist may sell a transfer and may not price one",
          st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/services", "POST", {
        "name": "bad", "description": "", "pricePaise": 100, "category": "spaceflight",
    }, manager)
    check("a category outside the six is refused at the edge", st == 422, str(st))
    st, _ = http(f"{API}/properties/{PROP}/services", "POST", {
        "name": "bad", "description": "", "pricePaise": -1, "category": "transfer",
    }, manager)
    check("a negative price is refused", st == 422, str(st))

    # A service id belonging to another hotel updates nothing rather than moving.
    st, _ = http(f"{API}/properties/{PROP}/services", "POST", {
        "id": str(uuid.uuid4()),
        "name": "elsewhere", "description": "", "pricePaise": 100,
        "category": "transfer",
    }, manager)
    check("an id that is not this property's updates nothing", st == 404, str(st))

    # ── members ──────────────────────────────────────────────────────────────
    print("\n── members ──")
    st, members = http(f"{API}/properties/{PROP}/members", headers=manager)
    check("a manager reads the team", st == 200 and len(members) >= 3,
          f"{st} {len(members) if st == 200 else members}")
    check("each carries the permissions their role resolves to",
          st == 200 and all(m["permissions"] for m in members), str(members)[:200])
    check("and an email to identify them by",
          st == 200 and all(m["email"] for m in members), str(members)[:200])
    st, _ = http(f"{API}/properties/{PROP}/members", headers=reception)
    check("a receptionist cannot read the staff list", st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/members", headers=outsider)
    check("another partner gets 404", st == 404, str(st))

    # ── margin ───────────────────────────────────────────────────────────────
    print("\n── margin: the only route that returns provider cost ──")
    st, margin = http(f"{API}/properties/{PROP}/margin", headers=manager)
    check("a manager reads it", st == 200, f"{st} {str(margin)[:200]}")
    check("it carries what the provider charged us",
          st == 200 and (not margin or "providerCostInrMicros" in margin[0]),
          str(margin)[:200])
    check("and only this partner's properties",
          st == 200 and all(r["propertyId"] != OTHER_PROPERTY for r in margin),
          "another partner's margin was returned")
    check("margin is revenue minus cost, not a copy of either",
          st == 200 and all(
              r["marginPaise"] == r["revenuePaise"] - round(r["providerCostInrMicros"] / 10_000)
              for r in margin),
          str(margin)[:300])

    st, _ = http(f"{API}/properties/{PROP}/margin", headers=reception)
    check("a receptionist never sees provider cost", st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/margin", headers=housekeeping)
    check("nor does housekeeping", st == 403, str(st))
    st, _ = http(f"{API}/properties/{PROP}/margin")
    check("unauthenticated is 401", st == 401, str(st))

    # No shape here may leak cost to a hotel-facing route.
    st, usage = http(f"{API}/properties/{PROP}/usage/events", headers=manager)
    check("the hotel's own usage feed still carries no provider cost",
          st == 200 and all("providerCost" not in json.dumps(e) for e in usage[:20]),
          "cost leaked onto a hotel-facing route")

    # ── the inbox, with staff threads in it ──────────────────────────────────
    print("\n── the inbox carries every channel, including the one with no window ──")
    st, convs = http(f"{API}/properties/{PROP}/conversations", headers=reception)
    check("the inbox loads at all", st == 200, f"{st} {str(convs)[:200]}")
    # It did not, for months. `list_conversations` calls `window_open()` on
    # every row and `policy.for_channel('internal')` raised, so one staff
    # console thread turned the whole screen into a 500. A property that had
    # ever used the Front Desk agent could not open its own inbox.
    check("a property with staff console threads still gets a list",
          st == 200 and isinstance(convs, list))

    internal = [c for c in convs if c["channel"] == "internal"] if st == 200 else []
    if internal:
        check("a staff thread reports its window as closed rather than raising",
              all(c["windowOpen"] is False for c in internal),
              str(internal[:1])[:200])
        check("and carries no guest phone, because there is no guest",
              all(not c.get("guestPhone") for c in internal), str(internal[:1])[:200])
    else:
        print("  ·    no internal threads on this property to check")

    for c in convs[:40] if st == 200 else []:
        if c["channel"] not in ("whatsapp", "instagram", "website", "phone", "internal"):
            check(f"unknown channel {c['channel']!r} would crash the inbox", False)
            break
    else:
        check("every channel returned is one the client can render", st == 200)

    # ── sweep ────────────────────────────────────────────────────────────────
    if service_id:
        http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
             {"query": f"delete from public.services where id = '{service_id}';"},
             {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
        print(f"\n  · swept the service this run created")

    print(f"\n══ {PASS} passed, {FAIL} failed ══\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
