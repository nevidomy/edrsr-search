from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow.parquet as pq

from court_search.domain.enums import Jurisdiction

PILOT_QUOTA: dict[Jurisdiction, int] = {
    Jurisdiction.CIVIL: 80_000,
    Jurisdiction.ADMINISTRATIVE: 40_000,
    Jurisdiction.CRIMINAL: 35_000,
    Jurisdiction.COMMERCIAL: 20_000,
    Jurisdiction.ADMINISTRATIVE_OFFENSE: 25_000,
}

MAX_SCAN_PER_FILE = 2_500


def scale_quotas(limit: int, base: dict[Jurisdiction, int]) -> dict[Jurisdiction, int]:
    total = sum(base.values())
    quotas = {key: max(1, round(value * limit / total)) for key, value in base.items()}
    drift = limit - sum(quotas.values())
    quotas[Jurisdiction.CIVIL] = max(1, quotas[Jurisdiction.CIVIL] + drift)
    return quotas


def file_row_counts(files: list[Path]) -> list[tuple[Path, int]]:
    counts: list[tuple[Path, int]] = []
    for path in files:
        parquet = pq.ParquetFile(path)
        counts.append((path, int(parquet.metadata.num_rows)))
    return counts


def allocate_file_quotas(files: list[tuple[Path, int]], want: int) -> list[tuple[Path, int, int]]:
    nonempty = [(path, rows) for path, rows in files if rows > 0]
    if not nonempty or want <= 0:
        return []
    total_rows = sum(rows for _, rows in nonempty)
    allocated: list[tuple[Path, int, int]] = []
    remaining = want
    for index, (path, rows) in enumerate(nonempty):
        if remaining <= 0:
            allocated.append((path, rows, 0))
            continue
        if index == len(nonempty) - 1:
            quota = min(remaining, rows)
        else:
            quota = min(remaining, rows, max(1, round(want * rows / total_rows)))
        remaining -= quota
        allocated.append((path, rows, quota))
    if remaining > 0:
        path, rows, quota = allocated[0]
        allocated[0] = (path, rows, quota + remaining)
    return allocated


def stride_indices(rows: int, quota: int, *, seed: str) -> list[int]:
    if quota <= 0 or rows <= 0:
        return []
    scan = min(rows, max(quota, MAX_SCAN_PER_FILE))
    take = min(quota, scan)
    if take == 1:
        return [0]
    step = max(1, scan // take)
    offset = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % step
    indices = []
    cursor = offset
    while len(indices) < take and cursor < scan:
        indices.append(cursor)
        cursor += step
    if len(indices) < take:
        extras = [i for i in range(scan) if i not in set(indices)]
        indices.extend(extras[: take - len(indices)])
    return indices[:take]
