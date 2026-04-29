"""
Polymarket pUSD On-Chain Approval
===================================
Directly submits ERC20 approve() transactions to Polygon.

After Polymarket Exchange V2 (April 28, 2026), collateral is pUSD —
not USDC.e. This script approves pUSD for the V2 exchange contracts.

Run AFTER wrap_pusd.py has converted your USDC.e to pUSD.

Usage:
    python3 approve_usdc.py
"""

import sys
from dotenv import dotenv_values
from web3 import Web3

# ── Polygon contracts ─────────────────────────────────────────────────
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon-rpc.com",
]

# pUSD: Polymarket USD (V2 collateral token, backed 1:1 by native USDC)
# Replaces USDC.e as of Polymarket Exchange V2 (April 28, 2026)
USDC_E = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")

SPENDERS = [
    ("CTF Exchange (V2)",         "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("NegRisk CTF Exchange (V2)", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("USDC Transfer Helper (V2)", "0xe2222d279d744050d28e00520010520000310F59"),
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


def main():
    env = dotenv_values(".env")
    pk = env.get("POLY_PRIVATE_KEY", "").strip()
    funder = env.get("POLY_FUNDER_ADDRESS", "").strip()

    if not pk:
        print("❌ POLY_PRIVATE_KEY not found in .env")
        sys.exit(1)

    w3 = None
    for rpc in POLYGON_RPCS:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if candidate.is_connected():
                print(f"  RPC:     {rpc}")
                w3 = candidate
                break
        except Exception:
            continue

    if w3 is None:
        print("❌ Could not connect to any Polygon RPC. Check network.")
        sys.exit(1)

    account = w3.eth.account.from_key(pk)
    wallet = Web3.to_checksum_address(funder if funder else account.address)
    print(f"  Wallet:  {wallet}")

    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    # Show current balance
    raw_bal = usdc.functions.balanceOf(wallet).call()
    balance = raw_bal / 1e6
    print(f"  Balance: ${balance:.2f} USDC\n")

    for name, spender_raw in SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)

        # Check current allowance
        current = usdc.functions.allowance(wallet, spender).call()
        current_usdc = current / 1e6
        print(f"--- {name} ---")
        print(f"  Spender:   {spender}")
        print(f"  Allowance: ${current_usdc:.2f} USDC")

        if current >= MAX_UINT256 // 2:
            print("  ✅ Already approved (max). Skipping.\n")
            continue

        # Build and send approve tx
        print("  Submitting approve() transaction...")
        try:
            nonce = w3.eth.get_transaction_count(wallet)
            gas_price = w3.eth.gas_price

            tx = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
                "from":     wallet,
                "nonce":    nonce,
                "gas":      100_000,
                "gasPrice": gas_price,
                "chainId":  137,
            })

            signed = w3.eth.account.sign_transaction(tx, pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  Tx sent: {tx_hash.hex()}")
            print("  Waiting for confirmation...")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                print(f"  ✅ Approved! Block: {receipt.blockNumber}")
            else:
                print(f"  ❌ Transaction reverted. Hash: {tx_hash.hex()}")
        except Exception as e:
            print(f"  ❌ Error: {e}")
        print()

    # Final verification
    print("--- Final Allowance Check ---")
    for name, spender_raw in SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)
        current = usdc.functions.allowance(wallet, spender).call()
        status = "✅" if current > 0 else "❌"
        print(f"  {status} {name}: ${current/1e6:.2f} USDC")


if __name__ == "__main__":
    main()
