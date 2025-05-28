
# Hyperliquid Trading Bot

This repository contains a simple trading bot built on top of the
[hyperliquid-python-sdk](./hyperliquid-python-sdk).

## Setup

1. Ensure Python 3.12 is available.
2. Export your wallet credentials before running the bot:

```bash
export WALLET_PRIVATE_KEY=<your_private_key>
export WALLET_ADDRESS=<your_wallet_address>
```

Optional environment variables:

```bash
export BASE_URL=https://api.hyperliquid.xyz      # default value
export RPC_URL=https://api.hyperliquid.xyz/rpc   # default value
```

If your environment sets `http_proxy` or `https_proxy`, unset them before
running the bot so requests go directly to the Hyperliquid API:

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

3. Start the bot using the provided script which sets up a virtual
   environment and runs `main.py`:

```bash
./run.sh
```
=======
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

## Usage

The bot is started by running `main.py` and exclusively trades the
`UBTC/USDC` spot pair.

```bash
python main.py
```

Make sure you have configured the credentials in `config.py` before starting the
bot.

