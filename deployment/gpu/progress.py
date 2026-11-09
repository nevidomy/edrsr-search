#!/usr/bin/env python3
"""Embedding progress across every machine that holds vectors, plus what is indexed.

    progress.py                         # one snapshot as a table
    progress.py --watch 300             # refresh the table every 5 min
    progress.py --serve 127.0.0.1:8090,<tailnet-ip>:8090 --watch 30
                                        # small web page, regenerated every 2 min

Locations (each optional; unreachable ones are reported, not fatal):
  archive  <storage.archive>/embeddings/<spec>     big disk (label: $ARCHIVE_LABEL, default "archive")
  here     <storage.embeddings>/<spec>             this machine
  gpu      ssh $EXTRA_GPU_SSH (e.g. "gpu-box")     another encoder; label $EXTRA_GPU_NAME, path $EXTRA_GPU_ROOT
  rental   ssh $RENTAL_SSH or data/state/rental-ssh (e.g. "-p 41234 root@1.2.3.4")
Writes data/reports/embedding-progress.json and appends to embedding-progress-history.jsonl.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from court_search.embed.run import EMBED_SPEC_NAME, _source_for_section
from court_search.embed.sections import SECTIONS
from court_search.runtime import load_config

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("progress_probe.py")
REPORTS = ROOT / "data" / "reports"
HISTORY = REPORTS / "embedding-progress-history.jsonl"
ARCHIVE_LABEL = os.environ.get("ARCHIVE_LABEL", "archive")
HOME_LOCATIONS = (ARCHIVE_LABEL, "here")
RATE_WINDOW_S = 3600
HISTORY_KEEP = 6000  # ~2 days at 30 s
SPARK_WINDOW_S = 12 * 3600
SPARK_POINTS = 48


# --- collection -----------------------------------------------------------------

def probe_local(path: Path) -> dict:
    out = subprocess.run([sys.executable, str(PROBE), str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def probe_ssh(ssh_args: list[str], path: str, logs: str | None = None) -> dict:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
           *ssh_args, "python3", "-", path, *([logs] if logs else [])]
    out = subprocess.run(cmd, input=PROBE.read_text(), capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else f"exit {out.returncode}")
    return json.loads(out.stdout)


def rental_ssh() -> list[str] | None:
    value = os.environ.get("RENTAL_SSH")
    if value is None:
        state = ROOT / "data" / "state" / "rental-ssh"
        value = state.read_text().strip() if state.exists() else ""
    return shlex.split(value) if value else None


def indexed_by_section(url: str) -> tuple[dict[str, int], int | None]:
    body = json.dumps({"size": 0, "track_total_hits": True, "aggs": {"s": {"terms": {"field": "jurisdiction", "size": 20}}}}).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}/passages-{EMBED_SPEC_NAME}/_search", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except Exception:
        return {}, None
    buckets = data["aggregations"]["s"]["buckets"]
    return {b["key"]: b["doc_count"] for b in buckets}, data["hits"]["total"]["value"]


def snapshot() -> dict:
    config = load_config()
    spec_home = config.storage.embeddings / EMBED_SPEC_NAME
    spec_archive = config.storage.archive / "embeddings" / EMBED_SPEC_NAME
    targets: list[tuple[str, str, object]] = [
        (ARCHIVE_LABEL, "archive disk", lambda: probe_local(spec_archive)),
        ("here", "this machine", lambda: probe_local(spec_home)),
    ]
    extra = os.environ.get("EXTRA_GPU_SSH", "")
    if extra:
        extra_root = os.environ.get("EXTRA_GPU_ROOT", f"~/data/edrsr/embeddings/{EMBED_SPEC_NAME}")
        targets.append((os.environ.get("EXTRA_GPU_NAME", "gpu"), extra,
                        lambda: probe_ssh(shlex.split(extra), extra_root)))
    rental = rental_ssh()
    if rental:
        rental_root = os.environ.get("RENTAL_ROOT", f"/workspace/edrsr/embeddings/{EMBED_SPEC_NAME}")
        rental_logs = os.environ.get("RENTAL_LOGS", "/workspace/edrsr/logs")
        targets.append(("rental", " ".join(rental), lambda: probe_ssh(rental, rental_root, rental_logs)))
    locations: dict[str, dict] = {}
    machines = []
    for name, address, fn in targets:
        started = time.time()
        try:
            data = fn()
            locations[name] = data
            machines.append({"name": name, "address": address, "ok": True, "probe_s": round(time.time() - started, 1),
                             **{k: data.get(k) for k in ("host", "root", "free_gb", "total_gb", "load", "cpus", "gpus",
                                                         "encoders", "log_errors", "log_age_s", "inputs", "input_shards")}})
        except Exception as exc:  # report and keep going
            machines.append({"name": name, "address": address, "ok": False, "error": str(exc)[:240]})

    indexed, indexed_total = indexed_by_section(config.search.url)
    now = time.time()
    rows = []
    recent_passages = recent_bytes = 0
    remaining_bytes = 0
    for section in SECTIONS:
        sid = section["id"]
        try:
            source = _source_for_section(config, section)
            raw = sorted((source.path / "data").glob("*.parquet"))
        except ValueError:
            raw = []
        total_bytes = sum(path.stat().st_size for path in raw)
        merged: dict[str, list] = {}
        where: dict[str, int] = {}
        home_files: set[str] = set()
        workers = []
        for loc, data in locations.items():
            sec = data["sections"].get(sid)
            if not sec:
                continue
            if sec["done"]:
                where[loc] = len(sec["done"])
            if loc in HOME_LOCATIONS:
                home_files.update(sec["done"])
            merged.update(sec["done"])
            workers += [dict(w, location=loc) for w in sec["workers"] if w.get("age_s", 1e9) < 900]
        passages = sum(v[0] for v in merged.values())
        done_bytes = sum(v[2] for v in merged.values())
        for passages_n, _docs, size, updated in merged.values():
            try:
                finished = datetime.fromisoformat(updated).timestamp()
            except ValueError:
                continue
            if now - finished <= RATE_WINDOW_S:
                recent_passages += passages_n
                recent_bytes += size
        est_total = passages / done_bytes * total_bytes if done_bytes and total_bytes else None
        left = max(total_bytes - done_bytes, 0)
        remaining_bytes += left
        rows.append({
            "section": sid,
            "decisions": section["rows"],
            "files_done": len(merged),
            "files_total": len(raw),
            "pct_bytes": round(100 * done_bytes / total_bytes, 1) if total_bytes else 0.0,
            "passages": passages,
            "passages_est_total": int(est_total) if est_total else None,
            "estimate_from": "section" if est_total else None,
            "indexed": indexed.get(sid, 0),
            "home_files": len(home_files),
            "where": where,
            "workers": workers,
            "remaining_bytes": left,
            "done_bytes": done_bytes,
            "total_bytes": total_bytes,
        })
    # Sections not started yet: assume the corpus-wide passages-per-byte (rough, marked "~").
    seen_bytes = sum(r["done_bytes"] for r in rows)
    if seen_bytes:
        per_byte = sum(r["passages"] for r in rows) / seen_bytes
        for r in rows:
            if r["passages_est_total"] is None and r["total_bytes"]:
                r["passages_est_total"] = int(per_byte * r["total_bytes"])
                r["estimate_from"] = "corpus average"
    rate_b = recent_bytes / RATE_WINDOW_S
    totals = {section["folder"]: (section["id"], r["files_total"]) for section, r in zip(SECTIONS, rows)}
    for m in machines:
        arriving, shares = [], []
        for folder, count in (m.get("inputs") or {}).items():
            sid, total = totals.get(folder, (folder, 0))
            if folder in (m.get("input_shards") or {}):
                shares.append({"section": sid, "have": count, "total": total, "note": m["input_shards"][folder]})
            elif total and count < total:
                arriving.append({"section": sid, "have": count, "total": total})
        m["inputs_arriving"] = arriving
        m["inputs_shares"] = shares
    # F: and "here" are the same computer: one machine row, a disk entry per location.
    merged_machines: dict[str, dict] = {}
    for m in machines:
        key = m.get("host") or m["name"]
        disk = {"name": m["name"], "free_gb": m.get("free_gb"), "total_gb": m.get("total_gb")}
        if key in merged_machines and m["ok"]:
            merged_machines[key]["name"] += f" + {m['name']}"
            merged_machines[key]["disks"].append(disk)
            merged_machines[key]["log_errors"] = (merged_machines[key].get("log_errors") or []) + (m.get("log_errors") or [])
            merged_machines[key]["inputs_arriving"] = merged_machines[key].get("inputs_arriving") or m.get("inputs_arriving")
            merged_machines[key]["inputs_shares"] = merged_machines[key].get("inputs_shares") or m.get("inputs_shares")
            if m.get("log_age_s") is not None:
                merged_machines[key]["log_age_s"] = m["log_age_s"]
        else:
            merged_machines[key] = dict(m, disks=[disk] if m["ok"] else [])
    machines = list(merged_machines.values())
    return {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ts": now,
        "spec": EMBED_SPEC_NAME,
        "sections": rows,
        "indexed_total": indexed_total,
        "rate_passages_per_s": round(recent_passages / RATE_WINDOW_S, 1),
        "eta_s": int(remaining_bytes / rate_b) if rate_b else None,
        "machines": machines,
    }


def record(snap: dict) -> list[dict]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "embedding-progress.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n")
    point = {"ts": snap["ts"], "passages": sum(r["passages"] for r in snap["sections"]),
             "indexed": snap["indexed_total"], "rate": snap["rate_passages_per_s"]}
    history = []
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines():
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
    history = (history + [point])[-HISTORY_KEEP:]
    HISTORY.write_text("".join(json.dumps(p) + "\n" for p in history))
    return history


# --- formatting -----------------------------------------------------------------

def human(n: float | None) -> str:
    if n is None:
        return "-"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def hours(seconds: float | None) -> str:
    if not seconds:
        return "-"
    return f"{seconds / 3600:.1f} h" if seconds < 72 * 3600 else f"{seconds / 86400:.1f} d"


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def render_text(snap: dict) -> str:
    lines = [f"Embedding progress ({snap['spec']})  {snap['at']}", ""]
    header = f"{'section':23s} {'decisions':>9s} {'files':>11s} {'done':>6s} {'passages':>17s} {'indexed':>8s} {'at home':>8s}  where"
    lines += [header, "-" * len(header)]
    tot_p = tot_est = tot_idx = 0
    for r in snap["sections"]:
        est = r["passages_est_total"]
        tot_p += r["passages"]
        tot_est += est or r["passages"]
        tot_idx += r["indexed"]
        passages = human(r["passages"]) + (f" / ~{human(est)}" if est and r["pct_bytes"] < 100 else "")
        where = ", ".join(f"{k} {v}" for k, v in r["where"].items()) or "-"
        if r["workers"]:
            where += f"  [{len(r['workers'])} workers]"
        home = f"{r['home_files']}/{r['files_total']}"
        lines.append(
            f"{r['section']:23s} {human(r['decisions']):>9s} {r['files_done']:>5d}/{r['files_total']:<5d} "
            f"{r['pct_bytes']:>5.1f}% {passages:>17s} {human(r['indexed']):>8s} {home:>8s}  {where}"
        )
    lines.append("-" * len(header))
    pending = any(r["pct_bytes"] < 100 for r in snap["sections"])
    lines.append(f"{'total':23s} {'':>9s} {'':>11s} {'':>6s} "
                 f"{human(tot_p) + (' / ~' + human(tot_est) if pending else ''):>17s} {human(tot_idx):>8s}")
    lines.append("")
    rate = snap["rate_passages_per_s"]
    lines.append(f"rate (last hour): {rate:.0f} passages/s   ETA all encoding: {hours(snap['eta_s'])}" if rate
                 else "rate (last hour): idle")
    for m in snap["machines"]:
        if not m["ok"]:
            lines.append(f"! {m['name']} unreachable: {m['error']}")
            continue
        gpu = ", ".join(f"{g['util']:.0f}%" for g in m["gpus"] if g.get("util") is not None)
        free = ", ".join(f"{d['name']} {d['free_gb']:.0f} GB" for d in m["disks"])
        lines.append(f"  {m['name']:9s} {m['host']:16s} free {free}  load {m['load']}  "
                     f"encoders {m['encoders']}  gpu util [{gpu}]")
        for item in m.get("inputs_arriving") or []:
            lines.append(f"    inputs arriving: {item['section']} {item['have']}/{item['total']}")
        for err in m.get("log_errors") or []:
            lines.append(f"    ! {ago(err['age_s'])} {err['log']}: {err['msg'][:160]}")
    return "\n".join(lines)


CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--border:rgba(11,11,11,.10);--accent:#2a78d6;--track:#cde2fb;
--good:#0ca30c;--warning:#fab219;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--page:#0d0d0d;
--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--border:rgba(255,255,255,.10);
--accent:#3987e5;--track:#0d366b}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--grid:#2c2c2a;--border:rgba(255,255,255,.10);--accent:#3987e5;--track:#0d366b}
*{box-sizing:border-box}body{margin:0}
.viz-root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--page);color:var(--text-primary);
min-height:100vh;padding:24px}
.wrap{max-width:1280px;margin:0 auto}
h1{font-size:20px;font-weight:600;margin:0 0 4px}h2{font-size:15px;font-weight:600;margin:28px 0 10px}
.sub{color:var(--text-secondary);font-size:13px;margin:0 0 20px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.tile .label{font-size:13px;color:var(--text-secondary)}.tile .value{font-size:26px;font-weight:600;margin-top:4px}
.tile .note{font-size:12px;color:var(--muted);margin-top:2px}
.tile svg{display:block;margin-top:8px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:500;color:var(--text-secondary);padding:9px 10px;border-bottom:1px solid var(--grid);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
td.where{min-width:150px}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.meter{display:flex;align-items:center;gap:6px;min-width:110px}
.meter .track{flex:1;height:8px;border-radius:4px;background:var(--track);overflow:hidden}
.meter .fill{height:100%;border-radius:4px;background:var(--accent)}
.meter .pct{font-variant-numeric:tabular-nums;color:var(--text-secondary);font-size:12px;width:46px;text-align:right}
.muted{color:var(--muted)}.small{font-size:12px}
.status{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.dot.good{background:var(--good)}.dot.warning{background:var(--warning)}.dot.critical{background:var(--critical)}
.err{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--text-secondary);margin:4px 0;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot.idle{background:transparent;border:2px solid var(--muted);width:10px;height:10px}
.gpus{display:flex;gap:3px;align-items:flex-end;height:22px;margin-top:4px}
.gpus .g{width:8px;height:22px;border-radius:2px;background:var(--track);position:relative;overflow:hidden}
.gpus .g i{position:absolute;left:0;right:0;bottom:0;background:var(--accent);border-radius:2px}
details{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-top:8px}
details summary{cursor:pointer;font-size:13px}
details .row{display:grid;grid-template-columns:110px 150px 1fr;gap:10px;font-size:12px;padding:4px 0;
border-top:1px solid var(--grid)}
details .row:first-of-type{border-top:none}
details .msg{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-word;color:var(--text-secondary)}
"""


