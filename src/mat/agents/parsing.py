"""Helpers to extract structured JSON from free-form LLM text."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the JSON object in ``text`` or ``None`` when none can be parsed.

    Prefers the last fenced ```json block (final answers usually come last);
    falls back to the span between the first ``{`` and the last ``}``.
    """
    candidates = _FENCED_JSON.findall(text)[::-1]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None
