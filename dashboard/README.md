# stresstest dashboard

Team-visible live view of stresstest health and on-chain confirmation stats.

- **URL:** https://storage.cloud.google.com/btq-testnet-stresstest-dashboard/index.html
- **Access:** any `btq.tech` Google Workspace account (sign in required).
- **Refresh:** the updater runs every 5 min and re-uploads `index.html` with data embedded; in-browser, the page reloads itself every 60 s to pick up the new upload.

## Layout

- `index.html` — static dashboard (Chart.js, no build step). Contains a `__DATA__` placeholder inside `<script id="app-data" type="application/json">` that the updater substitutes with the latest payload before upload.
- `update_dashboard.py` — runs on the stresstest VM. Reads `~/stresstest-health.jsonl` plus every matching tx CSV (main at `/home/christam/btq-stresstest/stresstest_txlog.csv` plus any parallel instances at `/tmp/stresstest_w*/stresstest-w*_txlog.csv`), runs an incremental reorg-safe block sweep (re-scans the last 100 blocks from tip each run), captures block weights, renders the HTML from `~/dashboard-template.html`, and uploads both `data.json` and `index.html` to the bucket. Per-tick `attempts_5m` / `success_*` / `failed_5m` / `success_rate_5m` are recomputed across all CSVs, so the dashboard reflects total load across every live stresstest wallet, not just main's.

## Architecture

- GCS bucket: `gs://btq-testnet-stresstest-dashboard` (project `btq-testnet-faucet`, `asia-east1`, uniform bucket-level access).
- IAM: `domain:btq.tech` → `roles/storage.objectViewer`; the stresstest VM's default compute SA (`302969431188-compute@developer.gserviceaccount.com`) → `roles/storage.objectAdmin` scoped to this bucket.
- VM authenticates via the GCE metadata server (no key files). Requires VM access-scope `cloud-platform`.
- Cron on VM: `1-59/5 * * * * /usr/bin/python3 /home/christam/update_dashboard.py >> /home/christam/update_dashboard.log 2>&1`.
- HTML template on VM: `~/dashboard-template.html` (this repo's `dashboard/index.html`, scp'd over).

## Deployment

Both the template and the updater live on the VM. To update either:

```sh
# Template (edit locally, then scp to VM — next cron tick renders and uploads it)
gcloud compute scp dashboard/index.html \
  btq-testnet-stresstest:/home/christam/dashboard-template.html \
  --zone=asia-east1-a --project=btq-testnet-faucet

# Updater (edit locally, then scp to VM — no restart needed; next cron tick picks it up)
gcloud compute scp dashboard/update_dashboard.py \
  btq-testnet-stresstest:/home/christam/update_dashboard.py \
  --zone=asia-east1-a --project=btq-testnet-faucet
```

To force an immediate refresh without waiting for cron:

```sh
gcloud compute ssh btq-testnet-stresstest --zone=asia-east1-a --project=btq-testnet-faucet \
  --command='/usr/bin/python3 /home/christam/update_dashboard.py'
```

## Data contract

`update_dashboard.py` emits two artifacts. They share the same JSON shape.

- `data.json` — raw payload, uploaded directly. Consumable by CLI / other tools.
- `index.html` — the template with `__DATA__` replaced by the JSON payload (forward slashes in `</…>` sequences are escaped so they can't break out of the `<script>` tag).

```json
{
  "generated_at": "ISO timestamp",
  "ticks": [ /* flattened rows from stresstest-health.jsonl, last 2016 (7 days of 5-min ticks).
                per-tick fields: ts, height, peers, mempool_size, mempool_bytes,
                utxos_1conf, last_block_age_sec, attempts_5m, success_dil_5m,
                success_std_5m, failed_5m, success_rate_5m,
                err_map_at, err_insufficient_funds, err_no_confirmed_utxos, err_rpc_other,
                alerts */ ],
  "sweep": {
    "total":         <int>,   // success txids seen across all stresstest CSVs
    "confirmed":     <int>,
    "pending":       <int>,   // in mempool
    "missing":       <int>,   // neither in chain nor mempool
    "latency_p50":   <sec>,
    "latency_p90":   <sec>,
    "latency_p99":   <sec>,
    "latency_max":   <sec>,
    "latency_hist":  { "labels": [...], "counts": [...] },  // aggregate over retained confirmed submissions
    "buckets":       [ /* per-5-min-submission-window: {ts, conf, pend, miss, lat_hist[]} */ ],
    "bucket_bins":   [ "<30s", "30-60s", "1-2m", ... ],     // labels for per-bucket lat_hist
    "block_weights": [ /* per mined block: {h: height, t: blocktime, w: weight_wu} */ ],
    "pending_max_age_sec": <sec>,
    "missing_max_age_sec": <sec>,
    "chain_tip":     <height>
  }
}
```

The sweep caches `{txid: [height, blocktime]}` plus `{height: [blocktime, weight]}` in `~/.cache/btq_dashboard_sweep.json` on the VM. Each run re-scans the last 100 blocks from tip (reorg safety) and anything beyond `last_scanned`; older entries beyond the 7-day retention window are evicted. Only txids matching our stresstest submissions are cached, so the cache stays small.
