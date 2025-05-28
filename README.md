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
