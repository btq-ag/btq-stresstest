#!/usr/bin/env python3
# Copyright (c) 2026 The BTQ Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

"""BTQ Testnet Transaction Stress Test

Periodically sends small random transactions to configured peer addresses.
Designed for multiple participants to run simultaneously, sending BTQ to
each other to generate testnet transaction volume.
"""

import argparse
import csv
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path

logger = logging.getLogger("btq-stresstest")

SATOSHI = Decimal("0.00000001")


class RPCError(Exception):
    pass


class StressTest:
    def __init__(self, config, dry_run=False):
        self.config = config
        self.dry_run = dry_run or config.get("dry_run", False)
        self.total_spent = Decimal("0")
        self.round_num = 0
        self.shutdown = False
        self.csv_path = None

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        name = signal.Signals(signum).name
        logger.info("Received %s, shutting down after current round...", name)
        self.shutdown = True

    def btq_cli(self, *args):
        """Call btq-cli with configured options and return stdout."""
        node = self.config["node"]
        cmd = [node["cli_path"]]
        if node.get("network"):
            cmd.append(node["network"])
        if node.get("rpc_wallet"):
            cmd.append(f'-rpcwallet={node["rpc_wallet"]}')
        if node.get("datadir"):
            cmd.append(f'-datadir={node["datadir"]}')
        cmd.extend(str(a) for a in args)

        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0:
                    err = result.stderr.strip()
                    if "Could not connect" in err or "Connection refused" in err:
                        if attempt < 2:
                            wait = 5 * (attempt + 1)
                            logger.warning(
                                "Node connection failed (attempt %d/3), "
                                "retrying in %ds...", attempt + 1, wait
                            )
                            time.sleep(wait)
                            continue
                    raise RPCError(err)
                return result.stdout.strip()
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    logger.warning(
                        "RPC call timed out (attempt %d/3), retrying...",
                        attempt + 1,
                    )
                    continue
                raise RPCError("RPC call timed out after 3 attempts")
        raise RPCError("Failed after 3 attempts")

    def btq_cli_json(self, *args):
        """Call btq-cli and parse JSON output."""
        return json.loads(self.btq_cli(*args))

    def verify_node(self):
        """Check that the node is reachable and synced."""
        try:
            height = int(self.btq_cli("getblockcount"))
            info = self.btq_cli_json("getblockchaininfo")
            chain = info.get("chain", "unknown")
            logger.info(
                "Connected to node: chain=%s height=%d", chain, height
            )
            return True
        except (RPCError, json.JSONDecodeError) as e:
            logger.error("Cannot connect to node: %s", e)
            return False

    def export_addresses(self):
        """Generate and print addresses for sharing with other participants."""
        print("\nGenerating addresses for peer sharing...\n")

        addresses = {}

        try:
            addr = self.btq_cli("getnewaddress", "", "bech32")
            addresses["standard"] = addr
            print(f"  Standard: {addr}")
        except RPCError as e:
            print(f"  Standard: failed ({e})")

        try:
            addr = self.btq_cli("getnewdilithiumaddress")
            addresses["dilithium"] = addr
            print(f"  Dilithium: {addr}")
        except RPCError as e:
            print(f"  Dilithium: failed ({e})")

        if not addresses:
            print("\nError: Could not generate any addresses.")
            return

        snippet = json.dumps(
            {"name": "your-node-name", "addresses": addresses}, indent=2
        )
        print(f"\nPaste this into other participants' peers array:\n\n{snippet}\n")

    def get_balance(self):
        """Get current wallet balance as Decimal."""
        bal_str = self.btq_cli("getbalance")
        return Decimal(bal_str)

    def has_confirmed_utxos(self):
        """Check if there are any confirmed UTXOs available."""
        utxos = self.btq_cli_json("listunspent", "1")
        return len(utxos) > 0

    def select_targets(self):
        """Pick random (peer, address, address_type) targets for this round."""
        peers = self.config["peers"]
        if not peers:
            return []

        sched = self.config["schedule"]
        txs_cfg = sched.get("txs_per_round", 3)
        if isinstance(txs_cfg, list) and len(txs_cfg) == 2:
            n = random.randint(txs_cfg[0], txs_cfg[1])
        else:
            n = int(txs_cfg)
        dilithium_pct = sched.get("use_dilithium_pct", 50)

        targets = []
        for _ in range(n):
            peer = random.choice(peers)
            addrs = peer.get("addresses", {})

            has_std = bool(addrs.get("standard"))
            has_dil = bool(addrs.get("dilithium"))

            if has_std and has_dil:
                use_dil = random.randint(1, 100) <= dilithium_pct
                if use_dil:
                    targets.append((peer["name"], addrs["dilithium"], "dilithium"))
                else:
                    targets.append((peer["name"], addrs["standard"], "standard"))
            elif has_dil:
                targets.append((peer["name"], addrs["dilithium"], "dilithium"))
            elif has_std:
                targets.append((peer["name"], addrs["standard"], "standard"))

        return targets

    def random_amount(self):
        """Generate a random amount in the configured range."""
        sched = self.config["schedule"]
        min_amt = Decimal(sched["amount_min"])
        max_amt = Decimal(sched["amount_max"])

        range_sats = int((max_amt - min_amt) / SATOSHI)
        if range_sats <= 0:
            return min_amt
        random_sats = random.randint(0, range_sats)
        return (min_amt + Decimal(random_sats) * SATOSHI).quantize(
            SATOSHI, rounding=ROUND_DOWN
        )

    def send_transaction(self, address, amount):
        """Send a transaction and return the txid, or None on failure."""
        if self.dry_run:
            logger.info("[DRY RUN] Would send %s BTQ to %s", amount, address)
            return "dry-run-no-txid"

        try:
            txid = self.btq_cli("sendtoaddress", address, str(amount))
            return txid
        except RPCError as e:
            err = str(e)
            if "Insufficient funds" in err:
                logger.warning("Insufficient funds for %s to %s", amount, address)
            elif "Invalid address" in err:
                logger.error("Invalid address: %s", address)
            else:
                logger.error("RPC error sending to %s: %s", address, e)
            return None

    def log_tx_csv(self, timestamp, peer, addr_type, address, amount, txid, status):
        """Append a row to the CSV transaction log."""
        if not self.csv_path:
            return
        file_exists = self.csv_path.exists()
        with open(self.csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "round", "peer", "address_type",
                    "address", "amount", "txid", "status",
                ])
            writer.writerow([
                timestamp, self.round_num, peer, addr_type,
                address, str(amount), txid or "", status,
            ])

    def run_round(self):
        """Execute a single round of transactions."""
        self.round_num += 1
        safety = self.config["safety"]
        sched = self.config["schedule"]

        max_total = Decimal(safety.get("max_total_spend", "0"))
        if max_total > 0 and self.total_spent >= max_total:
            logger.info(
                "Lifetime spend limit reached (%s/%s BTQ). Exiting.",
                self.total_spent, max_total,
            )
            self.shutdown = True
            return

        balance = self.get_balance()
        min_bal = Decimal(safety.get("min_balance", "0"))
        logger.info(
            "Round #%d | Balance: %s BTQ (min: %s)",
            self.round_num, balance, min_bal,
        )

        if balance < min_bal:
            logger.warning(
                "Balance %s below minimum %s, skipping round.",
                balance, min_bal,
            )
            return

        if not self.has_confirmed_utxos():
            logger.warning("No confirmed UTXOs available, skipping round.")
            return

        targets = self.select_targets()
        if not targets:
            logger.warning("No valid targets, skipping round.")
            return

        amounts = [self.random_amount() for _ in targets]

        max_per_round = Decimal(sched.get("max_spend_per_round", "0"))
        if max_per_round > 0:
            total = sum(amounts)
            if total > max_per_round:
                scale = max_per_round / total
                amounts = [
                    (a * scale).quantize(SATOSHI, rounding=ROUND_DOWN)
                    for a in amounts
                ]

        sent = 0
        round_total = Decimal("0")
        for i, ((peer, address, addr_type), amount) in enumerate(
            zip(targets, amounts)
        ):
            txid = self.send_transaction(address, amount)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")

            if txid:
                sent += 1
                round_total += amount
                self.total_spent += amount
                status = "success" if not self.dry_run else "dry_run"
                short_txid = txid[:16] + "..." if len(txid) > 16 else txid
                logger.info(
                    "  TX %d/%d: %s BTQ -> %s (%s) txid=%s",
                    i + 1, len(targets), amount, peer, addr_type, short_txid,
                )
            else:
                status = "failed"
                logger.warning(
                    "  TX %d/%d: %s BTQ -> %s (%s) FAILED",
                    i + 1, len(targets), amount, peer, addr_type,
                )

            self.log_tx_csv(ts, peer, addr_type, address, amount, txid, status)

        logger.info(
            "Round #%d complete: %d/%d sent, total=%s BTQ (lifetime: %s)",
            self.round_num, sent, len(targets), round_total, self.total_spent,
        )

    def run_loop(self):
        """Main loop: run rounds with configured interval."""
        interval = self.config["schedule"].get("interval_seconds", 60)
        logger.info(
            "Starting stress test loop (interval=%ds, dry_run=%s)",
            interval, self.dry_run,
        )

        while not self.shutdown:
            try:
                self.run_round()
            except RPCError as e:
                logger.error("RPC error during round: %s", e)
            except Exception as e:
                logger.error("Unexpected error during round: %s", e)

            if self.shutdown:
                break

            logger.debug("Sleeping %ds until next round...", interval)
            for _ in range(interval):
                if self.shutdown:
                    break
                time.sleep(1)

        logger.info(
            "Stress test stopped. Rounds: %d, Total spent: %s BTQ",
            self.round_num, self.total_spent,
        )

    def run_once(self):
        """Run a single round and exit."""
        logger.info("Running single round (dry_run=%s)", self.dry_run)
        try:
            self.run_round()
        except RPCError as e:
            logger.error("RPC error during round: %s", e)
        except Exception as e:
            logger.error("Unexpected error during round: %s", e)


