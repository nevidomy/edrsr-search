from __future__ import annotations

from typing import Any

_encoder: Any = None


def encode_query(text: str, *, max_length: int = 1000) -> list[float]:
    """One BGE-M3 vector. Loads the model on first call; GPU if CUDA is free."""
    import numpy as np

    from court_search.embed.run import _load_encoder

    global _encoder
    if _encoder is None:
        _encoder = _load_encoder(batch_size=8, max_length=max_length, device=None)
    vectors = _encoder.encode([text])
    if not vectors:
        raise RuntimeError("query encoder returned no vector")
    return np.asarray(vectors[0], dtype="float32").tolist()