def meter(pct: float, label: str) -> str:
    pct = max(0.0, min(100.0, pct))
    return (f'<div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pct:.1f}" '
            f'aria-label="{html.escape(label)}" title="{html.escape(label)}"><div class="track">'
            f'<div class="fill" style="width:{pct:.2f}%"></div></div><span class="pct">{pct:.1f}%</span></div>')


def status(kind: str, text: str) -> str:
    icon = {"good": "✓ ", "warning": "⚠ ", "critical": "✕ ", "idle": ""}[kind]
    return f'<span class="status"><span class="dot {kind}" aria-hidden="true"></span>{icon}{html.escape(text)}</span>'


def sparkline(history: list[dict], key: str) -> str:
    cutoff = time.time() - SPARK_WINDOW_S
    points = [p for p in history if p.get(key) is not None and p["ts"] >= cutoff]
    if len(points) > SPARK_POINTS:  # sample evenly, always keep the latest point
        step = len(points) / SPARK_POINTS
        points = [points[int(i * step)] for i in range(SPARK_POINTS - 1)] + [points[-1]]
    if len(points) < 2:
        return ""
    w, h = 150, 32
    values = [p[key] for p in points]
    top = max(values) or 1
    xs = [i * (w - 4) / (len(values) - 1) + 2 for i in range(len(values))]
    ys = [h - 3 - (v / top) * (h - 6) for v in values]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    labels = "; ".join(f"{datetime.fromtimestamp(p['ts']).strftime('%H:%M')} {human(p[key])}" for p in points[-6:])
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="recent {key}: {labels}">'
            f'<title>{html.escape(labels)}</title>'
            f'<path d="{path}" fill="none" stroke="var(--muted)" stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4" fill="var(--accent)" stroke="var(--surface-1)" stroke-width="2"/></svg>')


