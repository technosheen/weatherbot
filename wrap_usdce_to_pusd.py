#!/usr/bin/env python3
"""Convert wallet's USDC.e -> pUSD via Polymarket CollateralOnramp (1:1, no fee)."""
import os
import sys
from web3 import Web3
from eth_abi import encode

PK = os.environ.get("PK")
if not PK:
    print("Error: PK env var not set"); sys.exit(1)

USDCE  = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PUSD   = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
ONRAMP = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")
RPC    = "https://polygon-bor-rpc.publicnode.com"

w3 = Web3(Web3.HTTPProvider(RPC))
if not w3.is_connected():
    print("Error: RPC down"); sys.exit(1)

acc = w3.eth.account.from_key(PK)
W = acc.address

def erc20_balance(token, addr):
    raw = w3.eth.call({'to': token, 'data': '0x70a08231' + encode(['address'],[addr]).hex()})
    return int.from_bytes(raw, 'big')

def erc20_allowance(token, owner, spender):
    raw = w3.eth.call({'to': token, 'data': '0xdd62ed3e' + encode(['address','address'],[owner, spender]).hex()})
    return int.from_bytes(raw, 'big')

usdce_bal_before = erc20_balance(USDCE, W)
pusd_bal_before  = erc20_balance(PUSD, W)
print(f"Before: USDC.e={usdce_bal_before/1e6:.6f}  pUSD={pusd_bal_before/1e6:.6f}")
if usdce_bal_before == 0:
    print("Nothing to wrap."); sys.exit(0)

amount = usdce_bal_before
gas_price = int(w3.eth.gas_price * 1.15)
nonce = w3.eth.get_transaction_count(W)

# Step 1: approve(ONRAMP, amount) on USDC.e
existing = erc20_allowance(USDCE, W, ONRAMP)
if existing < amount:
    approve_data = '0x095ea7b3' + encode(['address','uint256'],[ONRAMP, amount]).hex()
    tx = {
        'from': W, 'to': USDCE, 'data': approve_data,
        'gas': 80000, 'gasPrice': gas_price, 'nonce': nonce, 'chainId': 137,
    }
    print(f"Approving USDC.e -> Onramp for {amount/1e6:.6f} (nonce {nonce}, gas {w3.from_wei(gas_price,'gwei'):.1f} gwei)")
    signed = acc.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  approve tx: {h.hex()}")
    rec = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rec.status != 1:
        print(f"  approve FAILED block {rec.blockNumber}"); sys.exit(1)
    print(f"  approve OK block {rec.blockNumber}")
    nonce += 1
else:
    print(f"Allowance already sufficient ({existing/1e6:.6f}); skipping approve")

# Step 2: wrap(USDC.e, W, amount) on Onramp — selector 0x62355638
wrap_data = '0x62355638' + encode(['address','address','uint256'],[USDCE, W, amount]).hex()
# Estimate gas
try:
    gas_est = w3.eth.estimate_gas({'from': W, 'to': ONRAMP, 'data': wrap_data})
    gas_limit = int(gas_est * 1.3)
    print(f"wrap gas estimate: {gas_est}, using limit {gas_limit}")
except Exception as e:
    print(f"gas estimation failed: {e}; falling back to 250000")
    gas_limit = 250000
tx = {
    'from': W, 'to': ONRAMP, 'data': wrap_data,
    'gas': gas_limit, 'gasPrice': gas_price, 'nonce': nonce, 'chainId': 137,
}
print(f"Wrapping {amount/1e6:.6f} USDC.e -> pUSD (nonce {nonce})")
signed = acc.sign_transaction(tx)
h = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f"  wrap tx: {h.hex()}")
rec = w3.eth.wait_for_transaction_receipt(h, timeout=180)
print(f"  wrap status: {'SUCCESS' if rec.status == 1 else 'FAILED'} block {rec.blockNumber} gas_used {rec.gasUsed}")
print(f"  Polygonscan: https://polygonscan.com/tx/{h.hex()}")

usdce_bal_after = erc20_balance(USDCE, W)
pusd_bal_after  = erc20_balance(PUSD, W)
print(f"After:  USDC.e={usdce_bal_after/1e6:.6f}  pUSD={pusd_bal_after/1e6:.6f}")
