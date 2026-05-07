"""Diagnose where pUSD actually lives — EOA vs Polymarket Proxy vs Safe.

Run on the server:
    source .venv/bin/activate && python3 diagnose_signer.py

If pUSD is at the EOA: keep POLY_SIG_TYPE=0, POLY_FUNDER_ADDRESS empty.
If pUSD is at a different address (Proxy/Safe): set POLY_SIG_TYPE=1 (or 2)
and POLY_FUNDER_ADDRESS=<that address> in .env, then restart.
"""

import re
from dotenv import dotenv_values
from web3 import Web3
from eth_account import Account

env = dotenv_values(".env")
pk = re.sub(r"\s+", "", env["POLY_PRIVATE_KEY"]).strip().strip('"').strip("'")
if not pk.startswith("0x"):
    pk = "0x" + pk

eoa = Account.from_key(pk).address
print(f"EOA (signer.address()):   {eoa}")
print(f"POLY_SIG_TYPE:            {env.get('POLY_SIG_TYPE')}")
print(f"POLY_FUNDER_ADDRESS:      {env.get('POLY_FUNDER_ADDRESS') or '(empty)'}")
print()

w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDCe = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ABI = [
    {
        "constant": True,
        "inputs": [{"name": "o", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]
p = w3.eth.contract(address=PUSD, abi=ABI)
u = w3.eth.contract(address=USDCe, abi=ABI)

print("--- EOA balances ---")
print(f"  pUSD:   ${p.functions.balanceOf(eoa).call() / 1e6:,.4f}")
print(f"  USDC.e: ${u.functions.balanceOf(eoa).call() / 1e6:,.4f}")

# Polymarket Proxy Factory (V1 proxies still active for V2 collateral)
PROXY_FACTORY = Web3.to_checksum_address("0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b")
factory_abi = [
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}],
        "name": "getProxy",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]
try:
    pf = w3.eth.contract(address=PROXY_FACTORY, abi=factory_abi)
    proxy = pf.functions.getProxy(eoa).call()
    print(f"\n--- Polymarket Proxy for this EOA: {proxy} ---")
    if proxy and int(proxy, 16) != 0:
        print(f"  pUSD:   ${p.functions.balanceOf(proxy).call() / 1e6:,.4f}")
        print(f"  USDC.e: ${u.functions.balanceOf(proxy).call() / 1e6:,.4f}")
except Exception as e:
    print(f"\n  proxy lookup err: {e}")

print()
print("Decision:")
print("  - If EOA holds pUSD  -> sig_type=0, funder empty (current). Order sig should work.")
print("  - If Proxy holds pUSD -> set POLY_SIG_TYPE=1 and POLY_FUNDER_ADDRESS=<proxy>")
