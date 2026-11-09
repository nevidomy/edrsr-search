#!/usr/bin/env python3
"""Summarize embedding output under one spec dir. Stdlib only, so it runs anywhere:

    python3 progress_probe.py ~/data/edrsr/embeddings/bge-m3-dense-v1 [LOGS_DIR]
    ssh box python3 - /workspace/edrsr/embeddings/bge-m3-dense-v1 /workspace/edrsr/logs < progress_probe.py

Prints JSON: per section, every finished file (passages, documents, source bytes,
finish time), live worker progress; plus machine state: GPUs, running encoder
processes, load, free disk, and recent error lines from LOGS_DIR. Read-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ERROR_RE = re.compile(r"Error\b|error:|failed|Killed|out of memory|OOM")
# Traceback scaffolding: the useful part is the final exception line, not these.
NOISE_RE = re.compile(r"^\s*(Traceback|File \"|raise |\^|~|During handling|The above exception|return |\w+ = )")
REQUEST_ID_RE = re.compile(r"\s*\(Request ID:[^)]*\)")
EXC_PREFIX_RE = re.compile(r"^[\w.]+(Error|Exception): ")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_MAX_AGE_S = 24 * 3600


def section_summary(section_dir: Path) -> dict:
    done: dict[str, list] = {}
    files_dir = section_dir / "files"
    if files_dir.is_dir():
        for path in files_dir.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if state.get("status") != "done" or not state.get("has_vectors"):
                continue
            if not (section_dir / str(state.get("shard") or "")).is_file():
                continue
            done[str(state.get("source_file") or path.stem + ".parquet")] = [
                int(state.get("passages") or 0),
                int(state.get("documents") or 0),
                int(state.get("source_size") or 0),
                str(state.get("updated_at") or ""),
            ]
    workers = []
    workers_dir = section_dir / "workers"
    if workers_dir.is_dir():
        for path in sorted(workers_dir.glob("*.json")):
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            info["age_s"] = int(time.time() - path.stat().st_mtime)
            workers.append(info)
    return {"done": done, "workers": workers}


def gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi") or ("/usr/lib/wsl/lib/nvidia-smi" if os.path.exists("/usr/lib/wsl/lib/nvidia-smi") else None)
    if not exe:
        return []
    fields = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
    try:
        out = subprocess.run([exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7:
            continue
        def num(value: str) -> float | None:
            try:
                return float(value)
            except ValueError:
                return None
        rows.append({"index": int(parts[0]), "name": parts[1], "util": num(parts[2]), "mem_used": num(parts[3]),
                     "mem_total": num(parts[4]), "temp": num(parts[5]), "power": num(parts[6])})
    return rows


def encoder_processes() -> int:
    count = 0
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        # Real argv elements only, so a shell whose script text mentions them does not count.
        if b"embed-passages" in argv and b"--worker" in argv:
            count += 1
    return count


def log_errors(logs: Path, limit: int = 8) -> tuple[list[dict], float | None]:
    """Recent distinct error messages from *.log under ``logs``: [{log, age_s, msg, count}]."""
    if not logs.is_dir():
        return [], None
    found: dict[tuple[str, str], dict] = {}
    newest = None
    now = time.time()
    for path in logs.glob("*.log"):
        mtime = path.stat().st_mtime
        newest = max(newest or 0, mtime)
        if now - mtime > LOG_MAX_AGE_S:
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 200_000))
            tail = ANSI_RE.sub("", handle.read().decode("utf-8", "replace")).replace("\r", "\n").splitlines()
        for line in tail:
            line = line.strip()
            if not line or NOISE_RE.match(line) or not ERROR_RE.search(line):
                continue
            msg = REQUEST_ID_RE.sub("", line)[:400]
            # "requests.exceptions.HTTPError: X" and "huggingface_hub.errors.HfHubHTTPError: X" are one problem.
            key = (path.name, EXC_PREFIX_RE.sub("", msg))
            entry = found.setdefault(key, {"log": path.name, "age_s": int(now - mtime), "msg": msg, "count": 0})
            entry["count"] += 1
    entries = sorted(found.values(), key=lambda item: item["age_s"])
    return entries[:limit], newest


def inputs(data_root: Path) -> dict[str, int]:
    """Source parquet files present per dataset folder (edrsr-*/data) next to the embeddings."""
    counts = {}
    for folder in sorted(data_root.glob("edrsr-*")):
        data = folder / "data"
        if data.is_dir():
            counts[folder.name] = sum(1 for _ in data.glob("*.parquet"))
    return counts


def input_shards(data_root: Path) -> dict[str, str]:
    """Folders that hold only a deliberate share of files (a SHARD note next to data/)."""
    notes = {}
    for marker in sorted(data_root.glob("edrsr-*/SHARD")):
        try:
            notes[marker.parent.name] = marker.read_text(encoding="utf-8").strip()[:200]
        except OSError:
            continue
    return notes


def main() -> None:
    root = Path(sys.argv[1]).expanduser()
    logs = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else None
    sections = {}
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "files").is_dir():
                sections[child.name] = section_summary(child)
    usage = shutil.disk_usage(root if root.exists() else Path("/"))
    errors, newest_log = log_errors(logs) if logs else ([], None)
    print(json.dumps({
        "host": socket.gethostname(),
        "root": str(root),
        "now": time.time(),
        "free_gb": round(usage.free / 1e9, 1),
        "total_gb": round(usage.total / 1e9, 1),
        "load": round(os.getloadavg()[0], 1) if hasattr(os, "getloadavg") else None,
        "cpus": os.cpu_count(),
        "gpus": gpus(),
        "encoders": encoder_processes(),
        "log_errors": errors,
        "inputs": inputs(root.parent.parent),
        "input_shards": input_shards(root.parent.parent),
        "log_age_s": int(time.time() - newest_log) if newest_log else None,
        "sections": sections,
    }))


if __name__ == "__main__":
    os.nice(5) if hasattr(os, "nice") else None
    main()
