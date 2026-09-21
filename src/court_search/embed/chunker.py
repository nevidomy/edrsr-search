from __future__ import annotations

from collections.abc import Callable
from typing import Any

from court_search.text.paragraphs import split_paragraphs

CHUNKING_VERSION = "para-pack-v1"
TARGET_TOKENS = 800
HARD_LIMIT_TOKENS = 1000
OVERLAP_TOKENS = 80


def estimate_tokens(text: str) -> int:
    """XLM-R / BGE-M3 ballpark when the real tokenizer is not loaded."""
    if not text:
        return 0
    return max(1, (len(text) + 2) // 3)


def chunk_text(
    text: str,
    *,
    doc_id: str,
    text_hash: str,
    count_tokens: Callable[[str], int] = estimate_tokens,
    target_tokens: int = TARGET_TOKENS,
    hard_limit_tokens: int = HARD_LIMIT_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    pieces: list[tuple[int, int, str]] = []
    for para in split_paragraphs(text):
        piece = text[para.char_start : para.char_end]
        if not piece.strip():
            continue
        if count_tokens(piece) <= hard_limit_tokens:
            pieces.append((para.char_start, para.char_end, piece))
            continue
        pieces.extend(_split_long(piece, para.char_start, count_tokens, hard_limit_tokens, overlap_tokens))

    packed: list[tuple[int, int, str]] = []
    buf_start = 0
    buf_end = 0
    buf: list[str] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens, buf_start, buf_end
        if not buf:
            return
        packed.append((buf_start, buf_end, "\n\n".join(buf)))
        buf = []
        buf_tokens = 0

    for start, end, piece in pieces:
        n = count_tokens(piece)
        if buf and buf_tokens + n > target_tokens:
            flush()
        if not buf:
            buf_start = start
        buf.append(piece)
        buf_end = end
        buf_tokens += n
    flush()

    out: list[dict[str, Any]] = []
    for index, (start, end, piece) in enumerate(packed):
        out.append(
            {
                "passage_id": f"{doc_id}:{text_hash}:{CHUNKING_VERSION}:{start}",
                "doc_id": doc_id,
                "text_hash": text_hash,
                "ordinal": index,
                "char_start": start,
                "char_end": end,
                "text": piece,
                "token_count": count_tokens(piece),
            }
        )
    return out


def _split_long(
    text: str,
    origin: int,
    count_tokens: Callable[[str], int],
    hard_limit_tokens: int,
    overlap_tokens: int,
) -> list[tuple[int, int, str]]:
    parts: list[tuple[int, int, str]] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        lo, hi = cursor, length
        best = cursor + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if count_tokens(text[cursor:mid]) <= hard_limit_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        start = origin + cursor
        end = origin + best
        parts.append((start, end, text[cursor:best]))
        if best >= length:
            break
        overlap = min(best - cursor, max(1, overlap_tokens * 3))
        nxt = best - overlap
        cursor = nxt if nxt > cursor else best
    return parts
