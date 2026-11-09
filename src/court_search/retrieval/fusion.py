from __future__ import annotations


def rrf_fuse(*ranked_ids: list[str], k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ids in ranked_ids:
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return [doc_id for doc_id, _score in sorted(scores.items(), key=lambda item: item[1], reverse=True)]


def order_by_scores(scores: list[float]) -> list[int]:
    """Highest score first; original index breaks ties."""
    return sorted(range(len(scores)), key=lambda index: (-scores[index], index))
