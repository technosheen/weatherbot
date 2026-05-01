#!/usr/bin/env python3
"""Sweep native MATIC (POL) balance to target address, leaving only gas."""
import os
import sys
from web3 import Web3

PK = os.environ.get("PK")
if not PK:
    print("Error: PK env var not set"); sys.exit(1)

TO = Web3.to_checksum_address("0xEEd463dC00c202081421b7f0887F4E0b3884be0e")
RPC = "https://polygon-bor-rpc.publicnode.com"

w3 = Web3(Web3.HTTPProvider(RPC))
if not w3.is_connected():
    print("Error: RPC down"); sys.exit(1)

acc = w3.eth.account.from_key(PK)
WALLET = acc.address

bal = w3.eth.get_balance(WALLET)
print(f"MATIC balance: {w3.from_wei(bal, 'ether')}  (raw: {bal})")
if bal == 0:
    print("Nothing to send."); sys.exit(0)

gas_price = int(w3.eth.gas_price * 1.2)
gas_limit = 21000
fee = gas_price * gas_limit
send_amt = bal - fee
if send_amt <= 0:
    print(f"Balance too low to cover gas (fee={w3.from_wei(fee,'ether')} MATIC)."); sys.exit(1)

tx = {
    'from': WALLET,
    'to': TO,
    'value': send_amt,
    'gas': gas_limit,
    'gasPrice': gas_price,
    'nonce': w3.eth.get_transaction_count(WALLET),
    'chainId': 137,
}

print(f"Sending {w3.from_wei(send_amt,'ether')} MATIC to {TO}")
print(f"GasPrice: {w3.from_wei(gas_price,'gwei'):.2f} gwei  Fee: {w3.from_wei(fee,'ether')} MATIC")
signed = acc.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f"TX broadcast: {tx_hash.hex()}")
print("Waiting for receipt...")
rec = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
print(f"Status: {'SUCCESS' if rec.status == 1 else 'FAILED'}")
print(f"Block: {rec.blockNumber} | Gas used: {rec.gasUsed}")
print(f"Polygonscan: https://polygonscan.com/tx/{tx_hash.hex()}")
