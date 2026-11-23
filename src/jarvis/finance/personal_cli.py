"""Run the personal CFO through the existing local review application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def runtime_identity() -> dict[str, Any]:
    """Describe this startup, not the current checkout of an older process."""
    path = Path(__file__).resolve()
    root = path.parents[3]
    package = path.parent.parent
    files = sorted(p for p in package.rglob('*') if p.is_file() and not p.is_symlink()
        and p.suffix in {'.py', '.js', '.css', '.html'})
    files.extend(p for p in (root/'devtools'/'CFO_SERVICE.vbs', root/'devtools'/'CFO.cmd')
        if p.is_file() and not p.is_symlink())
    digest = hashlib.sha256()
    for source in files:
        digest.update(source.relative_to(root).as_posix().encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(source.read_bytes()).digest())
    head = None
    dirty = None
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True,
            text=True, timeout=2, check=True).stdout.strip()
        dirty = bool(subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal',
            '--', 'src/jarvis', 'devtools/CFO_SERVICE.vbs', 'devtools/CFO.cmd'], cwd=root, capture_output=True,
            text=True, timeout=2, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {'pid':os.getpid(), 'started_at':datetime.now(UTC).isoformat(),
        'source_head':head, 'source_dirty':dirty, 'source_root':str(root),
        'cli_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'runtime_source_sha256':digest.hexdigest(), 'runtime_source_files':len(files)}


def main() -> int:
    """Back up before schema initialization, then serve a single loopback worker."""
    parser = argparse.ArgumentParser(description="Personal Jarvis CFO")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("data/cfo-review"))
    parser.add_argument("--refresh", action="store_true", help="One read-only Sber refresh before serving")
    parser.add_argument("--check-bank", action="store_true", help="Check bank once and exit with sanitized status")
    parser.add_argument('--no-jobs', action='store_true', help='Serve web only, without background jobs or Telegram polling')
    args = parser.parse_args()
    if args.no_jobs and (args.refresh or args.check_bank):
        parser.error('--no-jobs cannot be combined with bank actions')
    from dotenv import load_dotenv
    load_dotenv(Path(".env"))
    from jarvis.core.config import settings
    from jarvis.finance.db import FinanceDB
    from jarvis.finance.personal_cfo import PersonalCFO, backup_database
    db = FinanceDB(args.database or settings.db_path)
    args.workspace.mkdir(parents=True, exist_ok=True)
    # OS lock is held throughout initialization and serving, not merely sync.
    lock = (args.workspace / "personal.lock").open("a+b")
    try:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                if lock.read(1) == b"":
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print(f"CFO workspace already in use or inaccessible. Open http://127.0.0.1:{args.port}/")
                return 2
        else:
            from importlib import import_module
            fcntl = import_module("fcntl")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if db.path.exists():
            backup_database(db, args.workspace / "backups")
        db.init()
        cfo = PersonalCFO(db)
        if args.refresh or args.check_bank:
            result = cfo.refresh()
            print("Sber:", result["status"], result.get("code", ""))
            if args.check_bank:
                return 0 if result["status"] == "updated" else 2
        from jarvis.finance.review_store import ReviewStore
        store = ReviewStore(args.workspace)
        if not store.path.exists():
            store.initialize({"source_kind": "pdf", "apply_allowed": False, "items": [], "groups": []})
        from secrets import token_urlsafe

        import uvicorn
        from fastapi import FastAPI

        from jarvis.finance.review_auth import publish_owner_key
        from jarvis.finance.review_ui import create_review_app
        owner_key = token_urlsafe(32)
        publish_owner_key(args.workspace, owner_key)
        app = create_review_app(store, owner_key, personal_cfo=cfo, web_only=args.no_jobs)
        app.state.runtime_build = {**runtime_identity(), 'no_jobs':args.no_jobs,
            'release_mode':'web-only' if args.no_jobs else 'scheduled',
            'task_name':os.environ.get('JARVIS_CFO_TASK_NAME')}
        if not args.no_jobs:
            from contextlib import asynccontextmanager

            from jarvis.finance.personal_runtime import PersonalRuntime
            runtime = PersonalRuntime(cfo, args.workspace)
            original_lifespan = app.router.lifespan_context

            @asynccontextmanager
            async def lifespan(application: FastAPI) -> AsyncIterator[Mapping[str, Any]]:
                async with original_lifespan(application) as state:
                    runtime.start()
                    try:
                        yield state or {}
                    finally:
                        runtime.stop()

            app.router.lifespan_context = lifespan
        print(json.dumps({'runtime_build':app.state.runtime_build}), flush=True)
        print(f"JARVIS CFO: http://127.0.0.1:{args.port}/")
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
