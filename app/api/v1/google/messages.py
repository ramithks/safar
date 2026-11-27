"""
What a hotel owner reads when Google goes wrong.

One table, one function, and a rule: **no slug from Google or from our own
error types ever reaches a screen.** `invalid_grant`, `SERVICE_DISABLED` and
`PERMISSION_DENIED` are precise, useful, and meaningless to the person who runs
a nine-room haveli in Udaipur.

Every sentence here has to pass three tests:

  1. It says what happened, in words about the hotel's business.
  2. It says who can fix it — the owner, or us.
  3. It contains no API, OAuth, token, scope, endpoint or project vocabulary.

The third is the one that slips. "Your OAuth token has expired, please
re-authorize" fails it; "Your Google connection has expired — reconnect it and
we'll pick up where we left off" passes.

`actionable` exists so the UI knows whether to show a Reconnect button. A
problem the owner cannot fix must not be presented as one they can, because a
hotelier who reconnects four times against an outage learns the product is
broken.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class HumanError:
    message: str
    # True when there is something the owner can do right now.
    actionable: bool
    # The button, when there is one.
    action: str | None = None


_UNKNOWN = HumanError(
    "Something went wrong talking to Google. We're looking into it — please try again shortly.",
    actionable=False,
)

_MESSAGES: dict[str, HumanError] = {
    # ── the owner can fix these ──────────────────────────────────────────
    "reconnect_required": HumanError(
        "Your Google connection has expired. Reconnect it and we'll pick up where we left off.",
        actionable=True,
        action="reconnect",
    ),
    "not_connected": HumanError(
        "Google Business Profile isn't connected yet.",
        actionable=True,
        action="connect",
    ),
    "no_refresh_token": HumanError(
        "Google didn't give us lasting access to your profile. Please connect again and "
        "accept all the permissions on Google's screen.",
        actionable=True,
        action="reconnect",
    ),
    "no_permission": HumanError(
        "This Google account can't manage that hotel's profile. Sign in with the Google "
        "account that owns the listing, or ask its owner to give you access.",
        actionable=True,
        action="reconnect",
    ),
    "no_accounts": HumanError(
        "That Google account doesn't have a Business Profile yet. Create one for your hotel "
        "on Google, then connect it here.",
        actionable=True,
        action="reconnect",
    ),
    "no_locations": HumanError(
        "We couldn't find any hotels on that Google account. If you manage your listing from "
        "a different Google account, connect that one instead.",
        actionable=True,
        action="reconnect",
    ),
    "not_verified": HumanError(
        "Google hasn't verified this listing yet, so it won't show your reviews to us. "
        "Finish verification with Google and this will start working on its own.",
        actionable=False,
    ),
    "consent_declined": HumanError(
        "You cancelled before Google finished connecting. Nothing has changed.",
        actionable=True,
        action="connect",
    ),
    "state_expired": HumanError(
        "That took a little too long and the link timed out. Please start again.",
        actionable=True,
        action="connect",
    ),
    "bad_state": HumanError(
        "We couldn't finish connecting Google securely. Please start again from Settings.",
        actionable=True,
        action="connect",
    ),
    "save_failed": HumanError(
        "Google connected, but we couldn't finish saving it. Please try connecting again.",
        actionable=True,
        action="connect",
    ),
    "already_claimed": HumanError(
        "That Google listing is already connected to another hotel on StaySuite. Disconnect it "
        "there first, or pick a different listing.",
        actionable=True,
        action="choose",
    ),

    # ── ours, not theirs ─────────────────────────────────────────────────
    "not_configured": HumanError(
        "Google Business Profile isn't switched on for StaySuite yet. We're working on it — "
        "nothing for you to do.",
        actionable=False,
    ),
    "not_approved": HumanError(
        "Reviews and posts aren't available yet while Google finishes approving StaySuite. "
        "Your profile is connected and everything else keeps working.",
        actionable=False,
    ),
    "google_unavailable": HumanError(
        "Google isn't responding at the moment. We'll keep trying — nothing is lost.",
        actionable=False,
    ),
    "rate_limited": HumanError(
        "Google is asking us to slow down. Please try again in a few minutes.",
        actionable=False,
    ),
    "not_found": HumanError(
        "We couldn't find that on your Google profile any more. It may have been changed or "
        "removed on Google's side.",
        actionable=False,
    ),
    "draft_failed": HumanError(
        "We couldn't write a draft this time. Please try again, or write the reply yourself.",
        actionable=False,
    ),
}


def humanise(reason: str | None) -> HumanError:
    return _MESSAGES.get(reason or "", _UNKNOWN)
