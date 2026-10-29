"""Encryption at rest (AES-256-GCM): camera/NVR passwords and gallery reference images.

The key comes from CREDENTIALS_KEY or, if unset, from a key file created on
first start with owner-only permissions. Losing the key means re-entering
device passwords and enrolling every authorized person's reference images
again; back it up together with the database.
"""

import base64
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings

_NONCE_BYTES = 12
_TAG_BYTES = 16  # AES-GCM authentication tag, appended to the ciphertext
_PREFIX = "v1:"  # format version, so the scheme can change later
_BLOB_MAGIC = b"AISENC1\x00"  # same role for encrypted files

# Operator-facing reasons. They never contain the stored value. AES-GCM cannot
# tell a changed key from a damaged value (both fail the authentication tag).
UNKNOWN_FORMAT = "Stored credential has an unknown format (the stored value is damaged)"
DAMAGED = "Stored credential is damaged"
KEY_OR_DAMAGED = "Stored credential cannot be decrypted: the credentials key changed or the stored value is damaged"
REPAIR_HINT = "Edit the device and enter its password again, or restore the previous credentials key from its backup"


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
        """Every failure is a CredentialError with one of the reasons above; nothing else escapes."""
        if not isinstance(token, str) or not token.startswith(_PREFIX):
            raise CredentialError(UNKNOWN_FORMAT)
        try:
            raw = base64.urlsafe_b64decode(token[len(_PREFIX):])  # binascii.Error is a ValueError
        except ValueError:
            raise CredentialError(DAMAGED) from None
        if len(raw) < _NONCE_BYTES + _TAG_BYTES:
            raise CredentialError(DAMAGED)  # truncated: no room for nonce and tag
        try:
            plaintext = self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode())
        except InvalidTag:
            raise CredentialError(KEY_OR_DAMAGED) from None
        try:
            return plaintext.decode()
        except UnicodeDecodeError:
            raise CredentialError(DAMAGED) from None

    def encrypt_bytes(self, data: bytes, context: str) -> bytes:
        """Binary form for files: magic, nonce, ciphertext and tag. `context` is bound as associated data."""
        nonce = secrets.token_bytes(_NONCE_BYTES)
        return _BLOB_MAGIC + nonce + self._aead.encrypt(nonce, data, context.encode())

    def decrypt_bytes(self, blob: bytes, context: str) -> bytes:
        """Every failure is a CredentialError (UNKNOWN_FORMAT, DAMAGED or KEY_OR_DAMAGED)."""
        if not blob.startswith(_BLOB_MAGIC):
            raise CredentialError(UNKNOWN_FORMAT)
        raw = blob[len(_BLOB_MAGIC):]
        if len(raw) < _NONCE_BYTES + _TAG_BYTES:
            raise CredentialError(DAMAGED)
        try:
            return self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode())
        except InvalidTag:
            raise CredentialError(KEY_OR_DAMAGED) from None
