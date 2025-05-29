
# Hyperliquid Trading Bot

This repository contains a simple trading bot built on top of the
[hyperliquid-python-sdk](./hyperliquid-python-sdk).

## Setup

1. Ensure Python 3.12 is available.
2. Install dependencies from the bundled SDK:

```bash
pip install -e hyperliquid-python-sdk
```

3. Export your wallet credentials before running the bot:

```bash
export WALLET_PRIVATE_KEY=<your_private_key>
export WALLET_ADDRESS=<your_wallet_address>
```

`run.sh` prüft diese Variablen beim Start und bricht mit einer Fehlermeldung
ab, falls sie fehlen.

Optional environment variables:

```bash
export BASE_URL=https://api.hyperliquid.xyz      # default value
```

`BASE_URL` should only contain the root API domain. **Do not** append `/rpc` or
any other suffix—using the wrong URL leads to `404` errors when fetching
`openOrders`.

Example values for mainnet:

```bash
export BASE_URL=https://api.hyperliquid.xyz
```

And for testnet:

```bash
# export BASE_URL=https://api.hyperliquid-testnet.xyz
```

If your environment sets `http_proxy` or `https_proxy`, unset them before
running the bot so requests go directly to the Hyperliquid API:

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

4. Start the bot using the provided script which sets up a virtual
   environment and runs `main.py`:

```bash
./run.sh
```

# Hyperliquid Liquidity Bot

This repository contains a simple example bot that provides liquidity on the
Hyperliquid exchange using the bundled Python SDK.

## Checking available markets

The SDK exposes available markets via `Info.name_to_coin`:

```python
from hyperliquid.info import Info
info = Info("https://api.hyperliquid.xyz")
print(info.name_to_coin.keys())
```

The bot itself is locked to the `UBTC/USDC` pair, but you may find the snippet
above useful for reference.

To quickly confirm your credentials and `BASE_URL` are set up correctly, try
retrieving your open orders:

```python
from hyperliquid.info import Info

info = Info(BASE_URL, skip_ws=True)
print(info.open_orders(WALLET_ADDRESS))
```

If this prints a list (even an empty one) then the Hyperliquid API is
reachable.  See
[`hyperliquid-python-sdk/examples/cancel_open_orders.py`](hyperliquid-python-sdk/examples/cancel_open_orders.py)
for a more complete example that cancels any resting orders.

## Usage

The bot is started by running `main.py` and exclusively trades the
`UBTC/USDC` spot pair.

```bash
python main.py
```

Make sure you have configured the credentials in `config.py` before starting the
bot.

## Adjusting order size

The bot sizes orders by USD value using the `usd_order_size` parameter.
For each quote the base asset amount is computed from the current price
so that the order is worth approximately this many dollars.  The default
is `100`, meaning each order is for about one hundred USDC.

```python
bot = SpotLiquidityBot(usd_order_size=100.0,
                       spread=0.0004,
                       max_order_age=60,
                       price_expiry_threshold=500,
                       extra_sell_levels=2)
```

`extra_sell_levels` controls how many additional sell orders are layered above
the reference price when the market drops. These extra orders help capture
partial profits if the price rebounds after a sharp decline.

`spread=0.0004` means orders are quoted 0.04% away from the mid price
on each side. This small buffer keeps them from filling immediately
while still providing tight liquidity.

## Startup test order

If you want to quickly verify that trading works, you can instruct the bot to
place a small limit order as soon as it starts. Provide `start_order_price` and
`start_order_size` when constructing the bot:

```python
bot = SpotLiquidityBot(start_order_price=90000, start_order_size=0.001)
```

The bot normally starts **without** a test order; the example above would submit
a buy for `0.001` BTC at `90,000` USDC right after launch.

On every startup the bot also cleans up any open orders that may still
be resting on the exchange. This ensures stale orders don't consume
capital before new quotes are placed. The `cancel_all_open_orders` helper now
sends a single `bulk_cancel` request so **all** markets are cleared, mirroring
the "Cancel All" button in the Hyperliquid UI. After the cleanup the bot
refreshes its internal state with any orders that remain so the
expiration timer works even across restarts.

## Repricing behaviour

Orders are periodically repriced when the mid price drifts too far from the
original order price. The `reprice_threshold` parameter uses a fixed
default value of `0.005` (~0.5% away from the original price).

When `dynamic_reprice_on_bbo` is enabled, cancelled orders are replaced
immediately after a best bid/offer update instead of waiting for the normal
`check_interval` loop.

## Order expiration

Each order is tagged with a timestamp when placed. If an order remains open
longer than `max_order_age` seconds (default: `60`) **and** the mid price has
moved at least `$500` away from the order's price, it will be cancelled on the
next iteration of the main loop. The `$500` value can be adjusted via the
`price_expiry_threshold` parameter.


## Running tests

Install the Python dependencies using `requirements.txt` and the additional
packages required for tests listed in `requirements-dev.txt`:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

Then execute the test suite:

```bash
pytest
```

## Cancel all outstanding UBTC/USDC orders

`cancel_orders.py` removes any open orders resting on the UBTC/USDC spot market. It
only touches this specific market and leaves all others intact.

Run it like any other helper script:

```bash
python cancel_orders.py
```

`WALLET_PRIVATE_KEY`, `WALLET_ADDRESS` and, optionally, `BASE_URL` must be set in
your environment as described above.

