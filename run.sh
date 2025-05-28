#!/bin/bash

set -e

# === Konfiguration ===
PYTHON_VERSION="python3.12"
VENV_DIR=".venv"
LOG_DIR="logs"

# Proxy-Umgebungsvariablen deaktivieren, damit Requests direkt an die
# Hyperliquid-API gesendet werden können.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# === Virtuelle Umgebung erstellen (falls nicht vorhanden) ===
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "[INFO] Erstelle virtuelle Umgebung mit $PYTHON_VERSION..."
  rm -rf "$VENV_DIR"
  $PYTHON_VERSION -m venv "$VENV_DIR"
  echo "[INFO] Virtuelle Umgebung erstellt."
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install -e hyperliquid-python-sdk >/dev/null
fi

# === Logs-Ordner erstellen (falls nicht vorhanden) ===
mkdir -p $LOG_DIR

# === Trading-Bot starten ===
echo "[INFO] Starte Trading-Bot..."
$VENV_DIR/bin/python main.py
