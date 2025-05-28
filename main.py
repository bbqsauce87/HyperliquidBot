import logging
import random
import time
from threading import Lock
import os
import sys

# Pfad zum SDK einfügen
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "hyperliquid-python-sdk"))

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from config import WALLET_ADDRESS, WALLET_PRIVATE_KEY, BASE_URL


class SpotLiquidityBot:
    """Simple spot market making bot using the Hyperliquid SDK.

    Parameters marked with `usd_size_min` and `usd_size_max` allow sizing
    orders by their value in USDC instead of specifying the base asset size.
    """

    def __init__(
        self,
        market: str = "UBTC/USDC",
        size_min: float = 5,
        size_max: float = 10,
        spread: float = 0.0002,
        check_interval: int = 5,
        log_file: str = "trade_log.txt",
        volume_log_file: str = "volume_log.txt",
        reprice_threshold: float = 0.005,
        dynamic_reprice_on_bbo: bool = False,
        debug: bool = False,
        *,
        usd_size_min: float | None = None,
        usd_size_max: float | None = None,
        start_order_price: float | None = None,
        start_order_size: float = 0.001,
    ) -> None:
        self.market = market
        if self.market != "UBTC/USDC":
            raise ValueError("SpotLiquidityBot only supports the UBTC/USDC market")
        self.size_min = size_min
        self.size_max = size_max
        self.usd_size_min = usd_size_min
        self.usd_size_max = usd_size_max
        self.spread = spread
        self.check_interval = check_interval
        self.reprice_threshold = reprice_threshold
        self.dynamic_reprice_on_bbo = dynamic_reprice_on_bbo
        self.debug = debug
        self.start_order_price = start_order_price
        self.start_order_size = start_order_size
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.open_orders: dict[int, dict] = {}
        self.levels = [1, 2]
        self.lock = Lock()

        # Setup SDK clients
        account = Account.from_key(WALLET_PRIVATE_KEY)
        self.exchange = Exchange(account, BASE_URL, account_address=WALLET_ADDRESS)
        self.info = Info(BASE_URL)
        self.address = WALLET_ADDRESS

        # Logs vorbereiten
        log_file = os.path.join("logs", log_file)
        volume_log_file = os.path.join("logs", volume_log_file)

        logging.basicConfig(
            level=logging.DEBUG if debug else logging.INFO,
            format="[%(asctime)s] %(message)s",
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )

        self.logger = logging.getLogger("bot")
        self.volume_log = open(volume_log_file, "a")
        self.processed_fills: set[str] = set()

        # Ensure the configured market exists in the SDK metadata before
        # attempting to subscribe. Otherwise, the SDK will raise a KeyError
        # when trying to map the name to a coin index which can be confusing.
        if self.market not in self.info.name_to_coin:
            available = ", ".join(sorted(self.info.name_to_coin.keys()))
            raise ValueError(
                f"Unknown market '{self.market}'. Available markets: {available}"
            )

        # BBO abonnieren
        self.info.subscribe({"type": "bbo", "coin": self.market}, self._on_bbo)

    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        print(msg)
        self.logger.info(msg)

    def _on_bbo(self, msg) -> None:
        """Callback für BBO-Updates."""
        if self.debug:
            self.logger.debug(f"BBO erhalten: {msg}")
        bid, ask = msg["data"]["bbo"]
        with self.lock:
            if bid is not None:
                self.best_bid = float(bid["px"])
            if ask is not None:
                self.best_ask = float(ask["px"])
            if self.dynamic_reprice_on_bbo:
                self._dynamic_reprice()

    def _mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def _random_size(self, mid: float) -> float:
        if self.usd_size_min is not None and self.usd_size_max is not None:
            usd_amt = random.uniform(self.usd_size_min, self.usd_size_max)
            return round(usd_amt / mid, 6)
        return round(random.uniform(self.size_min, self.size_max), 6)

    def _price_for_side(self, side: str, level: int, mid: float) -> float:
        mult = 1 + level * self.spread if side == "sell" else 1 - level * self.spread
        return round(mid * mult, 4)

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

    def cancel_order(self, oid: int) -> None:
        resp = self.exchange.cancel(self.market, oid)
        if resp.get("status") == "ok":
            self._log(f"Canceled order oid={oid}")
        else:
            self._log(f"Failed to cancel oid={oid}: {resp}")

    def _fetch_open_orders(self) -> dict[int, dict]:
        orders = self.info.open_orders(self.address)
        return {o["oid"]: o for o in orders if o["coin"] == self.market}

    def check_fills(self) -> None:
        chain_orders = self._fetch_open_orders()
        for oid, info in list(self.open_orders.items()):
            chain_info = chain_orders.get(oid)
            if chain_info:
                remaining = float(chain_info["sz"])
                if remaining < info["size"]:
                    filled = info["size"] - remaining
                    self._log(
                        f"Order partially filled: oid={oid}, side={info['side']}, filled={filled}, remaining={remaining}"
                    )
                    self.open_orders[oid]["size"] = remaining
                    mid = self._mid_price()
                    if mid:
                        level = info.get("level", 1)
                        new_side = "sell" if info["side"] == "buy" else "buy"
                        price = self._price_for_side(new_side, level, mid)
                        new_id = self._place_order(new_side, price, filled)
                        if new_id:
                            chain_orders[new_id] = {"side": "B" if new_side == "buy" else "A"}
                            self.open_orders[new_id] = {
                                "side": new_side,
                                "price": price,
                                "size": filled,
                                "level": level,
                            }
            else:
                self._log(
                    f"Order filled: oid={oid}, side={info['side']}, price={info['price']}, size={info['size']}"
                )
                mid = self._mid_price()
                if mid:
                    level = info.get("level", 1)
                    new_side = "sell" if info["side"] == "buy" else "buy"
                    price = self._price_for_side(new_side, level, mid)
                    new_id = self._place_order(new_side, price, info["size"])
                    if new_id:
                        chain_orders[new_id] = {"side": "B" if new_side == "buy" else "A"}
                        self.open_orders[new_id] = {
                            "side": new_side,
                            "price": price,
                            "size": info["size"],
                            "level": level,
                        }
                self.open_orders.pop(oid, None)
        self.open_orders = {oid: o for oid, o in self.open_orders.items() if oid in chain_orders}
        self._record_fills()

    def _record_fills(self) -> None:
        try:
            fills = self.info.user_fills(self.address)
        except Exception as exc:
            self._log(f"Error fetching fills: {exc}")
            return

        coin = self.market.split("/")[0]
        for fill in fills:
            if fill.get("coin") != coin:
                continue
            h = fill.get("hash")
            if h in self.processed_fills:
                continue
            self.processed_fills.add(h)
            size = fill.get("filledSz") or fill.get("sz")
            price = fill.get("avgPx") or fill.get("px")
            fee = fill.get("fee")
            self.volume_log.write(f"{size},{price},{fee}\n")

    def ensure_orders(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        for side in ("buy", "sell"):
            for level in self.levels:
                exists = any(
                    o["side"] == side and o.get("level", 1) == level
                    for o in self.open_orders.values()
                )
                if not exists:
                    price = self._price_for_side(side, level, mid)
                    size = self._random_size(mid)
                    oid = self._place_order(side, price, size)
                    if oid:
                        self.open_orders[oid] = {
                            "side": side,
                            "price": price,
                            "size": size,
                            "level": level,
                        }

    def reprice_orders(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        for oid, info in list(self.open_orders.items()):
            level = info.get("level", 1)
            target_price = self._price_for_side(info["side"], level, mid)
            if abs(target_price - info["price"]) / info["price"] > self.reprice_threshold:
                self._log(
                    f"Repricing order oid={oid}, old_price={info['price']}, target={target_price}"
                )
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)

    def _dynamic_reprice(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        for oid, info in list(self.open_orders.items()):
            level = info.get("level", 1)
            target_price = self._price_for_side(info["side"], level, mid)
            if abs(target_price - info["price"]) / info["price"] > self.reprice_threshold:
                self._log(
                    f"Repricing order oid={oid}, old_price={info['price']}, target={target_price}"
                )
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)

    def run(self) -> None:
        self._log("Bot started")
        # Wait for the first mid price so we can place initial orders
        while self._mid_price() is None:
            time.sleep(0.1)
        with self.lock:
            self.ensure_orders()
        while True:
            time.sleep(self.check_interval)
            with self.lock:
                self.reprice_orders()
                self.ensure_orders()
            self.check_fills()


if __name__ == "__main__":
    # Start placing normal volume-oriented orders around the market price
    bot = SpotLiquidityBot(usd_size_min=50, usd_size_max=100)
    bot.run()
