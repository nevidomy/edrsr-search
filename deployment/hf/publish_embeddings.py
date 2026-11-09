#!/usr/bin/env python3
"""Publish passage embeddings to a Hugging Face dataset repo, in resumable batches.

    .venv/bin/python deployment/hf/publish_embeddings.py --repo <user>/<name> [--public]
    .venv/bin/python deployment/hf/publish_embeddings.py --repo <user>/<name> --dry-run   # convert 1 file, no upload

For each shard under <embeddings-root>/<spec>/<section>/shards it writes the published
form to a staging dir on fast local disk (drops our internal file-position columns,
stores `vector` as float32[1024] so pandas/polars/DuckDB/`datasets` read it directly),
uploads a batch while the next one converts, checks the files exist on the Hub, then
deletes the staged copies. Files already on the Hub are skipped, so re-running resumes.
The source shards are only read. Needs `hf auth login` (or HF_TOKEN) with write access.
"""
from __future__ import annotations

import argparse
import json
import queue
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[2]
SECTIONS = ["commercial", "criminal", "administrative_offense", "civil", "administrative"]
DROP = ["source_file", "row_group", "row_offset"]
DIM = 1024
ROW_GROUP = 10_000
SAMPLE_ROWS = 2_000
STATE = ROOT / "data" / "state" / "hf-upload.json"
CARD = Path(__file__).with_name("dataset_card.md")


def convert(src: Path, dst: Path) -> int:
    """Shard -> published parquet. Returns rows; raises if anything looks off."""
    table = pq.read_table(src)
    table = table.drop_columns([c for c in DROP if c in table.column_names])
    raw = table.column("vector").combine_chunks()
    buf = b"".join(raw.to_pylist())
    vectors = np.frombuffer(buf, dtype=np.float16)
    if vectors.size != table.num_rows * DIM:
        raise ValueError(f"{src.name}: {vectors.size} values for {table.num_rows} rows")
    vec = pa.FixedSizeListArray.from_arrays(pa.array(vectors.astype(np.float32)), DIM)
    idx = table.column_names.index("vector")
    table = table.set_column(idx, pa.field("vector", pa.list_(pa.float32(), DIM)), vec)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd", row_group_size=ROW_GROUP)
    if pq.ParquetFile(tmp).metadata.num_rows != table.num_rows:
        raise ValueError(f"{dst.name}: row count changed while writing")
    tmp.rename(dst)
    return table.num_rows


def default_spec_dir() -> Path:
    from court_search.runtime import load_config

    config = load_config()
    archive = config.storage.archive / "embeddings" / "bge-m3-dense-v1"
    return archive if archive.exists() else config.storage.embeddings / "bge-m3-dense-v1"


def section_stats(spec_dir: Path) -> dict[str, dict]:
    stats = {}
    for section in SECTIONS:
        files = list((spec_dir / section / "files").glob("*.json"))
        docs = passages = 0
        for path in files:
            state = json.loads(path.read_text())
            docs += int(state.get("documents") or 0)
            passages += int(state.get("passages") or 0)
        stats[section] = {"files": len(files), "documents": docs, "passages": passages}
    return stats


