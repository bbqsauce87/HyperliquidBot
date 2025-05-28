#!/bin/bash

set -e

# === 🧾 Konfiguration ===
PYTHON_VERSION="python3.12"
VENV_DIR=".venv"
LOG_DIR="logs"

# === 🔐 Wallet-Zugangsdaten ===
export WALLET_PRIVATE_KEY="0x9766a78dfde13e3427b74ae751d1386b4c7319bc6bfef6e9bc21a31f6e4bfb3c"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export WALLET_ADDRESS="0x2604a13f7e643b8f5b3c894d6023d6de8c4e1682"

# === ⚙️ Virtuelle Umgebung erstellen (falls nicht vorhanden) ===
if [ ! -d "$VENV_DIR" ]; then
  echo "[INFO] Erstelle virtuelle Umgebung mit $PYTHON_VERSION..."
  $PYTHON_VERSION -m venv $VENV_DIR
  echo "[INFO] Virtuelle Umgebung erstellt."
fi

# === 📁 Log-Verzeichnis vorbereiten ===
mkdir -p $LOG_DIR

# === 🚀 Starte den Trading-Bot ===
echo "[INFO] Starte Trading-Bot..."
$VENV_DIR/bin/python main.py