def load_config(path):
    """Load and validate the JSON config file."""
    with open(path) as f:
        config = json.load(f)

    required_sections = ["node", "peers", "schedule", "safety"]
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing config section: {section}")

    if not config["peers"]:
        raise ValueError("No peers configured")

    for i, peer in enumerate(config["peers"]):
        if "name" not in peer:
            raise ValueError(f"Peer {i} missing 'name'")
        addrs = peer.get("addresses", {})
        if not addrs.get("standard") and not addrs.get("dilithium"):
            raise ValueError(f"Peer '{peer['name']}' has no addresses")

    for field in ("amount_min", "amount_max"):
        try:
            Decimal(config["schedule"][field])
        except (KeyError, InvalidOperation):
            raise ValueError(f"Invalid schedule.{field}")

    return config


def setup_logging(config, verbose=False):
    """Configure logging to file and optionally console."""
    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("log_file", "stresstest.log")
    log_level = getattr(logging, log_cfg.get("log_level", "INFO").upper(), logging.INFO)

    logger.setLevel(log_level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(log_file)
    fh.setLevel(log_level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    if verbose:
        ch = logging.StreamHandler()
        ch.setLevel(log_level)
        ch.setFormatter(formatter)
        logger.addHandler(ch)


def write_pid_file(path):
    """Write PID file for daemon mode."""
    with open(path, "w") as f:
        f.write(str(os.getpid()))


def remove_pid_file(path):
    """Remove PID file on exit."""
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="BTQ Testnet Transaction Stress Test"
    )
    parser.add_argument(
        "--config", required=True, help="Path to JSON config file"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be sent without sending",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as background daemon with PID file",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single round and exit (for cron)",
    )
    parser.add_argument(
        "--export-addresses", action="store_true",
        help="Generate and print addresses for sharing",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print to console in addition to log file",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (json.JSONDecodeError, ValueError, FileNotFoundError) as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    st = StressTest(config, dry_run=args.dry_run)

    if args.export_addresses:
        if not st.verify_node():
            sys.exit(1)
        st.export_addresses()
        return

    setup_logging(config, verbose=args.verbose or not args.daemon)

    if not st.verify_node():
        logger.error("Cannot connect to node. Exiting.")
        sys.exit(1)

    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("log_file", "stresstest.log")
    csv_file = Path(log_file).stem + "_txlog.csv"
    st.csv_path = Path(csv_file)

    pid_file = "stresstest.pid"

    if args.daemon:
        write_pid_file(pid_file)
        logger.info("Daemon started (PID %d)", os.getpid())

    try:
        if args.once:
            st.run_once()
        else:
            st.run_loop()
    finally:
        if args.daemon:
            remove_pid_file(pid_file)


if __name__ == "__main__":
    main()