def render_html(snap: dict, history: list[dict], refresh_s: int) -> str:
    e = html.escape
    rows = snap["sections"]
    tot_p = sum(r["passages"] for r in rows)
    tot_est = sum(r["passages_est_total"] or r["passages"] for r in rows)
    tot_idx = snap["indexed_total"] or sum(r["indexed"] for r in rows)
    workers = sum(len(r["workers"]) for r in rows)
    rate = snap["rate_passages_per_s"]
    kpis = [
        ("Passages encoded", human(tot_p), f"of ~{human(tot_est)} estimated ({100 * tot_p / tot_est:.0f}%)" if tot_est else "", sparkline(history, "passages")),
        ("Passages indexed", human(tot_idx), f"{100 * tot_idx / tot_est:.0f}% of estimate · searchable now" if tot_est else "", sparkline(history, "indexed")),
        ("Encoding rate", f"{rate:,.0f}/s" if rate else "idle", "passages per second, last hour", sparkline(history, "rate")),
        ("Encoding ETA", hours(snap["eta_s"]), f"{workers} active workers" if workers else "no active workers", ""),
    ]
    tiles = "".join(
        f'<div class="tile"><div class="label">{e(label)}</div><div class="value">{e(value)}</div>'
        f'<div class="note">{e(note)}</div>{spark}</div>' for label, value, note, spark in kpis
    )
    body_rows = []
    for r in rows:
        est = r["passages_est_total"]
        enc_pct = r["pct_bytes"]
        idx_pct = 100 * r["indexed"] / (est or r["passages"] or 1) if (est or r["passages"]) else 0
        home_pct = 100 * r["home_files"] / r["files_total"] if r["files_total"] else 0
        where = ", ".join(f"{k}: {v}" for k, v in r["where"].items()) or "-"
        if r["workers"]:
            rate_w = sum(w.get("passages_per_s") or 0 for w in r["workers"])
            where += f" · {len(r['workers'])} workers, {rate_w:,.0f}/s"
        est_note = (f'<div class="small muted">~{human(est)} est. ({e(r["estimate_from"])})</div>'
                    if est and enc_pct < 100 else "")
        body_rows.append(
            f"<tr><td>{e(r['section'].replace('_', ' '))}</td>"
            f"<td class='num'>{human(r['decisions'])}</td>"
            f"<td class='num'>{r['files_done']}/{r['files_total']}</td>"
            f"<td>{meter(enc_pct, f'{r['section']} encoded {enc_pct:.1f}% of source bytes')}</td>"
            f"<td class='num'>{human(r['passages'])}{est_note}</td>"
            f"<td>{meter(home_pct, f'{r['section']} vectors at home {home_pct:.1f}%')}</td>"
            f"<td>{meter(idx_pct, f'{r['section']} indexed {idx_pct:.1f}%')}</td>"
            f"<td class='num'>{human(r['indexed'])}</td>"
            f"<td class='small where'>{e(where)}</td></tr>"
        )
    machine_rows = []
    error_rows = []
    for m in snap["machines"]:
        if not m["ok"]:
            machine_rows.append(f"<tr><td>{e(m['name'])}</td><td>{status('critical', 'unreachable')}</td>"
                                f"<td colspan='5' class='small'>{e(m['error'])}</td></tr>")
            continue
        errs = m.get("log_errors") or []
        newest_err = min((x["age_s"] for x in errs), default=None)
        arriving = m.get("inputs_arriving") or []
        if m["encoders"]:
            quiet = m.get("log_age_s") is not None and m["log_age_s"] > 1800
            state = status("warning", "logs quiet") if quiet else status("good", f"encoding · {m['encoders']} workers")
        elif arriving:
            state = status("idle", "receiving data")
        elif newest_err is not None and newest_err < 1800:
            state = status("warning", "needs attention")
        else:
            state = status("idle", "idle")
        if errs:
            state += f"<div class='small muted'>{len(errs)} log errors, newest {ago(newest_err)}</div>"
        gpus = m["gpus"] or []
        if gpus:
            names = sorted({g["name"].replace("NVIDIA GeForce ", "") for g in gpus})
            util = sum(g.get("util") or 0 for g in gpus) / len(gpus)
            mem_used = sum(g.get("mem_used") or 0 for g in gpus) / 1024
            mem_total = sum(g.get("mem_total") or 0 for g in gpus) / 1024
            temp = max(g.get("temp") or 0 for g in gpus)
            power = sum(g.get("power") or 0 for g in gpus)
            strip = "".join(
                f"<span class='g' title='GPU {g['index']}: {g.get('util') or 0:.0f}% · "
                f"{(g.get('mem_used') or 0) / 1024:.1f}/{(g.get('mem_total') or 0) / 1024:.0f} GB · "
                f"{g.get('temp') or 0:.0f}°C · {g.get('power') or 0:.0f} W'><i style='height:{min(100, g.get('util') or 0):.0f}%'></i></span>"
                for g in gpus)
            gpu_cell = (f"{len(gpus)}× {e(' / '.join(names))} · {util:.0f}% avg · {mem_used:.0f}/{mem_total:.0f} GB · "
                        f"{temp:.0f}°C max · {power:.0f} W<div class='gpus' role='img' "
                        f"aria-label='per-GPU utilisation'>{strip}</div>")
        else:
            gpu_cell = "<span class='muted'>none</span>"
        inputs = " · ".join(f"{x['section']} {x['have']}/{x['total']}" for x in arriving)
        shares = " · ".join(f"{x['section']}: {x['have']} of {x['total']} (its share)" for x in (m.get("inputs_shares") or []))
        machine_rows.append(
            f"<tr><td>{e(m['name'])}<div class='small muted'>{e(m['host'] or '')}</div></td><td>{state}</td>"
            f"<td class='small'>{gpu_cell}</td><td class='num'>{m['encoders']}</td>"
            f"<td class='num'>{m['load']} / {m['cpus']}</td>"
            f"<td class='num'>{'<br>'.join(f'{e(d['name'])} {d['free_gb']:.0f} / {d['total_gb']:.0f} GB' for d in m['disks'])}</td>"
            f"<td class='small'>{('arriving: ' + e(inputs) + '<br>') if inputs else ''}{e(shares)}"
            f"{'' if (inputs or shares) else '<span class=muted>complete</span>'}</td></tr>"
        )
        for x in errs:
            times = f" (×{x['count']})" if x.get("count", 1) > 1 else ""
            error_rows.append((x["age_s"], f"<div class='row'><span>{ago(x['age_s'])}</span>"
                                           f"<span>{e(m['name'])} · {e(x['log'])}</span>"
                                           f"<span class='msg'>{e(x['msg'])}{times}</span></div>"))
    error_rows.sort(key=lambda item: item[0])
    errors_block = (
        f"<details><summary>Recent log errors ({len(error_rows)}, newest {ago(error_rows[0][0])}) — "
        f"from logs written in the last 24 h; old entries stay until the log rotates</summary>"
        f"{''.join(row for _, row in error_rows)}</details>" if error_rows else
        "<p class='small muted'>No errors in recent logs.</p>"
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="{refresh_s}">
<title>Embedding progress · {human(tot_p)} / ~{human(tot_est)}</title><style>{CSS}</style>
<script>const t=new URLSearchParams(location.search).get("theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t;</script></head>
<body class="viz-root"><div class="wrap">
<h1>Embedding progress</h1>
<p class="sub">{e(snap['spec'])} · updated {e(snap['at'])} · page refreshes every {refresh_s} s</p>
<div class="kpis">{tiles}</div>
<h2>Sections</h2>
<div class="card"><table>
<thead><tr><th>Section</th><th class="num">Decisions</th><th class="num">Files</th><th>Encoded</th>
<th class="num">Passages</th><th>At home</th><th>Indexed</th><th class="num">Indexed passages</th><th>Where the vectors are</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table></div>
<p class="small muted">Encoded = share of source bytes with vectors anywhere. At home = files on the archive disk or this machine.
Indexed = passages in OpenSearch against the estimated total. Estimates for unstarted sections use the corpus average.</p>
<h2>Machines</h2>
<div class="card"><table>
<thead><tr><th>Machine</th><th>State</th><th>GPUs</th><th class="num">Encoders</th>
<th class="num">Load / CPUs</th><th class="num">Free disk</th><th>Source files</th></tr></thead>
<tbody>{''.join(machine_rows)}</tbody></table></div>
{errors_block}
</div></body></html>"""


# --- serving --------------------------------------------------------------------

class Page:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.html = b"<p>collecting first snapshot...</p>"
        self.json = b"{}"

    def set(self, html_text: str, snap: dict) -> None:
        with self.lock:
            self.html = html_text.encode()
            self.json = json.dumps(snap, ensure_ascii=False, indent=2).encode()


def serve(addresses: list[str], page: Page) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            with page.lock:
                body, kind = {"/": (page.html, "text/html; charset=utf-8"),
                              "/progress.json": (page.json, "application/json")}.get(path, (None, None))
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    def bind_forever(address: str) -> None:
        host, port = address.rsplit(":", 1)
        while True:  # the tailnet address may not exist yet at boot; keep trying
            try:
                server = ThreadingHTTPServer((host, int(port)), Handler)
            except OSError as exc:
                print(f"cannot bind {address}: {exc}; retrying in 60 s", file=sys.stderr, flush=True)
                time.sleep(60)
                continue
            print(f"serving http://{address}/", file=sys.stderr, flush=True)
            server.serve_forever()

    for address in addresses:
        threading.Thread(target=bind_forever, args=(address,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="refresh every N seconds")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the table")
    parser.add_argument("--serve", metavar="HOST:PORT[,HOST:PORT]", help="serve a web page instead of printing")
    parser.add_argument("--html", metavar="PATH", help="also write the page to a file")
    args = parser.parse_args()
    page = Page()
    if args.serve:
        serve(args.serve.split(","), page)
        args.watch = args.watch or 120
    refresh = max(15, min(args.watch or 120, 300))
    while True:
        started = time.time()
        try:
            snap = snapshot()
            history = record(snap)
            html_text = render_html(snap, history, refresh)
            page.set(html_text, snap)
            if args.html:
                Path(args.html).write_text(html_text, encoding="utf-8")
            if not args.serve:
                text = json.dumps(snap, ensure_ascii=False, indent=2) if args.json else render_text(snap)
                print(("\033[2J\033[H" if args.watch else "") + text, flush=True)
        except Exception as exc:  # keep the page alive through transient failures
            print(f"{datetime.now().isoformat(timespec='seconds')} snapshot failed: {exc}", file=sys.stderr, flush=True)
            if not (args.watch or args.serve):
                raise
        if not (args.watch or args.serve):
            return
        time.sleep(max(1.0, args.watch - (time.time() - started)))


if __name__ == "__main__":
    main()
