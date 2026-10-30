"""Damaged stored credentials, built from a real token. Shared by the device and apply tests."""

import base64

from ai_surveillance.credentials import CredentialCipher

KINDS = ["unknown_format", "not_base64", "truncated", "shorter_than_tag", "invalid_tag", "changed_key", "not_utf8"]


def damage(kind: str, token: str, context: str, key: bytes) -> str:
    """Return a damaged version of `token` (a v1 ciphertext of `context`)."""
    raw = base64.urlsafe_b64decode(token[3:])
    encode = lambda data: "v1:" + base64.urlsafe_b64encode(data).decode()  # noqa: E731
    if kind == "unknown_format":
        return "v2:" + token[3:]
    if kind == "not_base64":
        return "v1:@@this is not base64@@"
    if kind == "truncated":
        return encode(raw[:6])  # less than a nonce
    if kind == "shorter_than_tag":
        return encode(raw[:20])  # nonce plus 8 bytes: no room for the 16-byte tag
    if kind == "invalid_tag":
        return encode(raw[:-1] + bytes([raw[-1] ^ 0x01]))
    if kind == "changed_key":
        return CredentialCipher(b"\x07" * 32).encrypt("fake-other-key-pass", context)
    if kind == "not_utf8":
        # Authentic for this key and device, but the plaintext is not UTF-8.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = b"\x01" * 12
        return encode(nonce + AESGCM(key).encrypt(nonce, b"\xff\xfe\xfd", context.encode()))
    raise ValueError(kind)
