"""Encryption of stored camera/NVR passwords (AES-256-GCM).

The key comes from CREDENTIALS_KEY or, if unset, from a key file created on
first start with owner-only permissions. Losing the key means re-entering
device passwords; it does not affect anything else.
"""

import base64
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings

_NONCE_BYTES = 12
_PREFIX = "v1:"  # format version, so the scheme can change later


class CredentialError(Exception):
    pass


def _decode_key(text: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(text.strip())
    except ValueError as exc:
        raise CredentialError("Credentials key is not valid base64") from exc
    if len(key) != 32:
        raise CredentialError("Credentials key must be 32 bytes")
    return key


def load_key(settings: Settings) -> bytes:
    if settings.credentials_key is not None:
        return _decode_key(settings.credentials_key.get_secret_value())
    path: Path = settings.credentials_key_file
    if path.exists():
        return _decode_key(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    # O_EXCL: never overwrite a key that appeared meanwhile.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(base64.urlsafe_b64encode(key).decode())
    return key


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, context: str) -> str:
        """Encrypt; `context` (the device id) is bound as associated data."""
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()

    def decrypt(self, token: str, context: str) -> str:
        if not token.startswith(_PREFIX):
            raise CredentialError("Unknown credential format")
        raw = base64.urlsafe_b64decode(token[len(_PREFIX):])
        try:
            return self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode()).decode()
        except InvalidTag as exc:
            raise CredentialError("Stored credential cannot be decrypted with the current key") from exc
