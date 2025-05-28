# hyperliquid.py
import time
import hmac
import hashlib
import requests
from config import API_KEY, API_SECRET, BASE_URL

def sign(payload):
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def place_order(coin, side, size, price, reduce_only=False):
    url = f"{BASE_URL}/api/v1/placeOrder"
    timestamp = str(int(time.time() * 1000))

    body = {
        "coin": coin,
        "side": side,  # "buy" oder "sell"
        "orderType": "limit",
        "size": size,
        "price": price,
        "reduceOnly": reduce_only
    }

    message = timestamp + API_KEY + str(body)
    signature = sign(message)

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": API_KEY,
        "X-SIGNATURE": signature,
        "X-TIMESTAMP": timestamp
    }

    response = requests.post(url, json=body, headers=headers)
    return response.json()
