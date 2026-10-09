"""JSON as ``JSONSerialization`` reads it — the Python twin of ``JSONInput.parse``, for
the files both front-ends read (the run book, the telemetry ledger).

``json`` takes more than ``JSONSerialization`` does: ``Infinity``/``NaN``, a number
past float range, a lone surrogate escape. Each of those is a document the Swift side
refuses whole, so it is refused here too, rather than read as runs or tasks only one
front-end can see. A key given twice keeps its first value, as ``JSONSerialization``
does on Darwin; ``json`` alone keeps the last.
"""

from __future__ import annotations

import json
import math
from typing import Any


def loads(text: str) -> Any:
    """``text`` decoded, or ``ValueError`` where ``JSONSerialization`` would throw."""
    value = json.loads(text, parse_constant=_refuse, parse_float=_finite,
                       parse_int=_in_range, object_pairs_hook=_first_wins)
    _check_text(value)
    return value


def _refuse(token: str) -> Any:
    raise ValueError(f"{token} is not JSON")


def _finite(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"{token} is past float range")
    return value


def _in_range(token: str) -> int:
    value = int(token)
    try:
        float(value)
    except OverflowError:
        raise ValueError("an integer past float range") from None
    return value


def _first_wins(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        out.setdefault(key, value)
    return out


def _check_text(value: Any) -> None:
    """A lone surrogate is the one string ``json`` decodes that UTF-8 cannot encode;
    ``UnicodeEncodeError`` is a ``ValueError``."""
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_text(key)
            _check_text(item)
    elif isinstance(value, list):
        for item in value:
            _check_text(item)
