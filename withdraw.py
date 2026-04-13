"""
Polymarket USDC.e Withdrawal
==============================
Shows your current wallet balance and lets you withdraw
any amount to any Polygon address.

How it works:
  Polymarket keeps USDC.e directly in your funder wallet on Polygon.
  Winning P&L is settled back there automatically after each window.
  Withdrawal = standard ERC20 transfer from your funder wallet.

Usage:
    python3 withdraw.py
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

USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
DECIMALS = 6

ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "to",     "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
]

SEPARATOR = "─" * 48


def connect() -> Web3:
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    print("❌  Could not connect to any Polygon RPC. Check network.")
    sys.exit(1)


def main():
    env = dotenv_values(".env")
    pk      = env.get("POLY_PRIVATE_KEY", "").strip()
    funder  = env.get("POLY_FUNDER_ADDRESS", "").strip()

    if not pk:
        print("❌  POLY_PRIVATE_KEY not found in .env")
        sys.exit(1)

    w3 = connect()
    account = w3.eth.account.from_key(pk)
    wallet  = Web3.to_checksum_address(funder if funder else account.address)
    usdc    = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    # ── Show wallet info ──────────────────────────────────────────────
    raw_balance = usdc.functions.balanceOf(wallet).call()
    balance     = raw_balance / 10**DECIMALS
    matic       = w3.eth.get_balance(wallet)
    matic_bal   = w3.from_wei(matic, "ether")

    print()
    print(SEPARATOR)
    print("  Polymarket Wallet")
    print(SEPARATOR)
    print(f"  Address : {wallet}")
    print(f"  USDC.e  : ${balance:,.2f}")
    print(f"  MATIC   : {float(matic_bal):.4f}  (gas)")
    print(SEPARATOR)

    if balance <= 0:
        print("\n  No USDC.e balance to withdraw.\n")
        sys.exit(0)

    if float(matic_bal) < 0.01:
        print("\n  ⚠️  Low MATIC balance — you may not have enough gas.")
        print("     Send at least 0.5 MATIC to this wallet before withdrawing.\n")

    # ── Ask destination ───────────────────────────────────────────────
    print()
    dest_raw = input("  Destination address (Polygon): ").strip()
    if not dest_raw:
        print("  Cancelled.")
        sys.exit(0)

    try:
        destination = Web3.to_checksum_address(dest_raw)
    except Exception:
        print("  ❌  Invalid address.")
        sys.exit(1)

    # ── Ask amount ────────────────────────────────────────────────────
    print(f"\n  Available: ${balance:,.2f} USDC.e")
    amt_raw = input("  Amount to withdraw (or 'all'): ").strip().lower()

    if amt_raw in ("all", ""):
        amount = balance
    else:
        try:
            amount = float(amt_raw)
        except ValueError:
            print("  ❌  Invalid amount.")
            sys.exit(1)

    if amount <= 0:
        print("  ❌  Amount must be greater than 0.")
        sys.exit(1)

    if amount > balance:
        print(f"  ❌  Insufficient balance. Max: ${balance:,.2f}")
        sys.exit(1)

    amount_raw = int(amount * 10**DECIMALS)
    remaining  = balance - amount

    # ── Confirm ───────────────────────────────────────────────────────
    print()
    print(SEPARATOR)
    print("  Withdrawal Summary")
    print(SEPARATOR)
    print(f"  From      : {wallet}")
    print(f"  To        : {destination}")
    print(f"  Amount    : ${amount:,.2f} USDC.e")
    print(f"  Remaining : ${remaining:,.2f} USDC.e")
    print(SEPARATOR)
    print()
    confirm = input("  Confirm withdrawal? [yes/no]: ").strip().lower()

    if confirm not in ("yes", "y"):
        print("\n  Cancelled. No transaction sent.\n")
        sys.exit(0)

    # ── Send transaction ──────────────────────────────────────────────
    print("\n  Submitting transaction...")
    try:
        nonce     = w3.eth.get_transaction_count(wallet)
        gas_price = w3.eth.gas_price

        tx = usdc.functions.transfer(destination, amount_raw).build_transaction({
            "from":     wallet,
            "nonce":    nonce,
            "gas":      100_000,
            "gasPrice": gas_price,
            "chainId":  137,
        })

        signed  = w3.eth.account.sign_transaction(tx, pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        print(f"  Tx sent : {tx_hash.hex()}")
        print("  Waiting for confirmation...")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 1:
            # Verify new balance
            new_raw = usdc.functions.balanceOf(wallet).call()
            new_bal = new_raw / 10**DECIMALS
            print()
            print(SEPARATOR)
            print(f"  ✅  Withdrawal confirmed! Block: {receipt.blockNumber}")
            print(f"  Sent      : ${amount:,.2f} USDC.e")
            print(f"  New balance: ${new_bal:,.2f} USDC.e")
            print(SEPARATOR)
        else:
            print(f"\n  ❌  Transaction reverted. Hash: {tx_hash.hex()}")
            print("      Check Polygonscan for details.")

    except Exception as e:
        print(f"\n  ❌  Error: {e}")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
