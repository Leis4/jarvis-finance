from __future__ import annotations

from typing import Any


def parse_iss_block(block: Any) -> list[dict[str, Any]]:
    """Parse one ISS block ({metadata, columns, data}) into list of dicts."""
    if not isinstance(block, dict):
        return []
    columns = block.get("columns")
    data = block.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, list):
            continue
        record: dict[str, Any] = {}
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                continue
            record[col] = row[i] if i < len(row) else None
        rows.append(record)
    return rows


def parse_iss_response(response: Any, block_name: str | None = None) -> list[dict[str, Any]]:
    """Parse ISS response. If block_name given, use that key, else first block."""
    if not isinstance(response, dict):
        return []
    if block_name is not None:
        return parse_iss_block(response.get(block_name))
    for value in response.values():
        if isinstance(value, dict) and "columns" in value and "data" in value:
            return parse_iss_block(value)
    return []


def first_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return first row or empty dict."""
    return rows[0] if rows else {}
