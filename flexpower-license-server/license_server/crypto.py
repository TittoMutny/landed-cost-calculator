"""Ed25519 signing of licence tokens.

Token format:  base64url(JSON payload) "." base64url(signature over the first part)

Only Flexpower holds the private key (on the licence server). The EMS app ships
with the public key and rejects any token that wasn't signed with it, so a
customer can't forge an "active" licence or impersonate the server.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _passphrase() -> bytes | None:
    p = os.environ.get("FLEXPOWER_LICENSE_KEY_PASSPHRASE")
    return p.encode() if p else None


def generate_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to overwrite the signing key")
    key = Ed25519PrivateKey.generate()
    pw = _passphrase()
    enc = serialization.BestAvailableEncryption(pw) if pw else serialization.NoEncryption()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def load_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=_passphrase())
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("signing key must be Ed25519")
    return key


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign_token(key: Ed25519PrivateKey, payload: dict) -> str:
    body = b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return f"{body}.{b64url(key.sign(body.encode()))}"
