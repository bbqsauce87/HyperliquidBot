"""Configuration for the trading bot.

Sensitive values are loaded from environment variables instead of being
hard-coded in the repository.
"""

import os

WALLET_PRIVATE_KEY = os.environ.get("WALLET_PRIVATE_KEY")
if not WALLET_PRIVATE_KEY:
    raise EnvironmentError("WALLET_PRIVATE_KEY environment variable not set")

WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS")
if not WALLET_ADDRESS:
    raise EnvironmentError("WALLET_ADDRESS environment variable not set")

BASE_URL = os.environ.get("BASE_URL", "https://api.hyperliquid.xyz")
RPC_URL = os.environ.get("RPC_URL", "https://api.hyperliquid.xyz/rpc")
