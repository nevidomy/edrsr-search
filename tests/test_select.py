from court_search.domain.enums import Jurisdiction
from court_search.ingest.select import PILOT_QUOTA, scale_quotas, stride_indices


def test_pilot_quotas_sum_to_limit() -> None:
    quotas = scale_quotas(200_000, PILOT_QUOTA)
    assert sum(quotas.values()) == 200_000
    assert quotas[Jurisdiction.CIVIL] >= quotas[Jurisdiction.COMMERCIAL]


def test_stride_is_deterministic() -> None:
    first = stride_indices(2500, 62, seed="pilot-v1:file")
    second = stride_indices(2500, 62, seed="pilot-v1:file")
    assert first == second
    assert len(first) == 62
    assert first == sorted(set(first))
