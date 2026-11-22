"""Encrypted storage for the SMTP password.

On Windows the password is encrypted with DPAPI (machine scope), so the file is
useless if copied to another PC. The install script also restricts the file so
only Administrators, SYSTEM and the service account can read it.

On other systems DPAPI does not exist; the file is written with owner-only
permissions instead and a warning is shown.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DPAPI_PREFIX = b"DPAPI1:"
_PLAIN_PREFIX = b"PLAIN1:"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04


def dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    fn.restype = wintypes.BOOL

    buf = ctypes.create_string_buffer(data, len(data))
    inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if encrypt else 0)
    if not fn(ctypes.byref(inp), None, None, None, None, flags, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def save_secret(path: Path, secret: str) -> bool:
    """Write the secret; returns True when it was encrypted with DPAPI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if dpapi_available():
        blob, encrypted = _DPAPI_PREFIX + _dpapi(secret.encode("utf-8"), True), True
    else:
        blob, encrypted = _PLAIN_PREFIX + secret.encode("utf-8"), False
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return encrypted


def load_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    blob = path.read_bytes()
    if blob.startswith(_DPAPI_PREFIX):
        if not dpapi_available():
            raise RuntimeError(f"{path} is DPAPI-encrypted and can only be read on the Windows PC that wrote it")
        return _dpapi(blob[len(_DPAPI_PREFIX):], False).decode("utf-8")
    if blob.startswith(_PLAIN_PREFIX):
        return blob[len(_PLAIN_PREFIX):].decode("utf-8")
    raise RuntimeError(f"{path} is not a recognised secret file")
