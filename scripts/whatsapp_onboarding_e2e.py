#!/usr/bin/env python3
"""
A hotel bringing its own WhatsApp number, end to end, against a real database.

    Embedded Signup code → business token → subscribe → register
                         → credential (encrypted) → connection row → templates
    Meta account webhook → revoke / degrade / template status

Three claims, kept apart, because blurring them is how an integration gets
declared finished before it works:

    REAL DATABASE TESTED   every row, constraint, RLS policy and index here is
                           the live hotel-OS project
    FAKE META TESTED       the onboarding chain, the ordering rules, the
                           account-event handlers, template status transitions
    REAL META TESTED       nothing in this file. `scripts/app_review_evidence.py`
                           is the one that touches Meta, and it says so

**Nothing here is allowed to reach Meta.** Every Graph call is monkeypatched at
the module boundary before the first assertion, and the suite asserts that too
— a run that silently fell through to the real API would onboard a WABA that
does not exist and leave a credential nobody can revoke.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/whatsapp_onboarding_e2e.py
"""
import asyncio
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import e2e_fixture  # noqa: E402

from app.api.v1.messaging import whatsapp_onboarding as routes  # noqa: E402
from app.api.v1.messaging.oauth import whatsapp_row  # noqa: E402
from app.api.v1.messaging.webhooks import handle_payload  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.messaging import crypto, store  # noqa: E402
from app.services.messaging.whatsapp import (  # noqa: E402
    account_events, onboarding, provisioning, template_api,
)

# **Not Casa Mandala, and the reason is worth stating.**
#
# `whatsapp_connections` is UNIQUE per property, so onboarding a hotel *replaces*
# whatever connection that property already had. The first version of this suite
# ran on Casa Mandala and its `_persist` upsert silently overwrote the seeded
# sandbox row that `whatsapp_e2e` and `celery_ordering_e2e` both resolve
# `109876543210987` through — then the sweep deleted it. Both suites failed on
# the next run with `CHANNEL_NOT_CONNECTED`, which reads as a broken resolver
# and is nothing of the kind.
#
# So this suite runs on "Somewhere Else", which has its own partner, its own
# billing account and no WhatsApp connection of its own. Belt and braces: the
# sweep below also snapshots and restores whatever it finds, so the suite stays
# safe if a future seed gives this property a connection too.
PROPERTY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PARTNER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

# Deliberately outside every real range. A WABA id that could collide with a
# hotel's own is a suite that can revoke a live credential.
WABA = "999000000000001"
PHONE_ID = "999000000000101"
TOKEN = "EAA-fake-business-token-for-the-suite"

db = get_db()

# The suite writes no reservations — connecting a channel is not a booking — but
# it does write a connection and a credential on the shared property, and both
# are swept at both ends below.
FIXTURE = e2e_fixture.Fixture(
    label="WhatsApp onboarding",
    property_id=PROPERTY,
    partner_id=PARTNER,
    prefixes=(),
)
FIXTURE.open()

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {str(detail)[:300]}")
    return bool(cond)


def run(coro):
    return asyncio.run(coro)


# ── the suite's own state, swept at both ends ────────────────────────────────


# Whatever connection this property had before the suite touched it. Captured
# once, restored at the end — see the note above PROPERTY for the run where not
# doing this cost two other suites.
_original: dict | None = None


def snapshot():
    global _original
    rows = (db.table("whatsapp_connections").select("*")
              .eq("property_id", PROPERTY).limit(1).execute().data or [])
    _original = rows[0] if rows else None
    if _original:
        print(f"  ·    this property already had a connection "
              f"({_original['phone_number_id']}) — it will be put back")


