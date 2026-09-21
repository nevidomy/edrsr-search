from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from court_search.domain.enums import JURISDICTION_BY_KIND, REGISTRY_REVIEW_URL, Jurisdiction
from court_search.domain.models import DecisionRecord, SourceRef
from court_search.text.normalize import normalize_case_number
from court_search.text.paragraphs import split_paragraphs


REQUIRED_COLUMNS = (
    "doc_id",
    "cause_num",
    "court_code",
    "judge",
    "adjudication_date",
    "judgment_code",
    "category_code",
    "year",
    "justice_kind",
    "justice_kind_name",
    "text",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Any, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        remaining = max_bytes
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            if remaining is not None:
                digest.update(chunk[:remaining])
                remaining -= len(chunk)
                if remaining <= 0:
                    break
            else:
                digest.update(chunk)
    return digest.hexdigest()


def parse_decision_date(value: Any) -> tuple[str | None, date | None]:
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    text = raw.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
        return raw, parsed.date()
    except ValueError:
        try:
            return raw, date.fromisoformat(raw[:10])
        except ValueError:
            return raw, None


def as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_jurisdiction(
    *,
    source_jurisdiction: Jurisdiction | None,
    justice_kind: int | None,
    justice_kind_name: str | None,
) -> tuple[Jurisdiction, list[str]]:
    issues: list[str] = []
    mapped = JURISDICTION_BY_KIND.get(justice_kind) if justice_kind is not None else None
    if source_jurisdiction and mapped and source_jurisdiction != mapped:
        issues.append(
            f"jurisdiction mismatch: repo={source_jurisdiction.value} kind={justice_kind}"
        )
        return source_jurisdiction, issues
    if mapped:
        return mapped, issues
    if source_jurisdiction:
        if justice_kind is not None:
            issues.append(f"unknown justice_kind={justice_kind}")
        return source_jurisdiction, issues
    raise ValueError("unable to resolve jurisdiction")


def document_version(*, text_hash: str, metadata: dict[str, Any]) -> str:
    payload = json.dumps({"text_hash": text_hash, **metadata}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_record(
    row: dict[str, Any],
    *,
    source: SourceRef,
    source_jurisdiction: Jurisdiction | None,
    build_paragraphs: bool = True,
) -> tuple[DecisionRecord | None, str | None]:
    text = row.get("text")
    if text is None or not str(text).strip():
        return None, "empty_text"
    text = str(text)
    doc_id = as_optional_str(row.get("doc_id"))
    if not doc_id:
        return None, "missing_doc_id"
    justice_kind = as_optional_int(row.get("justice_kind"))
    try:
        jurisdiction, _issues = resolve_jurisdiction(
            source_jurisdiction=source_jurisdiction,
            justice_kind=justice_kind,
            justice_kind_name=as_optional_str(row.get("justice_kind_name")),
        )
    except ValueError:
        return None, "unknown_jurisdiction"
    date_raw, decision_date = parse_decision_date(row.get("adjudication_date"))
    source_year = as_optional_int(row.get("year"))
    decision_year = decision_date.year if decision_date else None
    case_raw = as_optional_str(row.get("cause_num"))
    text_hash = sha256_text(text)
    version = document_version(
        text_hash=text_hash,
        metadata={
            "doc_id": doc_id,
            "case_number_raw": case_raw,
            "court_code": as_optional_str(row.get("court_code")),
            "decision_date": decision_date.isoformat() if decision_date else None,
            "jurisdiction": jurisdiction.value,
            "judgment_code": as_optional_str(row.get("judgment_code")),
            "category_code": as_optional_str(row.get("category_code")),
        },
    )
    return (
        DecisionRecord(
            doc_id=doc_id,
            document_version=version,
            case_number_raw=case_raw,
            case_number=normalize_case_number(case_raw),
            court_code=as_optional_str(row.get("court_code")),
            judge_raw=as_optional_str(row.get("judge")),
            decision_date_raw=date_raw,
            decision_date=decision_date,
            source_year=source_year,
            decision_year=decision_year,
            jurisdiction=jurisdiction,
            justice_kind=justice_kind,
            justice_kind_name=as_optional_str(row.get("justice_kind_name")),
            judgment_code=as_optional_str(row.get("judgment_code")),
            category_code=as_optional_str(row.get("category_code")),
            text=text,
            text_hash=text_hash,
            paragraphs=split_paragraphs(text) if build_paragraphs else [],
            source_refs=[source],
            registry_url=REGISTRY_REVIEW_URL.format(doc_id=doc_id),
            ingested_at=datetime.now(UTC),
        ),
        None,
    )
