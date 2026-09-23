"""Balances of a few large, publicly labelled exchange wallets, snapshotted each run.

Coins leaving exchange custody are usually going to self-custody -- held, not about to
be sold -- and coins arriving are usually about to be sold. So a large net drop across
exchange wallets reads as accumulation, a large net rise as supply coming to market.

This is the one source here that is NOT a disclosure. Nobody attests to anything; the
labels are the community's (Arkham, Etherscan name tags), and an exchange shuffling
coins between its own cold and hot wallets looks exactly like a customer withdrawal.
That is why it is off by default (--onchain) and why every alert says what it is.

Both ends are keyless: mempool.space's public API for bitcoin, a public Ethereum
JSON-RPC endpoint (eth_getBalance) for ether. Only wallets whose label is widely
agreed on and which hold a large balance are listed; ETH_RPC_URL overrides the node.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

MEMPOOL_ADDRESS_URL = "https://mempool.space/api/address/{address}"
DEFAULT_ETH_RPC = "https://ethereum-rpc.publicnode.com"

# (coin, address, label). Cold wallets only: a hot wallet churns by tens of
# thousands of coins a day on ordinary deposits and withdrawals.
WALLETS = (
    ("BTC", "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo", "Binance cold"),
    ("BTC", "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6", "Binance cold"),
    ("BTC", "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97", "Bitfinex cold"),
    ("BTC", "bc1ql49ydapnjafl5t2cp9zqpjwe6pdgmxy98859v2", "Robinhood cold"),
    ("ETH", "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8", "Binance 7"),
    ("ETH", "0xF977814e90dA44bFA03b6295A0616a897441aceC", "Binance 8"),
)


@dataclass
class WalletBalance:
    coin: str
    address: str
    label: str
    balance: float      # in coins


def btc_balance(address: str, session: requests.Session) -> float:
    resp = session.get(MEMPOOL_ADDRESS_URL.format(address=address), timeout=30)
    resp.raise_for_status()
    stats = resp.json()["chain_stats"]
    return (stats["funded_txo_sum"] - stats["spent_txo_sum"]) / 1e8


def eth_balance(address: str, session: requests.Session) -> float:
    resp = session.post(os.environ.get("ETH_RPC_URL") or DEFAULT_ETH_RPC, json={
        "jsonrpc": "2.0", "method": "eth_getBalance", "params": [address, "latest"], "id": 1,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "result" not in data:
        raise ValueError(f"eth_getBalance failed: {data.get('error')}")
    return int(data["result"], 16) / 1e18


def fetch_balances(session: requests.Session | None = None) -> list[WalletBalance]:
    session = session or requests.Session()
    out = []
    for coin, address, label in WALLETS:
        fetch = btc_balance if coin == "BTC" else eth_balance
        out.append(WalletBalance(coin, address, label, fetch(address, session)))
    return out