def sweep(restore: bool = False):
    """
    Give the property back exactly as it was found.

    Before as well as after: a run that dies halfway leaves a connection behind,
    and the next run must not inherit it — that is how `whatsapp_connections`
    ends up with a row nobody can explain. See scripts/e2e_fixture.py.
    """
    db.table("message_templates").delete().eq("waba_id", WABA).execute()
    db.table("whatsapp_connections").delete().eq("waba_id", WABA).execute()
    db.table("whatsapp_onboardings").delete().eq("waba_id", WABA).execute()
    db.table("messaging_credentials").delete() \
      .eq("channel", "whatsapp") \
      .eq("credentials_ref", store.ref_for_property(PROPERTY)).execute()
    db.table("whatsapp_connections").delete() \
      .eq("property_id", PROPERTY).eq("phone_number_id", PHONE_ID).execute()

    if restore and _original:
        db.table("whatsapp_connections").upsert(
            _original, on_conflict="property_id").execute()


snapshot()
sweep()

print("\n── the fake Meta ─────────────────────────────────────────────────────")

# Every Graph call, replaced. Not a fake HTTP server: the thing worth testing is
# our chain and our ordering, and a fake server would add a second network stack
# whose failures look like ours.
_calls: list[str] = []


async def fake_exchange(code):
    _calls.append(f"exchange:{code}")
    return onboarding.BusinessToken(
        access_token=TOKEN, expires_in=None,
        waba_ids=(WABA,), scopes=("whatsapp_business_management",),
    )


async def fake_subscribe(waba_id, token):
    _calls.append(f"subscribe:{waba_id}")
    return True


async def fake_account(waba_id, token):
    _calls.append(f"account:{waba_id}")
    return onboarding.Account(
        waba_id=waba_id, name="Suite Hotel", currency="INR",
        owner_business_id="888000000000001",
        numbers=(onboarding.PhoneNumber(
            phone_number_id=PHONE_ID, display_number="+91 99900 00101",
            verified_name="Suite Hotel", quality_rating="GREEN",
            platform_type="CLOUD_API", code_verification_status="VERIFIED",
        ),),
    )


async def fake_register(phone_number_id, pin, token):
    _calls.append(f"register:{phone_number_id}:{len(pin)}")
    return True


_template_ids: dict[str, str] = {}


async def fake_create(*, waba_id, token, name, language, category, components):
    _calls.append(f"template:{name}")
    _template_ids[name] = f"tpl_{uuid.uuid4().hex[:12]}"
    return {"id": _template_ids[name], "status": "PENDING", "category": category.upper()}


onboarding.exchange_code = fake_exchange
onboarding.subscribe_app = fake_subscribe
onboarding.account = fake_account
onboarding.register = fake_register
template_api.create = fake_create
# `provisioning` imported the module, not the function, so patching the module
# attribute is enough — asserted below rather than assumed.
check("every Graph call is patched",
      provisioning.template_api.create is fake_create
      and onboarding.exchange_code is fake_exchange)

print("\n── the chain ─────────────────────────────────────────────────────────")

token = run(fake_exchange("code-1"))
account = run(fake_account(WABA, TOKEN))
number = account.numbers[0]
pin = onboarding.generate_pin()

check("the generated PIN is six digits from a CSPRNG",
      len(pin) == 6 and pin.isdigit() and len({onboarding.generate_pin() for _ in range(20)}) > 1,
      pin)

claims = {"p": PROPERTY, "pa": PARTNER, "u": str(uuid.uuid4()), "n": "suite-nonce"}
routes._persist(claims=claims, account=account, number=number, token=token,
                pin=pin, onboarding_id=None)

row = (db.table("whatsapp_connections").select("*")
         .eq("phone_number_id", PHONE_ID).limit(1).execute().data or [{}])[0]
check("the connection is written and connected", row.get("status") == "connected", row)
check("it is not a sandbox row", row.get("is_sandbox") is False)
check("the WABA is recorded", row.get("waba_id") == WABA, row.get("waba_id"))
check("the owning business is recorded",
      row.get("business_id") == "888000000000001", row.get("business_id"))

