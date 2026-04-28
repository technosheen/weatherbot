#!/usr/bin/env python3
"""Detect weatherbot position opens/closes for Telegram notifications.

Read-only by default except for the marker state file. This script does not
trade, cancel, redeem, or modify market JSON.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
MARKETS_DIR = ROOT / "data" / "markets"
STATE_PATH = ROOT / "data" / "telegram_position_change_state.json"


def _load_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _label(pos: dict[str, Any], unit: str) -> str:
    low = pos.get("bucket_low")
    high = pos.get("bucket_high")
    def fmt(x: Any) -> str:
        if x is None:
            return "?"
        try:
            f = float(x)
            if f == float("inf"):
                return "inf"
            if f == float("-inf"):
                return "-inf"
            return f"{f:.1f}"
        except Exception:
            return str(x)
    return f"{fmt(low)}-{fmt(high)}{unit}"


def current_snapshot() -> dict[str, dict[str, Any]]:
    snap: dict[str, dict[str, Any]] = {}
    for path in sorted(MARKETS_DIR.glob("*.json")):
        try:
            m = json.loads(path.read_text())
        except Exception:
            continue
        pos = m.get("position") or {}
        if not pos:
            continue
        key = str(pos.get("token_id") or pos.get("order_id") or path.name)
        status = str(pos.get("status") or "unknown")
        snap[key] = {
            "file": path.name,
            "city": m.get("city_name") or m.get("city") or "Unknown",
            "date": m.get("date") or "?",
            "market_status": m.get("status"),
            "position_status": status,
            "resolved_outcome": m.get("resolved_outcome"),
            "bucket": _label(pos, m.get("unit") or ""),
            "entry_price": pos.get("entry_price"),
            "current_price": pos.get("data_api_curPrice") or pos.get("current_price") or pos.get("exit_price"),
            "shares": pos.get("wallet_shares") or pos.get("shares"),
            "pnl": pos.get("pnl") if pos.get("pnl") is not None else m.get("pnl"),
            "close_reason": pos.get("close_reason"),
            "opened_at": pos.get("opened_at"),
            "closed_at": pos.get("closed_at"),
        }
    return snap


def is_open(row: dict[str, Any]) -> bool:
    return row.get("position_status") == "open"


def is_closed(row: dict[str, Any]) -> bool:
    return row.get("position_status") == "closed" or row.get("market_status") == "resolved"


def money(v: Any) -> str:
    try:
        return f"${float(v):.3f}"
    except Exception:
        return "n/a"


def signed(v: Any) -> str:
    try:
        return f"{float(v):+.2f}"
    except Exception:
        return "n/a"


def format_open(row: dict[str, Any]) -> str:
    return (
        f"OPEN {row['city']} {row['date']} {row['bucket']} | "
        f"entry {money(row.get('entry_price'))} | shares {row.get('shares') or 'n/a'}"
    )


def format_close(row: dict[str, Any]) -> str:
    outcome = row.get("resolved_outcome") or row.get("close_reason") or "closed"
    return (
        f"CLOSE {row['city']} {row['date']} {row['bucket']} | "
        f"{outcome} | PnL {signed(row.get('pnl'))}"
    )


def detect_changes(prev: dict[str, dict[str, Any]], cur: dict[str, dict[str, Any]]) -> list[str]:
    changes: list[str] = []
    for key, row in sorted(cur.items(), key=lambda kv: (kv[1].get("date") or "", kv[1].get("city") or "")):
        old = prev.get(key)
        if old is None:
            if is_open(row):
                changes.append(format_open(row))
            continue
        if not is_open(old) and is_open(row):
            changes.append(format_open(row))
        if is_open(old) and is_closed(row):
            changes.append(format_close(row))
    # Also catch disappeared open records as closes/missing, rare but useful.
    for key, old in sorted(prev.items(), key=lambda kv: (kv[1].get("date") or "", kv[1].get("city") or "")):
        if key not in cur and is_open(old):
            changes.append(f"CLOSE/MISSING {old['city']} {old['date']} {old['bucket']} | previous open record disappeared")
    return changes


def write_state(snap: dict[str, dict[str, Any]]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "positions": snap,
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="initialize state without emitting changes")
    ap.add_argument("--commit", action="store_true", help="write current snapshot after successful notification")
    args = ap.parse_args()

    cur = current_snapshot()
    if args.init:
        write_state(cur)
        print(json.dumps({"initialized": True, "positions": len(cur), "state": str(STATE_PATH)}))
        return 0
    if args.commit:
        write_state(cur)
        print(json.dumps({"committed": True, "positions": len(cur), "state": str(STATE_PATH)}))
        return 0

    state = _load_json(STATE_PATH, {"positions": {}})
    prev = state.get("positions") or {}
    changes = detect_changes(prev, cur)
    open_count = sum(1 for r in cur.values() if is_open(r))
    closed_count = sum(1 for r in cur.values() if is_closed(r))
    if changes:
        lines = [
            "Weatherbot position change",
            f"{len(changes)} change(s) | open={open_count} closed/resolved={closed_count}",
            "",
            *changes[:20],
        ]
        if len(changes) > 20:
            lines.append(f"...and {len(changes)-20} more")
        print(json.dumps({"changed": True, "message": "\n".join(lines), "changes": changes}, ensure_ascii=False))
    else:
        print(json.dumps({"changed": False, "open": open_count, "closed_or_resolved": closed_count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
