import logging
import random
import time
from threading import Lock

from eth_account import Account

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from config import WALLET_ADDRESS, WALLET_PRIVATE_KEY, BASE_URL


class SpotLiquidityBot:
    """Simple spot market making bot using the Hyperliquid SDK."""

    def __init__(
        self,
        market: str = "BTC/USDC",
        size_min: float = 5,
        size_max: float = 10,
        spread: float = 0.0002,
        check_interval: int = 5,
        log_file: str = "trade_log.txt",
    ) -> None:
        self.market = market
        self.size_min = size_min
        self.size_max = size_max
        self.spread = spread
        self.check_interval = check_interval
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.open_orders: dict[int, dict] = {}
        self.lock = Lock()

        # Setup SDK clients
        account = Account.from_key(WALLET_PRIVATE_KEY)
        self.exchange = Exchange(account, BASE_URL, account_address=WALLET_ADDRESS)
        self.info = Info(BASE_URL)
        self.address = WALLET_ADDRESS

        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="[%(asctime)s] %(message)s",
        )
        self.logger = logging.getLogger("bot")

        # Subscribe to BBO updates via websocket
        self.info.subscribe({"type": "bbo", "coin": self.market}, self._on_bbo)

    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        print(msg)
        self.logger.info(msg)

    def _on_bbo(self, msg) -> None:
        """Callback for websocket BBO updates."""
        bid, ask = msg["data"]["bbo"]
        with self.lock:
            if bid is not None:
                self.best_bid = float(bid["px"])
            if ask is not None:
                self.best_ask = float(ask["px"])

    # ------------------------------------------------------------------
    def _mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def _random_size(self) -> float:
        return round(random.uniform(self.size_min, self.size_max), 6)

    def _place_order(self, side: str, price: float, size: float) -> int | None:
        is_buy = side == "buy"
        resp = self.exchange.order(
            self.market,
            is_buy,
            size,
            price,
            {"limit": {"tif": "Gtc"}},
        )
        if resp.get("status") == "ok":
            status = resp["response"]["data"]["statuses"][0]
            if "resting" in status:
                oid = status["resting"]["oid"]
                self._log(f"Placed {side} order oid={oid} price={price} size={size}")
                return oid
            if "filled" in status:
                self._log(
                    f"{side.capitalize()} market order filled {status['filled']['totalSz']} @ {status['filled']['avgPx']}"
                )
        else:
            self._log(f"Order error: {resp}")
        return None

    def _fetch_open_orders(self) -> dict[int, dict]:
        orders = self.info.open_orders(self.address)
        return {o["oid"]: o for o in orders if o["coin"] == self.market}

    def check_fills(self) -> None:
        chain_orders = self._fetch_open_orders()
        for oid, info in list(self.open_orders.items()):
            if oid not in chain_orders:
                self._log(
                    f"Order filled: oid={oid}, side={info['side']}, price={info['price']}, size={info['size']}"
                )
                mid = self._mid_price()
                if mid:
                    new_side = "sell" if info["side"] == "buy" else "buy"
                    price = round(
                        mid * (1 + self.spread) if new_side == "sell" else mid * (1 - self.spread),
                        4,
                    )
                    new_id = self._place_order(new_side, price, info["size"])
                    if new_id:
                        chain_orders[new_id] = {"side": "B" if new_side == "buy" else "A"}
                        self.open_orders[new_id] = {
                            "side": new_side,
                            "price": price,
                            "size": info["size"],
                        }
                self.open_orders.pop(oid, None)
        self.open_orders = {oid: o for oid, o in self.open_orders.items() if oid in chain_orders}

    def ensure_orders(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        sides = {info["side"] for info in self.open_orders.values()}
        if "buy" not in sides:
            price = round(mid * (1 - self.spread), 4)
            size = self._random_size()
            oid = self._place_order("buy", price, size)
            if oid:
                self.open_orders[oid] = {"side": "buy", "price": price, "size": size}
        if "sell" not in sides:
            price = round(mid * (1 + self.spread), 4)
            size = self._random_size()
            oid = self._place_order("sell", price, size)
            if oid:
                self.open_orders[oid] = {"side": "sell", "price": price, "size": size}

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._log("Bot started")
        while True:
            time.sleep(self.check_interval)
            with self.lock:
                self.ensure_orders()
            self.check_fills()


if __name__ == "__main__":
    bot = SpotLiquidityBot()
    bot.run()
