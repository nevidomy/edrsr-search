from __future__ import annotations

from court_search.domain.contracts import (
    CaseDecisionsResponse,
    CorpusInfo,
    FilterLookupResponse,
    ReadResponse,
    SearchResponse,
)


def search_text(result: SearchResponse) -> str:
    lines = [
        f"corpus={result.corpus_generation} mode={result.mode_used} returned={result.returned_count}",
    ]
    if result.degradation_reasons:
        lines.append("degraded: " + ", ".join(result.degradation_reasons))
    for hit in result.results:
        meta = hit.metadata
        lines.append(
            f"{hit.rank}. doc_id={meta.doc_id} case={meta.case_number_raw or meta.case_number} "
            f"date={meta.decision_date} court={meta.court_code}"
        )
        if meta.registry_url:
            lines.append(f"   source: {meta.registry_url}")
        for item in hit.evidence:
            quote = item.quote.replace("\n", " ")
            if len(quote) > 280:
                quote = quote[:277] + "..."
            lines.append(f"   {item.citation_id}")
            lines.append(f"   quote: {quote}")
    if not result.results:
        lines.append("No matching decisions in the installed snapshot.")
    return "\n".join(lines)


def read_text(result: ReadResponse) -> str:
    meta = result.metadata
    lines = [
        f"doc_id={meta.doc_id} version={meta.document_version} case={meta.case_number_raw}",
        f"complete={result.complete} truncated={result.response_truncated}",
    ]
    for paragraph in result.paragraphs:
        text = paragraph.text.replace("\n", " ")
        if len(text) > 500:
            text = text[:497] + "..."
        lines.append(f"[{paragraph.paragraph_id} {paragraph.char_start}-{paragraph.char_end}] {text}")
    return "\n".join(lines)


def case_text(result: CaseDecisionsResponse) -> str:
    lines = [
        f"case={result.case_number} count={result.returned_count} {result.scope_note}",
    ]
    for item in result.results:
        lines.append(f"- {item.doc_id} {item.decision_date} {item.judgment_code} {item.registry_url}")
    return "\n".join(lines)


def filters_text(result: FilterLookupResponse) -> str:
    lines = [f"field={result.field} dictionary={result.dictionary_version}"]
    for item in result.values:
        lines.append(f"- {item.code}: {item.label or '(no label)'}")
    return "\n".join(lines)


def corpus_text(result: CorpusInfo) -> str:
    lines = [
        f"status={result.status} ready={result.ready} generation={result.active_generation}",
        f"lexical={result.lexical_coverage} semantic={result.semantic_coverage} reranker={result.reranker_ready}",
        f"capabilities: {', '.join(result.capabilities)}",
    ]
    lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)
