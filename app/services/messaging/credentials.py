"""
Where a channel's secrets come from — which is never the database.

`whatsapp_connections.credentials_ref` names an entry; this module turns that
name into an actual token from the process environment. Two properties on one
deployment use two refs and two sets of environment variables:

    credentials_ref = 'default'   → WHATSAPP_ACCESS_TOKEN, WHATSAPP_APP_SECRET
    credentials_ref = 'casa'      → WHATSAPP_ACCESS_TOKEN__CASA, …

The indirection is the point. A token stored on the row is a token in every
backup, every read replica, every `select *` in a support session, and in the
PWA the moment somebody adds the column to a response model by accident. A row
that names `'casa'` tells an attacker nothing.

`credentials_ref = 'sandbox'` (settings.whatsapp_sandbox_ref) selects the fake
adapter and needs no secret at all — that is what makes a development
connection unable to reach a real phone even if someone points it at one.
"""
import logging
import os
from dataclasses import dataclass

from app.config import settings
from app.services.messaging.channels import NOT_CONFIGURED, MessagingError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelCredentials:
    access_token: str | None
    app_secret: str | None
    verify_token: str | None
    is_sandbox: bool


def _env(name: str, ref: str) -> str | None:
    """`WHATSAPP_ACCESS_TOKEN__CASA` for ref 'casa', falling back to the default."""
    if ref and ref != "default":
        scoped = os.environ.get(f"{name}__{ref.upper().replace('-', '_')}")
        if scoped:
            return scoped
    return os.environ.get(name)


def resolve(credentials_ref: str) -> ChannelCredentials:
    ref = credentials_ref or "default"
    if ref == settings.whatsapp_sandbox_ref:
        return ChannelCredentials(
            access_token=None, app_secret=None, verify_token=None, is_sandbox=True
        )

    return ChannelCredentials(
        access_token=_env("WHATSAPP_ACCESS_TOKEN", ref) or settings.whatsapp_access_token,
        app_secret=_env("WHATSAPP_APP_SECRET", ref) or settings.whatsapp_app_secret,
        verify_token=_env("WHATSAPP_VERIFY_TOKEN", ref) or settings.whatsapp_verify_token,
        is_sandbox=False,
    )


def adapter_for(connection: dict):
    """
    Build the adapter a connection should use.

    `is_sandbox` is read from the connection row, not inferred from the base
    URL, so a misconfigured hostname cannot silently turn a sandbox into a live
    send. A live connection with no token raises here — at construction, at the
    boundary — rather than producing a 401 from Meta three layers down that
    reads like a network problem.
    """
    from app.services.messaging.whatsapp.adapter import WhatsAppAdapter
    from app.services.messaging.whatsapp.fake import FakeWhatsAppAdapter

    creds = resolve(connection.get("credentials_ref") or "default")
    if connection.get("is_sandbox") or creds.is_sandbox:
        return FakeWhatsAppAdapter(phone_number_id=connection.get("phone_number_id") or "sandbox")

    if not creds.access_token:
        raise MessagingError(
            NOT_CONFIGURED,
            f"no WhatsApp access token for credentials_ref "
            f"{connection.get('credentials_ref')!r}",
        )

    return WhatsAppAdapter(
        phone_number_id=connection["phone_number_id"],
        access_token=creds.access_token,
    )
