"""
Reading and writing per-hotel channel credentials.

`credentials.py` answers "what token does this ref use?" and, before this
module, the only possible answer was an environment variable — which is fine
for the accounts we own and impossible for a hotel that connects its own,
because that would mean a redeploy per hotel.

So a ref now resolves in two steps, database first, environment second:

    credentials_ref = 'sandbox'   → the fake, no secret anywhere
    credentials_ref = 'prop:<id>' → messaging_credentials, encrypted
    credentials_ref = 'default'   → INSTAGRAM_ACCESS_TOKEN, ours

The environment is still authoritative for the accounts we operate, and stays
the fallback so nothing that works today stops working. All of this is
synchronous, called through `run_db`, matching every other DB helper here.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db.client import get_db
from app.services.messaging import crypto

logger = logging.getLogger(__name__)

TABLE = "messaging_credentials"

# A ref shaped like this means "look in the database". Prefixed rather than a
# bare uuid so a glance at a connection row says where the token lives, and so a
# hand-written ref can never collide with one of ours.
DB_REF_PREFIX = "prop:"


def ref_for_property(property_id: str) -> str:
    return f"{DB_REF_PREFIX}{property_id}"


def is_db_ref(credentials_ref: str | None) -> bool:
    return bool(credentials_ref) and credentials_ref.startswith(DB_REF_PREFIX)


def load_token(credentials_ref: str, channel: str) -> str | None:
    """The decrypted access token for a ref, or None if there isn't a live one."""
    if not is_db_ref(credentials_ref):
        return None

    db = get_db()
    row = (
        db.table(TABLE)
        .select("access_token_ciphertext,status,token_expires_at")
        .eq("channel", channel)
        .eq("credentials_ref", credentials_ref)
        .limit(1)
        .execute()
    )
    if not row.data:
        return None

    record = row.data[0]
    if record.get("status") != "active":
        logger.info("credential for %s is %s", credentials_ref, record.get("status"))
        return None

    # An expired token is not returned. Meta would answer 190 and the adapter
    # would classify it as NOT_CONFIGURED three layers down; refusing here gets
    # the owner a "reconnect Instagram" instead of a mystery.
    expires_at = record.get("token_expires_at")
    if expires_at:
        try:
            when = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if when <= datetime.now(timezone.utc):
                logger.warning("credential for %s expired at %s", credentials_ref, when)
                return None
        except ValueError:
            pass

    try:
        return crypto.decrypt(record["access_token_ciphertext"])
    except crypto.CredentialCryptoError:
        logger.exception("could not decrypt credential for %s", credentials_ref)
        return None


def load_refresh_token(credentials_ref: str, channel: str) -> str | None:
    """
    The decrypted refresh token for a ref, or None.

    Separate from `load_token` and deliberately not expiry-checked. A Google
    access token expires every hour and the refresh token is what survives that
    — refusing to hand it back because the *access* token beside it has gone
    stale would make the one credential that can fix the problem unreachable at
    exactly the moment it is needed.

    Only `status` gates it, because a revoked credential must stay dead.
    """
    if not is_db_ref(credentials_ref):
        return None

    db = get_db()
    row = (
        db.table(TABLE)
        .select("refresh_token_ciphertext,status")
        .eq("channel", channel)
        .eq("credentials_ref", credentials_ref)
        .limit(1)
        .execute()
    )
    if not row.data:
        return None

    record = row.data[0]
    if record.get("status") != "active":
        return None

    ciphertext = record.get("refresh_token_ciphertext")
    if not ciphertext:
        return None

    try:
        return crypto.decrypt(ciphertext)
    except crypto.CredentialCryptoError:
        logger.exception("could not decrypt refresh token for %s", credentials_ref)
        return None


def update_access_token(
    *,
    channel: str,
    credentials_ref: str,
    access_token: str,
    expires_in: int | None,
    refresh_token: str | None = None,
) -> bool:
    """
    Replace only the access token on an existing credential.

    A refresh is not a reconnection, and must not be written as one. `save_token`
    upserts the whole row, so calling it on the hourly Google refresh would
    overwrite `external_account_id`, `scopes` and `partner_id` with whatever the
    refresh response happened to contain — which for `external_account_id` is
    nothing. This touches the three columns a refresh actually changes and
    leaves the identity of the credential alone.
    """
    expires_at = None
    if expires_in:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()

    patch = {
        "access_token_ciphertext": crypto.encrypt(access_token),
        "token_expires_at": expires_at,
        "last_refreshed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Google normally omits this on refresh, but rotates it occasionally. When
    # it does arrive it is the new credential and the old one stops working.
    if refresh_token:
        patch["refresh_token_ciphertext"] = crypto.encrypt(refresh_token)

    db = get_db()
    result = (
        db.table(TABLE)
        .update(patch)
        .eq("channel", channel)
        .eq("credentials_ref", credentials_ref)
        .eq("status", "active")
        .execute()
    )
    return bool(result.data)


def save_token(
    *,
    channel: str,
    credentials_ref: str,
    property_id: str,
    partner_id: str,
    access_token: str,
    expires_in: int | None,
    scopes: tuple[str, ...] | list[str] | None,
    external_account_id: str | None,
    refresh_token: str | None = None,
) -> dict:
    """
    Store (or replace) the token for a ref. Reconnecting overwrites.

    `refresh_token` is optional and, when omitted, **the stored one is left
    alone**. That is not a convenience — it is required for correctness on
    Google. Google returns a refresh token on the first consent and omits it on
    every subsequent refresh, so writing `None` through on a routine hourly
    refresh would erase the only credential that can perform the next one, and
    the hotel would silently drop off Google an hour later.
    """
    expires_at = None
    if expires_in:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()

    payload = {
        "channel": channel,
        "credentials_ref": credentials_ref,
        "property_id": property_id,
        "partner_id": partner_id,
        "access_token_ciphertext": crypto.encrypt(access_token),
        "token_expires_at": expires_at,
        "scopes": list(scopes) if scopes else None,
        "external_account_id": external_account_id,
        "status": "active",
        "revoked_at": None,
        "last_refreshed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        payload["refresh_token_ciphertext"] = crypto.encrypt(refresh_token)

    db = get_db()
    result = (
        db.table(TABLE)
        .upsert(payload, on_conflict="channel,credentials_ref")
        .execute()
    )
    return (result.data or [{}])[0]


def revoke(*, channel: str, credentials_ref: str) -> bool:
    """
    Mark a credential revoked. The ciphertext stays.

    Deleting the row would be tidier and worse: a deauthorize webhook that
    arrives twice, or arrives for an account that reconnected in between, would
    silently remove a live credential. `status` makes the sequence recoverable
    and auditable, and `load_token` already refuses anything not active.
    """
    db = get_db()
    result = (
        db.table(TABLE)
        .update(
            {
                "status": "revoked",
                "revoked_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("channel", channel)
        .eq("credentials_ref", credentials_ref)
        .execute()
    )
    return bool(result.data)


def revoke_by_account(*, channel: str, external_account_id: str) -> list[dict]:
    """
    Revoke by the account id Meta names in a deauthorize callback.

    Meta tells us *who* removed the app, not which of our refs that was, so the
    lookup has to go the other way round.
    """
    db = get_db()
    rows = (
        db.table(TABLE)
        .select("credentials_ref,property_id")
        .eq("channel", channel)
        .eq("external_account_id", external_account_id)
        .eq("status", "active")
        .execute()
    )
    for row in rows.data or []:
        revoke(channel=channel, credentials_ref=row["credentials_ref"])
    return rows.data or []
