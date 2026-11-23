"""Current-user DPAPI session bytes for operator-owned storage; never bank auth."""
from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

MAGIC = b"JARVIS-DPAPI-STATE-v1\x00"
MAX_STATE = 1024 * 1024
_ENTROPY = b"jarvis-local-operator-state-v1"


class SessionStorageUnavailable(ValueError):
    """Return a fixed diagnostic without plaintext or OS secret material."""


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt" or type(data) is not bytes or not 0 < len(data) <= MAX_STATE + 65536:
        raise SessionStorageUnavailable("Windows session storage unavailable")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    original = ctypes.create_string_buffer(data)
    entropy = ctypes.create_string_buffer(_ENTROPY)
    incoming = _Blob(len(data), ctypes.cast(original, ctypes.POINTER(ctypes.c_ubyte)))
    extra = _Blob(len(_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    try:
        # UI_FORBIDDEN only: never LOCAL_MACHINE, never a hidden interactive prompt.
        if not function(ctypes.byref(incoming), None, ctypes.byref(extra), None, None, 1, ctypes.byref(output)):
            raise SessionStorageUnavailable("Windows session storage unavailable")
        if not output.data or not 0 < output.size <= MAX_STATE + 65536:
            raise SessionStorageUnavailable("Windows session storage unavailable")
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(original, 0, len(original))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)


def protect_state(plaintext: bytes) -> bytes:
    """Encrypt bounded operator state with current-user DPAPI and purpose entropy."""
    if type(plaintext) is not bytes or not 0 < len(plaintext) <= MAX_STATE:
        raise SessionStorageUnavailable("Windows session storage unavailable")
    return MAGIC + _crypt(plaintext, decrypt=False)


def unprotect_state(ciphertext: bytes) -> bytes:
    """Reject plaintext fallback, corrupted DPAPI blobs and oversized plaintext."""
    if type(ciphertext) is not bytes or not ciphertext.startswith(MAGIC):
        raise SessionStorageUnavailable("Windows session storage unavailable")
    result = _crypt(ciphertext[len(MAGIC):], decrypt=True)
    if len(result) > MAX_STATE:
        raise SessionStorageUnavailable("Windows session storage unavailable")
    return result


def save_state(path: Path, plaintext: bytes) -> None:
    """Publish ciphertext atomically; no plaintext temporary file or implicit mkdir."""
    ciphertext = protect_state(plaintext)
    if path.is_symlink() or not path.parent.is_dir():
        raise SessionStorageUnavailable("Windows session storage unavailable")
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".jarvis-state-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SessionStorageUnavailable("Windows session storage unavailable") from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                raise SessionStorageUnavailable("Windows session storage unavailable") from None


def load_state(path: Path) -> bytes:
    """Read only a bounded encrypted file; caller owns path/access/freshness policy."""
    if path.is_symlink():
        raise SessionStorageUnavailable("Windows session storage unavailable")
    try:
        with path.open("rb") as stream:
            ciphertext = stream.read(MAX_STATE + 65536 + len(MAGIC) + 1)
    except OSError:
        raise SessionStorageUnavailable("Windows session storage unavailable") from None
    return unprotect_state(ciphertext)
