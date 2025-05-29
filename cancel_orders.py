import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "hyperliquid-python-sdk"))

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from config import WALLET_PRIVATE_KEY, WALLET_ADDRESS, BASE_URL


def main() -> None:
    """Cancel all outstanding UBTC/USDC orders."""

    account = Account.from_key(WALLET_PRIVATE_KEY)
    info = Info(BASE_URL, skip_ws=True)
    exchange = Exchange(account, BASE_URL, account_address=WALLET_ADDRESS)

    coin = info.name_to_coin.get("UBTC/USDC", "UBTC/USDC")

    open_orders = info.open_orders(WALLET_ADDRESS)
    cancels = [
        {"coin": order["coin"], "oid": order["oid"]}
        for order in open_orders
        if order.get("coin") == coin
    ]

    if cancels:
        exchange.bulk_cancel(cancels)

    print(f"Canceled {len(cancels)} UBTC/USDC order(s).")


if __name__ == "__main__":
    main()

