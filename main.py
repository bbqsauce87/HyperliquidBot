import json
import random
import threading
import time
import logging

import requests
import websocket
from eth_account import Account
from eth_account.messages import encode_defunct

from config import WALLET_ADDRESS, WALLET_PRIVATE_KEY, BASE_URL, RPC_URL


class HyperliquidClient:
    """Minimal RPC client for Hyperliquid spot trading."""

    def __init__(self, address: str, private_key: str, rpc_url: str):
        self.address = address
        self.private_key = private_key
        self.rpc_url = rpc_url

    # internal helpers -------------------------------------------------
    def _sign_payload(self, method: str, params: dict) -> tuple[str, int]:
        timestamp = int(time.time())
        message = f"{self.address}-{method}-{json.dumps(params, sort_keys=True)}-{timestamp}"
        msg = encode_defunct(text=message)
        signed = Account.sign_message(msg, self.private_key)
        return signed.signature.hex(), timestamp

    def _rpc_call(self, method: str, params: dict):
        signature, timestamp = self._sign_payload(method, params)
        body = {
            "method": method,
            "params": {**params, "signature": signature, "timestamp": timestamp},
            "id": 1,
        }
        resp = requests.post(self.rpc_url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        return data.get("result")

    # public API -------------------------------------------------------
    def place_order(self, market: str, side: str, price: float, size: float) -> str | None:
        params = {
            "market": market,
            "side": side,
            "price": float(price),
            "size": float(size),
            "sender": self.address,
        }
        result = self._rpc_call("placeSpotOrder", params)
        return result.get("orderId") if result else None

    def cancel_order(self, order_id: str) -> None:
        params = {"orderId": order_id, "sender": self.address}
        self._rpc_call("cancelSpotOrder", params)

    def get_open_orders(self) -> list:
        params = {"sender": self.address}
        result = self._rpc_call("getOpenSpotOrders", params)
        return result if result else []


class SpotLiquidityBot:
    """Simple market making bot for Hyperliquid spot markets."""

    def __init__(
        self,
        client: HyperliquidClient,
        market: str = "BTC-USDC",
        size_min: float = 5,
        size_max: float = 10,
        spread: float = 0.0002,
        check_interval: int = 5,
        log_file: str = "trade_log.txt",
    ) -> None:
        self.client = client
        self.market = market
        self.size_min = size_min
        self.size_max = size_max
        self.spread = spread
        self.check_interval = check_interval
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.open_orders: dict[str, dict] = {}
        self.ws: websocket.WebSocketApp | None = None
        self.lock = threading.Lock()

        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="[%(asctime)s] %(message)s",
        )
        self.logger = logging.getLogger("bot")

    # logging helper ---------------------------------------------------
    def _log(self, msg: str) -> None:
        print(msg)
        self.logger.info(msg)

    # websocket handling -----------------------------------------------
    def _on_open(self, ws):
        self._log("WebSocket connected")
        subscribe = {
            "method": "subscribe",
            "topics": [f"orderbook.{self.market}"],
            "id": 1,
        }
        ws.send(json.dumps(subscribe))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            ob = data.get("data")
            if ob and "bids" in ob and "asks" in ob:
                with self.lock:
                    self.best_bid = float(ob["bids"][0][0])
                    self.best_ask = float(ob["asks"][0][0])
        except Exception as e:
            self._log(f"Error parsing WebSocket message: {e}")

    def _on_error(self, ws, error):
        self._log(f"WebSocket error: {error}")

    def _on_close(self, ws, status, msg):
        self._log(f"WebSocket closed: {status}, {msg}")

    def start_ws(self):
        ws_url = f"{BASE_URL.replace('https', 'wss')}/ws"
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        t = threading.Thread(target=self.ws.run_forever, daemon=True)
        t.start()

    # strategy helpers -------------------------------------------------
    def _mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def _random_size(self) -> float:
        return round(random.uniform(self.size_min, self.size_max), 6)

    # order management -------------------------------------------------
    def check_fills(self):
        chain_orders = {o["orderId"]: o for o in self.client.get_open_orders()}
        for oid, info in list(self.open_orders.items()):
            if oid not in chain_orders:
                self._log(
                    f"Order filled: id={oid}, side={info['side']}, price={info['price']}, size={info['size']}"
                )
                mid = self._mid_price()
                if mid:
                    if info["side"].lower() == "buy":
                        price = round(mid * (1 + self.spread), 4)
                        new_id = self.client.place_order(self.market, "sell", price, info["size"])
                        if new_id:
                            chain_orders[new_id] = {
                                "side": "sell",
                                "price": price,
                                "size": info["size"],
                            }
                    else:
                        price = round(mid * (1 - self.spread), 4)
                        new_id = self.client.place_order(self.market, "buy", price, info["size"])
                        if new_id:
                            chain_orders[new_id] = {
                                "side": "buy",
                                "price": price,
                                "size": info["size"],
                            }
                self.open_orders.pop(oid, None)
        self.open_orders = chain_orders

    def ensure_orders(self):
        mid = self._mid_price()
        if mid is None:
            return
        sides = {info["side"].lower() for info in self.open_orders.values()}
        if "buy" not in sides:
            price = round(mid * (1 - self.spread), 4)
            size = self._random_size()
            oid = self.client.place_order(self.market, "buy", price, size)
            if oid:
                self.open_orders[oid] = {"side": "buy", "price": price, "size": size}
        if "sell" not in sides:
            price = round(mid * (1 + self.spread), 4)
            size = self._random_size()
            oid = self.client.place_order(self.market, "sell", price, size)
            if oid:
                self.open_orders[oid] = {"side": "sell", "price": price, "size": size}

    # main loop --------------------------------------------------------
    def run(self):
        self.start_ws()
        while True:
            time.sleep(self.check_interval)
            with self.lock:
                self.ensure_orders()
            self.check_fills()


if __name__ == "__main__":
    client = HyperliquidClient(WALLET_ADDRESS, WALLET_PRIVATE_KEY, RPC_URL)
    bot = SpotLiquidityBot(client)
    bot.run()
