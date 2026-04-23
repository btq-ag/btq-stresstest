# stresstest dashboard

Team-visible live view of stresstest health and on-chain confirmation stats.

- **URL:** https://storage.cloud.google.com/btq-testnet-stresstest-dashboard/index.html
- **Access:** any `btq.tech` Google Workspace account (sign in required).
- **Refresh:** page polls `data.json` every 60 s; updater runs every 5 min.

## Layout

- `index.html` — static dashboard (Chart.js, no build step). Served from the bucket.
- `update_dashboard.py` — runs on the stresstest VM, reads `~/stresstest-health.jsonl` and every matching tx CSV (main at `/home/christam/btq-stresstest/stresstest_txlog.csv` plus any parallel instances at `/tmp/stresstest_w*/stresstest-w*_txlog.csv`), performs an incremental block sweep against the chain, writes `data.json`, uploads to the bucket. Per-tick `attempts_5m` / `success_*` / `failed_5m` / `success_rate_5m` are recomputed across all CSVs, so the dashboard reflects total load across every live stresstest wallet, not just main's.

## Architecture

- GCS bucket: `gs://btq-testnet-stresstest-dashboard` (project `btq-testnet-faucet`, `asia-east1`, uniform bucket-level access).
- IAM: `domain:btq.tech` → `roles/storage.objectViewer`; default compute SA of `btq-testnet-stresstest` → `roles/storage.objectAdmin` scoped to this bucket.
- VM authenticates via metadata server (no key files). Requires VM access-scope `cloud-platform`.
- Cron on VM: `1-59/5 * * * * /usr/bin/python3 /home/christam/update_dashboard.py >> /home/christam/update_dashboard.log 2>&1`.

## Deployment

Both files live on the VM at `/home/christam/update_dashboard.py` and in the bucket at `gs://btq-testnet-stresstest-dashboard/index.html`. To update either:

```sh
# HTML (edit locally, then push to bucket)
gcloud storage cp dashboard/index.html \
  gs://btq-testnet-stresstest-dashboard/index.html \
  --cache-control=no-cache,max-age=30

# Updater (edit locally, then scp to VM — no restart needed; next cron tick picks it up)
gcloud compute scp dashboard/update_dashboard.py \
  btq-testnet-stresstest:/home/christam/update_dashboard.py \
  --zone=asia-east1-a --project=btq-testnet-faucet
```

## Data contract (`data.json`)

```json
{
  "generated_at": "ISO timestamp",
  "ticks": [ /* flattened rows from stresstest-health.jsonl, last 288 */ ],
  "sweep": {
    "total":     <int>,   // success txids seen in CSV
    "confirmed": <int>,
    "pending":   <int>,   // in mempool
    "missing":   <int>,   // neither in chain nor mempool
    "latency_p50": <sec>,
    "latency_p90": <sec>,
    "latency_p99": <sec>,
    "latency_max": <sec>,
    "latency_hist": { "labels": [...], "counts": [...] },
    "chain_tip": <height>
  }
}
```

The sweep caches `{txid: [height, blocktime]}` in `~/.cache/btq_dashboard_sweep.json` on the VM and only scans blocks since the last observed tip, so each run is cheap.
