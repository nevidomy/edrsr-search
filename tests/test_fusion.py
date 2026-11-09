from court_search.retrieval.fusion import order_by_scores, rrf_fuse


def test_rrf_prefers_overlap() -> None:
    fused = rrf_fuse(["a", "b", "c"], ["c", "d", "a"], k=60)
    assert fused[0] == "a" or fused[0] == "c"
    assert "a" in fused[:2] and "c" in fused[:2]


def test_order_by_scores_prefers_highest_and_keeps_ties() -> None:
    assert order_by_scores([0.2, 0.9, 0.9, 0.1]) == [1, 2, 0, 3]


def test_vector_bytes_roundtrip() -> None:
    import numpy as np

    original = np.linspace(-1, 1, 1024, dtype=np.float16)
    restored = np.frombuffer(original.tobytes(), dtype=np.float16).astype(np.float32)
    assert restored.shape == (1024,)
    assert float(np.max(np.abs(restored - original.astype(np.float32)))) < 1e-3
