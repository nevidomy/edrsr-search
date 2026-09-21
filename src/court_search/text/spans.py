from __future__ import annotations


def slice_text(text: str, char_start: int, char_end: int) -> str:
    if char_start < 0 or char_end < char_start or char_end > len(text):
        raise ValueError("span is outside the source text")
    return text[char_start:char_end]


def validate_quote(text: str, char_start: int, char_end: int, quote: str) -> None:
    actual = slice_text(text, char_start, char_end)
    if actual != quote:
        raise ValueError("quote does not match the source slice")


def citation_id(doc_id: str, document_version: str, char_start: int, char_end: int) -> str:
    return f"edrsr:{doc_id}:{document_version}:{char_start}-{char_end}"


def find_excerpt(text: str, needle: str) -> tuple[int, int] | None:
    if not needle:
        return None
    start = text.find(needle)
    if start < 0:
        compact = " ".join(needle.split())
        start = text.find(compact)
        if start < 0:
            return None
        needle = compact
    return start, start + len(needle)