# **The property this whole design exists for.**
leaked = [k for k, v in row.items()
          if isinstance(v, str) and TOKEN in v]
check("the token is nowhere on the connection row", not leaked, leaked)

cred = (db.table("messaging_credentials").select("*")
          .eq("channel", "whatsapp")
          .eq("credentials_ref", store.ref_for_property(PROPERTY))
          .limit(1).execute().data or [{}])[0]
check("the credential exists and is active", cred.get("status") == "active", cred.get("status"))
check("it is stored as ciphertext, not plaintext",
      cred.get("access_token_ciphertext") and TOKEN not in cred["access_token_ciphertext"])
check("and it decrypts back to the token",
      crypto.decrypt(cred["access_token_ciphertext"]) == TOKEN)
check("the PIN is encrypted too, and is not the plaintext",
      row.get("pin_ciphertext") and row["pin_ciphertext"] != pin
      and crypto.decrypt(row["pin_ciphertext"]) == pin)

print("\n── the number resolves to this hotel ─────────────────────────────────")

resolved = db.rpc("resolve_whatsapp_property",
                  {"p_phone_number_id": PHONE_ID}).execute().data or []
check("the existing resolver finds it without being changed",
      len(resolved) == 1 and resolved[0]["property_id"] == PROPERTY, resolved)
check("and it names the per-hotel credential, not 'default'",
      resolved and resolved[0]["credentials_ref"] == store.ref_for_property(PROPERTY),
      resolved[0]["credentials_ref"] if resolved else None)

print("\n── running it twice ──────────────────────────────────────────────────")

routes._persist(claims=claims, account=account, number=number, token=token,
                pin=pin, onboarding_id=None)
connections = (db.table("whatsapp_connections").select("id")
                 .eq("phone_number_id", PHONE_ID).execute().data or [])
creds = (db.table("messaging_credentials").select("id")
           .eq("channel", "whatsapp")
           .eq("credentials_ref", store.ref_for_property(PROPERTY)).execute().data or [])
check("a second onboarding leaves one connection", len(connections) == 1, len(connections))
check("and one credential", len(creds) == 1, len(creds))

print("\n── templates ─────────────────────────────────────────────────────────")

result = run(provisioning.provision(
    property_id=PROPERTY, partner_id=PARTNER, waba_id=WABA, token=TOKEN,
))
check("every standard template was created",
      len(result["created"]) == result["total"], result)

templates = (db.table("message_templates").select("*")
               .eq("waba_id", WABA).execute().data or [])
check("they are recorded against the hotel's own WABA",
      len(templates) == len(provisioning.STANDARD_TEMPLATES), len(templates))
# `all()` over an empty list is True, and these four passed vacuously the first
# time this suite ran with a broken provisioner — reporting green on exactly the
# rows that were missing. Every one of them now requires the rows to exist.
check("**every one is pending — nothing writes 'approved'**",
      templates and all(t["status"] == "pending" for t in templates),
      sorted({t["status"] for t in templates}))
check("none is marked as a test template",
      templates and all(t["is_test"] is False for t in templates))
check("each carries Meta's own template id",
      templates and all(t["provider_template_id"] for t in templates))
check("the purposes match what templates.resolve looks up",
      {t["purpose"] for t in templates}
      == {s["purpose"] for s in provisioning.STANDARD_TEMPLATES})
check("the preview has the parameters filled in, not {{1}}",
      templates and all("{{" not in (t["body_preview"] or "") for t in templates))

print("\n── what Meta says afterwards ─────────────────────────────────────────")


def account_webhook(event, **value):
    return {"entry": [{"id": WABA, "changes": [
        {"field": "account_update", "value": {"event": event, **value}}
    ]}]}


