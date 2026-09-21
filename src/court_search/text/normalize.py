from __future__ import annotations

import re

_SPACE = re.compile(r"[\s\u00a0]+")
_SEP = re.compile(r"[–—−]+")


def normalize_case_number(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    text = _SEP.sub("-", text)
    text = _SPACE.sub("", text)
    return text.casefold()


def looks_like_doc_id(query: str) -> str | None:
    stripped = query.strip()
    if stripped.isdigit() and 1 <= len(stripped) <= 18:
        return stripped.lstrip("0") and stripped or "0"
    return None
