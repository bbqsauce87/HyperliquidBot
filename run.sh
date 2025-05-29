#!/bin/bash

set -e

# === 🧾 Konfiguration ===
PYTHON_VERSION="python3.12"
VENV_DIR=".venv"
LOG_DIR="logs"

# === 🔐 Wallet-Zugangsdaten ===
# Die notwendigen Umgebungsvariablen müssen bereits gesetzt sein.
if [ -z "$WALLET_PRIVATE_KEY" ] || [ -z "$WALLET_ADDRESS" ]; then
  echo "[ERROR] WALLET_PRIVATE_KEY und WALLET_ADDRESS müssen gesetzt sein." >&2
  echo "Beispiel:" >&2
  echo "  export WALLET_PRIVATE_KEY=0x..." >&2
  echo "  export WALLET_ADDRESS=0x..." >&2
  exit 1
fi
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# === ⚙️ Virtuelle Umgebung erstellen (falls nicht vorhanden) ===
if [ ! -d "$VENV_DIR" ]; then
  echo "[INFO] Erstelle virtuelle Umgebung mit $PYTHON_VERSION..."
  $PYTHON_VERSION -m venv "$VENV_DIR"
  echo "[INFO] Virtuelle Umgebung erstellt."
fi

# === 📦 Abhängigkeiten installieren ===
echo "[INFO] Installiere hyperliquid-python-sdk..."
"$VENV_DIR"/bin/pip install -e hyperliquid-python-sdk

for req_file in requirements*.txt; do
  if [ -f "$req_file" ]; then
    echo "[INFO] Installiere Abhängigkeiten aus $req_file..."
    "$VENV_DIR"/bin/pip install -r "$req_file"
  fi
done

# === 📁 Log-Verzeichnis vorbereiten ===
mkdir -p $LOG_DIR

# === 🚀 Starte den Trading-Bot ===
echo "[INFO] Starte Trading-Bot..."
$VENV_DIR/bin/python main.py
