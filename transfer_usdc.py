#!/usr/bin/env python3
"""Transfer full USDC.e balance to target address."""
import os
import sys
from web3 import Web3
from eth_abi import encode

PK = os.environ.get("PK")
WALLET = os.environ.get("WALLET", "0x93a65bA4E8D02eb162B49b38093F820779f80AC9")
if not PK:
    print("Error: PK env var not set"); sys.exit(1)

TO = Web3.to_checksum_address("0xEEd463dC00c202081421b7f0887F4E0b3884be0e")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
RPC = "https://polygon-bor-rpc.publicnode.com"

w3 = Web3(Web3.HTTPProvider(RPC))
if not w3.is_connected():
    print("Error: RPC down"); sys.exit(1)

acc = w3.eth.account.from_key(PK)
if acc.address.lower() != WALLET.lower():
    print(f"Warning: PK derives {acc.address}, but WALLET={WALLET}")
    print("Continuing with PK-derived address...")
    WALLET = acc.address

# ERC20 balanceOf
bal_data = encode(['address'], [WALLET])
raw_bal = w3.eth.call({
    'to': USDC,
    'data': '0x70a08231' + bal_data.hex()
})
amt = int.from_bytes(raw_bal, 'big')
print(f"USDC.e balance: {amt / 1e6:.6f}  (raw: {amt})")
if amt == 0:
    print("Nothing to send."); sys.exit(0)

nonce = w3.eth.get_transaction_count(WALLET)
gas_price = w3.eth.gas_price
gas_price = int(gas_price * 1.1)  # +10%

# transfer(address,uint256)
tx_data = (
    "0xa9059cbb" +
    TO[2:].lower().rjust(64, '0') +
    hex(amt)[2:].rjust(64, '0')
)

tx = {
    'from': WALLET,
    'to': USDC,
    'data': tx_data,
    'gas': 100000,
    'gasPrice': gas_price,
    'nonce': nonce,
    'chainId': 137,
}

print(f"Sending {amt / 1e6:.6f} USDC.e to {TO}")
print(f"Nonce: {nonce} | GasPrice: {w3.from_wei(gas_price, 'gwei'):.2f} gwei")
signed = acc.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f"TX broadcast: {tx_hash.hex()}")
print("Waiting for receipt...")
rec = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
print(f"Status: {'SUCCESS' if rec.status == 1 else 'FAILED'}")
print(f"Block: {rec.blockNumber} | Gas used: {rec.gasUsed}")
print(f"Polygonscan: https://polygonscan.com/tx/{tx_hash.hex()}")
