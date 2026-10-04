"""Byte-stable JSON serialisation.

Both `EvidenceBundle.bundle_hash` and the audit hash chain depend on the exact
bytes this produces. Two runs on the same logical content must emit identical
bytes, or replay and chain verification both break. Hence: sorted keys, no
whitespace, ASCII-escaped, and every non-JSON scalar pinned to one form.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)  # never float — 19.99 has no exact binary form
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.astimezone(timezone.utc).isoformat()
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not canonically serialisable: {type(o).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
