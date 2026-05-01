"""Auto-reconcile weatherbot positions whose CLOB order IDs aged out.

For every market with a position whose entry_status is 'unknown' or 'open' but
needs_reconciliation=True, query Polymarket data-api for actual wallet
holdings. If the wallet holds the matching token in size that matches local
shares, stamp `filled_wallet_reconciled` so the bot's safety check passes.

Read-mostly: only mutates market JSON files; never sends transactions.

Cron once an hour:
  0 * * * * cd ~/weatherbot && venv/bin/python weatherbot_reconcile.py >> logs/reconcile.log 2>&1
"""

import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
WALLET = os.getenv("POLYMARKET_FUNDER") or os.getenv("WALLET") or "0x93a65bA4E8D02eb162B49b38093F820779f80AC9"
TOLERANCE = 0.01  # share-count fuzz factor


def fetch_wallet_positions():
    r = requests.get(
        f"https://data-api.polymarket.com/positions?user={WALLET}&sizeThreshold=0",
        timeout=15,
    )
    r.raise_for_status()
    return {p["asset"]: p for p in r.json()}


def main() -> int:
    by_token = fetch_wallet_positions()
    now = datetime.now(timezone.utc).isoformat()
    reconciled = 0
    skipped = 0

    for path in sorted(glob.glob(str(ROOT / "data/markets/*.json"))):
        try:
            with open(path) as f:
                m = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        pos = m.get("position") or {}
        if not pos:
            continue
        if pos.get("entry_status") == "filled_wallet_reconciled":
            continue
        needs = pos.get("needs_reconciliation") or pos.get("status") == "pending_buy"
        is_unknown = pos.get("entry_status") in ("unknown", "open", None) and pos.get("status") == "open"
        if not (needs or is_unknown):
            continue

        token = pos.get("token_id")
        local_shares = float(pos.get("shares") or 0)
        if not token or local_shares <= 0:
            continue

        onchain = by_token.get(token)
        if not onchain:
            skipped += 1
            print(f"  skip {m.get('city')} {m.get('date')}: token not held on-chain")
            continue
        size = float(onchain.get("size") or 0)
        if abs(size - local_shares) / max(local_shares, 1e-9) > TOLERANCE:
            skipped += 1
            print(f"  skip {m.get('city')} {m.get('date')}: on-chain size {size} != local {local_shares}")
            continue

        pos["wallet_shares"] = local_shares
        pos["wallet_reconciled_at"] = now
        pos["entry_status"] = "filled_wallet_reconciled"
        pos["needs_reconciliation"] = False
        if pos.get("status") in ("pending_buy", None):
            pos["status"] = "open"
        if m.get("status") in ("closed", "resolved") and pos.get("status") == "open":
            m["status"] = "open"
        pos.setdefault("reconciliation_history", []).append({
            "event": "auto_reconcile_clob_age_out",
            "at": now,
            "reason": f"on-chain holds {size} shares (local {local_shares})",
        })
        with open(path, "w") as f:
            json.dump(m, f, indent=2)
        reconciled += 1
        print(f"  reconciled {m.get('city')} {m.get('date')}: shares={local_shares}")

    print(f"\nDone. reconciled={reconciled} skipped={skipped} wallet={WALLET[:10]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
