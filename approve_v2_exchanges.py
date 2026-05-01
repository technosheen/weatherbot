#!/usr/bin/env python3
"""MAX-approve pUSD + CTF to Polymarket CLOB v2 exchange contracts."""
import os
import sys
from web3 import Web3
from eth_abi import encode

PK = os.environ.get("PK")
if not PK:
    print("Error: PK env var not set"); sys.exit(1)

PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
CTF  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
SPENDERS = {
    "exchange_v2":          Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B"),
    "neg_risk_adapter":     Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    "neg_risk_exchange_v2": Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59"),
}
MAX_UINT = (1 << 256) - 1

w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
acc = w3.eth.account.from_key(PK)
W = acc.address
nonce = w3.eth.get_transaction_count(W)
gas_price = int(w3.eth.gas_price * 1.15)

def send(to, data, gas, label):
    global nonce
    tx = {'from': W, 'to': to, 'data': data, 'gas': gas,
          'gasPrice': gas_price, 'nonce': nonce, 'chainId': 137}
    signed = acc.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  [{label}] tx={h.hex()} (nonce {nonce})")
    rec = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    print(f"  [{label}] {'OK' if rec.status == 1 else 'FAIL'} blk {rec.blockNumber} gas {rec.gasUsed}")
    nonce += 1
    return rec.status == 1

def pusd_allowance(spender):
    raw = w3.eth.call({'to': PUSD, 'data': '0xdd62ed3e' + encode(['address','address'],[W, spender]).hex()})
    return int.from_bytes(raw,'big')

def ctf_is_approved(spender):
    raw = w3.eth.call({'to': CTF, 'data': '0xe985e9c5' + encode(['address','address'],[W, spender]).hex()})
    return int.from_bytes(raw,'big') == 1

print(f"Wallet: {W}")
print(f"GasPrice: {w3.from_wei(gas_price,'gwei'):.1f} gwei")

# 1-3: pUSD approvals
for name, sp in SPENDERS.items():
    if pusd_allowance(sp) >= MAX_UINT >> 1:
        print(f"pUSD -> {name}: already MAX-approved, skip")
        continue
    data = '0x095ea7b3' + encode(['address','uint256'],[sp, MAX_UINT]).hex()
    send(PUSD, data, 80000, f"approve pUSD->{name}")

# 4-5: CTF setApprovalForAll
for name, sp in SPENDERS.items():
    if ctf_is_approved(sp):
        print(f"CTF -> {name}: already approved-for-all, skip")
        continue
    data = '0xa22cb465' + encode(['address','bool'],[sp, True]).hex()
    send(CTF, data, 80000, f"setApprovalForAll CTF->{name}")

# Verify
print("\n-- verification --")
for name, sp in SPENDERS.items():
    a = pusd_allowance(sp)
    c = ctf_is_approved(sp)
    print(f"  {name:22s}: pUSD={'MAX' if a >= MAX_UINT >> 1 else a/1e6}  CTF_forAll={c}")
