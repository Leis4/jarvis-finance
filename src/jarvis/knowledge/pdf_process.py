"""Windows resource containment for a fixed parser; not filesystem/network isolation."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any


class PdfWorkerUnavailable(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__("PDF processing unavailable")
        self.code = code


@dataclass(frozen=True)
class WorkerLimits:
    seconds: float = 10
    memory_bytes: int = 256 * 1024 * 1024
    output_bytes: int = 8 * 1024 * 1024
    cpu_seconds: int = 5


DEFAULT_LIMITS = WorkerLimits()


class _Basic(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _Limits(ctypes.Structure):
    _fields_ = [("basic", _Basic), ("io", _IO), ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Startup(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        *[(name, wintypes.DWORD) for name in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD), ("reserved_data", ctypes.c_void_p),
        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _Process(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE), ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


def _kernel() -> Any:
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_Startup), ctypes.POINTER(_Process)], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        getattr(kernel, name).argtypes = arguments
        getattr(kernel, name).restype = result
    return kernel


def _run_script(script: Path, folder: Path, limits: WorkerLimits, cancel: Event | None = None) -> dict[str, int]:
    """Private fixed-parent runner; synthetic scripts are allowed only in native tests."""
    if os.name != "nt":
        raise PdfWorkerUnavailable("unsupported_platform")
    if cancel is not None and cancel.is_set():
        raise PdfWorkerUnavailable("cancelled")
    if (isinstance(limits.seconds, bool) or not 0 < limits.seconds <= 30
            or type(limits.memory_bytes) is not int or not 32 * 1024 * 1024 <= limits.memory_bytes <= 512 * 1024 * 1024
            or type(limits.output_bytes) is not int or not 1 <= limits.output_bytes <= 8 * 1024 * 1024
            or type(limits.cpu_seconds) is not int or not 1 <= limits.cpu_seconds <= 10):
        raise PdfWorkerUnavailable("invalid_limits")
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise PdfWorkerUnavailable("job_unavailable")
    process = _Process()
    assigned = False
    try:
        policy = _Limits()
        policy.basic.flags = 0x2000 | 0x100 | 0x200 | 0x8 | 0x2
        policy.basic.active = 1
        policy.basic.process_time = limits.cpu_seconds * 10000000
        policy.process_memory = policy.job_memory = limits.memory_bytes
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(policy), ctypes.sizeof(policy)):
            raise PdfWorkerUnavailable("job_limits_unavailable")
        executable = str(getattr(sys, "_base_executable", sys.executable))
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "TEMP": str(folder), "TMP": str(folder),
               "PATH": str(Path(executable).parent), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        environment = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, "-I", str(script),
            str(folder / "request.json"), str(folder / "response.json"), str(Path(sys.prefix) / "Lib" / "site-packages")]))
        startup = _Startup()
        startup.cb = ctypes.sizeof(startup)
        startup.flags = 0x101  # hidden, explicit NULL std handles; inherit_handles=false
        if not kernel.CreateProcessW(executable, command, None, None, False, 0x08000404,
                                    environment, str(folder), ctypes.byref(startup), ctypes.byref(process)):
            raise PdfWorkerUnavailable("process_unavailable")
        # No executable Python instruction runs before assignment succeeds.
        if not kernel.AssignProcessToJobObject(job, process.process):
            raise PdfWorkerUnavailable("job_assignment_unavailable")
        assigned = True
        if kernel.ResumeThread(process.thread) == 0xFFFFFFFF:
            raise PdfWorkerUnavailable("process_unavailable")
        deadline = time.monotonic() + limits.seconds
        while True:
            if cancel is not None and cancel.is_set():
                raise PdfWorkerUnavailable("cancelled")
            if time.monotonic() >= deadline:
                raise PdfWorkerUnavailable("timeout")
            output = folder / "response.json"
            if output.exists() and output.stat().st_size > limits.output_bytes:
                raise PdfWorkerUnavailable("output_limit")
            state = kernel.WaitForSingleObject(process.process, 10)
            if state == 0:
                break
            if state != 258:
                raise PdfWorkerUnavailable("process_unavailable")
        code = wintypes.DWORD()
        output = folder / "response.json"
        if output.exists() and output.stat().st_size > limits.output_bytes:
            raise PdfWorkerUnavailable("output_limit")
        if not kernel.GetExitCodeProcess(process.process, ctypes.byref(code)) or code.value != 0:
            raise PdfWorkerUnavailable("worker_failed")
        accounting = _Limits()
        if not kernel.QueryInformationJobObject(job, 9, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise PdfWorkerUnavailable("accounting_unavailable")
        return {"peak_process_memory": int(accounting.peak_process), "peak_job_memory": int(accounting.peak_job)}
    finally:
        cleanup_failed = False
        cleanup_deadline = time.monotonic() + 5
        if process.process:
            if assigned:
                kernel.TerminateJobObject(job, 1)
            else:
                kernel.TerminateProcess(process.process, 1)
            cleanup_failed = kernel.WaitForSingleObject(process.process, 5000) != 0
        if process.thread:
            # A signalled process is not proof that its thread handle is signalled yet.
            remaining_ms = max(0, int((cleanup_deadline - time.monotonic()) * 1000))
            cleanup_failed = kernel.WaitForSingleObject(process.thread, remaining_ms) != 0 or cleanup_failed
            cleanup_failed = not kernel.CloseHandle(process.thread) or cleanup_failed
        if process.process:
            cleanup_failed = not kernel.CloseHandle(process.process) or cleanup_failed
        cleanup_failed = not kernel.CloseHandle(job) or cleanup_failed
        if cleanup_failed:
            raise PdfWorkerUnavailable("cleanup_failed")


def process_pdf(raw: bytes, *, chunk_size: int = 500, overlap: int = 50,
                limits: WorkerLimits = DEFAULT_LIMITS, cancel: Event | None = None) -> dict[str, Any]:
    """Run the existing parser under hard native resource limits without fallback."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 8 * 1024 * 1024:
        raise PdfWorkerUnavailable("invalid_request")
    import base64
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-pdf-worker-") as temporary:
            folder = Path(temporary)
            request = {"format": "jarvis-pdf-worker-v1", "original": base64.b64encode(raw).decode("ascii"),
                       "chunk_size": chunk_size, "overlap": overlap, "output_limit": limits.output_bytes}
            (folder / "request.json").write_text(json.dumps(request), encoding="utf-8")
            _run_script(Path(__file__).with_name("pdf_worker.py"), folder, limits, cancel)
            output = folder / "response.json"
            if not output.is_file() or output.stat().st_size > limits.output_bytes:
                raise PdfWorkerUnavailable("output_limit")
            reply = json.loads(output.read_bytes().decode("utf-8"))
            if set(reply) != {"status", "artifact"} or reply["status"] != "ok" or not isinstance(reply["artifact"], dict):
                raise PdfWorkerUnavailable("parse_failed")
            sha = hashlib.sha256(raw).hexdigest()
            if reply["artifact"].get("source_sha256") != sha or reply["artifact"].get("source_uri") != "urn:sha256:" + sha:
                raise PdfWorkerUnavailable("evidence_mismatch")
            return dict(reply["artifact"])
    except PdfWorkerUnavailable:
        raise
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise PdfWorkerUnavailable("worker_unavailable") from None
