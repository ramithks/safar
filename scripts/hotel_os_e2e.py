#!/usr/bin/env python3
"""
Can a hotelier configure a hotel the way §45 says they must?

    signup ──► hotel ──► room (full) ──► rates ──► negotiation floor
                                                        │
                                    POST .../negotiation-preview
                                                        │
                       the same engine an Instagram guest reaches at 2am

`onboarding_e2e` proves a stranger can get to a sellable room. This proves they
can get to a *configured* one: occupancy split, beds, extra beds, meals, an
early-check-in ladder, group tiers, a negotiation floor, a team with a WhatsApp
number, notification routing, and a readiness report that counts all of it
without inventing a percentage.

Every assertion goes through the public HTTP surface holding a token minted by
the ordinary signup endpoint. Nothing here uses the service role except the
sweep, because a suite that writes with the service role proves the database
works and says nothing about whether a hotelier can reach it.

**The negatives matter as much as the positives.** A floor that another hotel
can read is worse than no floor; a rule scoped to somebody else's rate plan is
one that silently never fires. Both are asserted.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010
    ./venv/bin/python scripts/hotel_os_e2e.py

⚠ Creates one auth user at `@hotelos.staysuite.test` and the hotel it owns, and
sweeps both at each end. Nothing outside that namespace is touched.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
API = os.environ.get("STAYSUITE_API_BASE", "http://127.0.0.1:8010").rstrip("/") + "/api/v1/pms"

# Casa Mandala — a real property under a different partner. The cross-tenant
# negative, reused from onboarding_e2e so both suites point at the same hotel.
OTHER_PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

TAG = uuid.uuid4().hex[:8]
EMAIL = f"owner-{TAG}@hotelos.staysuite.test"
PASSWORD = "HotelOwner!2026"
HOTEL = f"Neelkanth Haveli {TAG[:4].upper()}"

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {detail}")
    return ok


def http(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


_raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(_raw.removeprefix("go-keyring-base64:")).decode()


def sql(query):
    st, body = http(f"https://api.supabase.com/v1/projects/{REF}/database/query", "POST",
                    {"query": query},
                    {"Authorization": f"Bearer {MGMT}", "User-Agent": "SupabaseCLI/2.0.0"})
    if st not in (200, 201):
        raise RuntimeError(f"SQL failed: {st} {str(body)[:300]}")
    return body


_keys = http(f"https://api.supabase.com/v1/projects/{REF}/api-keys?reveal=true",
             headers={"Authorization": f"Bearer {MGMT}",
                      "User-Agent": "SupabaseCLI/2.0.0"})[1]
ANON = [k["api_key"] for k in _keys if k.get("name") == "anon"][0]

_OURS = ("select p.id from public.properties p "
         "join public.partners pt on pt.id = p.partner_id "
         "join auth.users u on u.id = pt.owner_id "
         "where u.email like '%@hotelos.staysuite.test'")


def sweep():
    """Everything this suite has ever made, in dependency order."""
    sql(f"""
delete from public.hotel_config_audit where property_id in ({_OURS});
delete from public.agent_escalations where property_id in ({_OURS});
delete from public.team_notification_rules where property_id in ({_OURS});
delete from public.hotel_team_members where property_id in ({_OURS});
delete from public.negotiation_rules where property_id in ({_OURS});
delete from public.group_rate_tiers where property_id in ({_OURS});
delete from public.group_policies where property_id in ({_OURS});
delete from public.stay_time_rules where property_id in ({_OURS});
delete from public.meal_options where property_id in ({_OURS});
delete from public.experiences where property_id in ({_OURS});
delete from public.services where property_id in ({_OURS});
delete from public.agent_configs c using public.agents a
 where c.agent_id = a.id and a.property_id in ({_OURS});
delete from public.agents a where a.property_id in ({_OURS});
delete from public.partners pt using auth.users u
 where pt.owner_id = u.id and u.email like '%@hotelos.staysuite.test';
