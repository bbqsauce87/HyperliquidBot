import json
import time
import threading
import random
import requests
import websocket

from datetime import datetime

from eth_account.messages import encode_defunct
from eth_account.account import Account
from eth_account import Account as EthAccount  # Optional: Für Schlüsselerzeugung/Verwaltung
from config import (
    WALLET_PRIVATE_KEY,
    WALLET_ADDRESS,
    BASE_URL,
    RPC_URL,
)

class HyperliquidLiquidityBot:
    def __init__(
        self,
        trading_pair="BTC-USDC",
        position_size_min=50,
        position_size_max=100,
        spread_offset=0.0002,
        log_file="trade_log.txt"
    ):
        """
        trading_pair: z. B. "BTC-USDC" oder "ETH-USDC"
        position_size_min, position_size_max: USDC-Ordergröße (Random in diesem Intervall)
        spread_offset: Prozentsatz über/unter dem Midprice (0.0002 = 0.02%)
        """
        self.trading_pair = trading_pair
        self.position_size_min = position_size_min
        self.position_size_max = position_size_max
        self.spread_offset = spread_offset
        self.log_file = log_file

        # Orderbuch-Daten
        self.best_bid = None
        self.best_ask = None

        # Offene Orders (lokales Tracking)
        # order_id -> { "side": str, "price": float, "size": float }
        self.open_orders = {}

        # WebSocket
        self.ws = None
        self.lock = threading.Lock()

    #########################
    # Signatur-Helfer
    #########################
    def sign_payload(self, method: str, params: dict) -> (str, int):
        """
        Erzeuge eine (vereinfachte) Signatur für unseren RPC-Aufruf.
        - timestamp: wird ebenfalls signiert, damit jeder Call eindeutig ist
        - Die echte Implementierung hängt vom Hyperliquid-Agent-Wallet-Schema ab.
        """
        timestamp = int(time.time())
        # Beispielhafter "Message String":
        # Du kannst hier ein eigenes, sicheres Schema verwenden (z. B. EIP-712).
        # Hier nur ein simples "concatenate + sign".
        message_str = f"{WALLET_ADDRESS}-{method}-{json.dumps(params, sort_keys=True)}-{timestamp}"
        msg = encode_defunct(text=message_str)

        signed_message = Account.sign_message(msg, private_key=WALLET_PRIVATE_KEY)
        signature = signed_message.signature.hex()

        return signature, timestamp

    #########################
    # Logging
    #########################
    def log_action(self, message: str):
        """Loggt Aktionen in eine Textdatei mit Zeitstempel."""
        timestamp = datetime.utcnow().isoformat()
        out = f"[{timestamp}] {message}\n"
        print(out, end="")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(out)

    #########################
    # WebSocket-Handling
    #########################
    def start_websocket(self):
        ws_url = f"wss://api.hyperliquid.xyz/ws"  # <-- das hier ist korrekt eingerückt
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        wst = threading.Thread(target=self.ws.run_forever, daemon=True)
        wst.start()



    def on_open(self, ws):
        self.log_action("WebSocket connected.")
        # Exemplarisches RPC-Subscribe
        subscribe_message = {
            "method": "subscribe",
            "topics": [f"orderbook.{self.trading_pair}"],
            "id": 1,
        }
        ws.send(json.dumps(subscribe_message))

    def on_message(self, ws, message):
        """Wird aufgerufen bei neuen Nachrichten (Orderbuch-Updates, etc.)."""
        try:
            data = json.loads(message)

            # Neue Struktur beachten: "data" enthält direkt das Orderbuch
            if "data" in data:
                orderbook = data["data"]
                if "bids" in orderbook and "asks" in orderbook:
                    self.handle_orderbook_update(orderbook)

        except Exception as e:
            self.log_action(f"Error parsing WebSocket message: {e}")

    def on_error(self, ws, error):
        self.log_action(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.log_action(f"WebSocket closed: {close_status_code}, {close_msg}")

    def handle_orderbook_update(self, orderbook):
        """Aktualisiere best_bid/best_ask aus dem empfangenen Orderbuch-Objekt."""
        with self.lock:
            # Annahme: orderbook["bids"], orderbook["asks"] => Listen [ [price, size], ...]
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])

            if len(bids) > 0:
                self.best_bid = float(bids[0][0])
            if len(asks) > 0:
                self.best_ask = float(asks[0][0])

    #########################
    # RPC-Methoden
    #########################
    def place_spot_order(self, side, price, size):
        """
        placeSpotOrder via RPC. 
        side: "buy" / "sell"
        price, size: float
        """
        method = "placeSpotOrder"
        param_dict = {
            "market": self.trading_pair,
            "side": side,
            "price": float(price),
            "size": float(size),
            "sender": WALLET_ADDRESS
        }

        signature, timestamp = self.sign_payload(method, param_dict)
        param_dict["signature"] = signature
        param_dict["timestamp"] = timestamp

        body = {
            "method": method,
            "params": param_dict,
            "id": 1
        }

        try:
            resp = requests.post(RPC_URL, json=body, timeout=5)
            if resp.status_code == 200:
                result = resp.json()
                # result könnte so aussehen: {"result":{"orderId":"abc123"...}, "id":1}
                # Ggf. auf "error" prüfen
                if "error" in result:
                    err_msg = result["error"]
                    self.log_action(f"placeSpotOrder error: {err_msg}")
                    return None

                res = result.get("result", {})
                order_id = res.get("orderId", None)
                if order_id:
                    self.open_orders[order_id] = {
                        "side": side,
                        "price": price,
                        "size": size
                    }
                    self.log_action(f"Placed {side} limit order: orderId={order_id}, price={price}, size={size}")
                    return order_id
                else:
                    self.log_action(f"placeSpotOrder no orderId in result: {res}")
                    return None
            else:
                self.log_action(f"placeSpotOrder HTTP Error {resp.status_code}: {resp.text}")
                return None
        except Exception as e:
            self.log_action(f"Exception in place_spot_order: {e}")
            return None

    def cancel_spot_order(self, order_id):
        """cancelSpotOrder via RPC."""
        method = "cancelSpotOrder"
        param_dict = {
            "orderId": order_id,
            "sender": WALLET_ADDRESS
        }
        signature, timestamp = self.sign_payload(method, param_dict)
        param_dict["signature"] = signature
        param_dict["timestamp"] = timestamp

        body = {
            "method": method,
            "params": param_dict,
            "id": 1
        }

        try:
            resp = requests.post(RPC_URL, json=body, timeout=5)
            if resp.status_code == 200:
                result = resp.json()
                if "error" in result:
                    err_msg = result["error"]
                    self.log_action(f"cancelSpotOrder error: {err_msg}")
                    return

                self.log_action(f"Cancelled order: {order_id}")
                if order_id in self.open_orders:
                    del self.open_orders[order_id]
            else:
                self.log_action(f"cancelSpotOrder HTTP Error {resp.status_code}: {resp.text}")
        except Exception as e:
            self.log_action(f"Exception in cancel_spot_order: {e}")

    def get_open_spot_orders(self):
        """getOpenSpotOrders via RPC. Gibt eine Liste offener Orders zurück."""
        method = "getOpenSpotOrders"
        param_dict = {
            "sender": WALLET_ADDRESS
        }
        signature, timestamp = self.sign_payload(method, param_dict)
        param_dict["signature"] = signature
        param_dict["timestamp"] = timestamp

        body = {
            "method": method,
            "params": param_dict,
            "id": 1
        }
        try:
            resp = requests.post(RPC_URL, json=body, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data:
                    err_msg = data["error"]
                    self.log_action(f"getOpenSpotOrders error: {err_msg}")
                    return []
                result = data.get("result", [])
                return result  # z. B. [{"orderId": "...", "side": "...", "price": ..., "size": ...}, ...]
            else:
                self.log_action(f"getOpenSpotOrders HTTP Error {resp.status_code}: {resp.text}")
                return []
        except Exception as e:
            self.log_action(f"Exception in get_open_spot_orders: {e}")
            return []

    #########################
    # Strategielogik
    #########################
    def compute_mid_price(self):
        """Berechnet den Mid-Price aus best_bid und best_ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def check_fills(self):
        """
        Prüft, welche Orders nicht mehr offen sind (Lokal vs. getOpenSpotOrders).
        Wenn gefüllt, Gegenseite erneut platzieren.
        """
        open_orders_on_chain = self.get_open_spot_orders()
        open_ids_chain = [o["orderId"] for o in open_orders_on_chain]

        # Bestimme, welche Orders lokal getrackt, aber nicht mehr openOnChain sind
        filled_orders = []
        for local_id in list(self.open_orders.keys()):
            if local_id not in open_ids_chain:
                filled_orders.append(local_id)

        for order_id in filled_orders:
            # Order gefüllt oder geschlossen
            order_data = self.open_orders[order_id]
            side = order_data["side"]
            price = order_data["price"]
            size = order_data["size"]

            self.log_action(f"Order filled: {order_id}, side={side}, price={price}, size={size}")
            del self.open_orders[order_id]

            # Gegenseite platzieren (Beispiel: Buy -> Sell, Sell -> Buy)
            mid_price = self.compute_mid_price()
            if mid_price:
                if side.lower() == "buy":
                    new_price = round(mid_price * (1 + self.spread_offset), 4)
                    self.place_spot_order("sell", new_price, size)
                else:
                    new_price = round(mid_price * (1 - self.spread_offset), 4)
                    self.place_spot_order("buy", new_price, size)

    def _random_position_size(self):
        """Erzeugt eine zufällige Ordergröße zwischen position_size_min und position_size_max."""
        return round(random.uniform(self.position_size_min, self.position_size_max), 6)

    def main_loop(self):
        """Hauptschleife des Bots."""
        # WebSocket starten, um Orderbuch-Updates zu bekommen
        self.start_websocket()

        while True:
            time.sleep(5)  # Poll-Intervall anpassen

            with self.lock:
                mid_price = self.compute_mid_price()
                if mid_price is not None:
                    # Haben wir schon eine offene Buy-Order?
                    has_buy = any(o["side"].lower() == "buy" for o in self.open_orders.values())
                    has_sell = any(o["side"].lower() == "sell" for o in self.open_orders.values())

                    if not has_buy:
                        buy_price = round(mid_price * (1 - self.spread_offset), 4)
                        size = self._random_position_size()
                        self.place_spot_order("buy", buy_price, size)

                    if not has_sell:
                        sell_price = round(mid_price * (1 + self.spread_offset), 4)
                        size = self._random_position_size()
                        self.place_spot_order("sell", sell_price, size)

            # Prüfen, ob Orders gefüllt wurden
            self.check_fills()


if __name__ == "__main__":
    bot = HyperliquidLiquidityBot(
        trading_pair="BTC-USDC",
        position_size_min=5,
        position_size_max=5,
        spread_offset=0.0002,
        log_file="trade_log.txt"
    )
    
    bot.main_loop()
