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
    """Spot Market-Making Bot using the Hyperliquid SDK, adapted for correct tick-size and minimal sizes."""

    def __init__(
        self,
        market: str = "UBTC/USDC",
        # Annahme: Minimale Größenschritte ~ 0.0001 UBTC, hier einfach 0.001–0.01 zum Test
        size_min: float = 0.001,
        size_max: float = 0.01,
        # Spread z. B. 0.0002 = 0.02 %
        spread: float = 0.0002,
        check_interval: int = 5,
        log_file: str = "trade_log.txt",
        volume_log_file: str = "volume_log.txt",
        reprice_threshold: float = 0.005,
        dynamic_reprice_on_bbo: bool = False,
        debug: bool = True,
        # Angenommene Tick Size (z. B. 1 USDC- Schritt). 
        # Falls es 0.5 oder 0.1 sein soll, bitte anpassen.
        price_tick: float = 1.0
    ) -> None:
        self.market = market
        self.size_min = size_min
        self.size_max = size_max
        self.spread = spread
        self.check_interval = check_interval
        self.reprice_threshold = reprice_threshold
        self.dynamic_reprice_on_bbo = dynamic_reprice_on_bbo
        self.debug = debug
        self.price_tick = price_tick

        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.open_orders: dict[int, dict] = {}
        self.first_bbo_received = False
        self.lock = Lock()

        account = Account.from_key(WALLET_PRIVATE_KEY)
        self.exchange = Exchange(account, BASE_URL, account_address=WALLET_ADDRESS)
        self.info = Info(BASE_URL)
        self.address = WALLET_ADDRESS

        os.makedirs("logs", exist_ok=True)
        log_file = os.path.join("logs", log_file)
        volume_log_file = os.path.join("logs", volume_log_file)
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] %(message)s",
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )

        self.logger = logging.getLogger("bot")
        self.volume_log = open(volume_log_file, "a")
        self.processed_fills: set[str] = set()

        self._log("Bot initialized (tickSize fix).")

        # ACHTUNG: info.balances() existiert anscheinend nicht in deiner SDK-Version
        # => wir lassen diesen Teil weg oder implementieren "info.balances()" selbst, falls verfügbar.

        self.info.subscribe({"type": "bbo", "coin": self.market}, self._on_bbo)

    def _log(self, msg: str) -> None:
        print(msg)
        self.logger.info(msg)

    def _on_bbo(self, msg) -> None:
        if self.debug:
            self._log(f"[DEBUG] BBO erhalten: {msg}")

        bid, ask = msg["data"]["bbo"]
        with self.lock:
            if bid is not None:
                self.best_bid = float(bid["px"])
            if ask is not None:
                self.best_ask = float(ask["px"])

            if not self.first_bbo_received and (self.best_bid and self.best_ask):
                self.first_bbo_received = True
                mid_ = self._mid_price()
                self._log(f"Received first BBO: bid={self.best_bid} ask={self.best_ask} mid={mid_}")
                self._place_startup_order(mid_)

            if self.dynamic_reprice_on_bbo:
                self._dynamic_reprice()

    def _place_startup_order(self, mid: float) -> None:
        if mid is None:
            return
        startup_price = self._round_price(mid * (1 - 0.0001))
        startup_size = 0.005  # z. B. 0.005 UBTC
        self._log(f"Placing startup order: buy {startup_size} @ {startup_price}")
        oid = self._place_order("buy", startup_price, startup_size)
        if oid:
            self.open_orders[oid] = {"side": "buy", "price": startup_price, "size": startup_size}

    def _mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def _round_price(self, raw_px: float) -> float:
        """
        Rundet den Preis an das Tick-Raster an.
        Beispiel: price_tick=1 => 107340.26 -> 107340
                  price_tick=0.5 => 107340.26 -> 107340.0
        """
        return round(raw_px / self.price_tick) * self.price_tick

    def _random_size(self) -> float:
        return round(random.uniform(self.size_min, self.size_max), 6)

    def _place_order(self, side: str, raw_price: float, size: float) -> int | None:
        # Preis an Tick anpassen
        price = self._round_price(raw_price)
        is_buy = (side.lower() == "buy")
        if self.debug:
            self._log(f"[DEBUG] Attempting order: side={side}, px={price}, sz={size}")

        try:
            resp = self.exchange.order(
                self.market,
                is_buy,
                size,
                price,
                {"limit": {"tif": "Gtc"}},
            )
        except Exception as e:
            self._log(f"Exception while placing order: {e}")
            return None

        self._log(f"Full order response: {resp}")

        if resp.get("status") == "ok":
            data = resp["response"]["data"]
            statuses = data.get("statuses", [])
            if not statuses:
                self._log("Order response has no statuses. Possibly an error.")
                return None

            status = statuses[0]
            if "resting" in status:
                oid = status["resting"]["oid"]
                self._log(f"Placed {side} order oid={oid} price={price} size={size}")
                return oid
            if "filled" in status:
                filled_qty = status["filled"]["totalSz"]
                avg_price = status["filled"]["avgPx"]
                self._log(f"{side.capitalize()} order instantly filled {filled_qty} @ {avg_price}")
            if "error" in status:
                self._log(f"Order error: {status['error']}")
            if "rejected" in status:
                self._log(f"Order was rejected: {status['rejected']['reason']}")
        else:
            self._log(f"Order error (not 'ok'): {resp}")

        return None

    def _fetch_open_orders(self) -> dict[int, dict]:
        # Achtung: info.open_orders() existiert wohl, 'balances()' jedoch nicht
        try:
            open_os = self.info.open_orders(self.address)
            return {o["oid"]: o for o in open_os if o["coin"] == self.market}
        except Exception as e:
            self._log(f"Exception fetching open_orders: {e}")
            return {}

    def cancel_order(self, oid: int) -> None:
        try:
            resp = self.exchange.cancel(self.market, oid)
            self._log(f"Cancel response: {resp}")
            if resp.get("status") == "ok":
                self._log(f"Canceled order oid={oid}")
            else:
                self._log(f"Failed to cancel oid={oid}: {resp}")
        except Exception as e:
            self._log(f"Exception while canceling order {oid}: {e}")

    def check_fills(self) -> None:
        chain_orders = self._fetch_open_orders()
        for oid, info in list(self.open_orders.items()):
            chain_info = chain_orders.get(oid)
            if chain_info:
                # Teil-Fill?
                remaining = float(chain_info["sz"])
                if remaining < info["size"]:
                    filled = info["size"] - remaining
                    self._log(f"Order partially filled: oid={oid}, side={info['side']}, filled={filled}, remain={remaining}")
                    self.open_orders[oid]["size"] = remaining
                    # Gegenseite
                    mid = self._mid_price()
                    if mid is not None:
                        new_side = "sell" if info["side"] == "buy" else "buy"
                        px = self._round_price(mid * (1 + self.spread)) if new_side == "sell" else self._round_price(mid * (1 - self.spread))
                        new_id = self._place_order(new_side, px, filled)
                        if new_id:
                            chain_orders[new_id] = {"side": "B" if new_side == "buy" else "A"}
                            self.open_orders[new_id] = {"side": new_side, "price": px, "size": filled}
            else:
                # Komplett gefüllt oder gecancelt
                self._log(f"Order fully filled/canceled: oid={oid}, side={info['side']} px={info['price']} sz={info['size']}")
                mid = self._mid_price()
                if mid is not None:
                    new_side = "sell" if info["side"] == "buy" else "buy"
                    px = self._round_price(mid * (1 + self.spread)) if new_side == "sell" else self._round_price(mid * (1 - self.spread))
                    new_id = self._place_order(new_side, px, info["size"])
                    if new_id:
                        chain_orders[new_id] = {"side": "B" if new_side == "buy" else "A"}
                        self.open_orders[new_id] = {"side": new_side, "price": px, "size": info["size"]}
                self.open_orders.pop(oid, None)

        # Cleanup
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
            line = f"{size},{price},{fee}\n"
            self.volume_log.write(line)
            self.volume_log.flush()

    def ensure_orders(self) -> None:
        mid = self._mid_price()
        if mid is None:
            if self.debug:
                self._log("ensure_orders: mid price is None, skipping.")
            return
        sides = {info["side"] for info in self.open_orders.values()}

        if "buy" not in sides:
            buy_price = self._round_price(mid * (1 - self.spread))
            buy_size = self._random_size()
            self._log(f"ensure_orders: placing buy at {buy_price} size={buy_size}")
            oid = self._place_order("buy", buy_price, buy_size)
            if oid:
                self.open_orders[oid] = {"side": "buy", "price": buy_price, "size": buy_size}

        if "sell" not in sides:
            sell_price = self._round_price(mid * (1 + self.spread))
            sell_size = self._random_size()
            self._log(f"ensure_orders: placing sell at {sell_price} size={sell_size}")
            oid = self._place_order("sell", sell_price, sell_size)
            if oid:
                self.open_orders[oid] = {"side": "sell", "price": sell_price, "size": sell_size}

    def reprice_orders(self) -> None:
        mid = self._mid_price()
        if mid is None:
            return
        for oid, info in list(self.open_orders.items()):
            old_price = info["price"]
            dev = abs(mid - old_price) / (old_price if old_price != 0 else 1.0)
            if dev > self.reprice_threshold:
                self._log(f"Repricing order oid={oid}, old_price={old_price}, mid={mid}, dev={dev}")
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)

    def _dynamic_reprice(self) -> None:
        self.reprice_orders()

    def run(self) -> None:
        self._log("Bot started (tickSize approach).")
        while True:
            time.sleep(self.check_interval)
            with self.lock:
                self.reprice_orders()
                self.ensure_orders()
            self.check_fills()


if __name__ == "__main__":
    bot = SpotLiquidityBot(
        market="UBTC/USDC",
        # Test: Tick-Size = 1 USDC, min Size = 0.001 UBTC
        size_min=0.00005,
        size_max=0.0001,
        price_tick=1.0,
        debug=True
    )
    bot.run()
