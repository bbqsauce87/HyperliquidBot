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
    """
    Simple market-making bot for UBTC/USDC, placing exactly ~20 USD buy/sell orders
    at a chosen spread. No random sizing, no scheduled auto-close.
    """

    def __init__(
        self,
        market: str = "UBTC/USDC",
        # Fixes for user:
        # place EXACT 20$ orders => we compute size = 20 / price
        usd_order_size: float = 20.0,    # ~20 USD
        spread: float = 0.0004,         # e.g. 0.04%
        check_interval: int = 5,
        reprice_threshold: float = 0.005,
        dynamic_reprice_on_bbo: bool = False,
        debug: bool = True,
        price_tick: float = 1.0,
        max_order_age: int = 60,
        log_file: str = "trade_log.txt",
        volume_log_file: str = "volume_log.txt",
    ) -> None:

        self.market = market
        self.info = Info(BASE_URL)
        asset = self.info.name_to_asset(market)
        self.decimals = self.info.asset_to_sz_decimals[asset]
        # E.g. coin_code = "@142"
        self.coin_code = self.info.name_to_coin.get(market, market.split("/")[0])

        self.usd_order_size = usd_order_size
        self.spread = spread
        self.check_interval = check_interval
        self.reprice_threshold = reprice_threshold
        self.dynamic_reprice_on_bbo = dynamic_reprice_on_bbo
        self.debug = debug
        self.price_tick = price_tick
        self.max_order_age = max_order_age

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

        self._log(f"Bot init. coin_code={self.coin_code}, fixed USD order = {usd_order_size}, spread={spread}")
        # Subscribe to BBO
        self.info.subscribe({"type": "bbo", "coin": self.market}, self._on_bbo)

    def _log(self, msg: str) -> None:
        print(msg)
        self.logger.info(msg)

    # ------------------------------------------------------------------
    def _on_bbo(self, msg) -> None:
        if self.debug:
            self._log(f"[DEBUG] BBO => {msg}")
        bid, ask = msg["data"]["bbo"]

        with self.lock:
            if bid is not None:
                self.best_bid = float(bid["px"])
            if ask is not None:
                self.best_ask = float(ask["px"])

            if not self.first_bbo_received and self.best_bid and self.best_ask:
                self.first_bbo_received = True
                mid_ = self._mid_price()
                self._log(f"Received first BBO => mid={mid_}")
                self._place_startup_order(mid_)

            if self.dynamic_reprice_on_bbo:
                self._dynamic_reprice()

    def _place_startup_order(self, mid: float) -> None:
        """
        Place a single BUY order for ~20 USD at (mid*(1-0.0001)).
        """
        if mid is None:
            return
        px = self._round_price(mid * (1 - 0.0001))
        # size = 20 / px
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

    def _mid_price(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    def _round_price(self, raw_px: float) -> float:
        return round(raw_px / self.price_tick) * self.price_tick

    def _place_order(self, side: str, px: float, size: float) -> int | None:
        """Place a single GTC limit-order. size in base coin, px in USDC."""
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
        """Cancel a single open order by looking up the coin in self.open_orders."""
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

    # ------------------------------------------------------------------
    def _fetch_open_orders(self) -> dict[int, dict] | None:
        """Fetch all open orders from the API, store them by oid => data."""
        try:
            raw = self.info.open_orders(self.address)
            results = {}
            for o in raw:
                oid = o.get("oid")
                if oid is None:
                    continue
                results[oid] = o
            return results
        except Exception as e:
            self._log(f"Exception fetching open_orders => {e}")
            return None

    def load_open_orders(self) -> None:
        """
        Optional: fill self.open_orders from remote state.
        If you call this at startup, you'll sync local open_orders to the exchange side.
        """
        try:
            raw = self.info.open_orders(self.address)
        except Exception as exc:
            self._log(f"Failed to fetch open orders => {exc}")
            return
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

    # ------------------------------------------------------------------
    def check_fills(self) -> None:
        chain_orders = self._fetch_open_orders()
        if not chain_orders:
            self._log("check_fills => no open orders from exchange or error. skip.")
            self._record_fills()
            return

        # walk local open_orders
        for oid, info in list(self.open_orders.items()):
            chain_info = chain_orders.get(oid)
            if chain_info:
                remain = float(chain_info.get("sz", 0.0))
                if remain < info["size"]:
                    filled = info["size"] - remain
                    self._log(f"Partial fill => oid={oid}, side={info['side']}, filled={filled}, remain={remain}")
                    self.open_orders[oid]["size"] = remain
                    # Opposite side => place a new 20$ order
                    mid = self._mid_price()
                    if mid:
                        new_side = "sell" if info["side"]=="buy" else "buy"
                        px = self._round_price(mid * (1+self.spread)) if new_side=="sell" else self._round_price(mid*(1-self.spread))
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
                mid = self._mid_price()
                if mid:
                    new_side = "sell" if info["side"]=="buy" else "buy"
                    px = self._round_price(mid*(1+self.spread)) if new_side=="sell" else self._round_price(mid*(1-self.spread))
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
        self.open_orders = {
            oid: v for oid,v in self.open_orders.items() if oid in chain_orders
        }
        # log fills
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

    # ------------------------------------------------------------------
    def ensure_orders(self) -> None:
        """
        Make sure we have exactly 1 buy and 1 sell open, each ~20 USD at +/- spread around mid.
        """
        mid = self._mid_price()
        if not mid:
            if self.debug:
                self._log("ensure_orders => no mid, skip.")
            return

        sides = {o["side"] for o in self.open_orders.values()}

        if "buy" not in sides:
            buy_px = self._round_price(mid*(1-self.spread))
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
            sell_px = self._round_price(mid*(1+self.spread))
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

    # Optional: Du kannst diesen Aufruf weglassen, wenn du NICHT beim Start alles canceln willst
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
        # time.sleep(1)
        self.load_open_orders()

    def run(self) -> None:
        self._log("Bot started => no scheduled auto-close.")
        # Falls du am Start ALLE Orders löschen willst, rufe "cancel_all_open_orders()" hier auf:
        # self.cancel_all_open_orders()

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
        usd_order_size=20.0,   # always ~20$ orders
        spread=0.0004,
        price_tick=1.0,
        debug=True,
        max_order_age=60,
    )
    bot.run()
