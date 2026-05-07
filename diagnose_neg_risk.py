"""For each active 5-min market our bot would consider, compare:
  - Bot's computed neg_risk (from Gamma `enableNegRisk` field with fallback)
  - CLOB API's authoritative get_neg_risk(token_id)

A mismatch is the most likely cause of post-V2 `invalid signature` on /order
that survives credential rotation: signing against the wrong exchange contract.
"""

import json
import re
from dotenv import dotenv_values

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.constants import POLYGON

from feeds.markets import MarketDiscovery  # noqa
from core.config import CFG  # noqa

env = dotenv_values(".env")
pk = re.sub(r"\s+", "", env["POLY_PRIVATE_KEY"]).strip().strip('"').strip("'")
if not pk.startswith("0x"):
    pk = "0x" + pk

cc = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=POLYGON,
    key=pk,
    signature_type=int(env.get("POLY_SIG_TYPE", "0")),
    funder=env.get("POLY_FUNDER_ADDRESS") or None,
)
cc.set_api_creds(cc.create_or_derive_api_creds())

import urllib.request

url = (
    "https://gamma-api.polymarket.com/markets"
    "?closed=false&active=true&limit=200&order=endDate&ascending=true"
)
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
data = json.loads(urllib.request.urlopen(req, timeout=15).read())

mismatches = 0
checked = 0
for m in data:
    slug = m.get("slug", "")
    if "5-minute" not in slug and "-5m" not in slug:
        continue
    if not any(a.lower() in slug for a in ["btc", "eth", "sol", "hype"]):
        continue

    tids = m.get("clobTokenIds") or []
    if isinstance(tids, str):
        try:
            tids = json.loads(tids)
        except Exception:
            tids = []
    if not tids:
        continue

    # Bot's computed value (mirror of feeds/markets.py logic)
    asset = next((a for a in ["BTC", "ETH", "SOL", "HYPE"] if a.lower() in slug), "?")
    nr_flag = m.get("enableNegRisk")
    if nr_flag is None:
        nr_flag = m.get("negRisk")
    if nr_flag is None:
        nr_flag = m.get("neg_risk")
    if nr_flag is None:
        bot_nr = asset in ("BTC", "ETH", "SOL")
    else:
        bot_nr = bool(nr_flag)

    # Authoritative
    try:
        clob_nr = cc.get_neg_risk(str(tids[0]))
    except Exception as e:
        clob_nr = f"err: {e}"

    flag = "  " if bot_nr == clob_nr else "❌"
    print(
        f"{flag} {asset:4s} {slug[:60]:60s} "
        f"gamma.enableNegRisk={m.get('enableNegRisk')!s:5s} "
        f"bot={bot_nr!s:5s} clob={clob_nr!s:5s}"
    )
    checked += 1
    if bot_nr != clob_nr:
        mismatches += 1

print(f"\nchecked {checked} markets, {mismatches} mismatches")
