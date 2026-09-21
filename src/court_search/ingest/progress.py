from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:
        return "?"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


class ProgressLog:
    def __init__(self, paths: list[Path], *, total_rows: int, total_files: int, started_at: str | None = None) -> None:
        self.paths = paths
        self.total_rows = total_rows
        self.total_files = total_files
        self.started_at = started_at or datetime.now(UTC).isoformat()
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        elapsed_s: float,
        processed_rows: int,
        accepted: int,
        errors: int,
        files_done: int,
        current_file: str,
        source: str,
        file_index: int,
        source_files: int,
        chunk: int,
        chunk_gb: float,
        chunks_sealed: int,
        active: str | None,
        finished: bool = False,
    ) -> dict[str, Any]:
        remaining_rows = max(self.total_rows - processed_rows, 0)
        remaining_files = max(self.total_files - files_done, 0)
        rate = processed_rows / elapsed_s if elapsed_s > 0 and processed_rows else 0.0
        eta_s = remaining_rows / rate if rate > 0 else None
        payload = {
            "updated_at": datetime.now(UTC).isoformat(),
            "started_at": self.started_at,
            "finished": finished,
            "source": source,
            "file": current_file,
            "file_in_source": f"{file_index}/{source_files}",
            "files_done": files_done,
            "files_total": self.total_files,
            "files_remaining": remaining_files,
            "chunk": chunk,
            "chunks_sealed": chunks_sealed,
            "chunk_gb": round(chunk_gb, 2),
            "accepted": accepted,
            "errors": errors,
            "processed_rows": processed_rows,
            "rows_total": self.total_rows,
            "rows_remaining": remaining_rows,
            "docs_per_sec": round(rate, 1),
            "elapsed": format_duration(elapsed_s),
            "elapsed_s": int(elapsed_s),
            "eta": format_duration(eta_s),
            "eta_s": int(eta_s) if eta_s is not None else None,
            "active_generation": active,
        }
        line = (
            f"{payload['updated_at']} {source} {current_file} [{payload['file_in_source']}] "
            f"files={files_done}/{self.total_files} chunk={chunk} sealed={chunks_sealed} "
            f"chunk={chunk_gb:.1f}GB accepted={accepted:,} rate={rate:.1f}/s "
            f"elapsed={payload['elapsed']} eta={payload['eta']} "
            f"remain_docs={remaining_rows:,} remain_files={remaining_files} active={active}"
        )
        for path in self.paths:
            if path.suffix == ".json":
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        print(line, flush=True)
        return payload
