"""Offline pinned Windows artifact; activation/real banking deliberately unavailable."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jarvis.finance.sber_operator import OperatorBlocked
from jarvis.finance.sber_sdk_overlay import build_disposable_overlay


def runtime_lock() -> dict[str, Any]:
    """Load repository-fixed compatible public wheel identities, never caller repins."""
    lock: dict[str, Any] = json.loads(Path(__file__).with_name("sber_runtime.lock.json").read_text(encoding="utf-8"))
    if lock["format"] != "jarvis-sber-windows-cp311-runtime-v1" or sys.platform != "win32" or sys.version_info[:2] != (3, 11):
        raise OperatorBlocked("unsupported_candidate_platform")
    names = [p["name"] for p in lock["packages"]]
    if len(names) != len(set(names)):
        raise OperatorBlocked("runtime_lock_invalid")
    for package in lock["packages"]:
        if (not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", package["filename"])
                or not re.fullmatch(r"[a-f0-9]{64}", package["sha256"])):
            raise OperatorBlocked("runtime_lock_invalid")
    return lock


def _read(path: Path) -> bytes:
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise OperatorBlocked("artifact_file_denied")
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400 or not path.is_file() or path.stat().st_size > 32_000_000:
        raise OperatorBlocked("artifact_file_denied")
    raw = path.read_bytes()
    if len(raw) > 32_000_000:
        raise OperatorBlocked("artifact_file_denied")
    return raw


def _digest(path: Path) -> str:
    return hashlib.sha256(_read(path)).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise OperatorBlocked("runtime_download_redirect_denied")


def _open_public(url: str) -> Any:
    """Refuse redirection before sending a second request, not after receiving it."""
    return build_opener(_NoRedirect()).open(Request(url, headers={"User-Agent": "Jarvis-public-artifact-verifier/1"}), timeout=30)


def fetch_runtime_wheels(wheelhouse: Path) -> dict[str, Any]:
    """Fetch only frozen public HTTPS artifacts; never SDK/bank/auth execution."""
    lock = runtime_lock()
    if not wheelhouse.is_absolute():
        raise OperatorBlocked("absolute_wheelhouse_required")
    for part in (wheelhouse, *wheelhouse.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise OperatorBlocked("artifact_file_denied")
    wheelhouse.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for package in lock["packages"]:
        target = wheelhouse / package["filename"]
        if target.exists():
            if _digest(target) != package["sha256"]:
                raise OperatorBlocked("runtime_wheel_hash_mismatch")
            continue
        url = package["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "files.pythonhosted.org" or parsed.query or parsed.fragment:
            raise OperatorBlocked("runtime_download_source_denied")
        with _open_public(url) as response:
            if response.geturl() != url:
                raise OperatorBlocked("runtime_download_redirect_denied")
            raw = response.read(32_000_001)
        if len(raw) > 32_000_000 or hashlib.sha256(raw).hexdigest() != package["sha256"]:
            raise OperatorBlocked("runtime_wheel_hash_mismatch")
        # No overwrite and no unverified bytes published as an accepted wheel.
        with target.open("xb") as output:
            output.write(raw)
        downloaded += 1
    return {"status": "verified_public_wheels", "packages": len(lock["packages"]),
            "downloaded": downloaded, "bank_access": False}


def _own_wheel(wheel: Path) -> bytes:
    """Reject foreign Jarvis code; compare packaged members with this loaded checkout."""
    raw = _read(wheel)
    package_root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 1000:
            raise OperatorBlocked("jarvis_wheel_denied")
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.file_size > 2_000_000 or ".." in Path(info.filename).parts or "\\" in info.filename:
                raise OperatorBlocked("jarvis_wheel_denied")
            if info.filename.startswith("jarvis/"):
                source = package_root / info.filename.removeprefix("jarvis/")
                if not source.resolve().is_relative_to(package_root) or archive.read(info) != source.read_bytes():
                    raise OperatorBlocked("jarvis_wheel_not_current")
            elif not re.fullmatch(r"jarvis-0\.1\.0\.dist-info/(METADATA|WHEEL|RECORD)", info.filename):
                raise OperatorBlocked("jarvis_wheel_denied")
        expected = {"jarvis/" + str(p.relative_to(package_root)).replace("\\", "/")
                    for p in package_root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        if not expected <= set(names):
            raise OperatorBlocked("jarvis_wheel_incomplete")
    return raw


def prepare_candidate(wheelhouse: Path, jarvis_wheel: Path, destination: Path, *, uv: Path) -> dict[str, Any]:
    """Build a new owned persistent artifact from verified local wheels, offline."""
    lock = runtime_lock()
    jarvis_raw = _own_wheel(jarvis_wheel)
    verified: dict[str, bytes] = {}
    for package in lock["packages"]:
        raw = _read(wheelhouse / package["filename"])
        if hashlib.sha256(raw).hexdigest() != package["sha256"]:
            raise OperatorBlocked("runtime_wheel_hash_mismatch")
        verified[package["filename"]] = raw
    if not destination.is_absolute() or destination.exists():
        raise OperatorBlocked("new_candidate_directory_required")
    for parent in destination.parents:
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
            raise OperatorBlocked("candidate_path_redirected")
    destination.mkdir(parents=True)
    public = destination / "public"
    public.mkdir()
    for package in lock["packages"]:
        (public / package["filename"]).write_bytes(verified[package["filename"]])
    (public / jarvis_wheel.name).write_bytes(jarvis_raw)
    requirements = public / "requirements.txt"
    requirements.write_text("\n".join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}" for p in lock["packages"]) + "\n", encoding="utf-8")
    command = [str(uv), "pip", "install", "--python", sys.executable, "--target", str(public / "fixture-deps"),
               "--no-index", "--find-links", str(public), "--require-hashes", "--only-binary", ":all:", "-r", str(requirements)]
    result = subprocess.run(command, capture_output=True, timeout=120, check=False,
                            env={k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LOCALAPPDATA"}})
    if result.returncode:
        raise OperatorBlocked("offline_candidate_install_failed")
    # The operator slice uses the audited minimal closure, not Jarvis GPU/ML extras.
    jarvis_requirements = public / "jarvis-requirements.txt"
    jarvis_requirements.write_text(f"jarvis==0.1.0 --hash=sha256:{hashlib.sha256(jarvis_raw).hexdigest()}\n", encoding="utf-8")
    command[-1] = str(jarvis_requirements)
    command.append("--no-deps")
    result = subprocess.run(command, capture_output=True, timeout=120, check=False,
                            env={k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LOCALAPPDATA"}})
    if result.returncode:
        raise OperatorBlocked("offline_jarvis_install_failed")
    # Keep genuine installed SDK source untouched; overlays are a separate managed layer.
    build_disposable_overlay(public / "sber_unofficial_sdk-0.6.4-py3-none-any.whl", public / "sber_unofficial_mcp-0.6.4-py3-none-any.whl")
    state = destination / "operator-state"
    state.mkdir()
    from jarvis.finance.windows_session_store import save_state
    save_state(state / "storage-probe.dpapi", b"nonsecret-candidate-storage-probe")
    bootstrap = '''import hashlib,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'artifact.json').read_text(encoding='utf8'))
identity=manifest.pop('identity')
if manifest.get('format')!='jarvis-sber-candidate-v1' or manifest.get('mode')!='OPERATOR_CANDIDATE' or hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()!=identity:
    raise SystemExit('Candidate identity unavailable')
if '--expected-identity' in sys.argv:
    i=sys.argv.index('--expected-identity')
    if i+1>=len(sys.argv) or sys.argv[i+1]!=identity:
        raise SystemExit('Reviewed candidate identity mismatch')
    del sys.argv[i:i+2]
actual=set()
for base,folders,names in os.walk(root,topdown=True,followlinks=False):
    for name in folders+names:
        p=Path(base)/name
        if p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0)&0x400:
            raise SystemExit('Candidate path unavailable')
    folders[:]=[name for name in folders if name not in {'operator-state','__pycache__'}]
    for name in names:
        p=Path(base)/name
        if p!=root/'artifact.json' and p.suffix!='.pyc':actual.add(p.relative_to(root).as_posix())
if actual!=set(manifest['files']):raise SystemExit('Candidate inventory unavailable')
for name,expected in manifest['files'].items():
    p=root/name
    if p.is_symlink() or not p.resolve().is_relative_to(root) or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
        raise SystemExit('Candidate integrity unavailable')
sys.path.insert(0,str(root/'public/fixture-deps'))
sys.dont_write_bytecode=True
from jarvis.finance.sber_runtime_candidate import candidate_main
raise SystemExit(candidate_main(root,sys.argv[1:]))
'''
    (destination / "bootstrap.py").write_text(bootstrap, encoding="utf-8")
    (destination / "START.ps1").write_text('& python -I "$PSScriptRoot/bootstrap.py" --status\n', encoding="utf-8")
    files = {str(p.relative_to(destination)).replace("\\", "/"): _digest(p)
             for p in destination.rglob("*") if p.is_file() and p != state / "storage-probe.dpapi"
             and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    manifest = {"format": "jarvis-sber-candidate-v1", "mode": "OPERATOR_CANDIDATE", "files": files,
                "profile_directory": "operator-state", "activation": "blocked", "bank_verified": False}
    manifest["identity"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    (destination / "artifact.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return {"status": "prepared_candidate", "identity": manifest["identity"], "packages": len(lock["packages"]),
            "mode": "OPERATOR_CANDIDATE", "live_allowed": False, "database_modified": False}


def verify_candidate(root: Path) -> str:
    """Revalidate the reviewed immutable inventory in the managed worker itself."""
    try:
        manifest = json.loads(_read(root / "artifact.json"))
        identity = manifest.pop("identity")
        if (manifest.get("format") != "jarvis-sber-candidate-v1" or manifest.get("mode") != "OPERATOR_CANDIDATE"
                or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != identity):
            raise ValueError
        actual: set[str] = set()
        for base, folders, names in os.walk(root, followlinks=False):
            for name in folders + names:
                member = Path(base) / name
                if member.is_symlink() or getattr(member.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError
            folders[:] = [f for f in folders if f not in {"operator-state", "__pycache__"}]
            for name in names:
                member = Path(base) / name
                if member != root / "artifact.json" and member.suffix != ".pyc":
                    actual.add(member.relative_to(root).as_posix())
        if actual != set(manifest["files"]):
            raise ValueError
        for name, expected in manifest["files"].items():
            member = root / name
            if not member.resolve().is_relative_to(root.resolve()) or _digest(member) != expected:
                raise ValueError
        if not isinstance(identity, str):
            raise TypeError
        return identity
    except (OSError, ValueError, KeyError, TypeError):
        raise OperatorBlocked("operator_artifact_changed") from None


def candidate_inventory(parent: Path, *, active: Path | None = None) -> dict[str, Any]:
    """Inspect public artifact inventories; do not traverse private profile storage."""
    from datetime import UTC, datetime
    if not parent.is_absolute() or parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
        raise OperatorBlocked("inventory_parent_unavailable")
    rows: list[dict[str, Any]] = []
    for path in sorted(parent.glob("Jarvis-V9-Synthetic-Candidate-*")):
        row: dict[str, Any] = {"path": str(path), "active": active is not None and path == active,
            "identity": None, "status": "unavailable", "public_bytes": None,
            "profile_contents_read": False, "cleanup_recommendation": "retain"}
        try:
            if not path.is_dir() or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError
            source = path / "artifact.json"
            if source.is_symlink() or source.stat().st_size > 4 * 1024 * 1024:
                raise ValueError
            manifest = json.loads(_read(source))
            identity = manifest.pop("identity")
            if (manifest.get("format") != "jarvis-sber-candidate-v1" or manifest.get("mode") != "OPERATOR_CANDIDATE"
                    or manifest.get("activation") != "blocked" or manifest.get("bank_verified") is not False
                    or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != identity):
                raise ValueError
            files = manifest["files"]
            if not isinstance(files, dict) or len(files) > 100_000 or "bootstrap.py" not in files:
                raise ValueError
            total = source.stat().st_size
            for name, expected in files.items():
                member = Path(name)
                if member.is_absolute() or ".." in member.parts or "operator-state" in member.parts:
                    raise ValueError
                target = path / member
                for ancestor in (target, *target.parents):
                    if ancestor == parent:
                        break
                    if ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400:
                        raise ValueError
                if not target.is_file() or _digest(target) != expected:
                    raise ValueError
                total += target.stat().st_size
            row.update(identity=identity, status="verified_public_manifest", public_bytes=total,
                directory_created_at=datetime.fromtimestamp(path.stat().st_ctime, UTC).isoformat() if os.name == "nt" else None,
                private_state_usage="not_inspected", cleanup_recommendation="retain_active" if row["active"] else "manual_review_inactive_public_artifact")
        except (OSError, ValueError, TypeError, KeyError, OperatorBlocked):
            pass
        rows.append(row)
    complete = all(r["public_bytes"] is not None for r in rows)
    return {"candidates": rows, "public_bytes": sum(r["public_bytes"] for r in rows) if complete else None,
            "disk_usage_complete": complete,
            "profiles_scanned": False, "deleted": False, "database_modified": False}


def candidate_main(root: Path, args: list[str]) -> int:
    """Separate inspected candidate status and explicit DEMO; never bank activation."""
    parser = argparse.ArgumentParser(description="Jarvis: candidate offline, банк требует отдельной приёмки")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--demo", action="store_true")
    modes.add_argument("--connect", action="store_true")
    modes.add_argument("--operator-ui", action="store_true")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--profile", default="personal")
    parsed = parser.parse_args(args)
    if parsed.connect and parsed.state_root is None:
        print("Банк BLOCKED: нужен отдельный --state-root и интерактивное подтверждение в локальном UI.")
        return 2
    if parsed.operator_ui or parsed.connect:
        if parsed.state_root is None:
            parser.error("--operator-ui requires explicit --state-root outside Git")
        if parsed.state_root.resolve().is_relative_to(root.resolve()):
            parser.error("Persistent state must be outside the immutable candidate")
        from jarvis.finance.sber_captcha_viewer import operator_window
        from jarvis.finance.sber_launcher import prepare
        from jarvis.finance.sber_windows_compat import create_operator_state
        identity = verify_candidate(root)
        profile = parsed.profile if parsed.connect else "demo"
        mode = "human_read" if parsed.connect else "demo"
        if not parsed.state_root.exists():
            create_operator_state(parsed.state_root, configuration_id=profile, artifact_identity=identity, synthetic=not parsed.connect)
        runtime = parsed.state_root / "managed-runtime"
        if not runtime.exists():
            prepare(root / "public", workspace=runtime, state_root=parsed.state_root,
                    configuration_id=profile, artifact_identity=identity, operator_mode=mode)
        existing = json.loads((runtime / "runtime.json").read_text(encoding="utf8"))
        if (existing.get("operator_mode", "demo") != mode or existing["persistent"]["artifact_identity"] != identity
                or existing["persistent"]["configuration_id"] != profile):
            raise OperatorBlocked("operator_mode_or_build_changed")
        operator_window(runtime, artifact_identity=identity, human=parsed.connect)
        return 0
    if parsed.demo:
        from jarvis.finance.sber_launcher import prepare, rehearse
        fixture = prepare(root / "public")
        def answer(response: dict[str, Any]) -> list[str]:
            values = {"captcha_code": "ABCD", "otp": "123456", "new_pin": "31415", "new_pin_confirm": "31415"}
            return [values[key] for key in response["details"]["expects"]]
        print(json.dumps({"mode": "DEMO", **rehearse(fixture, answer)}))
        return 0
    print(json.dumps({"mode": "OPERATOR_CANDIDATE", "status": "blocked", "live_allowed": False,
                      "database_modified": False, "profile_directory": str(root / "operator-state"),
                      "activation": "separate_human_acceptance_required", "os_isolation": "unverified"}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build offline pinned Sber operator candidate")
    parser.add_argument("--fetch-wheelhouse", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--jarvis-wheel", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--inventory-parent", type=Path)
    parser.add_argument("--active-candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.inventory_parent:
            print(json.dumps(candidate_inventory(args.inventory_parent, active=args.active_candidate)))
            return 0
        if args.fetch_wheelhouse:
            print(json.dumps(fetch_runtime_wheels(args.fetch_wheelhouse)))
            return 0
        if not all((args.wheelhouse, args.jarvis_wheel, args.destination, args.uv)):
            parser.error("Build requires --wheelhouse, --jarvis-wheel, --destination and --uv")
        print(json.dumps(prepare_candidate(args.wheelhouse, args.jarvis_wheel, args.destination, uv=args.uv)))
        return 0
    except OperatorBlocked as error:
        code = str(error) if re.fullmatch(r"[a-z_]+", str(error)) else "candidate_unavailable"
        print("Candidate build unavailable: " + code + "; no bank action performed.")
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("Candidate build unavailable; no bank action performed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
