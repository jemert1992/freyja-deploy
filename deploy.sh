#!/bin/bash
set -e
echo "══════════════════════════════════════════════════════════"
echo "  FREYJA QUANT ENGINE v3 — WEATHER MODULE DEPLOY"
echo "  Pulling from GitHub..."
echo "══════════════════════════════════════════════════════════"

REPO="https://raw.githubusercontent.com/jemert1992/freyja-deploy/main"
BOT_DIR="/root/kalshi-bot"
DASH_DIR="/root/kalshi-bot/dashboard"

echo "[1/10] Stopping services..."
systemctl stop kalshi-bot || true
systemctl stop freyja-api || true
sleep 2

echo "[2/10] Downloading weather_config.py..."
curl -sL "$REPO/bot/weather_config.py" -o "$BOT_DIR/weather_config.py"

echo "[3/10] Downloading weather_strategy.py..."
curl -sL "$REPO/bot/weather_strategy.py" -o "$BOT_DIR/weather_strategy.py"

echo "[4/10] Downloading weather_scanner.py..."
curl -sL "$REPO/bot/weather_scanner.py" -o "$BOT_DIR/weather_scanner.py"

echo "[5/10] Downloading updated bot.py..."
curl -sL "$REPO/bot/bot.py" -o "$BOT_DIR/bot.py"

echo "[6/10] Downloading updated api_server.py..."
curl -sL "$REPO/bot/api_server.py" -o "$BOT_DIR/api_server.py"

echo "[7/10] Downloading updated dashboard..."
curl -sL "$REPO/dashboard/index.html" -o "$DASH_DIR/index.html"
curl -sL "$REPO/dashboard/style.css" -o "$DASH_DIR/style.css"
curl -sL "$REPO/dashboard/app.js" -o "$DASH_DIR/app.js"

echo "[8/10] Updating .env with weather config..."
ENV_FILE="$BOT_DIR/.env"
# Add weather config if not already present
grep -q "WEATHER_ENABLED" "$ENV_FILE" || cat >> "$ENV_FILE" << 'ENVBLOCK'

# ── Weather Module Config ────────────────────────
WEATHER_ENABLED=true
WEATHER_PAPER_MODE=true
WEATHER_MIN_EDGE=0.08
WEATHER_KELLY_FRACTION=0.25
WEATHER_MIN_BALANCE=5.0
WEATHER_MAX_POSITION=10.0
WEATHER_SCAN_INTERVAL=300
WEATHER_SIGMA_INFLATION=1.8
ENVBLOCK

echo "[9/10] Restarting services..."
systemctl start freyja-api
sleep 2
systemctl start kalshi-bot
sleep 3

echo "[10/10] Verifying deployment..."
API_RESPONSE=$(curl -s --max-time 10 -H "Authorization: Bearer freyja-ctrl-2026" http://localhost:8080/api/weather || echo "FAILED")
echo "Weather API response: $API_RESPONSE"

BOT_STATUS=$(systemctl is-active kalshi-bot)
API_STATUS=$(systemctl is-active freyja-api)
echo ""
echo "══════════════════════════════════════════════════════════"
echo "  DEPLOY COMPLETE"
echo "  Bot service: $BOT_STATUS"
echo "  API service: $API_STATUS"
echo "  Dashboard: http://143.244.141.197:8080/"
echo "  Weather tab available in the dashboard"
echo "══════════════════════════════════════════════════════════"
