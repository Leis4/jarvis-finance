"""Current-token Authz observations only; no ACL mutation or isolation claim."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.finance.windows_file_evidence import _Info, pinned_chain


class _Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class _Request(ctypes.Structure):
    _fields_ = [("desired", wintypes.DWORD), ("self_sid", ctypes.c_void_p),
        ("types", ctypes.c_void_p), ("type_count", wintypes.DWORD), ("optional", ctypes.c_void_p)]


class _Reply(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("granted", ctypes.POINTER(wintypes.DWORD)),
        ("sacl", ctypes.POINTER(wintypes.DWORD)), ("error", ctypes.POINTER(wintypes.DWORD))]


@dataclass(frozen=True)
class AccessEvidence:
    status: str
    granted: int | None = None
    context: str = "current_process_token"
    safe: bool = False
    isolation_verified: bool = False


def _check(descriptor: ctypes.c_void_p, desired: int) -> AccessEvidence:
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    authz: Any = ctypes.WinDLL("authz", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    authz.AuthzInitializeResourceManager.argtypes = [wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    authz.AuthzInitializeResourceManager.restype = wintypes.BOOL
    authz.AuthzInitializeContextFromToken.argtypes = [wintypes.DWORD, wintypes.HANDLE,
        ctypes.c_void_p, ctypes.c_void_p, _Luid, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    authz.AuthzInitializeContextFromToken.restype = wintypes.BOOL
    authz.AuthzAccessCheck.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(_Request),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_Reply), ctypes.c_void_p]
    authz.AuthzAccessCheck.restype = wintypes.BOOL
    authz.AuthzFreeContext.argtypes = [ctypes.c_void_p]
    authz.AuthzFreeResourceManager.argtypes = [ctypes.c_void_p]
    token, manager, context = (ctypes.c_void_p() for _ in range(3))
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            return AccessEvidence("unavailable")
        if not authz.AuthzInitializeResourceManager(1, None, None, None, "Jarvis observation", ctypes.byref(manager)):
            return AccessEvidence("unavailable")
        if not authz.AuthzInitializeContextFromToken(0, token, manager, None, _Luid(), None, ctypes.byref(context)):
            return AccessEvidence("unavailable")
        granted, sacl, error = (wintypes.DWORD() for _ in range(3))
        request = _Request(desired, None, None, 0, None)
        reply = _Reply(1, ctypes.pointer(granted), ctypes.pointer(sacl), ctypes.pointer(error))
        if not authz.AuthzAccessCheck(0, context, ctypes.byref(request), None, descriptor, None, 0, ctypes.byref(reply), None):
            return AccessEvidence("unavailable")
        if error.value not in (0, 5):
            return AccessEvidence("unavailable")
        return AccessEvidence("granted" if error.value == 0 and granted.value & desired == desired else "denied", granted.value)
    finally:
        if context.value:
            authz.AuthzFreeContext(context)
        if manager.value:
            authz.AuthzFreeResourceManager(manager)
        if token.value:
            kernel.CloseHandle(token)


def _desired(value: int) -> bool:
    return type(value) is int and value > 0 and value & ~0x20001 == 0


def _evaluate_current_sddl(sddl: str, desired: int = 1) -> AccessEvidence:
    """Evaluate an in-memory descriptor with a genuine token, never assign its ACL."""
    if os.name != "nt":
        return AccessEvidence("unsupported_platform")
    if not _desired(desired) or not isinstance(sddl, str) or not 0 < len(sddl) <= 4096:
        return AccessEvidence("invalid_input")
    advapi: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    try:
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            return AccessEvidence("invalid_descriptor")
        return _check(descriptor, desired)
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


def _evaluate_current_file(path: Path, *, root: Path, desired: int = 1) -> AccessEvidence:
    """Evaluate the opened object's actual descriptor, not bank access or other users."""
    if not _desired(desired):
        return AccessEvidence("invalid_input")
    with pinned_chain(path, root=root) as chain:
        if chain.status != "unverified" or not chain.elements:
            return AccessEvidence("unavailable")
        kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi: Any = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi.GetSecurityInfo.restype = wintypes.DWORD
        handle = kernel.CreateFileW(str(path), 0x20080, 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            return AccessEvidence("unavailable")
        descriptor, owner, dacl = (ctypes.c_void_p() for _ in range(3))
        try:
            info = _Info()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                return AccessEvidence("unavailable")
            if (int(info.volume), (int(info.index_high) << 32) | int(info.index_low)) != chain.elements[-1].identity:
                return AccessEvidence("identity_changed")
            if advapi.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) or not dacl.value:
                return AccessEvidence("unavailable")
            return _check(descriptor, desired)
        finally:
            if descriptor.value:
                kernel.LocalFree(descriptor)
            kernel.CloseHandle(handle)


def evaluate_current_sddl(sddl: str, desired: int = 1) -> AccessEvidence:
    """Refuse missing native dependencies without leaking diagnostics."""
    try:
        return _evaluate_current_sddl(sddl, desired)
    except (OSError, AttributeError):
        return AccessEvidence("unavailable")


def evaluate_current_file(path: Path, *, root: Path, desired: int = 1) -> AccessEvidence:
    """Return current-process observation, never permission for bank access."""
    try:
        return _evaluate_current_file(path, root=root, desired=desired)
    except (OSError, AttributeError):
        return AccessEvidence("unavailable")
