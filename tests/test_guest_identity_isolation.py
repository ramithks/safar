"""
One person, two hotels, two guest records — and no way to make it one.

The Instagram identity path exists because a returning guest was arriving as a
stranger: the resolver matched a 17-digit IGSID against the `phone` column, so
`guest_id` stayed null, `_link_reservation` never ran, and somebody who had
booked in that very thread was told nothing was found.

Fixing that means teaching guest records an Instagram handle, which is the point
at which the interesting question becomes *whose* record. These assert the two
rules that make the answer safe:

  * **Lookup is scoped to the property, in both branches.** The same person at
    two hotels is two rows, and joining them would be a cross-property leak
    dressed up as a convenience. `guests_property_instagram_idx` is unique on
    `(property_id, instagram_id)`, which is the same statement in the schema.
  * **A sender id is data, never a pattern.** The phone branch builds an
    `ilike`, where `%` and `_` are wildcards.
"""
import asyncio

import pytest

from app.services.messaging import service as messaging
from tests.fake_supabase import FakeDb

CASA = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
IGSID = "1376629097955726"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    for target in ("app.db.client", "app.services.messaging.service"):
        monkeypatch.setattr(f"{target}.get_db", lambda: fake)
    fake.seed("guests",
        {"id": "guest-casa", "property_id": CASA, "name": "Rohit at Casa",
         "phone": "+919812345678", "instagram_id": IGSID},
        {"id": "guest-other", "property_id": OTHER, "name": "Rohit at Somewhere Else",
         "phone": "+919812345678", "instagram_id": IGSID},
    )
    return fake


def resolve(property_id: str, channel: str, sender: str) -> dict:
    return messaging._upsert_conversation(
        property_id=property_id, partner_id=PARTNER, channel=channel,
        account_id="acct-1", external_id=f"thread-{sender}",
        sender_external_id=sender, sender_name=None,
    )


# ── the property boundary ────────────────────────────────────────────────────

def test_the_same_instagram_account_is_a_different_guest_at_each_hotel(db):
    assert resolve(CASA, "instagram", IGSID)["guest_id"] == "guest-casa"
    assert resolve(OTHER, "instagram", IGSID)["guest_id"] == "guest-other"


def test_the_same_phone_is_a_different_guest_at_each_hotel(db):
    assert resolve(CASA, "whatsapp", "+919812345678")["guest_id"] == "guest-casa"
    assert resolve(OTHER, "whatsapp", "+919812345678")["guest_id"] == "guest-other"


def test_a_hotel_that_has_never_met_this_guest_gets_a_stranger(db):
    conversation = resolve("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "instagram", IGSID)
    assert conversation["guest_id"] is None
    assert conversation["guest_name"] is None


# ── a sender id is data, not a pattern ───────────────────────────────────────

@pytest.mark.parametrize("hostile", ["%%%%%%%%%%", "__________",
                                     "+91%%%%%%%%%%", "%_%_%_%_%_"])
def test_a_wildcard_sender_matches_nobody(db, hostile):
    """
    The bug this forecloses: an `ilike` of `%%%%%%%%%%` matches the first guest
    at the property, and the thread is opened holding a stranger's name and
    booking history. It arrives inside a signature-verified Meta payload today,
    so this was never live — it was one webhook change from being. The same
    class of bug already cost this codebase a coupon bypass.
    """
    assert resolve(CASA, "whatsapp", hostile)["guest_id"] is None


def test_a_wildcard_beside_real_digits_is_simply_dropped(db):
    """
    Not a refusal — the wildcard stops being a wildcard and the digits that are
    left are a real number, which is the guest this is. Stripping is the fix
    rather than rejecting, because a phone written with a space or a dash is
    also "not digits" and belongs to somebody.
    """
    assert resolve(CASA, "whatsapp", "%9812345678")["guest_id"] == "guest-casa"


def test_a_sender_id_too_short_to_be_a_phone_matches_nobody(db):
    """`digits[-10:]` on a two-digit string is a two-character suffix, which
    matches far too much to be an identity."""
    assert resolve(CASA, "whatsapp", "+91")["guest_id"] is None


def test_a_real_number_still_matches_on_its_last_ten_digits(db):
    """Country codes are written a dozen ways; the subscriber number is not."""
    assert resolve(CASA, "whatsapp", "919812345678")["guest_id"] == "guest-casa"
    assert resolve(CASA, "whatsapp", "+91 98123 45678")["guest_id"] == "guest-casa"


def test_an_igsid_is_never_matched_against_a_phone(db):
    """
    The original bug, kept as a test: 17 digits matched no phone that has ever
    existed, so every returning Instagram guest arrived as a stranger.
    """
    db.rows("guests")[0]["instagram_id"] = None
    assert resolve(CASA, "instagram", IGSID)["guest_id"] is None
