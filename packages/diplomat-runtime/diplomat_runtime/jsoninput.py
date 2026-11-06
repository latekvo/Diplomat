"""JSON as ``JSONSerialization`` reads it — the Python twin of ``JSONInput.parse``, for
the files both front-ends read (the run book, the telemetry ledger).

``json`` takes more than ``JSONSerialization`` does: ``Infinity``/``NaN``, a number
past float range, a lone surrogate escape, nesting past :data:`MAX_DEPTH`. Each of
those is a document the Swift side refuses whole, so it is refused here too, rather
than read as runs or tasks only one front-end can see. (What ``JSONSerialization``
takes and ``json`` does not, ``JSONInput`` refuses.) So is a string opening with
U+FEFF, which Darwin's parser drops and ``json`` keeps. A key given twice keeps its
first value, as ``JSONSerialization`` does on Darwin; ``json`` alone keeps the last.
"""

from __future__ import annotations

import json
import math
from typing import Any


#: Deepest nesting read, the top-level value being 1. Twin: ``JSONInput.maxDepth``.
MAX_DEPTH = 512


def loads(text: str) -> Any:
    """``text`` decoded, or ``ValueError`` where ``JSONSerialization`` would throw."""
    try:
        value = json.loads(text, parse_constant=_refuse, parse_float=_finite,
                           parse_int=_in_range, object_pairs_hook=_first_wins)
    except RecursionError:
        raise ValueError("nested past the recursion limit") from None
    _check(value)
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


def _check(value: Any) -> None:
    """Nesting past :data:`MAX_DEPTH`, a string opening with U+FEFF, and a lone
    surrogate - the one string ``json`` decodes that UTF-8 cannot encode
    (``UnicodeEncodeError`` is a ``ValueError``)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            if item.startswith("\ufeff"):
                raise ValueError("a string opening with U+FEFF")
            item.encode("utf-8")
        elif isinstance(item, (dict, list)):
            if depth > MAX_DEPTH:
                raise ValueError(f"nested deeper than {MAX_DEPTH}")
            if isinstance(item, dict):
                stack.extend((key, depth) for key in item)
                item = item.values()
            stack.extend((child, depth + 1) for child in item)
