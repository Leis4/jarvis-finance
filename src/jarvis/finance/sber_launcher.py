"""Managed pinned MCP rehearsal launcher; live banking stays unconditionally blocked."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib
import json
import os
import re
import socket
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.finance.sber_operator import OperatorBlocked, OperatorStdio
from jarvis.finance.sber_sdk_overlay import build_disposable_overlay
from jarvis.finance.sber_windows_compat import (
    cancel_inbox,
    disposable_binding,
    ensure_fingerprint,
    operator_binding,
    read_private,
    submit_inbox,
    write_private,
)
from jarvis.mcp.adapters.sber import SberMCP
from jarvis.mcp.policy import require_sber_read_runtime
from jarvis.mcp.stdio_client import minimal_subprocess_env
from jarvis.services.invest_service import InvestService


def prepare(public: Path, *, workspace: Path | None = None, state_root: Path | None = None,
            configuration_id: str = "fixture", artifact_identity: str = "", operator_mode: str = "demo") -> Path:
    """Prepare fresh disposable source/state; never inspect an old bank profile."""
    sources = build_disposable_overlay(public / "sber_unofficial_sdk-0.6.4-py3-none-any.whl",
                                       public / "sber_unofficial_mcp-0.6.4-py3-none-any.whl")
    deps = public / "fixture-deps"
    if not (deps / "mcp").is_dir() or not (deps / "curl_cffi").is_dir():
        raise OperatorBlocked("fixture_dependencies_missing")
    if state_root is not None:
        # Metadata only: never adopt or read an older unbound banking profile.
        from jarvis.finance.sber_windows_compat import _configuration, _operator_root
        config = _configuration(_operator_root(state_root), configuration_id)
        if (operator_mode not in {"demo", "test_read", "human_read"}
                or config["synthetic"] is not (operator_mode != "human_read") or config["artifact_identity"] != artifact_identity):
            raise OperatorBlocked("demo_configuration_required")
    if workspace is None:
        root = Path(tempfile.mkdtemp(prefix="jarvis-v6-sdk-launcher-"))
    else:
        from jarvis.finance.sber_windows_compat import _operator_root
        root = _operator_root(workspace)
        if root.exists():
            raise OperatorBlocked("new_runtime_required")
        root.mkdir(parents=True)
    for member, body in sources.items():
        target = root / "code" / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    home = state_root.resolve() if state_root is not None else root / "jarvis-v6-sdk-state"
    if state_root is None:
        home.mkdir()
    (root / "runtime.json").write_text(json.dumps({"deps": str(deps.resolve()),
        "operator_mode": operator_mode,
        "public_wheels": str(public.resolve()),
        "persistent": ({"root": str(home), "configuration_id": configuration_id, "artifact_identity": artifact_identity}
                       if state_root is not None else None),
        "hashes": {member: hashlib.sha256(body).hexdigest() for member, body in sources.items()}}, sort_keys=True), encoding="utf-8")
    return root


def state_home(root: Path) -> Path:
    manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    state = manifest.get("persistent")
    return Path(state["root"]) if state is not None else root / "jarvis-v6-sdk-state"


def state_binding(root: Path) -> AbstractContextManager[None]:
    manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    state = manifest.get("persistent")
    if state is not None:
        from jarvis.finance.sber_windows_compat import _configuration, _operator_root
        config = _configuration(_operator_root(Path(state["root"])), state["configuration_id"])
        if config["synthetic"] is not (manifest.get("operator_mode", "demo") != "human_read"):
            raise OperatorBlocked("demo_configuration_required")
        return operator_binding(Path(state["root"]), configuration_id=state["configuration_id"], artifact_identity=state["artifact_identity"])
    return disposable_binding(state_home(root))


def profile_name(root: Path) -> str:
    state = json.loads((root / "runtime.json").read_text(encoding="utf-8")).get("persistent")
    return str(state["configuration_id"]) if state is not None else "fixture"


def local_profile_status(root: Path) -> str:
    """Inspect owned local cookie expiry, not live bank availability."""
    try:
        with state_binding(root):
            path = state_home(root) / "profiles" / f"{profile_name(root)}.json"
            if not path.exists():
                return "disconnected"
            body = json.loads(read_private(path))
            if body.get("version") != 3 or not isinstance(body.get("cookies"), list):
                return "unavailable"
            cookies = [c for c in body["cookies"] if c.get("name") in {"UFS-SESSION", "UFS-TOKEN"}]
            if len(cookies) != 2:
                return "unavailable"
            if any(c.get("expires") is not None and c["expires"] <= datetime.now(UTC).timestamp() for c in cookies):
                return "expired"
            return "saved"
    except (OSError, ValueError, KeyError, TypeError):
        return "unavailable"


def interactive_demo(root: Path, ask: Callable[..., list[str]]) -> dict[str, Any]:
    """Drive the same managed pipeline with native hidden input, DEMO only."""
    status = local_profile_status(root)
    if status == "unavailable":
        raise OperatorBlocked("owned_profile_unavailable")
    if status == "expired":
        raise OperatorBlocked("manual_disconnect_and_reconnect_required")
    with state_binding(root):
        if status == "disconnected":
            credentials = ask(["login", "password"])
            try:
                if len(credentials) != 2:
                    raise OperatorBlocked("operator_input_cancelled")
                write_private(state_home(root) / "credentials.json", {"login": credentials[0], "password": credentials[1]})
            finally:
                credentials.clear()
        def answer(reply: dict[str, Any]) -> list[str]:
            from jarvis.finance.sber_windows_compat import (
                active_captcha,
                challenge_active,
            )
            ticket = Path(reply["details"]["delivery"]["path"])
            image = None
            if reply["code"] == "captcha_required":
                _, image, _ = active_captcha(ticket, Path(reply["details"]["artifact_path"]))
            return ask(reply["details"]["expects"], image_bytes=image,
                       active=lambda ticket=ticket: challenge_active(ticket), problem=reply["details"].get("problem", ""))
        return rehearse(root, answer)


def interactive_operator_read(root: Path, *, ask: Callable[..., list[str]], confirm: Any, choose: Any, auth_mode: str | None = None) -> dict[str, Any]:
    """Run one explicitly authorized auth/products/select/page worker; never apply money."""
    from jarvis.finance.review_store import ReviewStore
    from jarvis.finance.sber_intake import collect_history, digest, native_review_packet
    from jarvis.finance.sber_windows_compat import (
        active_captcha,
        challenge_active,
        close_operator_authorization,
        issue_operator_authorization,
        operator_authorization,
        select_operator_resource,
    )
    today = datetime.now(UTC).date()
    with state_binding(root):
        status = local_profile_status(root)
        if status == "unavailable":
            raise OperatorBlocked("owned_profile_unavailable")
        mode = auth_mode or ("cold" if status == "disconnected" else "auto")
        if mode == "pin" and status == "disconnected":
            raise OperatorBlocked("operator_saved_profile_required")
        if status == "expired" and mode != "pin":
            raise OperatorBlocked("manual_disconnect_and_reconnect_required")
        permit = issue_operator_authorization(start=today - timedelta(days=7), end=today, confirm=confirm, auth_mode=mode)
        client = client_for(root, consent=True, authorization=permit)
        try:
            status = local_profile_status(root)
            if status == "unavailable":
                raise OperatorBlocked("owned_profile_unavailable")
            if status == "disconnected":
                credentials = ask(["login", "password"])
                try:
                    if len(credentials) != 2 or not all(credentials):
                        raise OperatorBlocked("operator_input_cancelled")
                    write_private(state_home(root) / "credentials.json", {"login": credentials[0], "password": credentials[1]})
                finally:
                    credentials.clear()
            reply = client.operator_auth("sber_auth_start", {"profile": profile_name(root), "mode": mode})
            for _ in range(8):
                if reply.get("status") != "challenge":
                    break
                ticket = Path(reply["details"]["delivery"]["path"])
                image = None
                if reply["code"] == "captcha_required":
                    _, image, _ = active_captcha(ticket, Path(reply["details"]["artifact_path"]))
                synthetic = json.loads((root / "runtime.json").read_text(encoding="utf-8"))["operator_mode"] == "test_read"
                def active(ticket: Path = ticket, synthetic: bool = synthetic) -> None:
                    operator_authorization(permit, synthetic=synthetic)
                    challenge_active(ticket)
                answers = ask(reply["details"]["expects"], image_bytes=image,
                    active=active, problem=reply["details"].get("problem", ""))
                try:
                    submit_inbox(ticket, answers)
                finally:
                    answers.clear()
                reply = client.operator_auth("sber_auth_continue", {"session_id": reply["session_id"], "wait_seconds": 0})
            if reply.get("status") != "ok":
                raise OperatorBlocked("operator_auth_unavailable")
            sid = reply["session_id"]
            products_reply = client.call_tool("sber_products", {"session_id": sid})
            if products_reply.get("status") != "ok":
                raise OperatorBlocked("operator_products_unavailable")
            products = products_reply["data"]
            resources = [p for key in ("accounts", "cards") for p in products[key] if p.get("operation_resource")]
            if not resources:
                raise OperatorBlocked("operator_resources_unavailable")
            resource = choose(resources)
            if resource not in {r["operation_resource"] for r in resources}:
                raise OperatorBlocked("operator_resource_unavailable")
            select_operator_resource(permit, resource, confirm=confirm)
            capture = collect_history(client, session_id=sid, expected_profile=profile_name(root),
                start=today - timedelta(days=7), end=today, limit=5, window_days=32, max_pages=1, resource=resource)
            if capture.get("status") == "unavailable" or len(capture["pages"]) != 1:
                raise OperatorBlocked("bounded_read_unavailable")
        finally:
            try:
                close_operator_authorization(permit)
            finally:
                client.close()
                cleanup_closed_challenges(root)
        # Keep original evidence encrypted; never persist a raw session identifier in review.
        original_hash = digest(capture)
        evidence = state_home(root) / ("evidence-" + uuid.uuid4().hex + ".dpapi")
        write_private(evidence, capture)
        for page in capture["pages"]:
            page["request"]["session_id"] = "closed-operator-session"
        capture["evidence_sha256"] = digest({k: v for k, v in capture.items() if k != "evidence_sha256"})
        packet = native_review_packet(capture, confirmed_mapping={}, include_unmapped=True)
        synthetic = json.loads((root / "runtime.json").read_text(encoding="utf-8"))["operator_mode"] == "test_read"
        packet["synthetic_demonstration"] = synthetic
        packet["encrypted_original"] = {"file": evidence.name, "original_sha256": original_hash}
        workspace = root / "native-review"
        ReviewStore(workspace).append_native_capture(packet)
        from jarvis.mcp.adapters.sber import rub_account_subtotal
        try:
            subtotal: str | None = str(rub_account_subtotal(products))
        except (ValueError, TypeError):
            subtotal = None
        return {"status": "observations_pending_session_closed", "mode": "TEST_READ" if synthetic else "HUMAN_READ",
            "bank_access_verified": not synthetic, "observed_rub_accounts": subtotal, "coverage": "unknown",
            "operations": len(capture["pages"][0]["response"]["operations"]), "review_workspace": str(workspace),
            "database_modified": False, "apply_allowed": False}


def client_for(root: Path, *, consent: bool, authorization: Path | None = None) -> OperatorStdio:
    """Create one managed fake MCP process using the existing operator client."""
    env = minimal_subprocess_env()
    # Carry the actual installed package identity across process boundaries;
    # isolated Python ignores ambient cwd/PYTHONPATH and user-site packages.
    boot = "import sys,runpy;sys.path.insert(0,sys.argv[1]);sys.argv=['sber_launcher','--worker','--runtime',sys.argv[2]];runpy.run_module('jarvis.finance.sber_launcher',run_name='__main__')"
    if authorization is not None:
        boot = boot.replace("sys.argv=['sber_launcher','--worker','--runtime',sys.argv[2]]",
                            "sys.argv=['sber_launcher','--worker','--runtime',sys.argv[2],'--authorization',sys.argv[3]]")
    command = [sys.executable, "-I", "-c", boot, str(Path(__file__).resolve().parents[2]), str(root)]
    if authorization is not None:
        command.append(str(authorization))
    return OperatorStdio(command,
                         consent=consent, env=env)


def _worker(root: Path, authorization: Path | None = None) -> None:
    require_sber_read_runtime(sys.argv, dict(os.environ))
    for key in list(os.environ):
        if key.startswith("SBER_"):
            os.environ.pop(key)
    manifest = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    public = Path(manifest["public_wheels"])
    expected_sources = build_disposable_overlay(public / "sber_unofficial_sdk-0.6.4-py3-none-any.whl",
                                                public / "sber_unofficial_mcp-0.6.4-py3-none-any.whl")
    expected_hashes = {key: hashlib.sha256(raw).hexdigest() for key, raw in expected_sources.items()}
    if manifest["hashes"] != expected_hashes:
        raise OperatorBlocked("runtime_source_changed")
    for member, expected in expected_hashes.items():
        path = root / "code" / member
        if not path.resolve().is_relative_to((root / "code").resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise OperatorBlocked("runtime_source_changed")
    deps = Path(manifest["deps"])
    for folder in (deps, deps / "win32", deps / "win32" / "lib", root / "code"):
        sys.path.insert(0, str(folder))
    dll = os.add_dll_directory(str(deps / "pywin32_system32"))
    home = state_home(root)
    with state_binding(root):
        operator_mode = manifest.get("operator_mode", "demo")
        scope = None
        if operator_mode != "demo":
            from jarvis.finance.sber_operator import WorkerReadScope
            if authorization is None or operator_mode not in {"test_read", "human_read"}:
                raise OperatorBlocked("operator_authorization_required")
            if operator_mode == "human_read":
                if "fixture_case" in manifest:
                    raise OperatorBlocked("operator_fixture_in_genuine_mode")
                from jarvis.finance.sber_runtime_candidate import verify_candidate
                identity = verify_candidate(public.parent)
                if (identity != manifest["persistent"]["artifact_identity"]
                        or not Path(__file__).resolve().is_relative_to(public / "fixture-deps")):
                    raise OperatorBlocked("operator_artifact_changed")
            scope = WorkerReadScope(authorization, synthetic=operator_mode == "test_read")
        # Reject accidental real SDK HTTP construction as well as Python sockets.
        def denied(*args: Any, **kwargs: Any) -> None:
            raise OperatorBlocked("bank_transport_forbidden_in_rehearsal")
        original_connect = socket.socket.connect
        def connect(sock: Any, address: Any) -> Any:
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}:
                return original_connect(sock, address)
            return denied()
        socket_class: Any = socket.socket
        if operator_mode != "human_read":
            socket_class.connect = connect
        sdk = importlib.import_module("sber_unofficial.session")
        server: Any = importlib.import_module("sber_unofficial_mcp.server")
        transport = importlib.import_module("sber_unofficial.transport")
        real_transport = transport.CurlAsyncTransport
        if operator_mode != "human_read":
            transport.CurlAsyncTransport.__init__ = denied
        device = importlib.import_module("sber_unofficial.deviceprint")
        ensure_fingerprint(home, server.DEVICEPRINT_FILE, device.generate_deviceprint)
        ensure_fingerprint(home, server.ANTIFRAUD_FILE, device.generate_antifraud_deviceprint)
        if operator_mode == "demo" and not (home / "credentials.json").exists():
            write_private(home / "credentials.json", {"login": "synthetic-login", "password": "synthetic-password"})
        server._REG = server.Registry(home)
        if scope is not None:
            if operator_mode == "test_read":
                from jarvis.finance.sber_protocol_fixture import (
                    ProtocolFixtureTransport,
                )
                scope.install(server, lambda *args, **kwargs: ProtocolFixtureTransport(*args, **kwargs, case=manifest.get("fixture_case")))
            else:
                scope.install(server, real_transport)
        else:
            _fake_bank(server, sdk)
        try:
            native = server._build_server()
            if scope is not None:
                scope.enforce_raw_tools(native)
            native.run(transport="stdio")
        finally:
            dll.close()


def _fake_bank(server: Any, sdk: Any) -> None:
    """Replace only HTTP transport; leave actual SDK auth/SRP/read methods intact."""
    from jarvis.finance.sber_protocol_fixture import ProtocolFixtureTransport

    pin: Any = importlib.import_module("sber_unofficial.pin")
    client: Any = importlib.import_module("sber_unofficial.client")
    pin.CurlAsyncTransport = ProtocolFixtureTransport
    client.CurlAsyncTransport = ProtocolFixtureTransport


def rehearse(root: Path, answer: Callable[[dict[str, Any]], list[str]]) -> dict[str, Any]:
    """Execute actual MCP cold/restore and read through existing Jarvis adapter."""
    challenges: list[str] = []
    old_sid = ""
    balance: dict[str, Any] = {}
    operation_count = 0
    review = root / "review"
    profile = profile_name(root)
    with state_binding(root):
        restored = (state_home(root) / "profiles" / f"{profile}.json").exists()
        for mode in (("auto",) if restored else ("cold", "auto")):
            client = client_for(root, consent=True)
            try:
                client.list_tools()
                if old_sid:
                    assert client.call_tool("sber_session_info", {"session_id": old_sid})["status"] == "error"
                response = client.operator_auth("sber_auth_start", {"profile": profile, "mode": mode})
                for _ in range(5):
                    if response.get("status") != "challenge":
                        break
                    challenges.append(str(response["code"]))
                    submit_inbox(Path(response["details"]["delivery"]["path"]), answer(response))
                    response = client.operator_auth("sber_auth_continue", {"session_id": response["session_id"], "wait_seconds": 0})
                if response.get("status") != "ok":
                    raise OperatorBlocked("fake_auth_incomplete")
                if mode == "auto" and response["data"]["reused"] != "profile":
                    raise OperatorBlocked("saved_profile_not_reused")
                old_sid = response["session_id"]
                if mode == "cold":
                    # Persist then close; read exactly one page only after restart.
                    continue
                from jarvis.finance.sber_windows_compat import (
                    issue_demo_read_authorization,
                )
                today = datetime.now(UTC).date()
                permit = issue_demo_read_authorization(resource="ct-account:synthetic-account",
                    start=today - timedelta(days=7), end=today) if json.loads((root / "runtime.json").read_text(encoding="utf-8")).get("persistent") is not None else None
                if permit is not None:
                    client.bind_demo_read_authorization(permit, old_sid)
                adapter = SberMCP(client)
                adapter.bind_read_session(old_sid, profile)
                balance = InvestService(sber=adapter, use_factory=False).get_balances()
                from jarvis.finance.review_store import ReviewStore
                from jarvis.finance.sber_intake import (
                    collect_history,
                    native_review_packet,
                )
                today = datetime.now(UTC).date()
                capture = collect_history(client, session_id=old_sid, expected_profile=profile,
                    start=today - timedelta(days=7), end=today, limit=5, max_pages=1)
                if capture.get("status") == "unavailable" or not capture["pages"]:
                    raise OperatorBlocked("bounded_read_unavailable")
                operation_count = len(capture["pages"][0]["response"]["operations"])
                if operation_count > 5:
                    raise OperatorBlocked("page_limit_exceeded")
                if mode == "auto":
                    mapping = {capture["pages"][0]["request"]["resource"]: "synthetic-local"}
                    packet = native_review_packet(capture, confirmed_mapping=mapping)
                    packet["synthetic_demonstration"] = True
                    if (review / "review.sqlite").exists():
                        review = root / "reviews" / uuid.uuid4().hex
                    ReviewStore(review).initialize(packet)
            finally:
                client.close()
                cleanup_closed_challenges(root)
    return {"status": "synthetic_rehearsal", "challenges": challenges, "restored_without_auth": True,
            "review_workspace": str(review),
            "products": {"accounts": len(balance["accounts"]), "cards": len(balance["cards"])},
            "observed_rub_accounts": balance["total_rub"], "coverage": "unknown", "operations": operation_count,
            "database_modified": False, "bank_access": False, "live_allowed": False}


def cleanup_closed_challenges(root: Path) -> None:
    """After verified owned-process shutdown remove only ephemeral fixture answers."""
    home = state_home(root).resolve()
    # Caller retains the enrolled binding while closing the owned process.
    from jarvis.finance.sber_windows_compat import _path
    _path(home)
    targets = [*(home / "inbox").glob("otp-*"), *home.glob("captcha-*.dpapi"), *home.glob("captcha-*.receipt")]
    for path in targets:
        if (not path.resolve().is_relative_to(home) or path.is_symlink() or
                not re.fullmatch(r"(?:otp-[0-9a-f]{32}\.(?:dpapi|lease|claim)|captcha-[0-9a-f]{32}\.(?:dpapi|receipt))", path.name)):
            raise OperatorBlocked("cleanup_scope_invalid")
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сбер: только disposable rehearsal, настоящий банк заблокирован")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--public-wheels", type=Path)
    parser.add_argument("--answer", type=Path)
    parser.add_argument("--captcha", type=Path)
    parser.add_argument("--rehearse", action="store_true")
    parser.add_argument("--serve-review", action="store_true")
    args = parser.parse_args()
    if args.live:
        print("Настоящее подключение заблокировано: отдельная операторская приёмка не выполнена.")
        return 2
    try:
        if args.worker and args.runtime:
            _worker(args.runtime, args.authorization)
            return 0
        if args.answer and args.runtime:
            if not sys.stdin.isatty():
                raise OperatorBlocked("interactive_operator_required")
            with state_binding(args.runtime):
                if args.captcha:
                    from jarvis.finance.sber_captcha_viewer import show_captcha
                    show_captcha(args.answer, args.captcha)
                try:
                    answer = getpass.getpass("Ответ (скрыт), пустой ввод — отмена: ")
                    if answer:
                        submit_inbox(args.answer, answer.split())
                    else:
                        cancel_inbox(args.answer)
                finally:
                    answer = ""
            print("Ответ передан локально; ожидайте проверки MCP.")
            return 0
        if not args.public_wheels:
            parser.error("Нужен --public-wheels для нового disposable rehearsal")
        root = prepare(args.public_wheels)
        if args.rehearse:
            def synthetic_answer(response: dict[str, Any]) -> list[str]:
                return [{"captcha_code": "ABCD", "otp": "123456", "new_pin": "31415", "new_pin_confirm": "31415", "pin": "31415"}[key]
                        for key in response["details"]["expects"]]
            print(json.dumps(rehearse(root, synthetic_answer), ensure_ascii=False))
            if args.serve_review:
                from jarvis.finance import review_cli
                original = sys.argv
                try:
                    sys.argv = ["review_cli", "--workspace", str(root / "review"), "serve"]
                    return review_cli.main()
                finally:
                    sys.argv = original
            return 0
        print("Disposable окружение подготовлено:", root)
        print("Настоящие банковские данные не использовались.")
        return 0
    except (OperatorBlocked, OSError, ValueError, RuntimeError):
        print("Операторская подготовка недоступна; настоящий банк не запускался.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
