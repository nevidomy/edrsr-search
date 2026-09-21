from __future__ import annotations

import json
import time
import uuid
from datetime import date, datetime
from typing import Any

from court_search import API_SCHEMA_VERSION, TOOL_SCHEMA_VERSION
from court_search.config import AppConfig
from court_search.dictionaries import DictionarySet
from court_search.domain.contracts import (
    CaseDecisionsRequest,
    CaseDecisionsResponse,
    CorpusInfo,
    CorpusInfoRequest,
    CoverageInfo,
    DecisionMetadata,
    EvidenceItem,
    FilterLookupRequest,
    FilterLookupResponse,
    FilterValue,
    ParagraphPage,
    ReadRequest,
    ReadResponse,
    RetrievalSignals,
    SearchFilters,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from court_search.domain.enums import FilterField, SearchMode
from court_search.domain.errors import (
    CorpusNotReady,
    InvalidArgument,
    ModelUnavailable,
    NotFound,
    UnknownFilter,
    VersionUnavailable,
)
from court_search.domain.models import DecisionRecord
from court_search.indexing.opensearch import OpenSearchIndex
from court_search.storage.db import StateStore
from court_search.text.normalize import looks_like_doc_id, normalize_case_number
from court_search.text.paragraphs import paragraphs_for_span
from court_search.text.spans import citation_id, find_excerpt, validate_quote


class SearchService:
    def __init__(self, config: AppConfig, store: StateStore, dictionaries: DictionarySet) -> None:
        self.config = config
        self.store = store
        self.dictionaries = dictionaries
        self.opensearch = OpenSearchIndex(config.search) if config.search.backend == "opensearch" else None

    def resolve_generation(self, requested: str | None) -> str:
        active = self.store.active_generation()
        name = requested or active
        if not name:
            raise CorpusNotReady(
                "No active corpus generation is available yet. "
                "Run `court-search ingest-slice` and try again.",
                details={"active_generation": None},
            )
        if not self.store.get_generation(name):
            raise InvalidArgument(f"Unknown corpus generation: {name}")
        return name

    def search(self, request: SearchRequest) -> SearchResponse:
        started = time.perf_counter()
        generation = self.resolve_generation(request.corpus_generation)
        if request.cursor:
            raise InvalidArgument("Cursor continuation is not yet enabled for this slice")
        if request.mode == SearchMode.SEMANTIC and not request.allow_fallback:
            raise ModelUnavailable("Semantic retrieval is not indexed for this slice")
        filters = request.filters
        effective = filters.model_copy(
            update={"case_number": normalize_case_number(filters.case_number) or filters.case_number}
        )
        unknown_dates_excluded = bool(effective.date_from or effective.date_to)
        degradation: list[str] = []
        mode_used = SearchMode.LEXICAL
        if request.mode == SearchMode.SEMANTIC:
            degradation.append("semantic_unavailable_fallback_lexical")
        elif request.mode == SearchMode.HYBRID:
            degradation.append("hybrid_unavailable_lexical_only")

        filter_payload = _filter_payload(effective, request.required_phrases)
        exact_id = looks_like_doc_id(request.query)
        hits: list[tuple[DecisionRecord, float, str]] = []
        if exact_id:
            record = self._get_record(generation, exact_id)
            if record and _passes_filters(record, effective):
                hits.append((record, 0.0, "exact"))
        if not hits:
            ranked = self._lexical_search(
                generation,
                request.query,
                filters=filter_payload,
                limit=max(request.limit * 5, 50),
            )
            hits = [(record, score, "lexical") for record, score in ranked]

        window = hits[:200]
        selected = window[: request.limit]
        results: list[SearchHit] = []
        for index, (record, score, stage) in enumerate(selected, start=1):
            evidence = _evidence_for(record, request.query, max_items=2)
            results.append(
                SearchHit(
                    rank=index,
                    metadata=self._metadata(record),
                    evidence=evidence,
                    retrieval_signals=RetrievalSignals(
                        lexical_rank=index,
                        fused_rank=index,
                        ranking_stage=stage,  # type: ignore[arg-type]
                        score=score,
                    ),
                )
            )
        payload = SearchResponse(
            request_id=_request_id(),
            schema_version=API_SCHEMA_VERSION,
            corpus_generation=generation,
            retrieval_version=self._retrieval_version(),
            effective_query=request.query,
            effective_filters=effective,
            mode_used=mode_used,
            rerank_used=False,
            degradation_reasons=degradation,
            coverage=CoverageInfo(
                lexical_decisions=self.store.count_decisions(generation),
                semantic_decisions=0,
                semantic_complete=False,
                generation=generation,
            ),
            results=results,
            returned_count=len(results),
            candidate_window_size=len(window),
            total_matches=len(window) if mode_used == SearchMode.LEXICAL else None,
            unknown_dates_excluded=unknown_dates_excluded,
            timings_ms={"total": int((time.perf_counter() - started) * 1000)},
        )
        return _fit_budget(payload, request.max_response_chars)

    def get_decision(self, request: ReadRequest) -> ReadResponse:
        generation = self.resolve_generation(request.corpus_generation)
        if request.cursor:
            raise InvalidArgument("Read cursors are accepted but not yet required for this slice")
        assert request.doc_id
        record = self._get_record(generation, request.doc_id)
        if record is None:
            raise NotFound(f"Decision {request.doc_id} was not found in generation {generation}")
        if request.document_version and request.document_version != record.document_version:
            raise VersionUnavailable(
                "The requested document version is not available in this generation",
                details={
                    "requested": request.document_version,
                    "available": record.document_version,
                },
            )
        start_index = 0
        if request.paragraph_start:
            ids = [p.paragraph_id for p in record.paragraphs]
            if request.paragraph_start not in ids:
                raise InvalidArgument(f"Unknown paragraph_start {request.paragraph_start}")
            start_index = ids.index(request.paragraph_start)
            if request.paragraph_end:
                if request.paragraph_end not in ids:
                    raise InvalidArgument(f"Unknown paragraph_end {request.paragraph_end}")
                end_index = ids.index(request.paragraph_end) + 1
            else:
                end_index = len(record.paragraphs)
        else:
            end_index = len(record.paragraphs)
        pages: list[ParagraphPage] = []
        used = 0
        truncated = False
        for paragraph in record.paragraphs[start_index:end_index]:
            text = record.text[paragraph.char_start : paragraph.char_end]
            if used + len(text) > request.max_response_chars and pages:
                truncated = True
                break
            if len(text) > request.max_response_chars:
                text = text[: request.max_response_chars]
                truncated = True
            pages.append(
                ParagraphPage(
                    paragraph_id=paragraph.paragraph_id,
                    char_start=paragraph.char_start,
                    char_end=paragraph.char_start + len(text) if truncated else paragraph.char_end,
                    text=text,
                )
            )
            used += len(text)
            if truncated:
                break
        complete = (start_index + len(pages) >= len(record.paragraphs)) and not truncated
        next_cursor = None
        if not complete and pages:
            last = pages[-1].paragraph_id
            ids = [p.paragraph_id for p in record.paragraphs]
            nxt = ids.index(last) + 1
            if nxt < len(ids):
                next_cursor = ids[nxt]
        return ReadResponse(
            request_id=_request_id(),
            corpus_generation=generation,
            metadata=self._metadata(record),
            paragraphs=pages,
            paragraph_manifest=[p.model_dump() for p in record.paragraphs],
            next_cursor=next_cursor,
            response_truncated=truncated,
            complete=complete,
        )

    def get_case_decisions(self, request: CaseDecisionsRequest) -> CaseDecisionsResponse:
        generation = self.resolve_generation(request.corpus_generation)
        normalized = normalize_case_number(request.case_number)
        if not normalized:
            raise InvalidArgument("case_number is empty after normalization")
        jurisdictions = [item.value for item in request.jurisdictions] if request.jurisdictions else None
        if self.opensearch is not None:
            records = self.opensearch.list_by_case(
                generation,
                normalized,
                court_codes=request.court_codes,
                jurisdictions=jurisdictions,
                limit=request.limit + 1,
            )
        else:
            records = self.store.list_by_case(
                generation,
                normalized,
                court_codes=request.court_codes,
                jurisdictions=jurisdictions,
                limit=request.limit + 1,
                after_date=None,
                after_id=None,
            )
        has_more = len(records) > request.limit
        records = records[: request.limit]
        raws = sorted({item.case_number_raw for item in records if item.case_number_raw})
        return CaseDecisionsResponse(
            request_id=_request_id(),
            corpus_generation=generation,
            case_number=normalized,
            case_number_raw_values=raws,
            normalization_collision=len(raws) > 1,
            results=[self._metadata(item) for item in records],
            returned_count=len(records),
            next_cursor=records[-1].doc_id if has_more and records else None,
        )

    def lookup_filters(self, request: FilterLookupRequest) -> FilterLookupResponse:
        if request.field not in FilterField:
            raise UnknownFilter(f"Unsupported filter field: {request.field}")
        values = [
            FilterValue.model_validate(item)
            for item in self.dictionaries.values(
                request.field,
                query=request.query,
                jurisdiction=request.jurisdiction,
                limit=request.limit,
            )
        ]
        return FilterLookupResponse(
            request_id=_request_id(),
            field=request.field,
            values=values,
            dictionary_version=self.dictionaries.version(request.field),
            dictionary_provenance=self.dictionaries.provenance(request.field),
        )

    def get_corpus_info(self, request: CorpusInfoRequest | None = None) -> CorpusInfo:
        request = request or CorpusInfoRequest()
        active = self.store.active_generation()
        requested = request.corpus_generation or active
        notes = [
            "A retrieved record is one judicial act. Several acts can belong to one case.",
            "Installed snapshot presence is not completeness of the live registry.",
            "Do not infer current legal status from rank or from a document being present.",
        ]
        capabilities = ["lookup_filters", "get_corpus_info"]
        if not requested or not self.store.get_generation(requested):
            return CorpusInfo(
                request_id=_request_id(),
                tool_schema_version=TOOL_SCHEMA_VERSION,
                active_generation=active,
                requested_generation=request.corpus_generation,
                ready=False,
                status="CORPUS_NOT_READY",
                capabilities=capabilities,
                notes=notes
                + ["Search and document reads will fail until a generation is ingested and activated."],
                sources=_source_status(self.config),
            )
        generation = self.store.get_generation(requested)
        assert generation
        counts = json.loads(generation["counts_json"])
        source_manifest = json.loads(generation["source_manifest_json"])
        dmin, dmax = self.store.date_bounds(requested)
        lexical = self.store.count_decisions(requested)
        jurisdictions = self.store.jurisdiction_counts(requested)
        if self.opensearch is not None and (lexical == 0 or not jurisdictions):
            stats = self.opensearch.stats(requested)
            lexical = stats["count"] or lexical
            jurisdictions = stats["by_jurisdiction"] or jurisdictions
            if stats.get("min_date"):
                dmin = _parse_date_only(stats["min_date"]) or dmin
            if stats.get("max_date"):
                dmax = _parse_date_only(stats["max_date"]) or dmax
        if isinstance(counts, dict) and counts.get("by_jurisdiction") and not jurisdictions:
            jurisdictions = {key: int(value) for key, value in counts["by_jurisdiction"].items()}
        if isinstance(counts, dict) and counts.get("accepted") and lexical == 0:
            lexical = int(counts["accepted"])
        capabilities.extend(["search_decisions_lexical", "get_decision", "get_case_decisions"])
        notes.extend(json.loads(generation["notes_json"]))
        return CorpusInfo(
            request_id=_request_id(),
            tool_schema_version=TOOL_SCHEMA_VERSION,
            active_generation=active,
            requested_generation=requested,
            ready=active == requested,
            status="ready" if active == requested else "staging",
            sources=source_manifest.get("sources", []),
            counts=counts if isinstance(counts, dict) else {"accepted": lexical},
            jurisdictions=jurisdictions,
            min_observed_decision_date=dmin,
            max_observed_decision_date=dmax,
            year_min=dmin.year if dmin else None,
            year_max=dmax.year if dmax else None,
            ingested_at=_parse_dt(generation.get("ingested_at")),
            lexical_coverage=lexical,
            semantic_coverage=0,
            semantic_complete=False,
            reranker_ready=False,
            embedding_spec=None,
            capabilities=capabilities,
            notes=notes,
        )

    def _retrieval_version(self) -> str:
        if self.opensearch is not None:
            return "lexical-os-uk-v1"
        return "lexical-fts5-v1"

    def _lexical_search(
        self,
        generation: str,
        query: str,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[tuple[DecisionRecord, float]]:
        if self.opensearch is None:
            return self.store.search(generation, query, filters=filters, limit=limit)
        return self.opensearch.search_records(generation, query, filters=filters, limit=limit)

    def _get_record(self, generation: str, doc_id: str) -> DecisionRecord | None:
        if self.opensearch is not None:
            record = self.opensearch.get(generation, doc_id)
            if record is not None:
                return record
        return self.store.get_decision(generation, doc_id)

    def _metadata(self, record: DecisionRecord) -> DecisionMetadata:
        return DecisionMetadata(
            doc_id=record.doc_id,
            document_version=record.document_version,
            case_number=record.case_number,
            case_number_raw=record.case_number_raw,
            court_code=record.court_code,
            court_label=self.dictionaries.label(FilterField.COURT, record.court_code),
            jurisdiction=record.jurisdiction,
            judgment_code=record.judgment_code,
            judgment_label=self.dictionaries.label(FilterField.JUDGMENT_TYPE, record.judgment_code),
            category_code=record.category_code,
            category_label=self.dictionaries.label(FilterField.CATEGORY, record.category_code),
            decision_date=record.decision_date,
            judge=record.judge_raw,
            registry_url=record.registry_url,
            source_refs=record.source_refs,
        )


def _request_id() -> str:
    return uuid.uuid4().hex


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_date_only(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _filter_payload(filters: SearchFilters, required_phrases: list[str] | None = None) -> dict[str, Any]:
    return {
        "jurisdictions": [item.value for item in filters.jurisdictions] if filters.jurisdictions else None,
        "court_codes": filters.court_codes,
        "judgment_codes": filters.judgment_codes,
        "category_codes": filters.category_codes,
        "case_number": filters.case_number,
        "date_from": filters.date_from.isoformat() if filters.date_from else None,
        "date_to": filters.date_to.isoformat() if filters.date_to else None,
        "required_phrases": required_phrases or None,
    }


def _passes_filters(record: DecisionRecord, filters: SearchFilters) -> bool:
    if filters.jurisdictions and record.jurisdiction not in filters.jurisdictions:
        return False
    if filters.court_codes and record.court_code not in filters.court_codes:
        return False
    if filters.judgment_codes and record.judgment_code not in filters.judgment_codes:
        return False
    if filters.category_codes and record.category_code not in filters.category_codes:
        return False
    if filters.case_number and record.case_number != filters.case_number:
        return False
    if filters.date_from or filters.date_to:
        if record.decision_date is None:
            return False
        if filters.date_from and record.decision_date < filters.date_from:
            return False
        if filters.date_to and record.decision_date > filters.date_to:
            return False
    return True


def _evidence_for(record: DecisionRecord, query: str, *, max_items: int) -> list[EvidenceItem]:
    excerpts: list[tuple[int, int]] = []
    tokens = [token for token in query.split() if len(token) > 2][:8]
    search_needles = [" ".join(tokens[:6])] if tokens else []
    search_needles.extend(tokens)
    for needle in search_needles:
        found = find_excerpt(record.text, needle)
        if not found:
            continue
        start, end = _widen(record.text, found[0], found[1], 1000)
        if any(not (end <= existing[0] or start >= existing[1]) for existing in excerpts):
            continue
        excerpts.append((start, end))
        if len(excerpts) >= max_items:
            break
    if not excerpts and record.paragraphs:
        first = record.paragraphs[0]
        end = min(first.char_end, first.char_start + 1000)
        excerpts.append((first.char_start, end))
    items: list[EvidenceItem] = []
    for start, end in excerpts:
        quote = record.text[start:end]
        validate_quote(record.text, start, end, quote)
        items.append(
            EvidenceItem(
                citation_id=citation_id(record.doc_id, record.document_version, start, end),
                doc_id=record.doc_id,
                document_version=record.document_version,
                passage_id=f"{record.doc_id}:{record.text_hash}:para-v1:{start}",
                paragraph_ids=paragraphs_for_span(record.paragraphs, start, end),
                char_start=start,
                char_end=end,
                quote=quote,
            )
        )
    return items


def _widen(text: str, start: int, end: int, budget: int) -> tuple[int, int]:
    extra = max(0, budget - (end - start))
    left = extra // 2
    new_start = max(0, start - left)
    new_end = min(len(text), new_start + budget)
    return new_start, new_end


def _fit_budget(response: SearchResponse, budget: int) -> SearchResponse:
    payload = response.model_dump_json()
    if len(payload) <= budget:
        return response
    trimmed = response.model_copy(deep=True)
    for hit in trimmed.results:
        for item in hit.evidence:
            if len(item.quote) > 400:
                item.quote = item.quote[:400]
                item.char_end = item.char_start + len(item.quote)
    trimmed.response_truncated = True
    return trimmed


def _source_status(config: AppConfig) -> list[dict[str, Any]]:
    rows = []
    for source in config.sources:
        data_dir = source.path / "data"
        parquet = list(data_dir.glob("*.parquet")) if data_dir.is_dir() else []
        rows.append(
            {
                "repo": source.repo,
                "path": str(source.path),
                "revision": source.revision,
                "present": data_dir.is_dir(),
                "parquet_files": len(parquet),
            }
        )
    return rows
