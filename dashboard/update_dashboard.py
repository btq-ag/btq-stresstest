#!/usr/bin/env python3
"""Generate dashboard data.json and upload to the public GCS bucket.

Reads ~/stresstest-health.jsonl and the stresstest CSV, runs an incremental
tx sweep against the chain, writes /tmp/btq_dashboard.data.json, uploads to
gs://btq-testnet-stresstest-dashboard/data.json.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HEALTH_JSONL = Path.home() / "stresstest-health.jsonl"
TX_CSV = Path("/home/christam/btq-stresstest/stresstest_txlog.csv")
CACHE = Path.home() / ".cache" / "btq_dashboard_sweep.json"
OUT = Path("/tmp/btq_dashboard.data.json")
BUCKET = "gs://btq-testnet-stresstest-dashboard"
TICK_WINDOW = 288  # 24h of 5-min ticks

CACHE.parent.mkdir(parents=True, exist_ok=True)


BTQ_CLI = "/usr/local/bin/btq-cli"
GCLOUD = "/usr/bin/gcloud"


def cli(*args, wallet=False):
    cmd = [BTQ_CLI]
    cmd += ["-rpcwallet=stresstest"] if wallet else []
    cmd += list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def jload(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def load_ticks(n: int) -> list[dict]:
    if not HEALTH_JSONL.exists():
        return []
    rows: list[dict] = []
    with HEALTH_JSONL.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Flatten some fields for the frontend
    flat = []
    for r in rows[-n:]:
        errs = r.get("errors_5m") or {}
        flat.append({
            "ts": r.get("ts"),
            "height": r.get("height"),
            "peers": r.get("peers"),
            "mempool_size": r.get("mempool_size"),
            "mempool_bytes": r.get("mempool_bytes"),
            "utxos_1conf": r.get("utxos_1conf"),
            "last_block_age_sec": r.get("last_block_age_sec"),
            "attempts_5m": r.get("attempts_5m"),
            "success_dil_5m": r.get("success_dil_5m"),
            "success_std_5m": r.get("success_std_5m"),
            "failed_5m": r.get("failed_5m"),
            "success_rate_5m": r.get("success_rate_5m"),
            "err_map_at": errs.get("map_at", 0),
            "alerts": r.get("alerts", []) or [],
        })
    return flat


def load_submissions() -> dict[str, datetime.datetime]:
    if not TX_CSV.exists():
        return {}
    out: dict[str, datetime.datetime] = {}
    with TX_CSV.open() as f:
        for row in csv.DictReader(f):
            if row.get("status") != "success":
                continue
            txid = row.get("txid") or ""
            if not txid or txid.startswith("dry-run"):
                continue
            ts_s = row.get("timestamp") or ""
            try:
                ts = datetime.datetime.fromisoformat(ts_s)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            out.setdefault(txid, ts)
    return out


def sweep(submissions: dict[str, datetime.datetime]) -> dict:
    cache = jload(CACHE.read_text()) if CACHE.exists() else {}
    confirmed: dict[str, list] = cache.get("confirmed", {})  # txid -> [height, blocktime]
    last_scanned = int(cache.get("last_scanned", 0) or 0)

    tip_s = cli("getblockcount")
    if not tip_s:
        return {}
    tip = int(tip_s)

    # Incremental: scan only blocks since last_scanned. On first run, scan back
    # far enough to cover earliest submission.
    if last_scanned == 0:
        start = max(tip - 3000, 1)
    else:
        start = max(last_scanned - 1, 1)  # overlap 1 for safety

    for h in range(start, tip + 1):
        bh = cli("getblockhash", str(h))
        if not bh:
            continue
        blk = jload(cli("getblock", bh, "1"))
        if not blk:
            continue
        btime = blk.get("time")
        for txid in blk.get("tx", []):
            confirmed[txid] = [h, btime]

    # Current mempool
    mempool = set(jload(cli("getrawmempool")) or [])

    # Classify
    conf_count = pend_count = miss_count = 0
    latencies: list[float] = []
    pending_ages: list[float] = []
    missing_ages: list[float] = []
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    for txid, sub_ts in submissions.items():
        if txid in confirmed:
            conf_count += 1
            bt = confirmed[txid][1]
            if bt is not None:
                lat = bt - sub_ts.timestamp()
                if lat >= 0:
                    latencies.append(lat)
        elif txid in mempool:
            pend_count += 1
            pending_ages.append(now - sub_ts.timestamp())
        else:
            miss_count += 1
            missing_ages.append(now - sub_ts.timestamp())

    # Save cache — but keep it bounded (drop confirmed entries older than 2 days)
    # to prevent unbounded growth.
    cutoff_time = now - 2 * 86400
    confirmed = {
        k: v for k, v in confirmed.items()
        if v[1] is None or v[1] > cutoff_time
    }
    CACHE.write_text(json.dumps({"confirmed": confirmed, "last_scanned": tip}))

    # Latency stats + histogram
    def pct(arr: list[float], p: float) -> float | None:
        if not arr:
            return None
        arr_sorted = sorted(arr)
        return arr_sorted[min(int(len(arr_sorted) * p), len(arr_sorted) - 1)]

    # Bucket latencies in the last 24h into a log-ish histogram
    recent_lats = [l for i, l in enumerate(latencies)
                   if (now - list(submissions.values())[i].timestamp()) < 86400] \
        if len(latencies) == len(submissions) else latencies
    # Simpler: just use all latencies for the histogram
    bins = [
        (0,   30,   "<30s"),
        (30,  60,   "30-60s"),
        (60,  120,  "1-2m"),
        (120, 300,  "2-5m"),
        (300, 600,  "5-10m"),
        (600, 1800, "10-30m"),
        (1800, 3600, "30-60m"),
        (3600, 10800, "1-3h"),
        (10800, float("inf"), ">3h"),
    ]
    hist_labels = [b[2] for b in bins]
    hist_counts = [0] * len(bins)
    for lat in latencies:
        for i, (lo, hi, _) in enumerate(bins):
            if lo <= lat < hi:
                hist_counts[i] += 1
                break

    return {
        "total": len(submissions),
        "confirmed": conf_count,
        "pending": pend_count,
        "missing": miss_count,
        "latency_p50": pct(latencies, 0.50),
        "latency_p90": pct(latencies, 0.90),
        "latency_p99": pct(latencies, 0.99),
        "latency_max": max(latencies) if latencies else None,
        "latency_hist": {"labels": hist_labels, "counts": hist_counts},
        "pending_max_age_sec": max(pending_ages) if pending_ages else None,
        "missing_max_age_sec": max(missing_ages) if missing_ages else None,
        "chain_tip": tip,
    }


def main():
    ticks = load_ticks(TICK_WINDOW)
    submissions = load_submissions()
    sweep_data = sweep(submissions) if submissions else {}

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ticks": ticks,
        "sweep": sweep_data,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))

    # Upload — fail loudly if this doesn't work
    r = subprocess.run(
        [GCLOUD, "storage", "cp", "--cache-control=no-cache,max-age=0",
         str(OUT), f"{BUCKET}/data.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"upload failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"ok: {len(ticks)} ticks, {sweep_data.get('total', 0)} txs swept, uploaded")


if __name__ == "__main__":
    main()
