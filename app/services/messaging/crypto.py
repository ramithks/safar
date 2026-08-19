"""
Encryption for the one thing we store that must not be readable in a backup.

`messaging_credentials.access_token_ciphertext` holds a Fernet token. The key
is `MESSAGING_CREDENTIALS_KEY` in the environment and is never written to
Postgres, so a database dump — the thing that actually leaks — decrypts to
nothing without a second, separately-held secret.

Fernet rather than raw AES because it is authenticated: a ciphertext somebody
has edited fails to decrypt instead of producing a different token that gets
sent to Meta.

A missing key raises rather than falling back to plaintext. Storing a token in
the clear because a variable was unset is exactly the failure this module
exists to prevent, and it would be invisible until the day of the leak.
"""
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)


class CredentialCryptoError(RuntimeError):
    """The key is missing or the ciphertext will not decrypt."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = (settings.messaging_credentials_key or "").strip()
    if not key:
        raise CredentialCryptoError(
            "MESSAGING_CREDENTIALS_KEY is not set — per-hotel channel tokens "
            "cannot be stored or read. Generate one with: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialCryptoError(
            "MESSAGING_CREDENTIALS_KEY is not a valid Fernet key (32 url-safe "
            "base64-encoded bytes)."
        ) from exc


def encrypt(plaintext: str) -> str:
    if not plaintext:
        raise CredentialCryptoError("refusing to encrypt an empty token")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Almost always a rotated key against old rows. Say so: "invalid token"
        # on its own sends people looking at Meta.
        raise CredentialCryptoError(
            "stored credential will not decrypt — MESSAGING_CREDENTIALS_KEY has "
            "most likely changed since it was written. The hotel must reconnect."
        ) from exc


def is_configured() -> bool:
    """Whether per-hotel credentials can be used at all, without raising."""
    try:
        _fernet()
        return True
    except CredentialCryptoError:
        return False
