from __future__ import annotations

import re

from court_search.domain.models import Paragraph

_SPLIT = re.compile(r"\n+")


def split_paragraphs(text: str) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    if not text:
        return paragraphs
    cursor = 0
    index = 1
    for match in _SPLIT.finditer(text):
        end = match.start()
        if end > cursor:
            paragraphs.append(
                Paragraph(
                    paragraph_id=f"p{index:04d}",
                    char_start=cursor,
                    char_end=end,
                )
            )
            index += 1
        cursor = match.end()
    if cursor < len(text):
        paragraphs.append(
            Paragraph(
                paragraph_id=f"p{index:04d}",
                char_start=cursor,
                char_end=len(text),
            )
        )
    if not paragraphs:
        paragraphs.append(Paragraph(paragraph_id="p0001", char_start=0, char_end=len(text)))
    return paragraphs


def paragraphs_for_span(paragraphs: list[Paragraph], char_start: int, char_end: int) -> list[str]:
    return [
        paragraph.paragraph_id
        for paragraph in paragraphs
        if paragraph.char_end > char_start and paragraph.char_start < char_end
    ]
