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
    """Simple market-making bot for the UBTC/USDC pair.

    Orders are automatically capped so that their USD value does not
    exceed ``max_usd_order_size``.
    """

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
        price_tick: float = 1.0,
        max_usd_order_size: float = 50.0,
        min_usd_order_size: float = 20.0,
        max_order_age: int = 60,
    ) -> None:
        """Create a new bot instance.

        Parameters are largely self explanatory. ``max_usd_order_size`` limits
        the value of any single order in USDC, while ``min_usd_order_size``
        ensures orders are not rejected for being too small.
        """

        self.market = market
        self.info = Info(BASE_URL)

        # Determine the allowed precision for order sizes of this market
        asset = self.info.name_to_asset(market)
        self.decimals = self.info.asset_to_sz_decimals[asset]
        self.coin_code = self.info.name_to_coin.get(market, market.split("/")[0])

        # Round provided size bounds to the permitted precision
        self.size_min = round(size_min, self.decimals)
        self.size_max = round(size_max, self.decimals)
        self.spread = spread
        self.check_interval = check_interval
        self.reprice_threshold = reprice_threshold
        self.dynamic_reprice_on_bbo = dynamic_reprice_on_bbo
        self.debug = debug
        self.price_tick = price_tick
        self.max_usd_order_size = max_usd_order_size
        self.min_usd_order_size = min_usd_order_size
        self.max_order_age = max_order_age

        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.open_orders: dict[int, dict] = {}
        self.first_bbo_received = False
        self.lock = Lock()

        account = Account.from_key(WALLET_PRIVATE_KEY)
        self.exchange = Exchange(account, BASE_URL, account_address=WALLET_ADDRESS)
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
            self.open_orders[oid] = {
                "side": "buy",
                "price": startup_price,
                "size": startup_size,
                "timestamp": time.time(),
            }

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
        return round(random.uniform(self.size_min, self.size_max), self.decimals)

    def _place_order(self, side: str, raw_price: float, size: float) -> int | None:
        # Preis an Tick anpassen
        price = self._round_price(raw_price)
        size = round(size, self.decimals)
        usd_value = price * size
        # Ensure the USD value of the order meets the configured minimum
        if usd_value < self.min_usd_order_size:
            min_size = round(self.min_usd_order_size / price, self.decimals)
            if min_size * price > self.max_usd_order_size:
                self._log(
                    f"Order skipped: price={price} cannot satisfy USD bounds {self.min_usd_order_size}-{self.max_usd_order_size}"
                )
                return None
            if self.debug:
                self._log(
                    f"[DEBUG] Increasing order size from {size} to {min_size} to meet min USD size"
                )
            size = min_size
            usd_value = price * size
        # Ensure the USD value of the order does not exceed the configured maximum
        if usd_value > self.max_usd_order_size:
            capped = round(self.max_usd_order_size / price, self.decimals)
            if capped <= 0:
                self._log(
                    f"Order skipped: price={price} would exceed max USD size {self.max_usd_order_size}"
                )
                return None
            if self.debug:
                self._log(
                    f"[DEBUG] Capping order size from {size} to {capped} due to max USD size"
                )
            size = capped
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

    def _fetch_open_orders(self) -> dict[int, dict] | None:
        """Return the current open orders for this wallet.

        If the request fails, ``None`` is returned so callers can
        differentiate between "no open orders" and "request failed".
        """
        try:
            open_os = self.info.open_orders(self.address)
            return {o["oid"]: o for o in open_os if o["coin"] == self.coin_code}
        except Exception as e:
            self._log(f"Exception fetching open_orders: {e}")
            return None

    def load_open_orders(self) -> None:
        """Populate ``self.open_orders`` from the exchange state.

        This is useful on startup so the bot is aware of any existing orders
        and the expiration timer can clean them up if needed.
        """
        try:
            raw = self.info.open_orders(self.address)
        except Exception as exc:
            self._log(f"Failed to fetch open orders: {exc}")
            return

        self.open_orders.clear()
        for order in raw:
            if order.get("coin") != self.coin_code:
                continue
            oid = order.get("oid")
            if oid is None:
                continue
            side = "buy" if order.get("side") == "B" else "sell"
            px = float(order.get("limitPx", 0.0))
            sz = float(order.get("sz", 0.0))
            ts = order.get("timestamp")
            if ts is not None:
                ts = ts / 1000.0  # api provides ms
            else:
                ts = time.time()
            self.open_orders[oid] = {
                "side": side,
                "price": px,
                "size": sz,
                "timestamp": ts,
            }

    def cancel_order(self, oid: int) -> None:
        try:
            # Follow the official SDK pattern by passing the canonical coin name
            # rather than the market pair when cancelling orders.
            resp = self.exchange.cancel(self.coin_code, oid)
            self._log(f"Cancel response: {resp}")
            if resp.get("status") == "ok":
                self._log(f"Canceled order oid={oid}")
            else:
                self._log(f"Failed to cancel oid={oid}: {resp}")
        except Exception as e:
            self._log(f"Exception while canceling order {oid}: {e}")

    def check_fills(self) -> None:
        chain_orders = self._fetch_open_orders()
        if chain_orders is None:
            self._log("check_fills: unable to fetch open orders; skipping check.")
            self._record_fills()
            return
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
                            chain_orders[new_id] = {
                                "side": "B" if new_side == "buy" else "A"
                            }
                            self.open_orders[new_id] = {
                                "side": new_side,
                                "price": px,
                                "size": filled,
                                "timestamp": time.time(),
                            }
            else:
                # Komplett gefüllt oder gecancelt
                self._log(f"Order fully filled/canceled: oid={oid}, side={info['side']} px={info['price']} sz={info['size']}")
                mid = self._mid_price()
                if mid is not None:
                    new_side = "sell" if info["side"] == "buy" else "buy"
                    px = self._round_price(mid * (1 + self.spread)) if new_side == "sell" else self._round_price(mid * (1 - self.spread))
                    new_id = self._place_order(new_side, px, info["size"])
                    if new_id:
                        chain_orders[new_id] = {
                            "side": "B" if new_side == "buy" else "A"
                        }
                        self.open_orders[new_id] = {
                            "side": new_side,
                            "price": px,
                            "size": info["size"],
                            "timestamp": time.time(),
                        }
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
                self.open_orders[oid] = {
                    "side": "buy",
                    "price": buy_price,
                    "size": buy_size,
                    "timestamp": time.time(),
                }

        if "sell" not in sides:
            sell_price = self._round_price(mid * (1 + self.spread))
            sell_size = self._random_size()
            self._log(f"ensure_orders: placing sell at {sell_price} size={sell_size}")
            oid = self._place_order("sell", sell_price, sell_size)
            if oid:
                self.open_orders[oid] = {
                    "side": "sell",
                    "price": sell_price,
                    "size": sell_size,
                    "timestamp": time.time(),
                }

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

    def cancel_expired_orders(self) -> None:
        now = time.time()
        mid = self._mid_price()
        for oid, info in list(self.open_orders.items()):
            ts = info.get("timestamp")
            if ts is None:
                continue
            if now - ts <= self.max_order_age:
                continue
            if mid is None:
                continue
            if abs(mid - info["price"]) >= 500:
                self._log(
                    f"Order oid={oid} older than {self.max_order_age}s and price moved {abs(mid - info['price'])}; canceling"
                )
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)

    def _dynamic_reprice(self) -> None:
        self.reprice_orders()

    def cancel_all_open_orders(self) -> None:
        """Cancel any resting orders across **all** markets before trading starts."""
        self._log("Checking for leftover open orders...")

        open_os = None
        for attempt in range(1, 4):
            try:
                open_os = self.info.open_orders(self.address)
                break
            except Exception as exc:
                self._log(
                    f"Attempt {attempt} failed to fetch open orders: {exc}"
                )
                if attempt < 3:
                    time.sleep(1)
        if open_os is None:
            self._log(
                "Failed to fetch open orders after 3 attempts; skipping cleanup."
            )
            return

        if not open_os:
            self._log("No open orders found.")
            self.open_orders.clear()
            return

        cancels = []
        for o in open_os:
            coin = o.get("coin")
            oid = o.get("oid")
            if coin is None or oid is None:
                continue
            cancels.append({"coin": coin, "oid": oid})

        if cancels:
            try:
                resp = self.exchange.bulk_cancel(cancels)
                self._log(f"Bulk cancel response: {resp}")
            except Exception as exc:
                self._log(f"Error during bulk cancel: {exc}")
        else:
            self._log("No cancelable orders found.")

        # give the API a moment to process cancellations and refresh local state
        time.sleep(1)
        self.load_open_orders()

    def run(self) -> None:
        self._log("Bot started (tickSize approach).")
        self.cancel_all_open_orders()
        while True:
            time.sleep(self.check_interval)
            with self.lock:
                self.cancel_expired_orders()
                self.reprice_orders()
                self.ensure_orders()
            self.check_fills()


if __name__ == "__main__":
    bot = SpotLiquidityBot(
        market="UBTC/USDC",
        # Test: Tick-Size = 1 USDC, min Size = 0.001 UBTC
        size_min=0.00005,
        size_max=0.0001,
        spread=0.0004,
        price_tick=1.0,
        debug=True,
        max_usd_order_size=50.0,
        min_usd_order_size=20.0,
        max_order_age=60,
    )
    bot.run()
