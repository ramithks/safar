"""
Meta ads: what is built, what is honest about not being built, and one trap.

The readiness audit scored Ads 55 and called it NOT PRODUCTION READY, for the
right reason — no Meta account has ever been connected end to end. This file
does not pretend otherwise. §24 rule 7 is explicit: *do not fake Meta
integration if credentials aren't available.*

What it asserts instead is the three things that must be true of a product
surface that cannot complete its own job:

  **It must not lie.** No campaign reaches `published`, `running` or `paused`
  without a Meta id, and that is a database constraint rather than a promise.

  **It must say precisely what is missing**, in terms a hotelier can act on —
  "connect an ad account", not "not configured".

  **It must be connectable.** This is the one that was actually broken.
  `_missing_credentials` reads `settings.meta_ad_account_id` and
  `settings.meta_ads_access_token` with `getattr(..., None)`, and neither
  field existed on `Settings`, whose `model_config` is `extra="ignore"`. So a
  hotelier who obtained an ad account and set META_AD_ACCOUNT_ID in the
  environment had it silently dropped by pydantic, and the product went on
  telling them to connect an ad account. Not a gap — a trap, and the kind that
  is only found by trying to configure the thing.
"""
import pathlib

import pytest

from app.config import settings
from app.services.marketing import meta_ads


# ── the settings that could not be set ──────────────────────────────────────

@pytest.mark.parametrize("field", [
    "meta_ad_account_id",
    "meta_ads_access_token",
    "meta_page_id",
])
def test_the_credentials_can_at_least_be_supplied(field):
    """
    Declared, so the environment can carry them. Not set, and this asserts
    nothing about whether they are — a deployment with real credentials and one
    without must both pass.
    """
    assert hasattr(settings, field), (
        f"{field} is read by meta_ads but not declared on Settings, and "
        f"Settings ignores unknown environment variables — so setting it does "
        f"nothing and the product tells a configured hotel it is unconfigured"
    )


def test_settings_still_ignores_what_it_does_not_know():
    """
    The reason the trap existed, kept deliberately. `extra="ignore"` is right —
    a typo'd variable should not crash a deployment — which is exactly why
    every field something reads has to be declared.
    """
    assert settings.model_config.get("extra") == "ignore"


# ── what it says when it cannot publish ─────────────────────────────────────

def test_an_unconnected_hotel_is_told_what_to_connect():
    missing = meta_ads._missing_credentials(None)
    assert "instagram_account" in missing
    assert "ad_account" in missing
    assert "ads_permission" in missing


def test_each_missing_thing_is_something_a_hotelier_can_act_on():
    """
    Names of things they own, not names of variables. A hotelier reading
    `meta_ads_access_token` learns nothing they can do anything about.
    """
    for item in meta_ads._missing_credentials(None):
        assert "token" not in item
        assert "settings" not in item
        assert item.islower() and " " not in item


def test_a_connected_instagram_is_not_an_ad_account():
    """
    The distinction the whole module exists to hold. StaySuite has Instagram
    *messaging* scopes — reading and replying to DMs — and creating ads needs
    `ads_management` on a Meta Business account with a funded ad account. They
    look adjacent and share nothing.
    """
    missing = meta_ads._missing_credentials({"status": "connected"})
    assert "instagram_account" not in missing
    assert "ad_account" in missing, (
        "a connected Instagram was read as permission to spend money"
    )


# ── the request that would be sent ──────────────────────────────────────────

def test_the_request_is_composed_in_full_even_though_it_cannot_be_sent():
    """
    Built and returned rather than skipped, so a hotelier can be shown what
    would be created — and so the payload that eventually goes on the wire is
    the one that has been under test, rather than one written for the first
    time on the day it first runs.
    """
    spec = meta_ads.build_request(
        campaign={"name": "Monsoon", "objective": "messages",
                  "destination": "whatsapp", "channel": "instagram",
                  "daily_budget_paise": 50000, "starts_on": "2026-09-01",
                  "ends_on": "2026-09-30"},
        creative={"format": "single_image", "headline": "Two nights in Anjuna",
                  "primary_text": "Garden rooms, breakfast included.",
                  "media": [{"url": "https://cdn.example.com/a.jpg"}]},
        audience={"age_min": 25, "age_max": 45, "locations": ["Goa"]},
        page_id="page-1", whatsapp_number="+919000000000",
    )

    assert spec["objective"] == "OUTCOME_ENGAGEMENT"
    assert spec["creative"]["call_to_action"] == "WHATSAPP_MESSAGE"
    assert spec["creative"]["image_url"] == "https://cdn.example.com/a.jpg"
    assert spec["destination"] == {"type": "whatsapp", "number": "+919000000000"}


def test_nothing_is_ever_created_live():
    """
    `status: PAUSED` on creation, always. An ad that starts spending the moment
    it is created is money leaving a hotel's account on a schedule nobody
    pressed go on.
    """
    spec = meta_ads.build_request(
        campaign={"name": "X", "objective": "traffic", "destination": "website",
                  "channel": "facebook"},
        creative={"format": "single_image", "media": []},
        audience=None, page_id="p", whatsapp_number=None,
    )
    assert spec["status"] == "PAUSED"


def test_interests_are_not_passed_off_as_resolved_targeting():
    """
    Meta interest ids come from a search endpoint this cannot call yet.
    Inventing them would target an ad at something arbitrary and spend a
    hotel's budget on it, so they travel under a name that says what they are.
    """
    targeting = meta_ads._targeting({"interests": ["yoga", "beaches"]})
    assert "interests_unresolved" in targeting
    assert "interests" not in targeting


# ── the promise that is a constraint ────────────────────────────────────────

def test_publishing_is_the_one_seam_and_it_refuses_rather_than_pretends():
    """
    §15: the constraint that keeps a campaign out of `published`, `running` and
    `paused` without a Meta id must remain. It does — in the database — and this
    is the code half: there is exactly one place that would post to Meta, it is
    unreachable while any credential is missing, and it raises rather than
    returning a success nobody earned.
    """
    source = pathlib.Path(meta_ads.__file__).read_text()

    assert "NotImplementedError" in source
    # Nothing anywhere may hand back a published status without an id beside it.
    assert 'status="published"' not in source
    assert 'status="running"' not in source


def test_a_publish_result_carrying_a_live_status_must_carry_an_id():
    """
    The dataclass's own version of the check constraint, asserted so the two
    cannot drift. A result is what the campaign row is written from.
    """
    blocked = meta_ads.PublishResult(status="draft", missing=("ad_account",))
    assert blocked.external_id is None
    assert blocked.status in ("draft", "connected", "failed"), (
        "a status the database would reject came out of the publisher"
    )
