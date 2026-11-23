"""Telegram rich message formatting utilities."""
from __future__ import annotations

import re
from typing import Any


def header(emoji: str, title: str, tag_mode: bool = False) -> str:
    """Format an h1 header tag.

    If ``tag_mode`` is ``True`` the raw HTML ``<h1>`` is returned.
    Otherwise the output is converted to Telegram‑compatible HTML via
    :func:`to_telegram_html`.
    """
    prefix = f"{emoji} " if emoji else ""
    raw = f"<h1>{prefix}{title}</h1>"
    return raw if tag_mode else to_telegram_html(raw)


def metric(label: str, value: Any, accent: str = "bold", tag_mode: bool = False) -> str:
    """Format a highlighted metric tag."""
    if accent == "code":
        raw = f"{label}: <code>{value}</code>"
    else:
        raw = f"{label}: <b>{value}</b>"
    return raw


def table(rows: list[list[str] | tuple[str, ...]], tag_mode: bool = False) -> str:
    """Format a 2D table as HTML table tags.

    When ``tag_mode`` is ``True`` the raw ``<table>`` HTML is returned.
    Otherwise the output is converted to Telegram‑compatible HTML via
    :func:`to_telegram_html`.
    """
    raw = "<table>\n"
    for row in rows:
        row_cells = "".join(f"<td>{cell}</td>" for cell in row)
        raw += f"  <tr>{row_cells}</tr>\n"
    raw += "</table>"
    return raw if tag_mode else to_telegram_html(raw)


def math(formula: str) -> str:
    """Format a mathematical expression tag."""
    return f"<math>{formula}</math>"


def divider() -> str:
    """Format a horizontal rule divider tag."""
    return "<hr>"


def to_telegram_html(text: str) -> str:
    """Convert extended rich tags (h1, table, math, hr) into valid Telegram Bot API HTML.

    Telegram Bot API supports only: <b>, <i>, <u>, <s>, <code>, <pre>, <blockquote>, <a>.
    Tags like <h1>, <table>, <math>, <hr> must be mapped to valid Telegram equivalents.
    """
    if not text:
        return ""

    # Replace <h1>...</h1> with <b><u>...</u></b>
    res = re.sub(r"<h1>(.*?)</h1>", r"<b><u>\1</u></b>\n", text, flags=re.DOTALL)

    # Replace <hr> with a clean unicode divider
    res = re.sub(r"<hr\s*/?>", "───────────────────────", res, flags=re.IGNORECASE)

    # Replace <math>...</math> with <code>...</code>
    res = re.sub(r"<math>(.*?)</math>", r"<code>\1</code>", res, flags=re.DOTALL)

    # Parse and convert <table>...</table> into aligned monospace text inside <pre>
    def _convert_table(match: re.Match[str]) -> str:
        table_html = match.group(1)
        # Extract rows
        tr_matches = re.findall(r"<tr>(.*?)</tr>", table_html, flags=re.DOTALL)
        parsed_rows: list[list[str]] = []
        for tr in tr_matches:
            td_matches = re.findall(r"<td>(.*?)</td>", tr, flags=re.DOTALL)
            parsed_rows.append([td.strip() for td in td_matches])

        if not parsed_rows:
            return ""

        # Calculate column widths
        col_count = max(len(r) for r in parsed_rows)
        widths = [0] * col_count
        for r in parsed_rows:
            for i, cell in enumerate(r):
                # strip any html tags for length calc
                clean_cell = re.sub(r"<[^>]+>", "", cell)
                widths[i] = max(widths[i], len(clean_cell))

        formatted_lines = []
        for r in parsed_rows:
            line_parts = []
            for i in range(len(r)):
                cell_val = r[i]
                clean_cell = re.sub(r"<[^>]+>", "", cell_val)
                padding = widths[i] - len(clean_cell)
                line_parts.append(cell_val + (" " * max(0, padding)))
            formatted_lines.append("  ".join(line_parts))

        return "<pre>\n" + "\n".join(formatted_lines) + "\n</pre>"

    res = re.sub(r"<table>(.*?)</table>", _convert_table, res, flags=re.DOTALL)

    return res
