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

3. Start the bot using the provided script which sets up a virtual
   environment and runs `main.py`:

```bash
./run.sh
```
