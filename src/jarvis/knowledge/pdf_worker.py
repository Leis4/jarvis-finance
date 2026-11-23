"""Fixed disposable PDF child, with no credential environment or inherited I/O."""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
from pathlib import Path


def main() -> int:
    request_path, output_path = map(Path, sys.argv[1:3])
    # The venv Windows redirector would create a second process. Run the pinned
    # base interpreter and add only declared dependency/source import directories,
    # without site.addsitedir/.pth execution or environment-based activation.
    sys.path.insert(0, sys.argv[3])
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        if request_path.name != "request.json" or output_path.name != "response.json" or request_path.parent != Path.cwd() or output_path.parent != Path.cwd():
            return 2
        if request_path.stat().st_size > 12 * 1024 * 1024:
            return 2
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if set(request) != {"format", "original", "chunk_size", "overlap", "output_limit"} or request["format"] != "jarvis-pdf-worker-v1":
            return 2
        if type(request["output_limit"]) is not int or not 1 <= request["output_limit"] <= 8 * 1024 * 1024:
            return 2
        def no_network(*args: object, **kwargs: object) -> None:
            raise OSError("Network disabled in PDF worker")
        socket.socket.connect = no_network  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = no_network  # type: ignore[method-assign,assignment]
        from jarvis.knowledge.loader import load_pdf_bytes_evidence
        artifact = load_pdf_bytes_evidence(base64.b64decode(request["original"], validate=True),
            chunk_size=request["chunk_size"], overlap=request["overlap"])
        data = json.dumps({"status": "ok", "artifact": artifact}, ensure_ascii=False).encode("utf-8")
        if len(data) > request["output_limit"]:
            return 3
        temporary = output_path.with_suffix(".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, output_path)
        return 0
    except Exception:  # noqa: BLE001 - child diagnostics must never escape
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