delete from auth.users where email like '%@hotelos.staysuite.test';
""")


def rupees(paise):
    return f"₹{(paise or 0) / 100:,.0f}"


def main():
    print(f"\n══ hotel OS: configuring a hotel end to end ══\n   {EMAIL}\n")
    sweep()

    # ── 1. a hotel to configure ──────────────────────────────────────────────
    print("── 1. a hotel to configure ──")
    st, body = http(f"{SB}/auth/v1/signup", "POST",
                    {"email": EMAIL, "password": PASSWORD}, {"apikey": ANON})
    token = (body or {}).get("access_token") if isinstance(body, dict) else None
    if not check("a stranger can sign up and gets a session", bool(token), str(body)[:200]):
        return 1
    auth = {"Authorization": f"Bearer {token}"}

    st, hotel = http(f"{API}/onboarding/hotel", "POST",
                     {"hotelName": HOTEL, "city": "Udaipur", "ownerName": "Rekha Menon",
                      "phone": "+919000000778"}, auth)
    if not check("the hotel is created", st == 201, f"{st} {str(hotel)[:250]}"):
        return 1
    pid = hotel["propertyId"]

    st, room = http(f"{API}/properties/{pid}/room-types", "POST",
                    {"name": "Lake View Deluxe", "totalUnits": 6,
                     "maxOccupancy": 3, "priceRupees": 5000}, auth)
    if not check("a room and its rate exist", st == 201, f"{st} {str(room)[:250]}"):
        return 1
    rid = room["id"]

    st, plans = http(f"{API}/properties/{pid}/rate-plans", headers=auth)
    plan_id = plans[0]["id"]

    # ── 2. the room, in full ─────────────────────────────────────────────────
    print("\n── 2. the room, in full ──")
    st, detail = http(f"{API}/properties/{pid}/room-types-detail", headers=auth)
    check("the detail view lists it", st == 200 and len(detail) == 1, f"{st} {str(detail)[:150]}")
    first = detail[0] if detail else {}
    check("a room made by the short form is sellable", first.get("sellable") is True,
          str(first.get("sellable")))
    check("and is NOT yet ready to sell — §2's bar is higher",
          first.get("readyToSell") is False, str(first.get("readyToSell")))
    missing_keys = {m["key"] for m in first.get("missing", [])}
    check("it names what is missing rather than a percentage",
          {"occupancy", "beds", "extra_bed", "description"} <= missing_keys,
          str(sorted(missing_keys)))

    st, saved = http(f"{API}/properties/{pid}/room-types/{rid}/detail", "PATCH",
                     {"maxAdults": 2, "maxChildren": 1, "childrenAllowed": "allowed",
                      "bedType": "King bed", "bedCount": 1,
                      "extraBed": "available", "extraBedRupees": 800,
                      "crib": "none",
                      "description": "Corner room with a balcony over Lake Pichola.",
                      "amenities": ["AC", "wifi", "hot water", "balcony"],
                      "view": "lake", "smoking": "non_smoking", "bathroom": "private"},
                     auth)
    check("the full editor saves", st == 200, f"{st} {str(saved)[:250]}")
    check("rupees became paise on the server", saved.get("extraBedPaise") == 80_000,
          str(saved.get("extraBedPaise")))
    check("'none' is stored as an answer, not as a blank",
          saved.get("crib") == "none", str(saved.get("crib")))

    # Absent means unchanged — the contract a ten-step wizard depends on.
    st, again = http(f"{API}/properties/{pid}/room-types/{rid}/detail", "PATCH",
                     {"view": "lake and garden"}, auth)
    check("saving one field leaves the others alone",
          again.get("bedType") == "King bed" and again.get("maxAdults") == 2,
          str(again)[:200])
    st, cleared = http(f"{API}/properties/{pid}/room-types/{rid}/detail", "PATCH",
                       {"view": None}, auth)
    check("an explicit null still clears", cleared.get("view") is None, str(cleared.get("view")))

    st, _ = http(f"{API}/properties/{OTHER_PROPERTY}/room-types/{rid}/detail", "PATCH",
                 {"description": "hijacked"}, auth)
    check("another hotel's path cannot reach this room", st == 404, str(st))

    # ── 3. arriving early ────────────────────────────────────────────────────
    print("\n── 3. arriving early, leaving late ──")
    for band in (
        {"kind": "early_in", "untilTime": "08:00", "chargeKind": "pct_of_night", "amount": 50},
        {"kind": "early_in", "untilTime": "10:00", "chargeKind": "fixed", "amount": 500},
        {"kind": "early_in", "untilTime": "14:00", "chargeKind": "free"},
        {"kind": "late_out", "untilTime": "14:00", "chargeKind": "free"},
        {"kind": "late_out", "untilTime": "17:00", "chargeKind": "fixed", "amount": 500},
    ):
        st, _ = http(f"{API}/properties/{pid}/stay-time-rules", "POST", band, auth)
        if st != 200:
            check(f"band {band['kind']} {band['untilTime']}", False, str(st))
    st, bands = http(f"{API}/properties/{pid}/stay-time-rules", headers=auth)
    check("the ladder is saved", st == 200 and len(bands) == 5, f"{st} {len(bands)}")
    fixed = [b for b in bands if b["chargeKind"] == "fixed"]
    check("a fixed charge is stored in paise", all(b["amount"] == 50_000 for b in fixed),
          str([b["amount"] for b in fixed]))
    pct = [b for b in bands if b["chargeKind"] == "pct_of_night"]
    check("a percentage is stored as a percentage, not multiplied by 100",
          pct and pct[0]["amount"] == 50, str([b["amount"] for b in pct]))

    st, dup = http(f"{API}/properties/{pid}/stay-time-rules", "POST",
                   {"kind": "early_in", "untilTime": "10:00", "chargeKind": "free"}, auth)
    check("two bands ending at the same time are refused", st == 409, f"{st} {str(dup)[:120]}")

    # ── 4. meals ─────────────────────────────────────────────────────────────
    print("\n── 4. meals ──")
    st, meal = http(f"{API}/properties/{pid}/meals", "PUT",
                    {"meal": "breakfast", "availability": "available", "priceRupees": 300,
                     "priceUnit": "per_person", "startsAt": "07:30", "endsAt": "10:30",
                     "diet": "both", "serviceStyle": "buffet"}, auth)
    check("breakfast is described", st == 200 and meal["pricePaise"] == 30_000,
          f"{st} {str(meal)[:200]}")
    st, meal2 = http(f"{API}/properties/{pid}/meals", "PUT",
                     {"meal": "breakfast", "availability": "available", "priceRupees": 350}, auth)
    st, meals = http(f"{API}/properties/{pid}/meals", headers=auth)
    check("saving it twice amends rather than duplicating",
          len([m for m in meals if m["meal"] == "breakfast"]) == 1, str(len(meals)))
    check("and the new price took", meal2["pricePaise"] == 35_000, str(meal2["pricePaise"]))

    # ── 5. group pricing ─────────────────────────────────────────────────────
    print("\n── 5. group pricing ──")
    for tier in (
        {"minUnits": 3, "maxUnits": 5, "discountPct": 5},
        {"minUnits": 6, "maxUnits": 9, "discountPct": 10},
        {"minUnits": 10, "negotiable": True},
    ):
        st, _ = http(f"{API}/properties/{pid}/group-tiers", "POST", tier, auth)
        if st != 200:
            check(f"tier {tier['minUnits']}+", False, str(st))
    st, tiers = http(f"{API}/properties/{pid}/group-tiers", headers=auth)
    check("the ladder is saved", st == 200 and len(tiers) == 3, f"{st} {len(tiers)}")

    st, bad = http(f"{API}/properties/{pid}/group-tiers", "POST",
                   {"minUnits": 4, "maxUnits": 2, "discountPct": 5}, auth)
    check("a band that is upside down is refused", bad and st == 422, str(st))
    st, empty = http(f"{API}/properties/{pid}/group-tiers", "POST", {"minUnits": 20}, auth)
    check("a tier that changes nothing is refused", st == 422, str(st))

    st, policy = http(f"{API}/properties/{pid}/group-policy", "PUT",
                      {"minUnits": 3, "depositPct": 40, "paymentDueDays": 14}, auth)
    check("the group policy saves", st == 200 and policy["depositPct"] == 40,
          f"{st} {str(policy)[:150]}")

    # A group price has to be *visible* on the booking path, not just stored.
    check_in = (date.today() + timedelta(days=30)).isoformat()
    check_out = (date.today() + timedelta(days=31)).isoformat()
    st, one = http(f"{API}/properties/{pid}/availability?checkIn={check_in}"
                   f"&checkOut={check_out}&units=1", headers=auth)
    st, six = http(f"{API}/properties/{pid}/availability?checkIn={check_in}"
                   f"&checkOut={check_out}&units=6", headers=auth)
    one_night = one[0]["ratePlans"][0]["perNightPaise"] if one and one[0]["ratePlans"] else 0
    six_night = six[0]["ratePlans"][0]["perNightPaise"] if six and six[0]["ratePlans"] else 0
    check("six rooms are priced below one — the tier reaches availability",
          six_night == 450_000 and one_night == 500_000,
          f"one={rupees(one_night)} six={rupees(six_night)}")

    # ── 6. the negotiation floor ─────────────────────────────────────────────
    print("\n── 6. the floor, and what the AI would say ──")
    st, rule = http(f"{API}/properties/{pid}/negotiation-rules", "POST",
                    {"scope": "property", "floorRupees": 4300, "targetRupees": 4700,
                     "autoPct": 10, "approvalPct": 20}, auth)
    check("a hotel-wide floor is saved", st == 200 and rule["floorPaise"] == 430_000,
          f"{st} {str(rule)[:200]}")

    st, second = http(f"{API}/properties/{pid}/negotiation-rules", "POST",
                      {"scope": "property", "floorRupees": 4400, "autoPct": 10}, auth)
    st, rules = http(f"{API}/properties/{pid}/negotiation-rules", headers=auth)
    check("saving it again amends the one rule rather than adding a second",
          len([r for r in rules if r["scope"] == "property"]) == 1, str(len(rules)))

    st, cross = http(f"{API}/properties/{pid}/negotiation-rules", "POST",
                     {"scope": "rate_plan", "ratePlanId": plan_id,
                      "floorRupees": 4300, "autoPct": 14, "approvalPct": 20}, auth)
    check("a rule can be scoped to one rate plan", st == 200, f"{st} {str(cross)[:200]}")

    st, foreign = http(f"{API}/properties/{pid}/negotiation-rules", "POST",
                       {"scope": "room_type", "roomTypeId": OTHER_PROPERTY, "autoPct": 50}, auth)
    check("a rule cannot be scoped to another hotel's room", foreign and st == 404, str(st))

    def preview(asked, units=1):
        st, out = http(f"{API}/properties/{pid}/negotiation-preview", "POST",
                       {"ratePlanId": plan_id, "checkIn": check_in, "checkOut": check_out,
                        "units": units,
                        **({"requestedNightlyRupees": asked} if asked is not None else {})},
                       auth)
        return st, out

    st, at_4700 = preview(4700)
    check("₹4,700 is granted on the spot — within the 14% the hotel allows",
          st == 200 and at_4700["verdict"] == "allowed",
          f"{st} {str(at_4700)[:200]}")
    check("and the number quoted is the one the quote will produce",
          at_4700.get("finalNightlyPaise") == 470_000, str(at_4700.get("finalNightlyPaise")))

    st, at_4300 = preview(4300)
    check("₹4,300 — exactly the floor — is granted too", at_4300["verdict"] == "allowed",
          str(at_4300)[:200])

    st, at_4000 = preview(4000)
    check("₹4,000 is countered, not refused", at_4000["verdict"] == "counter",
          str(at_4000)[:200])
    check("the counter is the floor and never below it",
          at_4000["counterNightlyPaise"] == 430_000, rupees(at_4000.get("counterNightlyPaise")))
    check("and the hotelier is shown the sentence, not the verdict code",
          "4,300" in at_4000.get("wouldSay", ""), at_4000.get("wouldSay", ""))

    st, at_1 = preview(1)
    check("₹1 does not break the floor either",
          at_1["counterNightlyPaise"] == 430_000, rupees(at_1.get("counterNightlyPaise")))

    st, no_ask = preview(None)
    check("'what's your best price' answers with the best price",
          no_ask["counterNightlyPaise"] == 430_000, str(no_ask)[:200])

    st, group_ask = preview(4000, units=6)
    check("on six rooms the negotiation starts from the group rate, not the rack rate",
          group_ask["publishedNightlyPaise"] == 450_000,
          rupees(group_ask.get("publishedNightlyPaise")))

    # §33: no projection anywhere may be labelled with the floor.
    leaky = [k for k in at_4000 if any(w in k.lower() for w in ("floor", "minimum", "target"))]
    check("the preview carries no field named after the floor", not leaky, str(leaky))

    # ── 7. the team ──────────────────────────────────────────────────────────
    print("\n── 7. the team, and who hears about what ──")
    st, sales = http(f"{API}/properties/{pid}/team", "POST",
                     {"name": "Vikram Rao", "role": "sales",
                      "whatsapp": "+91 98765 43210"}, auth)
    check("a team member with no login can exist", st == 200 and sales["memberId"] is None,
          f"{st} {str(sales)[:200]}")
    check("the number is normalised to digits with a country code",
          sales["whatsapp"] == "919876543210", str(sales.get("whatsapp")))

    st, dup = http(f"{API}/properties/{pid}/team", "POST",
                   {"name": "Someone Else", "role": "manager", "whatsapp": "919876543210"}, auth)
    check("the same number twice is refused", st == 409, f"{st} {str(dup)[:150]}")

    st, unreachable = http(f"{API}/properties/{pid}/team", "POST",
                           {"name": "Ghost", "role": "other"}, auth)
    check("somebody with no way to be reached is refused", st == 422, str(st))

    st, routed = http(f"{API}/properties/{pid}/notification-rules", "POST",
                      {"event": "group_inquiry", "teamMemberId": sales["id"],
                       "channels": ["whatsapp"]}, auth)
    check("group enquiries can be routed to sales", st == 200, f"{st} {str(routed)[:200]}")
    st, twice = http(f"{API}/properties/{pid}/notification-rules", "POST",
                     {"event": "group_inquiry", "teamMemberId": sales["id"],
                      "channels": ["whatsapp", "email"]}, auth)
    st, rules_out = http(f"{API}/properties/{pid}/notification-rules", headers=auth)
    check("routing the same event to the same person twice amends one rule",
          len(rules_out) == 1 and set(rules_out[0]["channels"]) == {"whatsapp", "email"},
          str(rules_out)[:200])
    check("and the rule names the person, not just their id",
          rules_out[0]["teamMemberName"] == "Vikram Rao", str(rules_out[0]))

    st, bad_event = http(f"{API}/properties/{pid}/notification-rules", "POST",
                         {"event": "the_hotel_burned_down", "teamMemberId": sales["id"]}, auth)
    check("an event nobody publishes is refused at the edge", st == 422, str(st))

    # ── 8. experiences and services ──────────────────────────────────────────
    print("\n── 8. what else the hotel sells ──")
    st, exp = http(f"{API}/properties/{pid}/experiences", "POST",
                   {"title": "Sunrise boat ride on Lake Pichola",
                    "priceRupees": 1200, "priceUnit": "per_person",
                    "durationMinutes": 90, "meetingPoint": "Hotel jetty",
                    "minParticipants": 2, "maxParticipants": 8, "minAge": 6}, auth)
    check("an experience is created", st == 200 and exp["pricePaise"] == 120_000,
          f"{st} {str(exp)[:200]}")
    st, upside = http(f"{API}/properties/{pid}/experiences", "POST",
                      {"title": "Impossible trek", "minParticipants": 10, "maxParticipants": 2},
                      auth)
    check("a party size that cannot exist is refused", st == 422, str(st))

    st, svc = http(f"{API}/properties/{pid}/services", "POST",
                   {"name": "Airport pickup", "category": "transfer", "pricePaise": 150_000,
                    "priceUnit": "per_vehicle", "advanceRequired": True,
                    "leadTimeHours": 12}, auth)
    check("a service can say what its price is per", st == 200 and svc["priceUnit"] == "per_vehicle",
          f"{st} {str(svc)[:200]}")

    # ── 9. readiness ─────────────────────────────────────────────────────────
    print("\n── 9. what is left ──")
    st, report = http(f"{API}/properties/{pid}/readiness/full", headers=auth)
    check("the report loads", st == 200, f"{st} {str(report)[:200]}")
    sections = {s["key"]: s for s in report.get("sections", [])}
    check("every section counts checks rather than guessing",
          all(s["passed"] <= s["total"] for s in sections.values()), str(list(sections)))
    check("a section short of everything never reads 100%",
          all(s["pct"] < 100 for s in sections.values() if not s["complete"]),
          str({k: v["pct"] for k, v in sections.items()}))
    check("the team section is complete now that somebody can be reached",
          sections.get("team", {}).get("complete") is True,
          str(sections.get("team", {}).get("missing")))
    check("the meals section is not, because lunch and dinner are unanswered",
          sections.get("meals", {}).get("passed") == 1,
          str(sections.get("meals", {}).get("passed")))
    check("pricing knows the floor and the group ladder are set",
          {"negotiation", "group"} & {m["key"] for m in sections.get("pricing", {}).get("missing", [])} == set(),
          str([m["key"] for m in sections.get("pricing", {}).get("missing", [])]))
    check("the headline is a count of named things, not a score",
          isinstance(report.get("remaining"), int) and report["remaining"] > 0,
          str(report.get("remaining")))
    check("the room is sellable and not yet complete",
          report["sellableRooms"] == 1 and report["readyRooms"] == 0,
          f"{report['sellableRooms']}/{report['readyRooms']}")

    # ── 10. the audit trail ──────────────────────────────────────────────────
    print("\n── 10. who changed the floor ──")
    st, log = http(f"{API}/properties/{pid}/config-audit", headers=auth)
    check("the log loads", st == 200 and isinstance(log, list), f"{st} {str(log)[:150]}")
    fields = {e["field"] for e in log}
    check("the floor change is recorded", "floor_paise" in fields, str(sorted(fields))[:250])
    check("and so is the change from 4,300 to 4,400",
          any(e["field"] == "floor_paise" and e["oldValue"] == "430000"
              and e["newValue"] == "440000" for e in log),
          str([(e["oldValue"], e["newValue"]) for e in log if e["field"] == "floor_paise"]))
    check("a room description is NOT recorded — this log is for money and access",
          "description" not in fields, str(sorted(fields))[:250])
    check("every entry names who did it",
          all(e.get("actor") for e in log), str(log[:1])[:200])

    # ── 11. what another hotel can see ───────────────────────────────────────
    print("\n── 11. what another hotel can see ──")
    for path in ("negotiation-rules", "group-tiers", "team", "escalations",
                 "readiness/full", "config-audit", "meals", "stay-time-rules",
                 "experiences", "room-types-detail"):
        st, _ = http(f"{API}/properties/{OTHER_PROPERTY}/{path}", headers=auth)
        check(f"{path} on another hotel is 404", st == 404, str(st))

    print(f"\n══ {passed} passed, {failed} failed ══\n")
    sweep()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sweep()
        raise
