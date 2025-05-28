# Hyperliquid Liquidity Bot

This repository contains a simple example bot that provides liquidity on the
Hyperliquid exchange using the bundled Python SDK.

## Checking available markets

Before running the bot you may want to know which trading pairs are supported.
The SDK exposes this via `Info.name_to_coin`:

```python
from hyperliquid.info import Info
info = Info("https://api.hyperliquid.xyz")
print(info.name_to_coin.keys())
```

Running the snippet above will print a dictionary view of all valid market
names. Any of those names can be used when starting the bot.

## Usage

The bot is started by running `main.py`. By default it operates on the
`BTC/USDC` market. You can override this either via a command line argument or
by setting the `MARKET` environment variable.

```bash
# Using an environment variable
MARKET="ETH/USDC" python main.py

# Or using the command line argument
python main.py --market ETH/USDC
```

Make sure you have configured the credentials in `config.py` before starting the
bot.
