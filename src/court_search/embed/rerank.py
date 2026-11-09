from __future__ import annotations

from typing import Any

MODEL_ID = "BAAI/bge-reranker-v2-m3"
MAX_PASSAGE_CHARS = 1200

_reranker: Any = None


def reranker_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def score_pairs(query: str, passages: list[str], *, batch_size: int = 8) -> list[float]:
    """Cross-encoder scores. Higher is more relevant. Loads GPU model on first call."""
    if not passages:
        return []
    model = _load()
    pairs = [(query, _clip(passage)) for passage in passages]
    scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    return [float(score) for score in scores]


def _clip(text: str) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= MAX_PASSAGE_CHARS:
        return compact
    return compact[:MAX_PASSAGE_CHARS]


def _load() -> Any:
    global _reranker
    if _reranker is None:
        import sys

        import torch
        from sentence_transformers import CrossEncoder

        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"loading {MODEL_ID} on {device} fp16", file=sys.stderr)
        kwargs: dict[str, Any] = {"device": device, "trust_remote_code": True, "max_length": 1024}
        try:
            _reranker = CrossEncoder(MODEL_ID, model_kwargs={"torch_dtype": torch.float16}, **kwargs)
        except TypeError:
            _reranker = CrossEncoder(MODEL_ID, **kwargs)
    return _reranker