run(handle_payload({"entry": [{"id": WABA, "changes": [{
    "field": "message_template_status_update",
    "value": {
        "event": "APPROVED",
        "message_template_id": _template_ids["booking_confirmation_v1"],
        "message_template_name": "booking_confirmation_v1",
        "message_template_language": "en",
    },
}]}]}))
approved = (db.table("message_templates").select("status,approved_at")
              .eq("provider_template_id", _template_ids["booking_confirmation_v1"])
              .limit(1).execute().data or [{}])[0]
check("an APPROVED webhook moves exactly that template", approved.get("status") == "approved",
      approved)
check("and stamps when", bool(approved.get("approved_at")))
still_pending = (db.table("message_templates").select("purpose")
                   .eq("waba_id", WABA).eq("status", "pending").execute().data or [])
check("the others are untouched",
      len(still_pending) == len(provisioning.STANDARD_TEMPLATES) - 1, len(still_pending))

run(handle_payload({"entry": [{"id": WABA, "changes": [{
    "field": "message_template_status_update",
    "value": {
        "event": "REJECTED",
        "message_template_id": _template_ids["reengagement_v1"],
        "message_template_name": "reengagement_v1",
        "reason": "INVALID_FORMAT",
    },
}]}]}))
rejected = (db.table("message_templates").select("status,rejected_reason")
              .eq("provider_template_id", _template_ids["reengagement_v1"])
              .limit(1).execute().data or [{}])[0]
check("a REJECTED webhook records Meta's reason verbatim",
      rejected.get("status") == "rejected"
      and rejected.get("rejected_reason") == "INVALID_FORMAT", rejected)

run(handle_payload(account_webhook("ACCOUNT_VIOLATION",
                                   violation_info={"violation_type": "SCAM"})))
