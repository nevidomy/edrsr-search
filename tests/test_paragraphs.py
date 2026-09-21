from court_search.text.paragraphs import paragraphs_for_span, split_paragraphs
from court_search.text.spans import citation_id, validate_quote


def test_split_paragraphs_uses_codepoints() -> None:
    text = "Перший абзац.\n\nДругий абзац."
    paragraphs = split_paragraphs(text)
    assert paragraphs[0].paragraph_id == "p0001"
    assert text[paragraphs[0].char_start : paragraphs[0].char_end] == "Перший абзац."
    assert text[paragraphs[1].char_start : paragraphs[1].char_end] == "Другий абзац."


def test_quote_must_match_source_slice() -> None:
    text = "Приклад тексту судового акта."
    validate_quote(text, 0, 7, "Приклад")
    assert citation_id("1000000001", "a" * 64, 0, 7).startswith("edrsr:1000000001:")


def test_paragraphs_for_span() -> None:
    text = "AAA\n\nBBB\n\nCCC"
    paragraphs = split_paragraphs(text)
    ids = paragraphs_for_span(paragraphs, 0, 8)
    assert ids[0] == "p0001"
