import logging
import random
import time
from threading import Lock
from collections import deque
import os
import sys

# Pfad zum SDK einfügen
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "hyperliquid-python-sdk"))

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from config import WALLET_ADDRESS, WALLET_PRIVATE_KEY, BASE_URL


class SpotLiquidityBot:
    """
    Simple market-making bot for UBTC/USDC, placing exactly ~100 USD buy/sell orders
    at a chosen spread, with:
      - Inventory mgmt
      - Crash detection by % threshold
      - Post-crash cooldown
    """

    def __init__(
        self,
        market: str = "UBTC/USDC",
        usd_order_size: float = 100.0,
        spread: float = 0.0004,
        check_interval: int = 5,
        reprice_threshold: float = 0.005,
        dynamic_reprice_on_bbo: bool = False,
        debug: bool = False,
        price_tick: float = 1.0,
        max_order_age: int = 60,
        price_expiry_threshold: float = 500.0,
        max_btc_position: float = 0.1,

        extra_sell_levels: int = 0,

        # Crash detection config:
        crash_threshold: float = 0.01,    # 1% Drop
        crash_window: int = 60,          # 60s lookback
        cooldown_after_crash: int = 180, # 3 min. no new orders

        log_file: str = "trade_log.txt",
        volume_log_file: str = "volume_log.txt",
    ) -> None:
        
        self.market = market
        self.info = Info(BASE_URL)
        asset = self.info.name_to_asset(market)
        self.decimals = self.info.asset_to_sz_decimals[asset]
        self.coin_code = self.info.name_to_coin.get(market, market.split("/")[0])

        self.usd_order_size = usd_order_size
        self.spread = spread
        self.check_interval = check_interval
        self.reprice_threshold = reprice_threshold
        self.dynamic_reprice_on_bbo = dynamic_reprice_on_bbo
        self.debug = debug
        self.price_tick = price_tick
        self.max_order_age = max_order_age
        self.price_expiry_threshold = price_expiry_threshold
        self.extra_sell_levels = extra_sell_levels
        self.sell_ref_price: float | None = None
        self.extra_sell_orders: list[int] = []
        self.base_sell_oid: int | None = None
        self.base_buy_oid: int | None = None

        # Crash detection & cooldown
        self.crash_threshold = crash_threshold
        self.crash_window = crash_window
        self.cooldown_after_crash = cooldown_after_crash
        self.price_history: deque[tuple[float, float]] = deque()
        self.last_crash_time: float = 0.0

        # Inventar:
        self.max_btc_position = max_btc_position
        self.btc_balance = 0.0
        self.usdc_balance = 0.0

        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.open_orders: dict[int, dict] = {}
        self.processed_fills: set[str] = set()
        self.first_bbo_received = False
        self.lock = Lock()

        account = Account.from_key(WALLET_PRIVATE_KEY)
        self.exchange = Exchange(account, BASE_URL, account_address=WALLET_ADDRESS)
        self.address = WALLET_ADDRESS

        os.makedirs("logs", exist_ok=True)
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

        self._log(
            f"Bot init. coin_code={self.coin_code}, order={usd_order_size}USD, "
            f"spread={spread}, maxPos={max_btc_position}, crash%={crash_threshold*100}%, "
            f"cooldown={cooldown_after_crash}s"
        )

        self.info.subscribe({"type": "bbo", "coin": self.market}, self._on_bbo)

    def _log(self, msg: str, level: int = logging.INFO) -> None:
        if level == logging.DEBUG and not self.debug:
            return
        if level >= logging.INFO:
            print(msg)
            self.logger.info(msg)
        else:
            self.logger.debug(msg)

    # ----------------------
    # BBO / Price logic
    # ----------------------
    def _on_bbo(self, msg) -> None:
        self._log(f"BBO => {msg}", logging.DEBUG)
        bid, ask = msg["data"]["bbo"]

        with self.lock:
            if bid is not None:
                self.best_bid = float(bid["px"])
            if ask is not None:
                self.best_ask = float(ask["px"])

            mid_now = self._mid_price()
            if mid_now is not None:
                now = time.time()
                self.price_history.append((now, mid_now))
                # keep only data within self.crash_window
                while self.price_history and (now - self.price_history[0][0]) > self.crash_window:
                    self.price_history.popleft()

            if not self.first_bbo_received and self.best_bid and self.best_ask:
                self.first_bbo_received = True
                mid_ = self._mid_price()
                self._log(f"Received first BBO => mid={mid_}")
                self._place_startup_order(mid_)

            if self.dynamic_reprice_on_bbo:
                self._dynamic_reprice()

    def _mid_price(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    def _round_price(self, raw_px: float) -> float:
        return round(raw_px / self.price_tick) * self.price_tick

    # ----------------------
    # Inventory & Spreads
    # ----------------------
    def _update_inventory(self, side: str, filled_amt: float, fill_price: float) -> None:
        """Update internal BTC/USDC balances after a fill."""
        if side == "buy":
            self.btc_balance += filled_amt
            self.usdc_balance -= (filled_amt * fill_price)
        else:  # side == "sell"
            self.btc_balance -= filled_amt
            self.usdc_balance += (filled_amt * fill_price)

        self._log(
            f"[INV] side={side}, filled={filled_amt:.8f} at {fill_price},  => "
            f"BTC={self.btc_balance:.8f}, USDC={self.usdc_balance:.2f}"
        )

    def _get_spreads(self) -> tuple[float, float]:
        """(buy_spread, sell_spread) je nach BTC-Balance."""
        ratio = 0.0
        if self.max_btc_position > 0:
            ratio = max(-1.0, min(1.0, self.btc_balance / self.max_btc_position))

        buy_spread = self.spread * (1 + ratio)
        sell_spread = self.spread * (1 - ratio)
        return buy_spread, sell_spread

    # ----------------------
    # Order creation/cancel
    # ----------------------
    def _place_startup_order(self, mid: float) -> None:
        """
        Place a single BUY order for ~100 USD at (mid*(1-0.0001)).
        """
        if mid is None:
            return
        px = self._round_price(mid * (1 - 0.0001))
        size = round(self.usd_order_size / px, self.decimals)

        self._log(f"Startup BUY => px={px}, size={size}")
        oid = self._place_order("buy", px, size)
        if oid:
            self.open_orders[oid] = {
                "side": "buy",
                "price": px,
                "size": size,
                "timestamp": time.time(),
                "coin": self.coin_code,
            }
            self.base_buy_oid = oid

    def _place_order(self, side: str, px: float, size: float) -> int | None:
        if px <= 0 or size <= 0:
            self._log(f"Invalid px/size => px={px}, sz={size}, skip order")
            return None
        is_buy = (side.lower() == "buy")

        self._log(f"place_order => side={side}, px={px}, size={size}", logging.DEBUG)
        try:
            resp = self.exchange.order(
                self.market,
                is_buy,
                size,
                px,
                {"limit": {"tif": "Gtc"}},
            )
        except Exception as e:
            self._log(f"Exception place_order => {e}")
            return None

        self._log(f"Full order resp => {resp}", logging.DEBUG)
        if resp.get("status") == "ok":
            data = resp["response"]["data"]
            st_list = data.get("statuses", [])
            if not st_list:
                self._log("No statuses => possibly error.", logging.DEBUG)
                return None
            st = st_list[0]
            if "resting" in st:
                oid = st["resting"]["oid"]
                self._log(f"Placed {side} => oid={oid}, px={px}, size={size}")
                return oid
            elif "filled" in st:
                fill_qty = st["filled"]["totalSz"]
                avg_px = st["filled"]["avgPx"]
                self._log(f"{side.capitalize()} instantly filled => qty={fill_qty}, px={avg_px}")
            elif "error" in st:
                err = st["error"]
                self._log(f"Order error => {err}")
            elif "rejected" in st:
                reason = st["rejected"]["reason"]
                self._log(f"Order rejected => {reason}")
        else:
            self._log(f"Order not 'ok' => {resp}")

        return None

    def cancel_order(self, oid: int) -> None:
        info = self.open_orders.get(oid)
        if not info:
            self._log(f"cancel_order => unknown oid={oid}", logging.DEBUG)
            return
        c = info["coin"]

        try:
            resp = self.exchange.cancel(c, oid)
            self._log(f"Cancel resp => {resp}", logging.DEBUG)
            if resp.get("status") == "ok":
                self._log(f"Canceled oid={oid}")
            else:
                self._log(f"Failed to cancel => {resp}")
        except Exception as e:
            self._log(f"Exception while cancel_order => {e}")

    def cancel_all_open_orders(self) -> None:
        if not self.open_orders:
            return
        cancels = [{"coin": info["coin"], "oid": oid} for oid, info in self.open_orders.items()]
        try:
            self.exchange.bulk_cancel(cancels)
        except Exception as e:
            self._log(f"bulk_cancel exception => {e}")
        for oid in list(self.open_orders.keys()):
            self.open_orders.pop(oid, None)
            if oid == self.base_sell_oid:
                self.base_sell_oid = None
                self.sell_ref_price = None
            if oid == self.base_buy_oid:
                self.base_buy_oid = None
            if oid in self.extra_sell_orders:
                self.extra_sell_orders.remove(oid)

    # ----------------------
    # Checking/Loading Orders
    # ----------------------
    def _fetch_open_orders(self) -> dict[int, dict] | None:
        try:
            raw = self.info.open_orders(self.address)
            results = {}
            for o in raw:
                oid = o.get("oid")
                if oid is not None:
                    results[oid] = o
            return results
        except Exception as e:
            self._log(f"Exception fetching open_orders => {e}")
            return None

    def load_open_orders(self) -> None:
        """Optional sync with remote state at startup."""
        try:
            raw = self.info.open_orders(self.address)
        except Exception as exc:
            self._log(f"Failed to fetch open orders => {exc}")
            return
        with self.lock:
            self.open_orders.clear()
            for o in raw:
                oid = o.get("oid")
                if oid is None:
                    continue
                side = "buy" if o.get("side") == "B" else "sell"
                px = float(o.get("limitPx", 0.0))
                sz = float(o.get("sz", 0.0))
                c = o.get("coin")
                ts = o.get("timestamp", time.time()*1000)/1000.0
                self.open_orders[oid] = {
                    "side": side,
                    "price": px,
                    "size": sz,
                    "timestamp": ts,
                    "coin": c
                }

    # ----------------------
    # Fills & partial-Fills
    # ----------------------
    def check_fills(self) -> None:
        chain_orders = self._fetch_open_orders()
        if not chain_orders:
            self._log("check_fills => no open orders from exchange or error. skip.", logging.DEBUG)
            self._record_fills()
            return

        with self.lock:
            for oid, info in list(self.open_orders.items()):
                chain_info = chain_orders.get(oid)
                if chain_info:
                    remain = float(chain_info.get("sz", 0.0))
                    if remain < info["size"]:
                        # => partial fill
                        filled = info["size"] - remain
                        self._log(f"Partial fill => oid={oid}, side={info['side']}, filled={filled}, remain={remain}")
                        self.open_orders[oid]["size"] = remain

                        self._update_inventory(info["side"], filled, info["price"])

                        # place new opposite order with dynamic spread
                        mid = self._mid_price()
                        if mid:
                            new_side = "sell" if info["side"] == "buy" else "buy"
                            buy_spread, sell_spread = self._get_spreads()
                            chosen_spread = sell_spread if new_side == "sell" else buy_spread
                            px = (self._round_price(mid * (1 + chosen_spread))
                                  if new_side == "sell"
                                  else self._round_price(mid * (1 - chosen_spread)))

                            new_sz = round(self.usd_order_size / px, self.decimals)
                            new_id = self._place_order(new_side, px, new_sz)
                            if new_id:
                                self.open_orders[new_id] = {
                                    "side": new_side,
                                    "price": px,
                                    "size": new_sz,
                                    "timestamp": time.time(),
                                    "coin": self.coin_code,
                                }
                                if new_side == "sell":
                                    self.base_sell_oid = new_id
                                    self.sell_ref_price = px
                                    self.extra_sell_orders = []
                                else:
                                    self.base_buy_oid = new_id
                else:
                    # fully filled or canceled
                    self._log(f"Order done => oid={oid}, side={info['side']} px={info['price']} sz={info['size']}")
                    self._update_inventory(info["side"], info["size"], info["price"])

                    mid = self._mid_price()
                    if mid:
                        new_side = "sell" if info["side"] == "buy" else "buy"
                        buy_spread, sell_spread = self._get_spreads()
                        chosen_spread = sell_spread if new_side == "sell" else buy_spread
                        px = (self._round_price(mid * (1 + chosen_spread))
                              if new_side=="sell"
                              else self._round_price(mid * (1 - chosen_spread)))

                        new_sz = round(self.usd_order_size / px, self.decimals)
                        new_id = self._place_order(new_side, px, new_sz)
                        if new_id:
                            self.open_orders[new_id] = {
                                "side": new_side,
                                "price": px,
                                "size": new_sz,
                                "timestamp": time.time(),
                                "coin": self.coin_code,
                            }
                            if new_side == "sell":
                                self.base_sell_oid = new_id
                                self.sell_ref_price = px
                                self.extra_sell_orders = []
                            else:
                                self.base_buy_oid = new_id
                    self.open_orders.pop(oid, None)
                    if oid == self.base_sell_oid:
                        self.base_sell_oid = None
                        self.sell_ref_price = None
                    if oid == self.base_buy_oid:
                        self.base_buy_oid = None
                    if oid in self.extra_sell_orders:
                        self.extra_sell_orders.remove(oid)

            self.open_orders = {oid: o for oid, o in self.open_orders.items() if oid in chain_orders}
            if self.base_sell_oid and self.base_sell_oid not in self.open_orders:
                self.base_sell_oid = None
                self.sell_ref_price = None
            if self.base_buy_oid and self.base_buy_oid not in self.open_orders:
                self.base_buy_oid = None
            self.extra_sell_orders = [oid for oid in self.extra_sell_orders if oid in self.open_orders]

        self._record_fills()

    def _record_fills(self) -> None:
        try:
            fills = self.info.user_fills(self.address)
        except Exception as e:
            self._log(f"Error fetching fills => {e}")
            return
        coin = self.market.split("/")[0]
        for f in fills:
            if f.get("coin") != coin:
                continue
            h = f.get("hash")
            if h in self.processed_fills:
                continue
            self.processed_fills.add(h)
            sz = f.get("filledSz") or f.get("sz")
            px = f.get("avgPx") or f.get("px")
            fee = f.get("fee")
            line = f"{sz},{px},{fee}\n"
            self.volume_log.write(line)
            self.volume_log.flush()

    # ----------------------
    # ensure Orders, reprice, etc.
    # ----------------------
    def ensure_orders(self) -> None:
        """
        1 buy + 1 sell, each ~100 USD at +/- spread around mid,
        BUT skip if we're still in crash-cooldown.
        """
        now = time.time()
        # if we had a crash in the last 'cooldown_after_crash' seconds => skip
        if (now - self.last_crash_time) < self.cooldown_after_crash:
            self._log("[COOLDOWN] => No new orders, still in post-crash cooldown", logging.DEBUG)
            return

        mid = self._mid_price()
        if not mid:
            self._log("ensure_orders => no mid, skip.", logging.DEBUG)
            return

        buy_spread, sell_spread = self._get_spreads()

        if self.base_buy_oid is None or self.base_buy_oid not in self.open_orders:
            buy_px = self._round_price(mid * (1 - buy_spread))
            buy_sz = round(self.usd_order_size / buy_px, self.decimals)
            self._log(f"ensure_orders => place BUY px={buy_px}, size={buy_sz}")
            oid = self._place_order("buy", buy_px, buy_sz)
            if oid:
                self.base_buy_oid = oid
                self.open_orders[oid] = {
                    "side": "buy",
                    "price": buy_px,
                    "size": buy_sz,
                    "timestamp": time.time(),
                    "coin": self.coin_code,
                }

        if self.base_sell_oid is None or self.base_sell_oid not in self.open_orders:
            sell_px = self._round_price(mid * (1 + sell_spread))
            sell_sz = round(self.usd_order_size / sell_px, self.decimals)
            self._log(f"ensure_orders => place SELL px={sell_px}, size={sell_sz}")
            oid = self._place_order("sell", sell_px, sell_sz)
            if oid:
                self.base_sell_oid = oid
                self.sell_ref_price = sell_px
                self.extra_sell_orders = []
                self.open_orders[oid] = {
                    "side": "sell",
                    "price": sell_px,
                    "size": sell_sz,
                    "timestamp": time.time(),
                    "coin": self.coin_code,
                }

        self._place_additional_sell_orders(mid)

    def _place_additional_sell_orders(self, mid: float) -> None:
        if self.extra_sell_levels <= 0:
            return
        if self.sell_ref_price is None:
            return

        levels_done = len(self.extra_sell_orders)
        while levels_done < self.extra_sell_levels:
            diff = self.sell_ref_price - mid
            threshold = (levels_done + 1) * 2 * self.spread * mid
            if diff >= threshold:
                px = self._round_price(
                    self.sell_ref_price + (levels_done + 1) * 2 * self.spread * mid
                )
                sz = round(self.usd_order_size / px, self.decimals)
                self._log(
                    f"place extra SELL level {levels_done+1} => px={px}, size={sz}",
                    logging.DEBUG,
                )
                oid = self._place_order("sell", px, sz)
                if oid:
                    self.open_orders[oid] = {
                        "side": "sell",
                        "price": px,
                        "size": sz,
                        "timestamp": time.time(),
                        "coin": self.coin_code,
                    }
                    self.extra_sell_orders.append(oid)
                    levels_done += 1
                    continue
            break

    def reprice_orders(self) -> None:
        mid = self._mid_price()
        if not mid:
            return
        for oid, info in list(self.open_orders.items()):
            old = info["price"]
            dev = abs(mid - old)/max(old, 1e-9)
            if dev > self.reprice_threshold:
                self._log(f"Reprice => oid={oid}, old={old}, mid={mid}, dev={dev}", logging.DEBUG)
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)
                if oid == self.base_sell_oid:
                    self.base_sell_oid = None
                    self.sell_ref_price = None
                if oid == self.base_buy_oid:
                    self.base_buy_oid = None
                if oid in self.extra_sell_orders:
                    self.extra_sell_orders.remove(oid)

    def cancel_expired_orders(self) -> None:
        now = time.time()
        mid = self._mid_price()
        if not mid:
            return
        for oid, info in list(self.open_orders.items()):
            age = now - info["timestamp"]
            deviation = abs(mid - info["price"])
            if age > self.max_order_age and deviation >= self.price_expiry_threshold:
                self._log(
                    f"cancel_expired => oid={oid}, age={age:.1f}s dev={deviation}",
                    logging.DEBUG,
                )
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)
                if oid == self.base_sell_oid:
                    self.base_sell_oid = None
                    self.sell_ref_price = None
                if oid == self.base_buy_oid:
                    self.base_buy_oid = None
                if oid in self.extra_sell_orders:
                    self.extra_sell_orders.remove(oid)

    def _dynamic_reprice(self) -> None:
        self.reprice_orders()

    # ----------------------
    # Crash detection (pct-only) + cooldown
    # ----------------------
    def check_crash(self) -> None:
        """
        1) highest in last crash_window seconds
        2) compare last price => drop >= crash_threshold => Crash
        3) Cancel all, flatten if needed, cooldown
        """
        if len(self.price_history) < 2:
            return

        latest_price = self.price_history[-1][1]
        highest_price = max(p for _, p in self.price_history)
        if highest_price <= 0:
            return

        drop_pct = (highest_price - latest_price) / highest_price
        if drop_pct >= self.crash_threshold:
            self._log(f"[CRASH] Drop of {drop_pct*100:.2f}% >= {self.crash_threshold*100:.2f}% threshold")
            self.cancel_all_open_orders()

            # Flatten BTC if we hold any
            if self.btc_balance > 0:
                px = self.best_bid or latest_price
                try:
                    resp = self.exchange.order(
                        self.market,
                        False,  # is_buy=False => sell
                        self.btc_balance,
                        px,
                        {"limit": {"tif": "Ioc"}},
                        reduce_only=True,
                    )
                    self._log(f"[CRASH] Flatten SELL resp => {resp}")
                    # In real code, parse fill to update inventory or wait for check_fills
                except Exception as exc:
                    self._log(f"[CRASH] Flatten SELL exception => {exc}")

            self.price_history.clear()
            self.last_crash_time = time.time()

    def run(self) -> None:
        self._log("Bot started => with 1% crash-schutz, 3min cooldown, 100$ orders.")
        # Optional: self.cancel_all_open_orders()

        while True:
            time.sleep(self.check_interval)
            with self.lock:
                self.cancel_expired_orders()
                self.reprice_orders()
                self.ensure_orders()
                self.check_crash()
            self.check_fills()


if __name__ == "__main__":
    bot = SpotLiquidityBot(
        market="UBTC/USDC",
        usd_order_size=100.0,   # ~100$ orders
        spread=0.0004,
        price_tick=1.0,
        debug=False,
        max_order_age=60,
        price_expiry_threshold=500,
        max_btc_position=0.1,
        crash_threshold=0.01,    # 1%
        crash_window=60,         # 60s
        cooldown_after_crash=180 # 3 minutes
    )
    bot.run()
