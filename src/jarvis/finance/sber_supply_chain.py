"""Compare public package source only; never import or execute wheel members."""
from __future__ import annotations

import ast
import hashlib
import io
import stat
import zipfile
from pathlib import Path
from typing import Any

ARTIFACTS = {
    "sber_unofficial": "330138f6facaef07f360958d68a1d0f068681aaf1a71c003bf00edf0c83292f3",
    "sber_unofficial_mcp": "a10d9ad5bd7f182c795068b0d7f166d58e779ee64a7e5f5ebb199ac40b8996bd",
}


def pinned_public_sources(wheel: Path, *, package: str) -> dict[str, bytes]:
    """Return bounded public Python members from the repository-pinned artifact."""
    if package not in ARTIFACTS or wheel.is_symlink() or wheel.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Unsupported public artifact")
    raw = wheel.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if len(raw) > 2 * 1024 * 1024 or actual != ARTIFACTS[package]:
        raise ValueError("Artifact hash mismatch")
    sources = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [i for i in archive.infolist() if i.filename.startswith(package + "/") and i.filename.endswith(".py")]
        if not members or len(members) > 64 or len({m.filename for m in members}) != len(members):
            raise ValueError("Unexpected public source inventory")
        for entry in members:
            if (entry.file_size > 1024 * 1024 or ".." in Path(entry.filename).parts
                    or "\\" in entry.filename or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Unsafe public source member")
            sources[entry.filename] = archive.read(entry)
    return sources


def compare_public_wheel(wheel: Path, installed_root: Path, *, package: str,
                         expected_sha256: str) -> dict[str, Any]:
    """Verify artifact bytes before bounded, read-only public-source comparison."""
    if package not in ARTIFACTS or expected_sha256 != ARTIFACTS[package]:
        raise ValueError("Pinned artifact identity mismatch")
    originals = pinned_public_sources(wheel, package=package)
    rows = []
    for member, original in originals.items():
        local = installed_root / member
        for candidate in [installed_root, *(installed_root / p for p in local.relative_to(installed_root).parents)]:
            if candidate.is_symlink() or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("Public source ancestor redirected")
        if (local.is_symlink() or not local.is_file() or local.stat().st_size > 1024 * 1024
                or getattr(local.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Public source absent or redirected")
        installed = local.read_bytes()
        if len(installed) > 1024 * 1024:
            raise ValueError("Public source size limit")
        try:
            left, right = ast.parse(original.decode("utf-8-sig")), ast.parse(installed.decode("utf-8-sig"))
        except (SyntaxError, UnicodeError):
            raise ValueError("Invalid public source") from None
        rows.append({"member": member, "installed_path": str(local),
                     "upstream_sha256": hashlib.sha256(original).hexdigest(),
                     "installed_sha256": hashlib.sha256(installed).hexdigest(),
                     "bytes_equal": original == installed,
                     "ast_equal": ast.dump(left) == ast.dump(right)})
    return {"artifact_sha256": ARTIFACTS[package], "package": package, "version": "0.6.4", "files": rows,
            "modified": sum(not r["bytes_equal"] for r in rows),
            "code_executed": False, "live_allowed": False, "attestation_verified": False,
            "trust": "repository_pinned_sha256", "os_isolation_verified": False}
