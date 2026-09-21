from court_search.ingest.progress import format_duration
from court_search.indexing.mappings import (
    decision_chunk_index_name,
    decision_index_name,
    should_roll_chunk,
    snapshot_id,
)
from court_search.domain.enums import Jurisdiction
from court_search.domain.models import DecisionRecord, SourceRef
from court_search.indexing.records import record_from_source, source_from_record
from court_search.text.paragraphs import split_paragraphs


def test_source_roundtrip_preserves_text_and_ids() -> None:
    text = "Перший абзац.\n\nДругий абзац про оренду."
    record = DecisionRecord(
        doc_id="1001",
        document_version="abc",
        case_number="761/1/20",
        case_number_raw="761/1/20",
        court_code="2601",
        judge_raw="Тест",
        decision_date_raw="2020-01-02",
        decision_date=__import__("datetime").date(2020, 1, 2),
        source_year=2020,
        decision_year=2020,
        jurisdiction=Jurisdiction.CIVIL,
        justice_kind=1,
        justice_kind_name="civil",
        judgment_code="2",
        category_code="1060",
        text=text,
        text_hash="hash",
        paragraphs=split_paragraphs(text),
        source_refs=[SourceRef(repo="overthelex/edrsr-tspk", relative_path="data/x.parquet")],
        registry_url="https://reyestr.court.gov.ua/Review/1001",
    )
    restored = record_from_source(source_from_record(record))
    assert restored.doc_id == record.doc_id
    assert restored.text == record.text
    assert restored.case_number == record.case_number
    assert restored.jurisdiction == Jurisdiction.CIVIL
    assert restored.paragraphs == record.paragraphs
    assert restored.source_refs[0].relative_path == "data/x.parquet"


def test_record_from_legacy_pilot_source() -> None:
    restored = record_from_source(
        {
            "doc_id": "9",
            "document_version": "v",
            "case_number": "2-85/05",
            "court_code": "617",
            "jurisdiction": "civil",
            "text": "Приклад тексту.",
        }
    )
    assert restored.registry_url.endswith("/9")
    assert restored.paragraphs
    assert restored.text_hash


def test_chunk_names_and_roll() -> None:
    assert decision_index_name("civil-v1") == "decisions-civil-v1-0"
    assert decision_chunk_index_name("civil-v1", 0) == "decisions-civil-v1-c000"
    assert snapshot_id(2) == "chunk-002"
    assert should_roll_chunk(80 * 1024**3, 80 * 1024**3) is True
    assert should_roll_chunk(79 * 1024**3, 80 * 1024**3) is False
    assert format_duration(3661) == "01:01:01"
    assert format_duration(86400 + 60) == "1d 00:01:00"
