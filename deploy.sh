#!/bin/bash
set -e
echo "══════════════════════════════════════════════════════════"
echo "  FREYJA QUANT ENGINE v2 — FULL UPGRADE DEPLOY"
echo "  Pulling from GitHub..."
echo "══════════════════════════════════════════════════════════"

REPO="https://raw.githubusercontent.com/jemert1992/freyja-deploy/main"
BOT_DIR="/root/kalshi-bot"
DASH_DIR="/root/kalshi-bot/dashboard"

echo "[1/8] Stopping services..."
systemctl stop kalshi-bot || true
systemctl stop freyja-api || true
sleep 2

echo "[2/8] Downloading bot.py (v2 — crypto killed, arb+journal integrated)..."
curl -sL "$REPO/bot/bot.py" -o "$BOT_DIR/bot.py"

echo "[3/8] Downloading weather modules (ECMWF ensemble upgrade)..."
curl -sL "$REPO/bot/weather_config.py" -o "$BOT_DIR/weather_config.py"
curl -sL "$REPO/bot/weather_strategy.py" -o "$BOT_DIR/weather_strategy.py"
curl -sL "$REPO/bot/weather_scanner.py" -o "$BOT_DIR/weather_scanner.py"
curl -sL "$REPO/bot/ecmwf_forecast.py" -o "$BOT_DIR/ecmwf_forecast.py"

echo "[4/8] Downloading arb scanner..."
curl -sL "$REPO/bot/arb_scanner.py" -o "$BOT_DIR/arb_scanner.py"

echo "[5/8] Downloading trade journal..."
curl -sL "$REPO/bot/trade_journal.py" -o "$BOT_DIR/trade_journal.py"

echo "[6/8] Downloading updated API server (v2 endpoints)..."
curl -sL "$REPO/bot/api_server.py" -o "$BOT_DIR/api_server.py"

echo "[7/8] Restarting services..."
systemctl start freyja-api
sleep 2
systemctl start kalshi-bot
sleep 3

echo "[8/8] Verifying deployment..."
BOT_STATUS=$(systemctl is-active kalshi-bot)
API_STATUS=$(systemctl is-active freyja-api)
echo ""
echo "══════════════════════════════════════════════════════════"
echo "  DEPLOY COMPLETE — FREYJA v2"
echo "  Bot service: $BOT_STATUS"
echo "  API service: $API_STATUS"
echo "  Dashboard: http://143.244.141.197:8080/"
echo "  New endpoints: /api/arb, /api/journal"
echo "══════════════════════════════════════════════════════════"
