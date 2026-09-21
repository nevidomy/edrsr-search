from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from court_search import API_SCHEMA_VERSION
from court_search.domain.enums import FilterField, Jurisdiction, SearchMode
from court_search.domain.models import SourceRef, StrictModel


class SearchFilters(StrictModel):
    jurisdictions: list[Jurisdiction] | None = None
    date_from: date | None = None
    date_to: date | None = None
    court_codes: list[str] | None = None
    judgment_codes: list[str] | None = None
    category_codes: list[str] | None = None
    case_number: str | None = None

    @field_validator("court_codes", "judgment_codes", "category_codes", "jurisdictions")
    @classmethod
    def _bound_lists(cls, value: list[Any] | None) -> list[Any] | None:
        if value is None:
            return None
        if len(value) > 50:
            raise ValueError("filter lists are limited to 50 values")
        return value

    @model_validator(mode="after")
    def _dates(self) -> SearchFilters:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: SearchMode = SearchMode.HYBRID
    filters: SearchFilters = Field(default_factory=SearchFilters)
    required_phrases: list[str] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=10, ge=1, le=20)
    rerank: bool = True
    allow_fallback: bool = True
    max_response_chars: int = Field(default=30_000, ge=4_000, le=60_000)
    corpus_generation: str | None = None
    cursor: str | None = None

    @field_validator("required_phrases")
    @classmethod
    def _phrases(cls, value: list[str]) -> list[str]:
        for phrase in value:
            if not phrase or len(phrase) > 200:
                raise ValueError("required phrases must be 1-200 characters")
        return value


class EvidenceItem(StrictModel):
    citation_id: str
    doc_id: str
    document_version: str
    passage_id: str
    paragraph_ids: list[str] = Field(default_factory=list)
    char_start: int
    char_end: int
    quote: str


class DecisionMetadata(StrictModel):
    doc_id: str
    document_version: str
    case_number: str | None = None
    case_number_raw: str | None = None
    court_code: str | None = None
    court_label: str | None = None
    jurisdiction: Jurisdiction | None = None
    judgment_code: str | None = None
    judgment_label: str | None = None
    category_code: str | None = None
    category_label: str | None = None
    decision_date: date | None = None
    judge: str | None = None
    registry_url: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class RetrievalSignals(StrictModel):
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    fused_rank: int | None = None
    ranking_stage: Literal["exact", "lexical", "fusion", "rerank"] = "lexical"
    score: float | None = None


class SearchHit(StrictModel):
    rank: int
    metadata: DecisionMetadata
    evidence: list[EvidenceItem] = Field(default_factory=list)
    retrieval_signals: RetrievalSignals = Field(default_factory=RetrievalSignals)


class CoverageInfo(StrictModel):
    lexical_decisions: int = 0
    semantic_decisions: int = 0
    semantic_complete: bool = False
    generation: str | None = None


class SearchResponse(StrictModel):
    request_id: str
    schema_version: str = API_SCHEMA_VERSION
    corpus_generation: str
    retrieval_version: str
    effective_query: str
    effective_filters: SearchFilters
    mode_used: SearchMode
    rerank_used: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    coverage: CoverageInfo
    candidate_limit_reached: bool = False
    results: list[SearchHit] = Field(default_factory=list)
    returned_count: int = 0
    candidate_window_size: int = 0
    next_cursor: str | None = None
    cursor_expires_at: datetime | None = None
    total_matches: int | None = None
    response_truncated: bool = False
    timings_ms: dict[str, int] = Field(default_factory=dict)
    unknown_dates_excluded: bool = False


class ReadRequest(StrictModel):
    doc_id: str | None = None
    document_version: str | None = None
    corpus_generation: str | None = None
    passage_id: str | None = None
    paragraph_start: str | None = None
    paragraph_end: str | None = None
    max_response_chars: int = Field(default=30_000, ge=4_000, le=60_000)
    cursor: str | None = None

    @model_validator(mode="after")
    def _selectors(self) -> ReadRequest:
        if not self.cursor and not self.doc_id:
            raise ValueError("doc_id is required unless a cursor is provided")
        if self.passage_id and (self.paragraph_start or self.paragraph_end):
            raise ValueError("passage_id cannot be combined with paragraph range")
        if self.paragraph_end and not self.paragraph_start:
            raise ValueError("paragraph_end requires paragraph_start")
        return self


class ParagraphPage(StrictModel):
    paragraph_id: str
    char_start: int
    char_end: int
    text: str


class ReadResponse(StrictModel):
    request_id: str
    schema_version: str = API_SCHEMA_VERSION
    corpus_generation: str
    metadata: DecisionMetadata
    paragraphs: list[ParagraphPage] = Field(default_factory=list)
    paragraph_manifest: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None
    response_truncated: bool = False
    complete: bool = False


class CaseDecisionsRequest(StrictModel):
    case_number: str = Field(min_length=1, max_length=200)
    court_codes: list[str] | None = None
    jurisdictions: list[Jurisdiction] | None = None
    limit: int = Field(default=50, ge=1, le=100)
    corpus_generation: str | None = None
    cursor: str | None = None


class CaseDecisionsResponse(StrictModel):
    request_id: str
    schema_version: str = API_SCHEMA_VERSION
    corpus_generation: str
    case_number: str
    case_number_raw_values: list[str] = Field(default_factory=list)
    normalization_collision: bool = False
    results: list[DecisionMetadata] = Field(default_factory=list)
    returned_count: int = 0
    next_cursor: str | None = None
    scope_note: str = (
        "These are the acts present in the installed snapshot that share this "
        "normalized case number. This is not proof of a complete appeal chain."
    )


class FilterLookupRequest(StrictModel):
    field: FilterField
    query: str | None = Field(default=None, max_length=200)
    jurisdiction: Jurisdiction | None = None
    limit: int = Field(default=20, ge=1, le=50)


class FilterValue(StrictModel):
    code: str
    label: str | None = None
    count: int | None = None
    count_scope: str | None = None
    dictionary_version: str | None = None


class FilterLookupResponse(StrictModel):
    request_id: str
    schema_version: str = API_SCHEMA_VERSION
    field: FilterField
    values: list[FilterValue] = Field(default_factory=list)
    dictionary_version: str | None = None
    dictionary_provenance: str | None = None


class CorpusInfoRequest(StrictModel):
    corpus_generation: str | None = None


class CorpusInfo(StrictModel):
    request_id: str
    schema_version: str = API_SCHEMA_VERSION
    tool_schema_version: str
    active_generation: str | None = None
    requested_generation: str | None = None
    ready: bool
    status: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, Any] = Field(default_factory=dict)
    jurisdictions: dict[str, int] = Field(default_factory=dict)
    year_min: int | None = None
    year_max: int | None = None
    min_observed_decision_date: date | None = None
    max_observed_decision_date: date | None = None
    ingested_at: datetime | None = None
    source_published_at: datetime | None = None
    authoritative_coverage_through: date | None = None
    lexical_coverage: int = 0
    semantic_coverage: int = 0
    semantic_complete: bool = False
    reranker_ready: bool = False
    embedding_spec: dict[str, Any] | None = None
    capabilities: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
