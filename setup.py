"""
Polymarket API Credential Generator

Reads wallet credentials from '.env', generates API keys,
and writes the complete config back to '.env'.

Usage:
  1. Fill in .env:
       POLY_PRIVATE_KEY=0x...
       POLY_FUNDER_ADDRESS=0x...

  2. Run:
       python setup.py

  3. Your .env will be updated with all credentials ready for the bot.
"""

import os
import sys
from dotenv import dotenv_values, set_key
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.constants import POLYGON
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

ENV_FILE = ".env"


def run_setup():
    # ── Read from .env ────────────────────────────────────────────
    if not os.path.exists(ENV_FILE):
        print(f"❌ '{ENV_FILE}' not found.")
        print("   Create it with:")
        print("     POLY_PRIVATE_KEY=0x...")
        print("     POLY_FUNDER_ADDRESS=0x...")
        sys.exit(1)

    pre = dotenv_values(ENV_FILE)
    pk = pre.get("POLY_PRIVATE_KEY", "").strip()
    funder = pre.get("POLY_FUNDER_ADDRESS", "").strip()
    sig_type = int(pre.get("POLY_SIG_TYPE", "0").strip())

    if not pk:
        print("❌ POLY_PRIVATE_KEY is missing in .env")
        sys.exit(1)

    # sig_type=1 required when funder != private key's derived address
    if funder and sig_type == 0:
        print("⚠️  POLY_FUNDER_ADDRESS is set but POLY_SIG_TYPE=0.")
        print("   For proxy accounts, POLY_SIG_TYPE should be 1.")
        print("   Update .env: POLY_SIG_TYPE=1  then re-run setup.py")

    print("--- 🔑 Generating API Credentials ---")
    print(f"  Private key: {pk[:6]}...{pk[-4:]}")
    print(f"  Funder:      {funder or '(not set)'}")
    print(f"  Sig type:    {sig_type}")

    # ── Initialize CLOB client ────────────────────────────────────
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=POLYGON,
        funder=funder or None,
        signature_type=sig_type,
    )

    # ── Derive API credentials ────────────────────────────────────
    creds = client.create_or_derive_api_key()
    print(f"  API Key:        {creds.api_key}")
    print(f"  API Secret:     {creds.api_secret[:8]}...")
    print(f"  API Passphrase: {creds.api_passphrase[:8]}...")

    # ── Write .env ────────────────────────────────────────────────
    set_key(ENV_FILE, "POLY_PRIVATE_KEY", pk)
    set_key(ENV_FILE, "POLY_FUNDER_ADDRESS", funder)
    set_key(ENV_FILE, "POLY_API_KEY", creds.api_key)
    set_key(ENV_FILE, "POLY_API_SECRET", creds.api_secret)
    set_key(ENV_FILE, "POLY_API_PASSPHRASE", creds.api_passphrase)
    set_key(ENV_FILE, "POLY_SIG_TYPE", str(sig_type))

    # ── Re-initialize client with full Level 2 credentials ───────
    from py_clob_client_v2.clob_types import ApiCreds

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=POLYGON,
        funder=funder or None,
        signature_type=sig_type,
        creds=ApiCreds(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase,
        ),
    )

    # ── Set allowance ─────────────────────────────────────────────
    print("\n--- 🛡️ Setting USDC.e Allowance ---")
    try:
        client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print("  ✅ Allowance set.")
    except Exception as e:
        print(f"  ⚠️  Allowance failed: {e}")

    # ── Verify allowance was actually registered ──────────────────
    print("\n--- 🔍 Verifying Balance & Allowance ---")
    try:
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw_balance = (
            resp.get("balance", 0)
            if isinstance(resp, dict)
            else getattr(resp, "balance", 0)
        )
        raw_allowance = (
            resp.get("allowance", 0)
            if isinstance(resp, dict)
            else getattr(resp, "allowance", 0)
        )
        balance = float(raw_balance or 0)
        allowance = float(raw_allowance or 0)
        if balance > 1_000_000:
            balance /= 1e6
        if allowance > 1_000_000:
            allowance /= 1e6
        print(f"  Balance:   ${balance:.2f} USDC")
        print(f"  Allowance: ${allowance:.2f} USDC")
        if allowance == 0:
            print("  ❌ Allowance is still 0 — the bot will not be able to trade!")
            print(
                "     Try running setup.py again, or approve manually on app.polymarket.com"
            )
        else:
            print("  ✅ Allowance confirmed. Bot is ready to trade.")
    except Exception as e:
        print(f"  ⚠️  Could not verify: {e}")

    print(f"\n✅ Done! Credentials written to '{ENV_FILE}'")
    print("   You can now run: python bot.py")


if __name__ == "__main__":
    run_setup()
