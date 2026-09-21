from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from court_search.domain.enums import Jurisdiction


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paragraph(StrictModel):
    paragraph_id: str
    char_start: int
    char_end: int


class SourceRef(StrictModel):
    repo: str
    revision: str | None = None
    relative_path: str
    file_sha256: str | None = None
    row_group: int | None = None
    row_offset: int | None = None


class DecisionRecord(StrictModel):
    doc_id: str
    document_version: str
    case_number_raw: str | None = None
    case_number: str | None = None
    court_code: str | None = None
    judge_raw: str | None = None
    decision_date_raw: str | None = None
    decision_date: date | None = None
    source_year: int | None = None
    decision_year: int | None = None
    jurisdiction: Jurisdiction
    justice_kind: int | None = None
    justice_kind_name: str | None = None
    judgment_code: str | None = None
    category_code: str | None = None
    text: str
    text_hash: str
    paragraphs: list[Paragraph] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    registry_url: str
    ingested_at: datetime | None = None
