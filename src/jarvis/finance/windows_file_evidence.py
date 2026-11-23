"""Native metadata observations only; never authorize private file access."""
from __future__ import annotations

import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileEvidence:
    status: str
    identity: tuple[int, int] | None = None
    owner_sid: str | None = None
    links: int | None = None
    safe: bool = False
    effective_access_verified: bool = False


class _Info(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
               ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
               ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
               ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
               ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


@dataclass(frozen=True)
class ChainEvidence:
    status: str
    elements: tuple[FileEvidence, ...] = ()
    safe: bool = False
    effective_access_verified: bool = False


@contextmanager
def pinned_chain(path: Path, *, root: Path, directory: bool = False,
                 expected_identity: tuple[int, int] | None = None) -> Iterator[ChainEvidence]:
    """Observe drive-to-leaf metadata while holding non-write/delete-sharing handles."""
    if os.name != "nt":
        yield ChainEvidence("unsupported_platform")
        return
    if (not path.is_absolute() or not root.is_absolute() or ".." in path.parts
            or ".." in root.parts or str(path).startswith("\\\\") or ":" in str(path)[2:]):
        yield ChainEvidence("unsupported_path")
        return
    try:
        path.relative_to(root)
    except ValueError:
        yield ChainEvidence("outside_root")
        return
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetVolumeInformationByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    handles: list[Any] = []
    observations: list[FileEvidence] = []
    volume: int | None = None
    status = "unverified"
    try:
        for item in [*reversed(path.parents), path]:
            handle = kernel.CreateFileW(str(item), 0x20080, 1, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                status = ("missing" if error in (2, 3) else "access_denied" if error == 5
                          else "sharing_violation" if error in (32, 33) else "metadata_unavailable")
                break
            handles.append(handle)
            observed = observe_file(item, root=Path(path.anchor),
                directory=item != path or directory,
                expected_identity=expected_identity if item == path else None)
            observations.append(observed)
            if observed.status != "unverified" or observed.identity is None:
                status = observed.status
                break
            fs = ctypes.create_unicode_buffer(32)
            if not kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, fs, len(fs)) or fs.value != "NTFS":
                status = "unsupported_filesystem"
                break
            if volume is not None and observed.identity[0] != volume:
                status = "volume_changed"
                break
            volume = observed.identity[0]
        yield ChainEvidence(status, tuple(observations))
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def observe_chain(path: Path, *, root: Path, directory: bool = False,
                  expected_identity: tuple[int, int] | None = None) -> ChainEvidence:
    """Return an observation only; released handles cannot protect a later reopen."""
    with pinned_chain(path, root=root, directory=directory, expected_identity=expected_identity) as evidence:
        return evidence


def observe_file(path: Path, *, root: Path, directory: bool = False,
                 expected_owner: str | None = None,
                 expected_identity: tuple[int, int] | None = None) -> FileEvidence:
    """Inspect an opened disk object without reading content or claiming ACL safety."""
    if os.name != "nt":
        return FileEvidence("unsupported_platform")
    if (not path.is_absolute() or not root.is_absolute() or ".." in path.parts
            or str(path).startswith("\\\\") or ":" in str(path)[2:]):
        return FileEvidence("unsupported_path")
    try:
        path.relative_to(root)
    except ValueError:
        return FileEvidence("outside_root")
    # No package import side effects; APIs are loaded only for explicit observation.
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.IsValidSecurityDescriptor.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(path), 0x20080, 1, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        return FileEvidence("missing" if error in (2, 3) else "insufficient_privilege_to_verify")
    descriptor, owner, dacl, sid_text = (ctypes.c_void_p() for _ in range(4))
    try:
        info = _Info()
        if kernel.GetFileType(handle) != 1 or not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            return FileEvidence("unsupported_filesystem")
        identity = (int(info.volume), (int(info.index_high) << 32) | int(info.index_low))
        if info.attributes & 0x400:
            return FileEvidence("reparse_point", identity)
        if bool(info.attributes & 0x10) != directory:
            return FileEvidence("unexpected_file_type", identity)
        if expected_identity is not None and expected_identity != identity:
            return FileEvidence("identity_changed", identity)
        if not directory and info.links != 1:
            return FileEvidence("multiple_links", identity, links=int(info.links))
        if advapi.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
            return FileEvidence("insufficient_privilege_to_verify", identity)
        if not descriptor.value or not advapi.IsValidSecurityDescriptor(descriptor):
            return FileEvidence("invalid_security_descriptor", identity)
        if not dacl.value:
            return FileEvidence("unsafe_acl", identity)
        if not owner.value or not advapi.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
            return FileEvidence("unverified", identity)
        sid = ctypes.wstring_at(sid_text)
        if expected_owner is not None and expected_owner != sid:
            return FileEvidence("wrong_owner", identity, sid, int(info.links))
        # DACL presence and SID identity do not prove effective access, inherited
        # trustee safety, ancestor traversal, filesystem policy or privilege isolation.
        return FileEvidence("unverified", identity, sid, int(info.links))
    finally:
        if sid_text.value:
            kernel.LocalFree(sid_text)
        if descriptor.value:
            kernel.LocalFree(descriptor)
        kernel.CloseHandle(handle)
