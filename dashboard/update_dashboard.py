#!/usr/bin/env python3
"""Generate dashboard data + HTML and upload to the btq.tech GCS bucket.

Reads ~/stresstest-health.jsonl and the stresstest CSV, runs an incremental
tx sweep against the chain, then uploads two files:
  - data.json        — raw payload (consumable by CLI / other tools)
  - index.html       — template from ~/dashboard-template.html with the
                       payload injected into a <script id="app-data"> tag
                       (the browser can't fetch data.json because GCS's
                       auth endpoint 302's to accounts.google.com; embedding
                       into the HTML lets the page load it on navigation).
"""
from __future__ import annotations

import csv
import datetime
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

HEALTH_JSONL = Path.home() / "stresstest-health.jsonl"
# All stresstest CSVs we aggregate over: main + any parallel instances.
# Each parallel wallet runs in /tmp/stresstest_wN/ and writes its own
# stresstest-wN_txlog.csv. Globbing picks up however many are live.
TX_CSV_GLOBS = [
    "/home/christam/btq-stresstest/stresstest_txlog.csv",
    "/tmp/stresstest_w*/stresstest-w*_txlog.csv",
]
CACHE = Path.home() / ".cache" / "btq_dashboard_sweep.json"
TEMPLATE = Path.home() / "dashboard-template.html"
OUT_JSON = Path("/tmp/btq_dashboard.data.json")
OUT_HTML = Path("/tmp/btq_dashboard.index.html")
BUCKET = "gs://btq-testnet-stresstest-dashboard"
TICK_WINDOW = 2016  # 7d of 5-min ticks
REORG_WINDOW = 100  # blocks near tip re-scanned every run for main-chain safety

