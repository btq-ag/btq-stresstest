# BTQ Testnet Transaction Stress Test

Periodically sends small random transactions to configured peer addresses.
Multiple participants run this on their own nodes to generate testnet
transaction volume.

## Quick Start

```bash
# 1. Make sure btqd is running on testnet
btqd -testnet -daemon

# 2. Copy and edit the config
cp stresstest.conf.json ~/stresstest.conf.json
# Edit cli_path, peer addresses, amounts, etc.

# 3. Generate your addresses to share with other participants
python3 btq-stresstest.py --config ~/stresstest.conf.json --export-addresses

# 4. Paste the output into other participants' peers array,
#    and paste their addresses into yours

# 5. Test with dry-run first
python3 btq-stresstest.py --config ~/stresstest.conf.json --dry-run --once

# 6. Run it
python3 btq-stresstest.py --config ~/stresstest.conf.json --daemon
```

## Usage

```
btq-stresstest.py --config CONFIG [--dry-run] [--daemon] [--once]
                   [--export-addresses] [--verbose]

Options:
  --config CONFIG       Path to JSON config file (required)
  --dry-run             Log what would be sent without actually sending
  --daemon              Run as background daemon with PID file
  --once                Run a single round and exit (for cron)
  --export-addresses    Generate and print addresses for sharing
  --verbose             Print to console in addition to log file
```

## Config Reference

See `stresstest.conf.json` for an example. Key fields:

| Section | Field | Description |
|---------|-------|-------------|
| `node.cli_path` | | Path to `btq-cli` binary |
| `node.network` | | Network flag, e.g. `-testnet` |
| `peers[].name` | | Human-readable peer name |
| `peers[].addresses.standard` | | Bech32 address |
| `peers[].addresses.dilithium` | | Dilithium PQC address |
| `schedule.interval_seconds` | | Seconds between rounds |
| `schedule.txs_per_round` | | Transactions per round |
| `schedule.amount_min` | | Minimum send amount (BTQ) |
| `schedule.amount_max` | | Maximum send amount (BTQ) |
| `schedule.use_dilithium_pct` | | % chance of using dilithium address |
| `schedule.max_spend_per_round` | | Cap total per round |
| `safety.min_balance` | | Skip round if balance below this |
| `safety.max_total_spend` | | Lifetime spend cap, then exit |

## Running with Cron

For periodic execution without a daemon:

```
*/5 * * * * /path/to/btq-stresstest.py --once --config /path/to/stresstest.conf.json
```

## Monitoring

```bash
# Watch the human-readable log
tail -f stresstest.log

# Analyze the CSV transaction log
cat stresstest_txlog.csv
```

## Stopping

```bash
# If running as daemon
kill $(cat stresstest.pid)

# Or Ctrl+C if running in foreground
```

The script finishes the current transaction before exiting cleanly.
