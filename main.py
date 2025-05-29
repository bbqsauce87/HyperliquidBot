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
    Simple market-making bot for UBTC/USDC, placing exactly ~200 USD buy/sell orders
    at a chosen spread. With Inventory mgmt, dynamic spreads, and age-based expiry.
    """

    def __init__(
        self,
        market: str = "UBTC/USDC",
        usd_order_size: float = 200.0,    # ~200 USD
        spread: float = 0.0004,         # e.g. 0.04%
        check_interval: int = 5,
        reprice_threshold: float = 0.005,
        dynamic_reprice_on_bbo: bool = False,
        debug: bool = True,
        price_tick: float = 1.0,
        max_order_age: int = 90,
        max_btc_position: float = 0.1,
        crash_threshold: float = 0.05,
        crash_window: int = 60,
        log_file: str = "trade_log.txt",
        volume_log_file: str = "volume_log.txt",
    ) -> None:

        self.market = market
        self.info = Info(BASE_URL)
        asset = self.info.name_to_asset(market)
        self.decimals = self.info.asset_to_sz_decimals[asset]
        # e.g. coin_code = "@142"
        self.coin_code = self.info.name_to_coin.get(market, market.split("/")[0])

        self.usd_order_size = usd_order_size
        self.spread = spread
        self.check_interval = check_interval
        self.reprice_threshold = reprice_threshold
        self.dynamic_reprice_on_bbo = dynamic_reprice_on_bbo
        self.debug = debug
        self.price_tick = price_tick
        self.max_order_age = max_order_age
        self.crash_threshold = crash_threshold
        self.crash_window = crash_window
        self.price_history: deque[tuple[float, float]] = deque()

        # Inventar-Management
        self.max_btc_position = max_btc_position
        self.btc_balance = 0.0
        self.usdc_balance = 0.0

        self.best_bid: float | None = None
        self.best_ask: float | None = None
        # open_orders = {oid: { side, price, size, timestamp, coin }}
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
            level=logging.INFO,
            format="[%(asctime)s] %(message)s",
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger("bot")
        self.volume_log = open(volume_log_file, "a")

        self._log(f"Bot init. coin_code={self.coin_code}, order={usd_order_size}USD, spread={spread}, maxPos={max_btc_position}")
        # Subscribe to BBO
        self.info.subscribe({"type": "bbo", "coin": self.market}, self._on_bbo)

    def _log(self, msg: str) -> None:
        print(msg)
        self.logger.info(msg)

    # ----------------------
    # BBO / Price logic
    # ----------------------
    def _on_bbo(self, msg) -> None:
        if self.debug:
            self._log(f"[DEBUG] BBO => {msg}")
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
                while self.price_history and now - self.price_history[0][0] > self.crash_window:
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

        self._log(f"[INV] side={side}, filled={filled_amt:.8f} at {fill_price},  => BTC={self.btc_balance:.8f}, USDC={self.usdc_balance:.2f}")

    def _get_spreads(self) -> tuple[float, float]:
        """Return (buy_spread, sell_spread) adjusted by current BTC position ratio."""
        # If we hold too many BTC => bigger buySpread, smaller sellSpread => disincentivize more BTC.
        ratio = 0.0
        if self.max_btc_position > 0:
            ratio = max(-1.0, min(1.0, self.btc_balance / self.max_btc_position))

        # By default both are self.spread
        # ratio>0 => we have too many BTC => buy spread grows, sell spread shrinks
        buy_spread = self.spread * (1 + ratio)
        sell_spread = self.spread * (1 - ratio)
        return buy_spread, sell_spread

    # ----------------------
    # Order creation/cancel
    # ----------------------
    def _place_order(self, side: str, px: float, size: float) -> int | None:
        if px <= 0 or size <= 0:
            self._log(f"Invalid px/size => px={px}, sz={size}, skip order")
            return None
        is_buy = (side.lower() == "buy")

        if self.debug:
            self._log(f"[DEBUG] place_order => side={side}, px={px}, size={size}")

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

        self._log(f"Full order resp => {resp}")

        if resp.get("status") == "ok":
            data = resp["response"]["data"]
            st_list = data.get("statuses", [])
            if not st_list:
                self._log("No statuses => possibly error.")
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
                # Possibly update inventory if it's an immediate fill
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
            self._log(f"cancel_order => unknown oid={oid}")
            return
        c = info["coin"]  # e.g. "@142"

        try:
            resp = self.exchange.cancel(c, oid)
            self._log(f"Cancel resp => {resp}")
            if resp.get("status") == "ok":
                self._log(f"Canceled oid={oid}")
            else:
                self._log(f"Failed to cancel => {resp}")
        except Exception as e:
            self._log(f"Exception while cancel_order => {e}")

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
                c = o.get("coin")  # e.g. "@142"
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
            self._log("check_fills => no open orders from exchange or error. skip.")
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
                        # Update local size
                        self.open_orders[oid]["size"] = remain

                        # Inventory update
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
                else:
                    # fully filled or canceled
                    self._log(f"Order done => oid={oid}, side={info['side']} px={info['price']} sz={info['size']}")
                    # => entire fill or manual cancel
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
                    self.open_orders.pop(oid, None)

            # remove local orders not on chain
            self.open_orders = {oid: o for oid, o in self.open_orders.items() if oid in chain_orders}

        self._record_fills()

    # ----------------------
    # Fills logging
    # ----------------------
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
        mid = self._mid_price()
        if not mid:
            if self.debug:
                self._log("ensure_orders => no mid, skip.")
            return

        sides = {o["side"] for o in self.open_orders.values()}

        buy_spread, sell_spread = self._get_spreads()

        if "buy" not in sides:
            buy_px = self._round_price(mid * (1 - buy_spread))
            buy_sz = round(self.usd_order_size / buy_px, self.decimals)
            self._log(f"ensure_orders => place BUY px={buy_px}, size={buy_sz}")
            oid = self._place_order("buy", buy_px, buy_sz)
            if oid:
                self.open_orders[oid] = {
                    "side": "buy",
                    "price": buy_px,
                    "size": buy_sz,
                    "timestamp": time.time(),
                    "coin": self.coin_code,
                }

        if "sell" not in sides:
            sell_px = self._round_price(mid * (1 + sell_spread))
            sell_sz = round(self.usd_order_size / sell_px, self.decimals)
            self._log(f"ensure_orders => place SELL px={sell_px}, size={sell_sz}")
            oid = self._place_order("sell", sell_px, sell_sz)
            if oid:
                self.open_orders[oid] = {
                    "side": "sell",
                    "price": sell_px,
                    "size": sell_sz,
                    "timestamp": time.time(),
                    "coin": self.coin_code,
                }

    def reprice_orders(self) -> None:
        mid = self._mid_price()
        if not mid:
            return
        for oid, info in list(self.open_orders.items()):
            old = info["price"]
            dev = abs(mid - old)/max(old, 1e-9)
            if dev > self.reprice_threshold:
                self._log(f"Reprice => oid={oid}, old={old}, mid={mid}, dev={dev}")
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)

    def cancel_expired_orders(self) -> None:
        now = time.time()
        mid = self._mid_price()
        if not mid:
            return
        for oid, info in list(self.open_orders.items()):
            age = now - info["timestamp"]
            if age > self.max_order_age:
                self._log(f"cancel_expired => oid={oid}, age={age:.1f}s")
                self.cancel_order(oid)
                self.open_orders.pop(oid, None)

    def _dynamic_reprice(self) -> None:
        self.reprice_orders()

    # ----------------------
    # Bulk-cancel etc.
    # ----------------------
    def cancel_all_open_orders(self) -> None:
        self._log("cancel_all_open_orders => fetch open orders.")
        try:
            open_os = self.info.open_orders(self.address)
        except Exception as exc:
            self._log(f"Failed to fetch open orders => {exc}")
            return
        if not open_os:
            self._log("No open orders => done.")
            return

        cancels = []
        for o in open_os:
            c = o.get("coin")
            oid = o.get("oid")
            if c and oid:
                cancels.append({"coin": c, "oid": oid})

        if cancels:
            try:
                resp = self.exchange.bulk_cancel(cancels)
                self._log(f"bulk_cancel => {resp}")
            except Exception as exc:
                self._log(f"Error bulk_cancel => {exc}")

        self._log("cancel_all_open_orders => done.")
        self.load_open_orders()

    def check_crash(self) -> None:
        if len(self.price_history) < 2:
            return
        latest = self.price_history[-1][1]
        highest = max(p for _, p in self.price_history)
        if highest <= 0:
            return
        drop = (highest - latest) / highest
        if drop >= self.crash_threshold:
            self._log(
                f"[CRASH] Detected drop of {drop*100:.2f}% within {self.crash_window}s"
            )
            self.cancel_all_open_orders()
            if self.btc_balance > 0:
                px = self.best_bid or latest
                try:
                    resp = self.exchange.order(
                        self.market,
                        False,
                        self.btc_balance,
                        px,
                        {"limit": {"tif": "Ioc"}},
                        reduce_only=True,
                    )
                    self._log(f"Crash sell resp => {resp}")
                except Exception as exc:
                    self._log(f"Crash sell exception => {exc}")
            self.price_history.clear()

    def run(self) -> None:
        self._log("Bot started => with inventory + dynamic spread merges.")
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
        usd_order_size=200.0,
        spread=0.0004,
        price_tick=1.0,
        debug=True,
        max_order_age=90,
        max_btc_position=0.1,
    )
    bot.run()
