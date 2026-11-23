"""Reproducible fail-closed patch candidate; no SDK import, installation or live I/O."""
from __future__ import annotations

import ast
import difflib
import hashlib
from pathlib import Path
from typing import Any

from jarvis.finance.sber_supply_chain import ARTIFACTS, pinned_public_sources

TARGETS = {
    "sber_unofficial/session.py": {"_validate_session_stat", "_validate_private_source",
                                    "_read_session_file", "_write_session_file", "from_har"},
    "sber_unofficial/_http.py": {"load_netscape_cookies"},
    "sber_unofficial_mcp/server.py": {"_require_private_dir"},
}
REFUSAL = "Windows private I/O security acceptance missing"


def _guard_source(source: str, names: set[str]) -> str:
    """Insert platform-specific refusal before existing selected function effects."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    if len(functions) != len(names) or {f.name for f in functions} != names:
        raise ValueError("Pinned repair target mismatch")
    for function in sorted(functions, key=lambda n: n.lineno, reverse=True):
        first = function.body[0]
        docstring = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
        insertion = (first.end_lineno if docstring else first.lineno - 1)
        if insertion is None:
            raise ValueError("Pinned repair target mismatch")
        indent = " " * first.col_offset
        lines[insertion:insertion] = [indent + 'if _jarvis_windows_os.name == "nt":\n',
                                     indent + f'    raise InsecureSessionFile("{REFUSAL}")\n']
    futures = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    preamble = futures[-1].end_lineno if futures else 0
    if not futures and tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        preamble = tree.body[0].end_lineno
    lines[preamble or 0:preamble or 0] = ["import os as _jarvis_windows_os\n"]
    result = "".join(lines)
    compile(result, "reviewed_public_candidate", "exec")  # Compile only; never execute upstream.
    return result


def build_fail_closed_patch(sdk_wheel: Path, mcp_wheel: Path) -> dict[str, Any]:
    """Build an inert review artifact against exactly pinned published0.6.4 bytes."""
    originals = {**pinned_public_sources(sdk_wheel, package="sber_unofficial"),
                 **pinned_public_sources(mcp_wheel, package="sber_unofficial_mcp")}
    diffs, bindings = [], []
    for member, targets in TARGETS.items():
        original = originals[member]
        before = original.decode("utf-8")
        after = _guard_source(before, targets)
        diffs.append("".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                fromfile="a/" + member, tofile="b/" + member)))
        bindings.append({"member": member, "original_sha256": hashlib.sha256(original).hexdigest(),
                         "candidate_sha256": hashlib.sha256(after.encode()).hexdigest(), "guards": sorted(targets)})
    patch = "".join(diffs)
    return {"format": "jarvis-sber-windows-refusal-candidate-v1", "patch": patch,
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(), "bindings": bindings,
            "upstream_artifacts": dict(ARTIFACTS), "installed_modified": False,
            "sdk_executed": False, "live_allowed": False, "windows_acl_implemented": False,
            "status": "blocked", "limitation": "Fail-closed mitigation, not functional Windows private I/O or isolation"}