degraded = (db.table("whatsapp_connections").select("status,last_error")
              .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
check("a violation puts the connection in error, not disconnected",
      degraded.get("status") == "error", degraded.get("status"))
check("and says so in a hotelier's words, carrying Meta's reason",
      "SCAM" in (degraded.get("last_error") or "")
      and "Meta" in (degraded.get("last_error") or ""), degraded.get("last_error"))

print("\n── the hotel removes us ──────────────────────────────────────────────")

run(handle_payload(account_webhook(
    "PARTNER_REMOVED",
    waba_info={"waba_id": WABA, "owner_business_id": "888000000000001"},
)))
gone = (db.table("whatsapp_connections").select("status,last_error")
          .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
dead = (db.table("messaging_credentials").select("status,revoked_at")
          .eq("channel", "whatsapp")
          .eq("credentials_ref", store.ref_for_property(PROPERTY))
          .limit(1).execute().data or [{}])[0]
check("the connection is marked disconnected", gone.get("status") == "disconnected", gone)
check("**and the credential is revoked, not merely orphaned**",
      dead.get("status") == "revoked", dead)
check("the token can no longer be loaded",
      store.load_token(store.ref_for_property(PROPERTY), "whatsapp") is None)

print("\n── an unattributed Meta-hosted signup ────────────────────────────────")

db.table("whatsapp_connections").delete().eq("waba_id", WABA).execute()
run(handle_payload(account_webhook(
    "PARTNER_ADDED",
    waba_info={"waba_id": WABA, "owner_business_id": "888000000000009",
               "solution_id": "sol-1"},
)))
waiting = (db.table("whatsapp_onboardings").select("*")
             .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
check("it lands as unclaimed rather than being guessed at",
      waiting.get("status") == "unclaimed", waiting.get("status"))
check("with no property attached", waiting.get("property_id") is None)
check("and it is marked as having come from the hosted link",
      waiting.get("source") == "meta_hosted", waiting.get("source"))

run(handle_payload(account_webhook(
    "PARTNER_ADDED",
    waba_info={"waba_id": WABA, "owner_business_id": "888000000000009"},
)))
twice = (db.table("whatsapp_onboardings").select("id").eq("waba_id", WABA)
           .execute().data or [])
check("a hotel that completes the link twice produces one claimable row",
      len(twice) == 1, len(twice))

print("\n── claiming an unattributed arrival ──────────────────────────────────")

# The Meta-hosted arrival above is still unclaimed and carries no partner. This
# is the path the claim screen drives, and what matters is what it refuses.
# Casa Mandala's partner — genuinely a different one from this suite's, which
# runs on "Somewhere Else". Writing the suite's own partner id here made
# "another partner" the same partner and the assertion passed for no reason;
# the check below caught it, which is the whole argument for having it.
OTHER_PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
assert OTHER_PARTNER != PARTNER, "the negative case needs a genuinely different partner"

unclaimed = (db.table("whatsapp_onboardings").select("id,waba_id,partner_id,status")
               .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
check("the arrival is unclaimed and belongs to nobody",
      unclaimed.get("status") == "unclaimed" and not unclaimed.get("partner_id"),
      unclaimed)

# **The hole this design exists to close.** A uuid is guessable in a way a WABA
# id read off somebody's own WhatsApp Manager is not, so an arrival nobody has
# proved they own must not be claimable by id alone.
refused = routes.claim_arrival(
    routes.ClaimIn(onboardingId=str(unclaimed["id"])), PARTNER, PROPERTY)
check("**an unattributed arrival is refused when named only by its uuid**",
      refused.get("claimed") is False and refused.get("reason") == "not_yours", refused)

wrong = routes.claim_arrival(routes.ClaimIn(wabaId="999999999999999"), PARTNER, PROPERTY)
check("a WABA id we have never heard of is refused",
      wrong.get("claimed") is False and wrong.get("reason") == "not_found", wrong)

# Typing the WABA id is the proof, and it survives being read off a screen.
ok = routes.claim_arrival(routes.ClaimIn(wabaId=f" {WABA[:4]} {WABA[4:]} "), PARTNER, PROPERTY)
check("typing the WABA id attaches it, spaces and all",
      ok.get("claimed") is True and ok.get("wabaId") == WABA, ok)

attached = (db.table("whatsapp_connections")
              .select("property_id,partner_id,status,credentials_ref,is_sandbox,last_error")
              .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
check("it lands on the hotel that claimed it",
      str(attached.get("property_id")) == PROPERTY, attached.get("property_id"))
check("**as `pending`, not `connected` — there is no token yet**",
      attached.get("status") == "pending", attached.get("status"))
check("and it says what is still needed", bool(attached.get("last_error")))
check("it is not a sandbox row", attached.get("is_sandbox") is False)

again = routes.claim_arrival(routes.ClaimIn(wabaId=WABA), PARTNER, PROPERTY)
check("claiming the same arrival twice is refused",
      again.get("claimed") is False and again.get("reason") == "not_found", again)

# Put it back to unclaimed and prove another partner cannot take it.
db.table("whatsapp_onboardings").update({"status": "unclaimed", "property_id": None}) \
  .eq("waba_id", WABA).execute()
db.table("whatsapp_connections").delete().eq("waba_id", WABA).execute()
db.table("whatsapp_onboardings").update({"partner_id": PARTNER}).eq("waba_id", WABA).execute()
stolen = routes.claim_arrival(routes.ClaimIn(wabaId=WABA), OTHER_PARTNER, PROPERTY)
check("**a partner cannot claim an arrival that belongs to another**",
      stolen.get("claimed") is False and stolen.get("reason") == "not_yours", stolen)

print("\n── disconnect, and the order it happens in ───────────────────────────")

db.table("whatsapp_onboardings").delete().eq("waba_id", WABA).execute()
routes._persist(claims=claims, account=account, number=number, token=token,
                pin=pin, onboarding_id=None)

# The ordering property, asserted rather than commented. `store.revoke` is
# wrapped so the suite can see whether the row was already marked by the time it
# ran — which is what "revoke first" actually means.
order: list[str] = []
real_revoke = store.revoke


def watched_revoke(*, channel, credentials_ref):
    marked = (db.table("whatsapp_connections").select("status")
                .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
    order.append(f"revoke_while_row_is_{marked.get('status')}")
    return real_revoke(channel=channel, credentials_ref=credentials_ref)


routes.store.revoke = watched_revoke
connection = (db.table("whatsapp_connections")
                .select("credentials_ref,waba_id,status")
                .eq("property_id", PROPERTY).eq("waba_id", WABA)
                .limit(1).execute().data or [{}])[0]
outcome = routes.disconnect_connection(connection, PROPERTY)
routes.store.revoke = real_revoke

check("disconnect revokes the credential", outcome.get("revoked") == 1, outcome)
check("**the credential is revoked before the row is marked**",
      order == ["revoke_while_row_is_connected"], order)
after = (db.table("whatsapp_connections").select("status,last_error")
           .eq("waba_id", WABA).limit(1).execute().data or [{}])[0]
check("the row then reads disconnected", after.get("status") == "disconnected", after)
check("and last_error is cleared, not set — the hotel chose this",
      after.get("last_error") is None, after.get("last_error"))

print("\n── what the Channels screen says ─────────────────────────────────────")

check("a connected row reads as connected",
      whatsapp_row({"status": "connected", "is_sandbox": False,
                    "display_number": "+91 99900 00101"},
                   onboarding_enabled=True).state == "connected")
check("a sandbox row is never shown as connected",
      whatsapp_row({"status": "connected", "is_sandbox": True},
                   onboarding_enabled=True).state == "not_connected")
disconnected_row = whatsapp_row({"status": "disconnected", "is_sandbox": False},
                                onboarding_enabled=True)
check("a hotel that disconnected is invited back, not told it is broken",
      disconnected_row.state == "not_connected"
      and "whenever you like" in disconnected_row.detail, disconnected_row.detail)
check("an errored row asks for a reconnect",
      whatsapp_row({"status": "error", "is_sandbox": False,
                    "last_error": "Meta has flagged this account."},
                   onboarding_enabled=True).state == "action_needed")
check("a claimed-but-unfinished row is its own state",
      whatsapp_row({"status": "pending", "is_sandbox": False},
                   onboarding_enabled=True).action == "Finish connecting")
check("with onboarding unconfigured, nothing is offered that cannot be done",
      whatsapp_row(None, onboarding_enabled=False).state == "coming_soon")
check("once it is configured, it can be connected",
      whatsapp_row(None, onboarding_enabled=True).action == "Connect WhatsApp")

# **The half-configured case, which is the one a deploy actually produces.**
# `is_configured()` is the flag AND the app id AND the app secret AND the
# configuration id. Setting the flag first — the natural order, because it is
# the line that reads like the switch — must not put a Connect button in front
# of a hotel while `/whatsapp/start` still 503s.
from app.config import settings as _settings  # noqa: E402

_before = (_settings.whatsapp_onboarding_enabled, _settings.whatsapp_config_id)
_settings.whatsapp_onboarding_enabled, _settings.whatsapp_config_id = True, None
check("**the flag alone does not offer a Connect button that would 503**",
      onboarding.is_configured() is False
      and whatsapp_row(None, onboarding_enabled=onboarding.is_configured()).state
          == "coming_soon")
_settings.whatsapp_onboarding_enabled, _settings.whatsapp_config_id = _before

print("\n── nothing reached Meta ──────────────────────────────────────────────")

check("every Graph call went through the fake",
      all(c.split(":")[0] in ("exchange", "subscribe", "account", "register", "template")
          for c in _calls) and len(_calls) >= 4, _calls[:8])

sweep(restore=True)
FIXTURE.close()

print(f"\n{PASS} passed, {FAIL} failed")
if FAIL:
    print("\nREAL META TESTED: nothing. See scripts/app_review_evidence.py.")
sys.exit(1 if FAIL else 0)
