from __future__ import annotations

import json
import sys
from typing import Any, Iterable


def table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    headers = [str(v) for v in headers]
    values = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [len(v) for v in headers]
    for row in values:
        widths = [max(width, len(value)) for width, value in zip(widths, row)]
    print("  ".join(value.ljust(width) for value, width in zip(headers, widths)))
    for row in values:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))


def emit(value: Any, as_json: bool = False) -> None:
    if as_json:
        json.dump(value, sys.stdout, indent=2, sort_keys=True, default=str)
        print()


def bytes_text(value: int | None) -> str:
    if value is None:
        return "unknown"
    units = ["bytes", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "bytes" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} bytes"
