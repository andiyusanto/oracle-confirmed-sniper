"""
Polymarket USDC.e → pUSD Conversion
=====================================
Converts your USDC.e balance to pUSD (Polymarket USD) via the official
Collateral Onramp contract on Polygon.

Required after Polymarket Exchange V2 upgrade (April 28, 2026).
pUSD is now the only accepted collateral for trading.

Step 1 of 2: Run this first.
Step 2 of 2: Then run approve_usdc.py to approve pUSD for exchange contracts.

Usage:
    python3 wrap_pusd.py
"""

import sys
from typing import Optional
from dotenv import dotenv_values
from web3 import Web3

# ── Polygon addresses (verified from docs.polymarket.com/resources/contracts) ─
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-rpc.com",
]

USDC_E          = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PUSD            = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
COLLATERAL_ONRAMP = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")

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

ONRAMP_ABI = [
    {
        "name": "wrap",
        "type": "function",
        "inputs": [
            {"name": "_asset",  "type": "address"},
            {"name": "_to",     "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


def connect_rpc() -> Optional[Web3]:
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"  RPC: {rpc}")
                return w3
        except Exception:
            continue
    return None


def send_tx(
    w3: Web3,
    tx: dict,
    pk: str,
    description: str,
) -> bool:
    signed = w3.eth.account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent ({description}): {tx_hash.hex()}")
    print("  Waiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        print(f"  ✅ Confirmed! Block: {receipt.blockNumber}")
        return True
    print(f"  ❌ Transaction reverted. Hash: {tx_hash.hex()}")
    return False


def main() -> None:
    env = dotenv_values(".env")
    pk = env.get("POLY_PRIVATE_KEY", "").strip()
    funder = env.get("POLY_FUNDER_ADDRESS", "").strip()

    if not pk:
        print("❌ POLY_PRIVATE_KEY not found in .env")
        sys.exit(1)

    w3 = connect_rpc()
    if w3 is None:
        print("❌ Could not connect to any Polygon RPC.")
        sys.exit(1)

    account = w3.eth.account.from_key(pk)
    wallet = Web3.to_checksum_address(funder if funder else account.address)
    print(f"  Wallet: {wallet}\n")

    usdc_e = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    pusd   = w3.eth.contract(address=PUSD,   abi=ERC20_ABI)
    onramp = w3.eth.contract(address=COLLATERAL_ONRAMP, abi=ONRAMP_ABI)

    # Check balances
    usdc_e_raw = usdc_e.functions.balanceOf(wallet).call()
    pusd_raw   = pusd.functions.balanceOf(wallet).call()
    usdc_e_bal = usdc_e_raw / 1e6
    pusd_bal   = pusd_raw   / 1e6
    print(f"  USDC.e balance: ${usdc_e_bal:.6f}")
    print(f"  pUSD   balance: ${pusd_bal:.6f}\n")

    if usdc_e_raw == 0:
        print("❌ No USDC.e to wrap. Your wallet has no USDC.e on Polygon.")
        sys.exit(1)

    # Add 30% buffer to avoid "replacement transaction underpriced" errors
    gas_price = int(w3.eth.gas_price * 1.3)

    # ── Step 1: Approve Collateral Onramp to spend USDC.e ─────────────────
    print("--- Step 1: Approve Collateral Onramp for USDC.e ---")
    current_allowance = usdc_e.functions.allowance(wallet, COLLATERAL_ONRAMP).call()
    print(f"  Current allowance: ${current_allowance / 1e6:.2f} USDC.e")

    if current_allowance < usdc_e_raw:
        nonce = w3.eth.get_transaction_count(wallet)
        tx = usdc_e.functions.approve(COLLATERAL_ONRAMP, MAX_UINT256).build_transaction({
            "from":     wallet,
            "nonce":    nonce,
            "gas":      100_000,
            "gasPrice": gas_price,
            "chainId":  137,
        })
        ok = send_tx(w3, tx, pk, "USDC.e approve")
        if not ok:
            sys.exit(1)
    else:
        print("  ✅ Already approved. Skipping.")
    print()

    # ── Step 2: Wrap USDC.e → pUSD ────────────────────────────────────────
    print(f"--- Step 2: Wrap ${usdc_e_bal:.6f} USDC.e → pUSD ---")
    # Use 'pending' to account for any still-pending transactions in the mempool
    nonce = w3.eth.get_transaction_count(wallet, "pending")
    tx = onramp.functions.wrap(USDC_E, wallet, usdc_e_raw).build_transaction({
        "from":     wallet,
        "nonce":    nonce,
        "gas":      200_000,
        "gasPrice": gas_price,
        "chainId":  137,
    })
    ok = send_tx(w3, tx, pk, "wrap")
    if not ok:
        sys.exit(1)
    print()

    # ── Final balance check ────────────────────────────────────────────────
    print("--- Final Balance Check ---")
    usdc_e_after = usdc_e.functions.balanceOf(wallet).call() / 1e6
    pusd_after   = pusd.functions.balanceOf(wallet).call()   / 1e6
    print(f"  USDC.e: ${usdc_e_after:.6f}")
    print(f"  pUSD:   ${pusd_after:.6f}")

    if pusd_after > 0:
        print("\n✅ Wrap complete. Now run: python3 approve_usdc.py")
    else:
        print("\n⚠️  pUSD balance still 0 — check RPC state or wait a block and recheck.")


if __name__ == "__main__":
    main()
