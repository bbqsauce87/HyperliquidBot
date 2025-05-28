#!/bin/bash

set -e

# === Konfiguration ===
PYTHON_VERSION="python3.12"
VENV_DIR=".venv"
LOG_DIR="logs"

# === Virtuelle Umgebung erstellen (falls nicht vorhanden) ===
if [ ! -d "$VENV_DIR" ]; then
  echo "[INFO] Erstelle virtuelle Umgebung mit $PYTHON_VERSION..."
  $PYTHON_VERSION -m venv $VENV_DIR
  echo "[INFO] Virtuelle Umgebung erstellt."
fi

# === Logs-Ordner erstellen (falls nicht vorhanden) ===
mkdir -p $LOG_DIR

# === Trading-Bot starten ===
echo "[INFO] Starte Trading-Bot..."
$VENV_DIR/bin/python main.py
