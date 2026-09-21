from court_search.embed.chunker import CHUNKING_VERSION, chunk_text, estimate_tokens
from court_search.embed.sections import SECTIONS, section_ids


def test_section_ids_match_five_jurisdictions() -> None:
    assert section_ids() == [
        "civil",
        "administrative",
        "criminal",
        "commercial",
        "administrative_offense",
    ]
    assert sum(item["rows"] for item in SECTIONS) == 95_778_927


def test_short_text_is_one_passage() -> None:
    passages = chunk_text("Коротке рішення суду.", doc_id="1", text_hash="abc")
    assert len(passages) == 1
    assert passages[0]["passage_id"].startswith("1:abc:" + CHUNKING_VERSION)
    assert passages[0]["char_start"] == 0


def test_long_text_splits_under_hard_limit() -> None:
    text = ("слово " * 2000).strip()
    passages = chunk_text(
        text,
        doc_id="2",
        text_hash="def",
        count_tokens=estimate_tokens,
        target_tokens=50,
        hard_limit_tokens=80,
        overlap_tokens=10,
    )
    assert len(passages) > 1
    assert all(item["token_count"] <= 80 for item in passages)
    assert passages[0]["ordinal"] == 0
