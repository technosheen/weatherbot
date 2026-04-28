#!/usr/bin/env python3
"""Plan or broadcast Polymarket CTF redemptions for resolved weatherbot wins.

Default mode is read-only. Use --broadcast only after reviewing the printed plan.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv
from web3 import Web3

import weatherbot_audit

ROOT = pathlib.Path(__file__).resolve().parent
RPC = "https://polygon-bor-rpc.publicnode.com"
CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEGRISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
COLLATERAL = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PARENT_COLLECTION_ID = "0x" + "00" * 32
INDEX_SETS = [1, 2]
CTF_ABI = [
    {
        "inputs": [
            {"internalType": "contract IERC20", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "", "type": "bytes32"},
            {"name": "", "type": "uint256"},
        ],
        "name": "payoutNumerators",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
# Gas-balance alert floor for auto-redeem from the bot. The bot keeps redeeming
# regardless; this just emits a warning when post-redeem MATIC drops below it.
GAS_FLOOR_USD = 2.0
_MATIC_PRICE_CACHE: dict[str, float] = {}
NEGRISK_ABI = [
    {
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]
# Backwards-compat alias for prior callers/imports.
ABI = CTF_ABI


@dataclass
class Redemption:
    file: str
    title: str
    market_id: str
    condition_id: str
    expected_value: float
    gas_estimate: int
    neg_risk: bool
    amounts: list[int]  # only used when neg_risk; ERC1155 balances per outcome (raw 1e6 units)
    target_contract: str


def _market_meta(market_id: str) -> dict:
    response = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=10)
    response.raise_for_status()
    payload = response.json()
    condition_id = payload.get("conditionId")
    if not condition_id:
        raise RuntimeError(f"missing conditionId for market {market_id}")
    raw_token_ids = payload.get("clobTokenIds")
    if isinstance(raw_token_ids, str):
        try:
            token_ids = json.loads(raw_token_ids)
        except Exception:
            token_ids = []
    else:
        token_ids = raw_token_ids or []
    return {
        "condition_id": condition_id,
        "neg_risk": bool(payload.get("negRisk")),
        "clob_token_ids": [int(t) for t in token_ids],
    }


def _market_condition_id(market_id: str) -> str:
    return _market_meta(market_id)["condition_id"]


def build_redemption_plan(root: pathlib.Path = ROOT, min_value: float = 0.000001) -> list[Redemption]:
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 10}))
    ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
    negrisk = w3.eth.contract(address=NEGRISK_ADAPTER, abi=NEGRISK_ABI)
    audit = weatherbot_audit.build_audit(root)
    wallet = Web3.to_checksum_address(audit["wallet"])
    plan: list[Redemption] = []
    for row in sorted(audit["claimable_rows"], key=lambda r: r["claimable_value"], reverse=True):
        if float(row["claimable_value"]) < min_value:
            continue
        market = json.load(open(root / "data" / "markets" / row["file"], encoding="utf-8"))
        market_id = str(market["position"]["market_id"])
        meta = _market_meta(market_id)
        condition_id = meta["condition_id"]
        if meta["neg_risk"]:
            token_ids = meta["clob_token_ids"]
            if len(token_ids) != 2:
                raise RuntimeError(f"expected 2 clobTokenIds for {market_id}, got {token_ids}")
            amounts = [int(ctf.functions.balanceOf(wallet, tid).call()) for tid in token_ids]
            if sum(amounts) == 0:
                # Nothing held on-chain — skip silently rather than emit a 0-payout tx.
                continue
            fn = negrisk.functions.redeemPositions(condition_id, amounts)
            target = NEGRISK_ADAPTER
        else:
            amounts = []
            fn = ctf.functions.redeemPositions(COLLATERAL, PARENT_COLLECTION_ID, condition_id, INDEX_SETS)
            target = CTF
        fn.call({"from": wallet})
        gas_estimate = int(fn.estimate_gas({"from": wallet}))
        plan.append(
            Redemption(
                file=row["file"],
                title=row["title"] or "",
                market_id=market_id,
                condition_id=condition_id,
                expected_value=float(row["claimable_value"]),
                gas_estimate=gas_estimate,
                neg_risk=meta["neg_risk"],
                amounts=amounts,
                target_contract=target,
            )
        )
    return plan


def print_plan(plan: list[Redemption]) -> None:
    total_value = sum(item.expected_value for item in plan)
    total_gas = sum(item.gas_estimate for item in plan)
    print("REDEMPTION PLAN")
    print(f"ctf={CTF}")
    print(f"negRiskAdapter={NEGRISK_ADAPTER}")
    print(f"collateral={COLLATERAL}")
    print(f"parentCollectionId={PARENT_COLLECTION_ID}")
    print(f"indexSets={INDEX_SETS}")
    print(f"tx_count={len(plan)} expected_total={total_value:.6f} estimated_gas_total={total_gas}")
    for i, item in enumerate(plan, 1):
        kind = "negrisk" if item.neg_risk else "ctf"
        amt = (
            f" amounts=[{item.amounts[0] / 1e6:.6f},{item.amounts[1] / 1e6:.6f}]"
            if item.neg_risk and len(item.amounts) == 2
            else ""
        )
        print(
            f"{i:02d}. [{kind}] expected={item.expected_value:.6f} gas={item.gas_estimate} "
            f"target={item.target_contract}{amt} file={item.file}"
        )
        print(f"    market_id={item.market_id} condition_id={item.condition_id}")
        print(f"    {item.title}")


def broadcast(plan: list[Redemption], root: pathlib.Path = ROOT) -> list[dict[str, Any]]:
    load_dotenv(root / ".env")
    private_key = os.environ.get("PK")
    if not private_key:
        raise RuntimeError("missing PK in .env")
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
    account = w3.eth.account.from_key(private_key)
    wallet = Web3.to_checksum_address(weatherbot_audit.WALLET)
    if account.address.lower() != wallet.lower():
        raise RuntimeError(f"PK address {account.address} does not match wallet {wallet}")
    ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
    negrisk = w3.eth.contract(address=NEGRISK_ADAPTER, abi=NEGRISK_ABI)
    nonce = w3.eth.get_transaction_count(wallet)
    gas_price = w3.eth.gas_price
    receipts: list[dict[str, Any]] = []
    for item in plan:
        if item.neg_risk:
            fn = negrisk.functions.redeemPositions(item.condition_id, item.amounts)
        else:
            fn = ctf.functions.redeemPositions(COLLATERAL, PARENT_COLLECTION_ID, item.condition_id, INDEX_SETS)
        tx = fn.build_transaction(
            {
                "from": wallet,
                "chainId": 137,
                "nonce": nonce,
                "gas": int(item.gas_estimate * 1.25) + 10_000,
                "gasPrice": gas_price,
            }
        )
        signed = w3.eth.account.sign_transaction(tx, private_key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        print(f"sent {item.file}: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        print(f"receipt {tx_hash.hex()}: status={receipt.status} gasUsed={receipt.gasUsed}")
        receipts.append({"file": item.file, "tx": tx_hash.hex(), "status": receipt.status, "gasUsed": receipt.gasUsed})
        if receipt.status != 1:
            raise RuntimeError(f"redemption failed for {item.file}: {tx_hash.hex()}")
        nonce += 1
    return receipts


# =============================================================================
# Auto-redeem hooks for bot_v3.py
# =============================================================================
# These call sites are entered from the bot's resolution loop right after a
# market is marked won. They are idempotent (skip if pos.redeemed_at set) and
# never mutate the bot's accounting balance — the bot already booked the win
# at resolution time; this is purely on-chain settlement.


def _matic_price_usd() -> float:
    if "p" in _MATIC_PRICE_CACHE:
        return _MATIC_PRICE_CACHE["p"]
    # Polygon migrated MATIC → POL; Coingecko ID is now "polygon-ecosystem-token".
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "polygon-ecosystem-token", "vs_currencies": "usd"},
            timeout=5,
        )
        r.raise_for_status()
        price = float(r.json()["polygon-ecosystem-token"]["usd"])
    except Exception:
        price = 0.10  # conservative fallback (~recent POL price) if Coingecko is unreachable
    _MATIC_PRICE_CACHE["p"] = price
    return price


def _gas_floor_alert(w3: Web3, wallet: str, gas_units: int, gas_price_wei: int) -> tuple[float, str | None]:
    """Return (matic_after_usd, alert_message_or_None)."""
    bal_wei = w3.eth.get_balance(Web3.to_checksum_address(wallet))
    cost_wei = gas_units * gas_price_wei
    after_wei = max(bal_wei - cost_wei, 0)
    after_matic = after_wei / 1e18
    after_usd = after_matic * _matic_price_usd()
    if after_usd < GAS_FLOOR_USD:
        return after_usd, (
            f"GAS_LOW post-redeem POL ~{after_matic:.4f} (~${after_usd:.2f}) "
            f"below ${GAS_FLOOR_USD:.2f} floor — top up Polygon gas"
        )
    return after_usd, None


def _build_redeem_clients() -> tuple[Web3, Any]:
    load_dotenv(ROOT / ".env")
    pk = os.environ.get("PK")
    if not pk:
        raise RuntimeError("missing PK in .env for auto-redeem")
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
    account = w3.eth.account.from_key(pk)
    wallet = Web3.to_checksum_address(weatherbot_audit.WALLET)
    if account.address.lower() != wallet.lower():
        raise RuntimeError(f"PK address {account.address} does not match wallet {wallet}")
    return w3, account


_AUTO_CLIENTS: dict[str, Any] = {}


def _auto_clients() -> tuple[Web3, Any]:
    if "w3" not in _AUTO_CLIENTS:
        w3, account = _build_redeem_clients()
        _AUTO_CLIENTS["w3"] = w3
        _AUTO_CLIENTS["account"] = account
    return _AUTO_CLIENTS["w3"], _AUTO_CLIENTS["account"]


def redeem_market(mkt: dict, *, dry_run: bool = False) -> dict:
    """Redeem one resolved winning market on-chain.

    Returns a dict with keys: redeemed (bool), skipped (bool), reason (str),
    tx (str|None), payout (float USDC.e credited to EOA), alert (str|None).
    Never raises on chain errors — encodes them in `reason` so the caller
    keeps running.
    """
    pos = mkt.get("position") or {}
    if pos.get("redeemed_at"):
        return {"redeemed": False, "skipped": True, "reason": "already_redeemed", "tx": None, "payout": 0.0, "alert": None}
    if mkt.get("resolved_outcome") != "win":
        return {"redeemed": False, "skipped": True, "reason": "not_a_win", "tx": None, "payout": 0.0, "alert": None}
    market_id = pos.get("market_id")
    if not market_id:
        return {"redeemed": False, "skipped": True, "reason": "no_market_id", "tx": None, "payout": 0.0, "alert": None}

    try:
        w3, account = _auto_clients()
    except Exception as e:
        return {"redeemed": False, "skipped": False, "reason": f"client_init_failed:{e}", "tx": None, "payout": 0.0, "alert": None}
    wallet = Web3.to_checksum_address(weatherbot_audit.WALLET)

    try:
        meta = _market_meta(str(market_id))
    except Exception as e:
        return {"redeemed": False, "skipped": False, "reason": f"meta_failed:{e}", "tx": None, "payout": 0.0, "alert": None}
    condition_id = meta["condition_id"]

    ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
    negrisk = w3.eth.contract(address=NEGRISK_ADAPTER, abi=NEGRISK_ABI)

    try:
        denom = ctf.functions.payoutDenominator(condition_id).call()
    except Exception:
        denom = 0
    if denom == 0:
        return {"redeemed": False, "skipped": True, "reason": "condition_not_reported_on_chain", "tx": None, "payout": 0.0, "alert": None}

    if meta["neg_risk"]:
        token_ids = meta["clob_token_ids"]
        if len(token_ids) != 2:
            return {"redeemed": False, "skipped": False, "reason": f"unexpected_token_count:{len(token_ids)}", "tx": None, "payout": 0.0, "alert": None}
        amounts = [int(ctf.functions.balanceOf(wallet, tid).call()) for tid in token_ids]
        if sum(amounts) == 0:
            return {"redeemed": False, "skipped": True, "reason": "no_tokens_held", "tx": None, "payout": 0.0, "alert": None}
        # Pre-flight: refuse to broadcast if expected payout is zero. Tokens-of-the-losing-outcome
        # would burn for 0 USDC.e + gas, which is exactly what we are guarding against.
        try:
            n0 = ctf.functions.payoutNumerators(condition_id, 0).call()
            n1 = ctf.functions.payoutNumerators(condition_id, 1).call()
        except Exception as e:
            return {"redeemed": False, "skipped": False, "reason": f"payout_lookup_failed:{e}", "tx": None, "payout": 0.0, "alert": None}
        expected_raw = (amounts[0] * n0 + amounts[1] * n1) // max(denom, 1)
        if expected_raw == 0:
            return {"redeemed": False, "skipped": True, "reason": "expected_payout_zero", "tx": None, "payout": 0.0, "alert": None}
        fn = negrisk.functions.redeemPositions(condition_id, amounts)
    else:
        fn = ctf.functions.redeemPositions(COLLATERAL, PARENT_COLLECTION_ID, condition_id, INDEX_SETS)

    try:
        fn.call({"from": wallet})
        gas_estimate = int(fn.estimate_gas({"from": wallet}))
    except Exception as e:
        return {"redeemed": False, "skipped": False, "reason": f"sim_failed:{e}", "tx": None, "payout": 0.0, "alert": None}

    gas_price = w3.eth.gas_price
    gas_buffered = int(gas_estimate * 1.25) + 10_000
    _, alert = _gas_floor_alert(w3, wallet, gas_buffered, gas_price)

    if dry_run:
        return {"redeemed": False, "skipped": True, "reason": "dry_run", "tx": None, "payout": 0.0, "alert": alert,
                "gas_estimate": gas_estimate, "gas_price": gas_price}

    try:
        nonce = w3.eth.get_transaction_count(wallet)
        tx = fn.build_transaction({
            "from": wallet, "chainId": 137, "nonce": nonce,
            "gas": gas_buffered, "gasPrice": gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, account.key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    except Exception as e:
        return {"redeemed": False, "skipped": False, "reason": f"broadcast_failed:{e}", "tx": None, "payout": 0.0, "alert": alert}

    tx_hex = tx_hash.hex()
    if receipt.status != 1:
        return {"redeemed": False, "skipped": False, "reason": "tx_reverted", "tx": tx_hex, "payout": 0.0, "alert": alert}

    payout = 0.0
    usdc_e = COLLATERAL.lower()
    for log in receipt.logs:
        if log.address.lower() == usdc_e and len(log.topics) == 3:
            to_addr = "0x" + log.topics[2].hex()[-40:]
            if to_addr.lower() == wallet.lower():
                payout += int(log.data.hex(), 16) / 1e6
    if payout == 0.0:
        return {"redeemed": False, "skipped": False, "reason": "tx_paid_zero", "tx": tx_hex, "payout": 0.0, "alert": alert}

    return {"redeemed": True, "skipped": False, "reason": "ok", "tx": tx_hex, "payout": payout, "alert": alert}


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or broadcast weatherbot resolved-position redemptions")
    parser.add_argument("--broadcast", action="store_true", help="sign and broadcast redemption transactions")
    parser.add_argument("--min-value", type=float, default=0.000001, help="minimum expected claimable value")
    args = parser.parse_args()
    plan = build_redemption_plan(min_value=args.min_value)
    print_plan(plan)
    if not args.broadcast:
        print("DRY_RUN_ONLY=true")
        return 0
    receipts = broadcast(plan)
    print(json.dumps({"receipts": receipts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
