#!/usr/bin/env python3
"""Read-only wallet/local reconciliation audit for weatherbot v3.

This module intentionally does not place, cancel, sell, redeem, or mutate bot
state. It separates active exposure from resolved/claimable wallet tokens so
post-resolution Polymarket positions do not get mistaken for live ghosts.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any

import requests

WALLET = "0x93a65ba4e8d02eb162B49b38093F820779f80AC9".lower()
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
ROOT = pathlib.Path(__file__).resolve().parent


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def is_terminal_local_close(local: dict[str, Any] | None) -> bool:
    if not local:
        return False
    if local.get("exit_status") in {"filled", "filled_wallet_sell_confirmed", "buy_cancelled", "resolved"}:
        return True
    if local.get("close_reason") in {"resolved", "buy_cancelled"}:
        return True
    return False


def classify_wallet_match(
    local: dict[str, Any] | None,
    live: dict[str, Any],
    auxiliary_file: str | None = None,
) -> dict[str, Any]:
    value = round(_float(live.get("currentValue")), 6)
    size = round(_float(live.get("size")), 6)
    price = _float(live.get("curPrice"))

    if auxiliary_file:
        bucket = "auxiliary_duplicate"
        active = value
        claimable = 0.0
    elif not local:
        bucket = "wallet_only_unexplained"
        active = value
        claimable = 0.0
    elif local.get("position_status") == "open":
        bucket = "active_open"
        active = value
        claimable = 0.0
    elif local.get("position_status") == "closed" and is_terminal_local_close(local):
        bucket = "claimable_or_resolved"
        active = 0.0
        claimable = value
    else:
        bucket = "closed_held_active_ghost"
        active = value
        claimable = 0.0

    return {
        "bucket": bucket,
        "file": auxiliary_file or (local or {}).get("file"),
        "title": live.get("title"),
        "asset": str(live.get("asset") or ""),
        "size": size,
        "price": price,
        "value": value,
        "active_exposure_value": round(active, 6),
        "claimable_value": round(claimable, 6),
    }


def load_local_markets(markets_dir: pathlib.Path) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[dict[str, Any]]]:
    by_token: dict[str, dict[str, Any]] = {}
    auxiliary_by_token: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for path in sorted(markets_dir.glob("*.json")):
        market = json.load(open(path, encoding="utf-8"))
        pos = market.get("position") or {}
        token = str(pos.get("token_id") or "")
        row = {
            "file": path.name,
            "market_status": market.get("status"),
            "position_status": pos.get("status"),
            "close_reason": pos.get("close_reason"),
            "exit_status": pos.get("exit_status"),
            "token": token,
            "shares": _float(pos.get("shares")),
            "needs_reconciliation": bool(pos.get("needs_reconciliation")),
        }
        rows.append(row)
        if token:
            by_token[token] = row
        for extra in market.get("wallet_reconciliation_extra_positions") or []:
            extra_pos = extra.get("position") or extra
            extra_token = str(extra_pos.get("token_id") or "")
            if extra_token:
                auxiliary_by_token[extra_token] = path.name
    return by_token, auxiliary_by_token, rows


def fetch_positions(wallet: str = WALLET) -> list[dict[str, Any]]:
    response = requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": wallet, "limit": 500},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_usdc(wallet: str = WALLET) -> float:
    data = "0x70a08231" + wallet.lower()[2:].rjust(64, "0")
    response = requests.post(
        POLYGON_RPC,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": USDC_E, "data": data}, "latest"],
        },
        timeout=10,
    )
    response.raise_for_status()
    return int(response.json()["result"], 16) / 1_000_000


def build_audit(root: pathlib.Path = ROOT, wallet: str = WALLET) -> dict[str, Any]:
    by_token, auxiliary_by_token, local_rows = load_local_markets(root / "data" / "markets")
    live_positions = fetch_positions(wallet)
    classified: list[dict[str, Any]] = []
    for live in live_positions:
        asset = str(live.get("asset") or "")
        classified.append(
            classify_wallet_match(
                by_token.get(asset),
                live,
                auxiliary_file=auxiliary_by_token.get(asset) if asset in auxiliary_by_token and asset not in by_token else None,
            )
        )

    local_open_missing = [row for row in local_rows if row["position_status"] == "open" and row["token"] and row["token"] not in {str(p.get("asset") or "") for p in live_positions}]
    open_top_closed = [row for row in local_rows if row["position_status"] == "open" and row["market_status"] in {"closed", "resolved"}]
    counts = collections.Counter(row["bucket"] for row in classified)
    config = json.load(open(root / "config.json", encoding="utf-8"))
    state = json.load(open(root / "data" / "state.json", encoding="utf-8"))
    usdc = fetch_usdc(wallet)
    active_value = round(sum(row["active_exposure_value"] for row in classified), 6)
    claimable_value = round(sum(row["claimable_value"] for row in classified), 6)
    wallet_position_value = round(sum(row["value"] for row in classified), 6)

    return {
        "wallet": wallet,
        "counts": dict(counts),
        "local_counts": dict(collections.Counter(row["position_status"] or "none" for row in local_rows)),
        "market_counts": dict(collections.Counter(row["market_status"] or "none" for row in local_rows)),
        "local_open_missing_wallet": len(local_open_missing),
        "open_top_closed": len(open_top_closed),
        "needs_reconciliation": sum(1 for row in local_rows if row["needs_reconciliation"]),
        "wallet_usdc": round(usdc, 6),
        "active_position_value": active_value,
        "claimable_or_resolved_value": claimable_value,
        "wallet_position_value": wallet_position_value,
        "wallet_economic_total": round(usdc + wallet_position_value, 6),
        "config_balance": config.get("balance"),
        "state_balance": state.get("balance"),
        "accounting_minus_wallet_economic": round(_float(config.get("balance")) - (usdc + wallet_position_value), 6),
        "claimable_rows": [row for row in classified if row["bucket"] == "claimable_or_resolved" and row["claimable_value"] > 0],
        "unexplained_rows": [row for row in classified if row["bucket"] == "wallet_only_unexplained"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only weatherbot wallet/local audit")
    parser.add_argument("--json", action="store_true", help="emit full JSON")
    args = parser.parse_args()
    audit = build_audit()
    if args.json:
        print(json.dumps(audit, indent=2))
        return 0

    print("WEATHERBOT READ-ONLY AUDIT")
    print(f"wallet_usdc={audit['wallet_usdc']}")
    print(f"active_position_value={audit['active_position_value']}")
    print(f"claimable_or_resolved_value={audit['claimable_or_resolved_value']}")
    print(f"wallet_position_value={audit['wallet_position_value']}")
    print(f"wallet_economic_total={audit['wallet_economic_total']}")
    print(f"config_balance={audit['config_balance']} state_balance={audit['state_balance']}")
    print(f"accounting_minus_wallet_economic={audit['accounting_minus_wallet_economic']}")
    print(f"counts={audit['counts']}")
    print(f"local_counts={audit['local_counts']} market_counts={audit['market_counts']}")
    print(f"local_open_missing_wallet={audit['local_open_missing_wallet']} open_top_closed={audit['open_top_closed']} needs_reconciliation={audit['needs_reconciliation']}")
    print("claimable_positive:")
    for row in sorted(audit["claimable_rows"], key=lambda r: r["claimable_value"], reverse=True):
        print(f"  {row['claimable_value']:>8.4f}  {row['file']}  {row['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
