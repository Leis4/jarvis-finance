"""Build disposable pinned-source codec overlays; never patch installed packages."""
from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from jarvis.finance.sber_sdk_repair import TARGETS, _guard_source
from jarvis.finance.sber_supply_chain import pinned_public_sources

_BINDINGS = {
    "sber_unofficial/client.py": {"AsyncSberClient._mutate": "deny_bank_write()",
                                  "AsyncSberClient._mutate_in_sequence": "deny_bank_write()"},
    "sber_unofficial/session.py": {"_read_session_file": "read_private(path, require_private=require_private)",
                                   "_write_session_file": "write_private(path, payload)"},
    "sber_unofficial/models.py": {"_write_private_json": "write_private(path, value)"},
    "sber_unofficial_mcp/server.py": {"_require_private_dir": "private_dir(path)",
        "Registry.arm_inbox": "arm_inbox(self, st, expects)", "Registry.read_inbox": "read_inbox(self, st)",
        "Registry.clear_inbox": "clear_inbox(self, st)",
        "_ensure_fingerprint": "ensure_fingerprint(home, name, generator)"},
}


def read_response_complete(path: str, data: Mapping[str, object]) -> bool:
    """Distinguish declared empty collections from missing native read evidence."""
    if path == "/uoh-bh/v1/operations/list":
        body = data.get("body")
        return isinstance(body, Mapping) and isinstance(body.get("operations"), list)
    if path == "/main-screen/rest/v2/m1/web/section/meta":
        node: object = data
        for key in ("body", "sections", "technicalSection", "sectionProductData"):
            if not isinstance(node, Mapping) or key not in node:
                return False
            node = node[key]
        if not isinstance(node, Mapping):
            return False
        sections = [node[key] for key in ("ctaccounts", "sharingCtAccounts", "accounts", "cardsInWallet") if key in node]
        return bool(sections) and all(isinstance(s, Mapping) and isinstance(s.get("data"), list) for s in sections)
    return True


def _bind(source: str, bindings: dict[str, str]) -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    targets = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in bindings:
            targets.append((node, bindings[node.name]))
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name + "." + method.name in bindings:
                    targets.append((method, bindings[node.name + "." + method.name]))
    if len(targets) != len(bindings):
        raise ValueError("Pinned codec target mismatch")
    for target, expression in sorted(targets, key=lambda pair: pair[0].lineno, reverse=True):
        first = target.body[0]
        indent = " " * first.col_offset
        lines[first.lineno - 1:target.end_lineno] = [indent + "from jarvis.finance import sber_windows_compat as codec\n",
                                                  indent + "return codec." + expression + "\n"]
    result = "".join(lines)
    compile(result, "disposable_sdk_overlay", "exec")
    return result


def build_disposable_overlay(sdk_wheel: Path, mcp_wheel: Path) -> dict[str, bytes]:
    """Return verified bytes with explicit codecs; unsupported Windows paths refuse."""
    sources = {**pinned_public_sources(sdk_wheel, package="sber_unofficial"),
               **pinned_public_sources(mcp_wheel, package="sber_unofficial_mcp")}
    for member, raw in list(sources.items()):
        source = raw.decode("utf-8")
        if member in TARGETS:
            source = _guard_source(source, TARGETS[member])
        if member in _BINDINGS:
            source = _bind(source, _BINDINGS[member])
        if member == "sber_unofficial/client.py":
            tree = ast.parse(source)
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AsyncSberClient")
            lines = source.splitlines(keepends=True)
            for node in sorted((n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {"_request", "_reauthenticate_profile"}), key=lambda n: n.lineno, reverse=True):
                first = node.body[0]
                indent = " " * first.col_offset
                if node.name == "_reauthenticate_profile":
                    lines[first.lineno - 1:node.end_lineno] = [indent + 'raise AuthenticationExpired("Operator authentication required")\n']
                else:
                    lines[first.lineno - 1:first.lineno - 1] = [indent + 'if path in MUTATION_PATHS:\n',
                        indent + '    from jarvis.mcp.errors import MCPPolicyDenied\n',
                        indent + '    raise MCPPolicyDenied("Banking writes forbidden")\n']
            source = "".join(lines)
            tree = ast.parse(source)
            decoder = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_decode_response")
            returned = next(n for n in decoder.body if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == "data")
            lines = source.splitlines(keepends=True)
            lines[returned.lineno - 1:returned.lineno - 1] = [
                '    from jarvis.finance.sber_sdk_overlay import read_response_complete\n',
                '    if not read_response_complete(path, data):\n',
                '        raise ApiError("Incomplete native read response")\n']
            source = "".join(lines)
        if member == "sber_unofficial_mcp/server.py":
            tree = ast.parse(source)
            advance = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_advance")
            lines = source.splitlines(keepends=True)
            lines[advance.lineno - 1] = lines[advance.lineno - 1].replace("def _advance(", "def _advance_sdk(")
            source = "".join(lines) + '\nasync def _advance(reg, st, lines):\n    from jarvis.finance.sber_windows_compat import advance_with_retry\n    return await advance_with_retry(reg, st, lines, _advance_sdk)\n'
            tree = ast.parse(source)
            node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_arm_captcha")
            lines = source.splitlines(keepends=True)
            first = node.body[0]
            indent = " " * first.col_offset
            lines[first.lineno - 1:node.end_lineno] = [indent + 'from jarvis.finance.sber_windows_compat import arm_captcha\n',
                                                    indent + 'return await arm_captcha(reg, st, exc, audio=audio)\n']
            source = "".join(lines)
            tree = ast.parse(source)
            gate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_allow_writes")
            lines = source.splitlines(keepends=True)
            first = gate.body[0]
            lines[first.lineno - 1:gate.end_lineno] = [
                '    from jarvis.mcp.policy import require_sber_read_runtime\n',
                '    from jarvis.mcp.errors import MCPPolicyDenied\n',
                '    require_sber_read_runtime(sys.argv, dict(os.environ))\n',
                '    if _ALLOW_WRITES_FLAG:\n',
                '        raise MCPPolicyDenied("Banking writes forbidden")\n',
                '    return False\n']
            source = "".join(lines)
            tree = ast.parse(source)
            lines = source.splitlines(keepends=True)
            expiry_functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"_arm", "_arm_from_exc"}]
            if len(expiry_functions) != 2:
                raise ValueError("Pinned expiry target mismatch")
            for function in sorted(expiry_functions, key=lambda n: n.lineno, reverse=True):
                returned = next(n for n in function.body if isinstance(n, ast.Return))
                lines[returned.lineno - 1:returned.lineno - 1] = [
                    '    from jarvis.finance.sber_windows_compat import sync_challenge_expiry\n',
                    '    sync_challenge_expiry(st)\n']
            source = "".join(lines)
        compile(source, member, "exec")
        sources[member] = source.encode("utf-8")
    return sources