def render_card(stats: dict[str, dict], repo: str, code_url: str) -> str:
    rows = "\n".join(
        f"| `{s}` | {v['files']:,} | {v['documents']:,} | {v['passages']:,} |" for s, v in stats.items()
    )
    total = sum(v["passages"] for v in stats.values())
    card = CARD.read_text(encoding="utf-8")
    for key, value in {"STATS_TABLE": rows, "TOTAL_PASSAGES": f"{total:,}", "REPO": repo, "CODE_URL": code_url}.items():
        card = card.replace("{{" + key + "}}", value)
    return card


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="<user-or-org>/<dataset-name>")
    parser.add_argument("--root", default=None, help="shard spec dir (default: from the court-search config)")
    parser.add_argument("--staging", default=str(Path.home() / "data" / "hf-staging"))
    parser.add_argument("--sections", nargs="*", default=SECTIONS)
    parser.add_argument("--batch-gb", type=float, default=15.0)
    parser.add_argument("--public", action="store_true", help="create the repo public (default private)")
    parser.add_argument("--dry-run", action="store_true", help="convert one shard and stop")
    parser.add_argument("--code-url", default="https://github.com/nevidomy/edrsr-search")
    args = parser.parse_args()

    spec_dir = Path(args.root) if args.root else default_spec_dir()
    staging = Path(args.staging)
    staging.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        (staging / "README.dry-run.md").write_text(
            render_card(section_stats(spec_dir), args.repo, args.code_url), encoding="utf-8")
        src = sorted((spec_dir / args.sections[0] / "shards").glob("*.parquet"))[0]
        dst = staging / "dry-run" / src.name
        t = time.time()
        rows = convert(src, dst)
        print(f"dry run: {src.name} -> {dst} rows={rows} {src.stat().st_size/1e6:.0f} MB -> "
              f"{dst.stat().st_size/1e6:.0f} MB in {time.time()-t:.1f}s; schema:\n{pq.read_schema(dst)}")
        return

    api = HfApi()
    user = api.whoami()["name"]
    api.create_repo(args.repo, repo_type="dataset", private=not args.public, exist_ok=True)
    remote = set(api.list_repo_files(args.repo, repo_type="dataset"))
    print(f"{datetime.now().isoformat(timespec='seconds')} logged in as {user}; repo {args.repo}; "
          f"{sum(1 for f in remote if f.endswith('.parquet'))} parquet files already there", flush=True)

    # Card + sample subset first, so the repo is browsable from the start.
    stats = section_stats(spec_dir)
    (staging / "README.md").write_text(render_card(stats, args.repo, args.code_url), encoding="utf-8")
    for section in SECTIONS:
        first = sorted((spec_dir / section / "shards").glob("*.parquet"))
        if not first or f"data/sample/{section}.parquet" in remote:
            continue
        sample = staging / "data" / "sample" / f"{section}.parquet"
        convert(first[len(first) // 2], sample)
        t = pq.read_table(sample).slice(0, SAMPLE_ROWS)
        pq.write_table(t, sample, compression="zstd")
    api.upload_folder(repo_id=args.repo, repo_type="dataset", folder_path=str(staging),
                      allow_patterns=["README.md", "data/sample/*.parquet"],
                      commit_message="Dataset card and sample subset")
    shutil.rmtree(staging / "data" / "sample", ignore_errors=True)

    todo = []
    for section in args.sections:
        for src in sorted((spec_dir / section / "shards").glob("*.parquet")):
            rel = f"data/{section}/{src.name}"
            if rel not in remote:
                todo.append((section, src, rel))
    total_bytes = sum(src.stat().st_size for _, src, _ in todo)
    print(f"{len(todo)} files to publish ({total_bytes/1e9:.0f} GB of source shards)", flush=True)

    batches: queue.Queue = queue.Queue(maxsize=2)  # at most ~3 batches on local disk

    def producer() -> None:
        batch, size = [], 0
        for section, src, rel in todo:
            convert(src, staging / rel)
            batch.append(rel)
            size += (staging / rel).stat().st_size
            if size >= args.batch_gb * 1e9:
                batches.put(batch)
                batch, size = [], 0
        if batch:
            batches.put(batch)
        batches.put(None)

    errors: list[BaseException] = []

    def guarded() -> None:
        try:
            producer()
        except BaseException as exc:  # surface conversion failures to the main thread
            errors.append(exc)
            batches.put(None)

    threading.Thread(target=guarded, daemon=True).start()
    started, sent_bytes, sent_files = time.time(), 0, 0
    while True:
        batch = batches.get()
        if batch is None:
            break
        size = sum((staging / rel).stat().st_size for rel in batch)
        sections = sorted({rel.split("/")[1] for rel in batch})
        for attempt in range(1, 6):
            try:
                api.upload_folder(repo_id=args.repo, repo_type="dataset", folder_path=str(staging),
                                  allow_patterns=batch,
                                  commit_message=f"Add {len(batch)} files ({', '.join(sections)})")
                break
            except Exception as exc:  # network hiccups: retry the same batch
                print(f"upload attempt {attempt} failed: {exc!s:.200}; retrying in {30*attempt}s", flush=True)
                time.sleep(30 * attempt)
        else:
            sys.exit("giving up after 5 failed attempts; re-run to resume")
        remote = set(api.list_repo_files(args.repo, repo_type="dataset"))
        missing = [rel for rel in batch if rel not in remote]
        if missing:
            sys.exit(f"{len(missing)} files not visible on the Hub after upload, e.g. {missing[:3]}; re-run to resume")
        for rel in batch:
            (staging / rel).unlink()
        sent_bytes += size
        sent_files += len(batch)
        rate = sent_bytes / max(1.0, time.time() - started)
        left = (len(todo) - sent_files) / max(1, sent_files) * sent_bytes
        state = {"repo": args.repo, "files_done": sent_files, "files_total": len(todo),
                 "gb_done": round(sent_bytes / 1e9, 1), "mb_per_s": round(rate / 1e6, 1),
                 "eta_h": round(left / rate / 3600, 1) if rate else None,
                 "updated_at": datetime.now(UTC).isoformat()}
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        print(f"{datetime.now().isoformat(timespec='seconds')} {sent_files}/{len(todo)} files, "
              f"{sent_bytes/1e9:.0f} GB, {rate/1e6:.1f} MB/s, ETA {state['eta_h']} h", flush=True)
    if errors:
        raise errors[0]
    print("all files published", flush=True)


if __name__ == "__main__":
    main()
