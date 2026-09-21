from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from court_search.domain.enums import REGISTRY_REVIEW_URL, Jurisdiction
from court_search.domain.models import DecisionRecord, SourceRef
from court_search.ingest.canonical import as_optional_str, sha256_text
from court_search.text.paragraphs import split_paragraphs


def source_from_record(record: DecisionRecord) -> dict[str, Any]:
    return {
        "doc_id": record.doc_id,
        "document_version": record.document_version,
        "case_number": record.case_number,
        "case_number_raw": record.case_number_raw,
        "court_code": record.court_code,
        "jurisdiction": record.jurisdiction.value,
        "judgment_code": record.judgment_code,
        "category_code": record.category_code,
        "decision_date": record.decision_date.isoformat() if record.decision_date else None,
        "decision_date_raw": record.decision_date_raw,
        "source_year": record.source_year,
        "decision_year": record.decision_year,
        "justice_kind": record.justice_kind,
        "justice_kind_name": record.justice_kind_name,
        "judge": record.judge_raw,
        "text_hash": record.text_hash,
        "registry_url": record.registry_url,
        "source_refs_json": json.dumps(
            [item.model_dump(mode="json") for item in record.source_refs],
            ensure_ascii=False,
        ),
        "text": record.text,
    }


def record_from_source(src: dict[str, Any]) -> DecisionRecord:
    text = str(src.get("text") or "")
    text_hash = src.get("text_hash") or sha256_text(text)
    refs_raw = src.get("source_refs_json")
    if refs_raw:
        source_refs = [SourceRef.model_validate(item) for item in json.loads(refs_raw)]
    else:
        source_refs = []
    decision_date = _parse_date(src.get("decision_date"))
    doc_id = str(src["doc_id"])
    return DecisionRecord(
        doc_id=doc_id,
        document_version=str(src.get("document_version") or text_hash),
        case_number_raw=src.get("case_number_raw"),
        case_number=src.get("case_number"),
        court_code=as_optional_str(src.get("court_code")),
        judge_raw=as_optional_str(src.get("judge")),
        decision_date_raw=as_optional_str(src.get("decision_date_raw")),
        decision_date=decision_date,
        source_year=src.get("source_year"),
        decision_year=src.get("decision_year"),
        jurisdiction=Jurisdiction(src["jurisdiction"]),
        justice_kind=src.get("justice_kind"),
        justice_kind_name=as_optional_str(src.get("justice_kind_name")),
        judgment_code=as_optional_str(src.get("judgment_code")),
        category_code=as_optional_str(src.get("category_code")),
        text=text,
        text_hash=str(text_hash),
        paragraphs=split_paragraphs(text),
        source_refs=source_refs,
        registry_url=src.get("registry_url") or REGISTRY_REVIEW_URL.format(doc_id=doc_id),
    )


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