BUCKET_SEC = 300  # 5-min sweep buckets, keyed by floor(ts, 300)
LAT_BINS = [
    (0, 30), (30, 60), (60, 120), (120, 300), (300, 600),
    (600, 1800), (1800, 3600), (3600, 10800), (10800, float("inf")),
]
LAT_BIN_LABELS = ["<30s", "30-60s", "1-2m", "2-5m", "5-10m",
                  "10-30m", "30-60m", "1-3h", ">3h"]

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
            "err_insufficient_funds": errs.get("insufficient_funds", 0),
            "err_no_confirmed_utxos": errs.get("no_confirmed_utxos", 0),
            "err_rpc_other": errs.get("rpc_error_other", 0),
            "alerts": r.get("alerts", []) or [],
        })

    # Override attempts / success / failed / success_rate with cross-CSV totals
    # so the dashboard reflects ALL stresstest wallets' activity, not just the
    # main one the health script tracks. Bucket each CSV row by the 5-min
    # boundary (floor to 300s) and map that to the matching tick.
    bucket_to_idx: dict[int, int] = {}
    for i, t in enumerate(flat):
        try:
            ts = datetime.datetime.fromisoformat(str(t["ts"]).replace("Z", "+00:00"))
        except Exception:
            continue
        bucket_to_idx[int(ts.timestamp()) // 300 * 300] = i

    if bucket_to_idx:
        agg = {i: {"att": 0, "dil": 0, "std": 0, "fail": 0} for i in bucket_to_idx.values()}
        for row in _iter_csv_rows():
            ts_s = row.get("timestamp") or ""
            try:
                ts = datetime.datetime.fromisoformat(ts_s)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            bucket = int(ts.timestamp()) // 300 * 300
            idx = bucket_to_idx.get(bucket)
            if idx is None:
                continue
            status = row.get("status")
            atype = row.get("address_type", "") or ""
            if status == "success":
                agg[idx]["att"] += 1
                if atype == "dilithium":
                    agg[idx]["dil"] += 1
                else:
                    agg[idx]["std"] += 1
            elif status == "failed":
                agg[idx]["att"] += 1
                agg[idx]["fail"] += 1

        for idx, c in agg.items():
            t = flat[idx]
            t["attempts_5m"] = c["att"]
            t["success_dil_5m"] = c["dil"]
            t["success_std_5m"] = c["std"]
            t["failed_5m"] = c["fail"]
            ok = c["dil"] + c["std"]
            t["success_rate_5m"] = round(ok / c["att"], 3) if c["att"] > 0 else None

    return flat


def _csv_paths() -> list[str]:
    """Expand TX_CSV_GLOBS to actual CSV files that exist, deduped."""
    seen: list[str] = []
    for pattern in TX_CSV_GLOBS:
        for p in glob.glob(pattern):
            if p not in seen and Path(p).is_file():
                seen.append(p)
    return seen


def _iter_csv_rows():
    """Yield every row across every known stresstest CSV (main + parallels)."""
    for path in _csv_paths():
        with open(path) as f:
            for row in csv.DictReader(f):
                yield row


def load_submissions() -> dict[str, datetime.datetime]:
    """Merge success txids -> submission timestamp across all stresstest CSVs."""
    out: dict[str, datetime.datetime] = {}
    for row in _iter_csv_rows():
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
    blocks: dict[str, list] = cache.get("blocks", {})        # str(height) -> [blocktime, weight]
    last_scanned = int(cache.get("last_scanned", 0) or 0)

    tip_s = cli("getblockcount")
    if not tip_s:
        return {}
    tip = int(tip_s)

    # Scan strategy:
    # - First run (empty cache): go back ~3000 blocks to cover earliest submission.
    # - Subsequent runs: re-scan the last REORG_WINDOW blocks from tip so any reorg
    #   near the tip invalidates the stale block's entries and picks up the new
    #   main-chain block's tx list. Also covers any heights past last_scanned.
    # Before scanning, evict any cache entries at heights we're about to rescan
    # so orphaned-block txs don't linger.
    if last_scanned == 0:
        start = max(tip - 3000, 1)
    else:
        start = max(min(last_scanned, tip - REORG_WINDOW), 1)

    confirmed = {k: v for k, v in confirmed.items() if v[0] < start}
    blocks = {k: v for k, v in blocks.items() if int(k) < start}

    # Only cache txids we actually care about (our stresstest submissions).
    # Shrinks the cache dramatically vs. caching every tx in every block.
    submission_set = set(submissions.keys())

    for h in range(start, tip + 1):
        bh = cli("getblockhash", str(h))
        if not bh:
            continue
        blk = jload(cli("getblock", bh, "1"))
        if not blk:
            continue
        btime = blk.get("time")
        weight = blk.get("weight")
        if weight is None:
            # Fall back to getblockstats for weight if not in getblock response
            stats = jload(cli("getblockstats", bh, '["total_weight"]'))
            if stats:
                weight = stats.get("total_weight")
        blocks[str(h)] = [btime, weight]
        for txid in blk.get("tx", []):
            if txid in submission_set:
                confirmed[txid] = [h, btime]

    # Current mempool
    mempool = set(jload(cli("getrawmempool")) or [])

    # Classify + bucket into 5-min windows keyed by floor(submission_ts, 300)
    conf_count = pend_count = miss_count = 0
    latencies: list[float] = []
    pending_ages: list[float] = []
    missing_ages: list[float] = []
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    buckets: dict[int, dict] = {}

    def bucket_for(sub_epoch: float) -> dict:
        key = int(sub_epoch) // BUCKET_SEC * BUCKET_SEC
        b = buckets.get(key)
        if b is None:
            b = {"ts": key, "conf": 0, "pend": 0, "miss": 0,
                 "lat_hist": [0] * len(LAT_BINS)}
            buckets[key] = b
        return b

    def add_to_hist(hist: list[int], lat: float):
        for i, (lo, hi) in enumerate(LAT_BINS):
            if lo <= lat < hi:
                hist[i] += 1
                return

    for txid, sub_ts in submissions.items():
        sub_epoch = sub_ts.timestamp()
        b = bucket_for(sub_epoch)
        if txid in confirmed:
            conf_count += 1
            b["conf"] += 1
            bt = confirmed[txid][1]
            if bt is not None:
                lat = bt - sub_epoch
                if lat >= 0:
                    latencies.append(lat)
                    add_to_hist(b["lat_hist"], lat)
        elif txid in mempool:
            pend_count += 1
            b["pend"] += 1
            pending_ages.append(now - sub_epoch)
        else:
            miss_count += 1
            b["miss"] += 1
            missing_ages.append(now - sub_epoch)

    # Save cache — retain confirmed entries as long as we might reference them
    # in a horizon selection (7 d, matches TICK_WINDOW). Any entry older than
    # that can't appear in the dashboard's time range and is safe to drop.
    cache_retention = TICK_WINDOW * BUCKET_SEC  # 7d
    cutoff_time = now - cache_retention
    confirmed = {
        k: v for k, v in confirmed.items()
        if v[1] is None or v[1] > cutoff_time
    }
    # Retain block weights the same duration as confirmed txids (7 d)
    blocks = {
        k: v for k, v in blocks.items()
        if v[0] is not None and v[0] > cutoff_time
    }
    CACHE.write_text(json.dumps({
        "confirmed": confirmed,
        "blocks": blocks,
        "last_scanned": tip,
    }))

    # Latency stats + histogram
    def pct(arr: list[float], p: float) -> float | None:
        if not arr:
            return None
        arr_sorted = sorted(arr)
        return arr_sorted[min(int(len(arr_sorted) * p), len(arr_sorted) - 1)]

    # All-time aggregate histogram
    hist_counts = [0] * len(LAT_BINS)
    for lat in latencies:
        add_to_hist(hist_counts, lat)

    # Emit buckets oldest-first, capped to the same 7d horizon as ticks.
    retention = TICK_WINDOW * BUCKET_SEC
    bucket_cutoff = int(now) - retention
    bucket_list = sorted(
        (b for b in buckets.values() if b["ts"] >= bucket_cutoff),
        key=lambda b: b["ts"],
    )

    # Emit block weights oldest-first, same 7d retention. Each entry:
    # {"h": height, "t": blocktime, "w": weight_wu}. Weight may be None on
    # a few blocks if the RPC didn't return it — client treats as missing.
    block_list = sorted(
        (
            {"h": int(k), "t": v[0], "w": v[1]}
            for k, v in blocks.items()
            if v[0] is not None and v[0] >= bucket_cutoff
        ),
        key=lambda b: b["h"],
    )

    return {
        "total": len(submissions),
        "confirmed": conf_count,
        "pending": pend_count,
        "missing": miss_count,
        "latency_p50": pct(latencies, 0.50),
        "latency_p90": pct(latencies, 0.90),
        "latency_p99": pct(latencies, 0.99),
        "latency_max": max(latencies) if latencies else None,
        "latency_hist": {"labels": LAT_BIN_LABELS, "counts": hist_counts},
        "buckets": bucket_list,
        "bucket_bins": LAT_BIN_LABELS,
        "block_weights": block_list,
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
    payload_json = json.dumps(payload, separators=(",", ":"))
    OUT_JSON.write_text(payload_json)

    # Inject payload into the HTML template. The placeholder __DATA__ lives
    # inside <script id="app-data" type="application/json">. JSON can contain
    # the sequence "</script>" in a string and break out of the tag; guard
    # against that by escaping forward slashes in closing tags.
    if not TEMPLATE.exists():
        print(f"template missing: {TEMPLATE}", file=sys.stderr)
        sys.exit(1)
    safe_json = payload_json.replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("__DATA__", safe_json)
    OUT_HTML.write_text(html)

    # Upload both — fail loudly on either
    for src, dst, ctype in [
        (OUT_JSON, "data.json",  "application/json"),
        (OUT_HTML, "index.html", "text/html; charset=utf-8"),
    ]:
        r = subprocess.run(
            [GCLOUD, "storage", "cp",
             "--cache-control=no-cache,max-age=0",
             f"--content-type={ctype}",
             str(src), f"{BUCKET}/{dst}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"upload {dst} failed: {r.stderr}", file=sys.stderr)
            sys.exit(1)
    print(f"ok: {len(ticks)} ticks, {sweep_data.get('total', 0)} txs swept, uploaded")


if __name__ == "__main__":
    main()
