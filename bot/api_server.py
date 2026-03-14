#!/usr/bin/env python3
"""
Freyja Quant Engine v2 — Control API Server + Dashboard Host
Runs on the DigitalOcean droplet at 0.0.0.0:8080
Pure Python 3 stdlib — no pip packages required.

Usage:
    python3 api_server.py
    # or
    nohup python3 api_server.py &

Dashboard:
    GET  /              — Dashboard UI (static files from ./dashboard/)
API Endpoints:
    GET  /api/health   — Health check (no auth)
    GET  /api/status   — Bot service status
    POST /api/bot/start|stop|restart — Bot control
    GET  /api/state    — Bot state.json
    GET  /api/log      — Last 200 lines of bot.log
    GET  /api/config   — Current .env config
    POST /api/config   — Update .env config & restart
    GET  /api/balance  — Balance from state.json
    GET  /api/weather  — Weather dashboard data (NWS + ECMWF + markets)
    GET  /api/sports   — Sports module data (ESPN + Kalshi live games)
    GET  /api/arb      — Arb scanner results (v2)
    GET  /api/journal  — Trade journal + calibration stats (v2)
"""

import json
import mimetypes
import os
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error

# ── Configuration ──────────────────────────────────────────────

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
AUTH_TOKEN = "freyja-ctrl-2026"

BOT_DIR = Path("/root/kalshi-bot")
STATE_FILE = BOT_DIR / "state.json"
LOG_FILE = BOT_DIR / "bot.log"
ENV_FILE = BOT_DIR / ".env"
SERVICE_NAME = "kalshi-bot"
DASHBOARD_DIR = BOT_DIR / "dashboard"

# Weather config
NWS_API_BASE_URL = "https://api.weather.gov"
KALSHI_PUBLIC_API = "https://api.elections.kalshi.com/trade-api/v2"

# Allowed static file extensions → MIME types
STATIC_TYPES = {
    '.html': 'text/html',
    '.css':  'text/css',
    '.js':   'application/javascript',
    '.json': 'application/json',
    '.png':  'image/png',
    '.svg':  'image/svg+xml',
    '.ico':  'image/x-icon',
}

# ── Helpers ──────────────────────────────────────────────────

def read_env() -> dict:
    """Parse .env file into a dict, ignoring comments and blanks."""
    config = {}
    if not ENV_FILE.exists():
        return config
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        config[key.strip()] = value.strip()
    return config


def write_env(updates: dict) -> None:
    """Update .env file: preserve comments, update existing keys, append new."""
    lines = []
    existing_keys = set()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    existing_keys.add(key)
                else:
                    lines.append(line)
            else:
                lines.append(line)
    # Append new keys
    for key, value in updates.items():
        if key not in existing_keys:
            lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def get_service_status() -> dict:
    """Get systemd service status for kalshi-bot."""
    result = {"service": SERVICE_NAME}
    try:
        active = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=5
        )
        result["status"] = active.stdout.strip()
    except Exception as e:
        result["status"] = "unknown"
        result["error"] = str(e)

    # Get PID and uptime
    try:
        show = subprocess.run(
            ["systemctl", "show", SERVICE_NAME,
             "--property=MainPID,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        for line in show.stdout.strip().splitlines():
            if line.startswith("MainPID="):
                result["pid"] = int(line.split("=", 1)[1])
            elif line.startswith("ActiveEnterTimestamp="):
                ts = line.split("=", 1)[1].strip()
                result["active_since"] = ts
    except Exception:
        pass

    result["ts"] = time.time()
    return result


def service_action(action: str) -> dict:
    """Start, stop, or restart the bot service."""
    try:
        proc = subprocess.run(
            ["systemctl", action, SERVICE_NAME],
            capture_output=True, text=True, timeout=15
        )
        return {
            "action": action,
            "success": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "ts": time.time(),
        }
    except Exception as e:
        return {"action": action, "success": False, "error": str(e), "ts": time.time()}


def read_state() -> dict:
    """Read state.json."""
    if not STATE_FILE.exists():
        return {"error": "state.json not found"}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        return {"error": f"Failed to parse state.json: {e}"}


def read_log(lines: int = 200) -> str:
    """Read last N lines of bot.log."""
    if not LOG_FILE.exists():
        return ""
    try:
        proc = subprocess.run(
            ["tail", "-n", str(lines), str(LOG_FILE)],
            capture_output=True, text=True, timeout=5
        )
        return proc.stdout
    except Exception:
        # Fallback: read entire file and slice
        try:
            all_lines = LOG_FILE.read_text().splitlines()
            return "\n".join(all_lines[-lines:])
        except Exception:
            return ""


def get_balance() -> dict:
    """Get balance computed from state.json trade history and positions."""
    state = read_state()
    if "error" in state:
        return state

    # Compute from trade history (matches bot's state.py schema)
    trades = state.get("trades", [])
    positions = state.get("positions", {})
    daily_pnl = state.get("daily_pnl", {})

    # Read paper balance setting from .env
    env = read_env()
    starting_balance = float(env.get("PAPER_STARTING_BALANCE", "100.0"))

    # Total realized P&L from all trades
    total_pnl = sum(t.get("pnl_dollars", 0) for t in trades)

    # Cost locked in open positions
    locked = 0.0
    for ticker, pos in positions.items():
        entry = pos.get("entry_price", 0)
        contracts = pos.get("contracts", 0)
        locked += (entry / 100.0) * contracts

    # Unrealized P&L from open positions
    unrealized = 0.0
    for ticker, pos in positions.items():
        entry = pos.get("entry_price", 0)
        current = pos.get("current_price", entry)
        contracts = pos.get("contracts", 0)
        if current is not None:
            unrealized += ((current - entry) / 100.0) * contracts

    cash = starting_balance + total_pnl - locked
    portfolio = cash + locked + unrealized

    # Today's P&L
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_pnl = daily_pnl.get(today, 0.0)

    # Win rate
    wins = sum(1 for t in trades if t.get("pnl_dollars", 0) > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    return {
        "portfolio_value": round(portfolio, 2),
        "cash_balance": round(cash, 2),
        "total_pnl": round(total_pnl, 2),
        "session_pnl": round(session_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "locked_in_positions": round(locked, 2),
        "starting_balance": starting_balance,
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "open_positions": len(positions),
        "ts": time.time(),
    }


# ── Weather Helpers ────────────────────────────────────────────────

import math
import re

WEATHER_CITIES = {
    "NY":  {"name": "New York",      "lat": 40.7128, "lon": -74.0060, "sigma": 2.5},
    "CHI": {"name": "Chicago",       "lat": 41.8781, "lon": -87.6298, "sigma": 3.0},
    "MIA": {"name": "Miami",         "lat": 25.7617, "lon": -80.1918, "sigma": 2.0},
    "AUS": {"name": "Austin",        "lat": 30.2672, "lon": -97.7431, "sigma": 2.5},
    "LA":  {"name": "Los Angeles",   "lat": 34.0522, "lon": -118.2437, "sigma": 2.0},
    "PHX": {"name": "Phoenix",       "lat": 33.4484, "lon": -112.0740, "sigma": 3.0},
    "MSP": {"name": "Minneapolis",   "lat": 44.9778, "lon": -93.2650, "sigma": 3.5},
    "SF":  {"name": "San Francisco", "lat": 37.7749, "lon": -122.4194, "sigma": 2.0},
    "LV":  {"name": "Las Vegas",     "lat": 36.1699, "lon": -115.1398, "sigma": 3.0},
    "SEA": {"name": "Seattle",       "lat": 47.6062, "lon": -122.3321, "sigma": 2.5},
    "ATL": {"name": "Atlanta",       "lat": 33.7490, "lon": -84.3880, "sigma": 2.5},
    "BOS": {"name": "Boston",        "lat": 42.3601, "lon": -71.0589, "sigma": 2.8},
    "DAL": {"name": "Dallas",        "lat": 32.7767, "lon": -96.7970, "sigma": 2.5},
    "DC":  {"name": "Washington DC", "lat": 38.9072, "lon": -77.0369, "sigma": 2.5},
    "NO":  {"name": "New Orleans",   "lat": 29.9511, "lon": -90.0715, "sigma": 2.2},
    "SA":  {"name": "San Antonio",   "lat": 29.4241, "lon": -98.4936, "sigma": 2.5},
    "HOU": {"name": "Houston",       "lat": 29.7604, "lon": -95.3698, "sigma": 2.2},
    "OKC": {"name": "Oklahoma City", "lat": 35.4676, "lon": -97.5164, "sigma": 3.0},
}

_nws_grid_cache = {}
_nws_forecast_cache = {}
_weather_market_cache = {}


def _nws_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "FreyjaQuantEngine/1.0 (tech@freyjafinancialgroup.net)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _kalshi_public_get(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "FreyjaQuantEngine/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm_cdf(x):
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x_abs)
    y = 1.0 - (
        ((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
         - 0.284496736) * t + 0.254829592
    ) * t * math.exp(-x_abs * x_abs)
    erf_val = sign * y
    return 0.5 * (1.0 + erf_val)


def get_nws_forecast(city_code):
    now = time.time()
    if city_code in _nws_forecast_cache:
        ts, data = _nws_forecast_cache[city_code]
        if now - ts < 1800:
            return data

    city = WEATHER_CITIES.get(city_code)
    if not city:
        return None

    if city_code not in _nws_grid_cache:
        try:
            points = _nws_get(f"{NWS_API_BASE_URL}/points/{city['lat']:.4f},{city['lon']:.4f}")
            props = points.get("properties", {})
            _nws_grid_cache[city_code] = {
                "office": props.get("gridId", ""),
                "gridX": props.get("gridX", 0),
                "gridY": props.get("gridY", 0),
                "forecast_url": props.get("forecast", ""),
            }
        except Exception as e:
            print(f"[Weather] NWS grid resolve failed for {city_code}: {e}")
            return None

    grid = _nws_grid_cache[city_code]
    forecast_url = grid.get("forecast_url")
    if not forecast_url:
        forecast_url = (f"{NWS_API_BASE_URL}/gridpoints/{grid['office']}/"
                        f"{grid['gridX']},{grid['gridY']}/forecast")

    try:
        data = _nws_get(forecast_url)
        periods = data.get("properties", {}).get("periods", [])
        daytime = []
        for p in periods:
            if p.get("isDaytime"):
                temp = p.get("temperature")
                unit = p.get("temperatureUnit", "F")
                if temp is not None:
                    if unit == "C":
                        temp = temp * 9 / 5 + 32
                    daytime.append({
                        "name": p.get("name", ""),
                        "date": p.get("startTime", "")[:10],
                        "high_f": temp,
                        "short": p.get("shortForecast", ""),
                        "detail": p.get("detailedForecast", ""),
                        "wind": p.get("windSpeed", ""),
                        "wind_dir": p.get("windDirection", ""),
                    })
        _nws_forecast_cache[city_code] = (now, daytime)
        return daytime
    except Exception as e:
        print(f"[Weather] NWS forecast failed for {city_code}: {e}")
        return None


def get_weather_markets(city_code):
    now = time.time()
    if city_code in _weather_market_cache:
        ts, data = _weather_market_cache[city_code]
        if now - ts < 120:
            return data

    series = f"KXHIGH{city_code}"
    url = f"{KALSHI_PUBLIC_API}/markets?series_ticker={series}&status=open&limit=100"
    try:
        data = _kalshi_public_get(url)
        markets = data.get("markets", [])
        _weather_market_cache[city_code] = (now, markets)
        return markets
    except Exception as e:
        print(f"[Weather] Kalshi markets failed for {city_code}: {e}")
        return []


def get_weather_dashboard_data():
    result = {"cities": {}, "ts": time.time()}

    env = read_env()
    sigma_inflation = float(env.get("WEATHER_SIGMA_INFLATION", "1.8"))
    min_edge = float(env.get("WEATHER_MIN_EDGE", "0.08"))
    active_str = env.get("WEATHER_ACTIVE_CITIES", "ALL")

    result["config"] = {
        "enabled": env.get("WEATHER_ENABLED", "true").lower() in ("1", "true", "yes"),
        "paper_mode": env.get("WEATHER_PAPER_MODE", "true").lower() in ("1", "true", "yes"),
        "min_edge": min_edge,
        "kelly_fraction": float(env.get("WEATHER_KELLY_FRACTION", "0.25")),
        "sigma_inflation": sigma_inflation,
    }

    if active_str.upper() == "ALL":
        active_codes = list(WEATHER_CITIES.keys())
    else:
        active_codes = [c.strip().upper() for c in active_str.split(",") if c.strip()]

    for code in active_codes:
        city = WEATHER_CITIES.get(code)
        if not city:
            continue

        city_data = {
            "name": city["name"], "code": code,
            "sigma_base": city["sigma"],
            "forecast": None, "markets": [],
        }

        forecast = get_nws_forecast(code)
        if forecast:
            city_data["forecast"] = forecast

        markets_raw = get_weather_markets(code)
        if markets_raw and forecast:
            for m in markets_raw:
                title = m.get("title", "")
                yes_ask = m.get("yes_ask")
                no_bid = m.get("no_bid")
                if yes_ask is None and no_bid is not None:
                    yes_ask = 100 - no_bid

                close_time = m.get("close_time", "")
                mkt_date = close_time[:10] if close_time else ""
                matched_fc = next((fc for fc in forecast if fc["date"] == mkt_date), None)

                model_prob = None
                edge = None
                if matched_fc:
                    high = matched_fc["high_f"]
                    sigma = city["sigma"] * sigma_inflation
                    # Strip markdown bold markers (**text**)
                    clean_title = re.sub(r'\*\*', '', title)
                    title_l = clean_title.lower()
                    low, up = None, None

                    # New Kalshi format: >N°, <N°, N-M°
                    mg = re.search(r'[>≥]\s*(\d+)\s*°', clean_title)
                    if mg:
                        low = float(mg.group(1))
                    ml = re.search(r'[<≤]\s*(\d+)\s*°', clean_title)
                    if ml and low is None:
                        up = float(ml.group(1))
                    mr2 = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*°', clean_title)
                    if mr2 and low is None and up is None:
                        low, up = float(mr2.group(1)), float(mr2.group(2))

                    # Old Kalshi format: "X or above", "X or below", "between X and Y"
                    if low is None and up is None:
                        mb = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(?:below|lower)', title_l)
                        if mb:
                            up = float(mb.group(1))
                        ma = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(?:above|higher)', title_l)
                        if ma:
                            low = float(ma.group(1))
                        mr = re.search(r'(?:between\s+)?(\d+)\s*°?\s*f?\s+(?:and|to)\s+(\d+)', title_l)
                        if mr:
                            low, up = float(mr.group(1)), float(mr.group(2))

                    if low is None and up is not None:
                        model_prob = _norm_cdf((up - high) / sigma) if sigma > 0 else 0
                    elif low is not None and up is None:
                        model_prob = 1.0 - _norm_cdf((low - high) / sigma) if sigma > 0 else 0
                    elif low is not None and up is not None:
                        p_above_low = 1.0 - _norm_cdf((low - high) / sigma)
                        p_above_high = 1.0 - _norm_cdf((up + 1 - high) / sigma)
                        model_prob = p_above_low - p_above_high

                    if model_prob is not None and yes_ask is not None:
                        edge = model_prob - (yes_ask / 100.0)

                city_data["markets"].append({
                    "ticker": m.get("ticker", ""),
                    "title": title,
                    "date": mkt_date,
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": m.get("no_ask"),
                    "volume": m.get("volume", 0) or 0,
                    "model_prob": round(model_prob, 3) if model_prob is not None else None,
                    "market_implied": round(yes_ask / 100.0, 3) if yes_ask else None,
                    "edge": round(edge, 3) if edge is not None else None,
                    "forecast_high": matched_fc["high_f"] if matched_fc else None,
                })

        result["cities"][code] = city_data

    return result


# ── HTTP Handler ───────────────────────────────────────────────────

class FreyjaHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Override to print to stdout with timestamp."""
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
              f"{self.client_address[0]} - {format % args}")

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def check_auth(self) -> bool:
        """Check Bearer token. Returns True if authorized."""
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {AUTH_TOKEN}":
            return True
        self.send_json({"error": "Unauthorized"}, 401)
        return False

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    # ── OPTIONS (preflight) ──────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    # ── Static file serving ───────────────────────────────────────

    def serve_static(self, path):
        """Serve dashboard static files. Returns True if handled."""
        # Default to index.html for root or empty path
        if path in ('', '/'):
            path = '/index.html'

        # Only serve from dashboard dir — prevent directory traversal
        clean = path.lstrip('/')
        if '..' in clean:
            return False

        file_path = DASHBOARD_DIR / clean
        if not file_path.is_file():
            return False

        ext = file_path.suffix.lower()
        content_type = STATIC_TYPES.get(ext)
        if content_type is None:
            return False

        try:
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(data)
            return True
        except Exception:
            return False

    # ── GET routes ───────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path

        # Health check — no auth
        if path.rstrip('/') == '/api/health':
            self.send_json({"ok": True, "ts": time.time()})
            return

        # API routes — require auth
        if path.startswith('/api/'):
            if not self.check_auth():
                return

            api_path = path.rstrip('/')
            if api_path == '/api/status':
                self.send_json(get_service_status())
            elif api_path == '/api/state':
                self.send_json(read_state())
            elif api_path == '/api/log':
                self.send_text(read_log(200))
            elif api_path == '/api/config':
                self.send_json(read_env())
            elif api_path == '/api/balance':
                self.send_json(get_balance())
            elif api_path == '/api/weather':
                try:
                    self.send_json(get_weather_dashboard_data())
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            elif api_path.startswith('/api/weather/forecast/'):
                city_code = api_path.split('/')[-1].upper()
                try:
                    forecast = get_nws_forecast(city_code)
                    if forecast:
                        self.send_json({"city": city_code, "forecast": forecast})
                    else:
                        self.send_json({"error": f"No forecast for {city_code}"}, 404)
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            elif api_path.startswith('/api/weather/markets/'):
                city_code = api_path.split('/')[-1].upper()
                try:
                    markets = get_weather_markets(city_code)
                    self.send_json({"city": city_code, "markets": markets})
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            elif api_path == '/api/sports':
                try:
                    self.send_json(get_sports_data())
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            elif api_path == '/api/arb':
                try:
                    self.send_json(get_arb_scan_data())
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            elif api_path == '/api/journal':
                try:
                    self.send_json(get_journal_data())
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            else:
                self.send_json({"error": "Not found"}, 404)
            return

        # Static files — no auth (the dashboard itself)
        if self.serve_static(path):
            return

        # Fallback: serve index.html for SPA-style routing
        if self.serve_static('/index.html'):
            return

        self.send_json({"error": "Not found"}, 404)

    # ── POST routes ───────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if not self.check_auth():
            return

        if path == "/api/bot/start":
            self.send_json(service_action("start"))

        elif path == "/api/bot/stop":
            self.send_json(service_action("stop"))

        elif path == "/api/bot/restart":
            self.send_json(service_action("restart"))

        elif path == "/api/config":
            body = self.read_body()
            try:
                updates = json.loads(body)
            except Exception:
                self.send_json({"error": "Invalid JSON"}, 400)
                return
            write_env(updates)
            # Restart bot to pick up new config
            restart_result = service_action("restart")
            self.send_json({
                "config_updated": True,
                "restart": restart_result,
                "ts": time.time(),
            })

        else:
            self.send_json({"error": "Not found"}, 404)


# ── Arb Scanner Helpers (v2) ───────────────────────────────────────────────────

# ── Sports Data ────────────────────────────────────────────────

def get_sports_data():
    """Fetch live NBA game data from ESPN + Kalshi sports markets."""
    result = {
        "ts": time.time(),
        "live_games": [],
        "all_games": [],
        "markets": {"spread": [], "total": []},
        "module_enabled": True,
    }

    # Step 1: Get live scoreboard from ESPN
    try:
        espn_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        req = urllib.request.Request(espn_url, headers={
            "User-Agent": "Freyja-Sports/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            scoreboard = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        result["espn_error"] = str(e)
        return result

    events = scoreboard.get("events", [])

    for event in events:
        try:
            competitions = event.get("competitions", [{}])
            if not competitions:
                continue
            comp = competitions[0]

            status_obj = event.get("status", {})
            status_type = status_obj.get("type", {})
            state = status_type.get("state", "pre")
            period = status_obj.get("period", 0)
            clock = status_obj.get("displayClock", "0:00")
            status_detail = status_type.get("shortDetail", "")

            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = away = None
            for c in competitors:
                if c.get("homeAway") == "home":
                    home = c
                else:
                    away = c

            if not home or not away:
                continue

            home_score = int(home.get("score", 0))
            away_score = int(away.get("score", 0))
            home_abbr = home.get("team", {}).get("abbreviation", "???")
            away_abbr = away.get("team", {}).get("abbreviation", "???")
            home_name = home.get("team", {}).get("displayName", home_abbr)
            away_name = away.get("team", {}).get("displayName", away_abbr)
            home_logo = home.get("team", {}).get("logo", "")
            away_logo = away.get("team", {}).get("logo", "")
            home_record = home.get("records", [{}])[0].get("summary", "") if home.get("records") else ""
            away_record = away.get("records", [{}])[0].get("summary", "") if away.get("records") else ""

            # Win probability from odds or predictor
            home_wp = 0.5
            odds = comp.get("odds", [])
            if odds:
                for o in odds:
                    ht = o.get("homeTeamOdds", {})
                    if "winPercentage" in ht:
                        home_wp = float(ht["winPercentage"]) / 100.0

            game_data = {
                "game_id": event.get("id", ""),
                "status": state,
                "status_detail": status_detail,
                "period": period,
                "clock": clock,
                "home_team": home_abbr,
                "away_team": away_abbr,
                "home_name": home_name,
                "away_name": away_name,
                "home_logo": home_logo,
                "away_logo": away_logo,
                "home_record": home_record,
                "away_record": away_record,
                "home_score": home_score,
                "away_score": away_score,
                "total_points": home_score + away_score,
                "spread": home_score - away_score,
                "home_win_prob": round(home_wp, 3),
                "venue": comp.get("venue", {}).get("fullName", ""),
            }

            result["all_games"].append(game_data)
            if state == "in":
                result["live_games"].append(game_data)
        except Exception:
            continue

    # Step 2: Get Kalshi spread/total markets for today's games
    for series, key in [("KXNBASPREAD", "spread"), ("KXNBATOTAL", "total")]:
        try:
            mkts_url = f"{KALSHI_PUBLIC_API}/markets?series_ticker={series}&status=open&limit=100"
            mkts_data = _kalshi_public_get(mkts_url)
            for m in mkts_data.get("markets", []):
                vol = m.get("volume", 0)
                result["markets"][key].append({
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "yes_bid": m.get("yes_bid", 0),
                    "yes_ask": m.get("yes_ask", 0),
                    "no_bid": m.get("no_bid", 0),
                    "no_ask": m.get("no_ask", 0),
                    "volume": vol,
                    "open_interest": m.get("open_interest", 0),
                    "last_price": m.get("last_price", 0),
                })
        except Exception as e:
            result[f"{key}_error"] = str(e)

    result["game_count"] = len(result["all_games"])
    result["live_count"] = len(result["live_games"])
    result["spread_markets"] = len(result["markets"]["spread"])
    result["total_markets"] = len(result["markets"]["total"])

    return result


def get_arb_scan_data():
    """Run a quick arb scan using the Kalshi public API and return results."""
    result = {"ts": time.time(), "opportunities": [], "events_scanned": 0}

    try:
        # Fetch active events
        events_url = f"{KALSHI_PUBLIC_API}/events?status=open&limit=100"
        events_data = _kalshi_public_get(events_url)
        events = events_data.get("events", [])
        result["events_scanned"] = len(events)

        profitable_opps = []
        FEE_RATE = 0.07

        for event in events[:100]:
            event_ticker = event.get("event_ticker", "")
            if not event_ticker:
                continue

            try:
                markets_url = f"{KALSHI_PUBLIC_API}/events/{event_ticker}/markets"
                markets_data = _kalshi_public_get(markets_url)
                markets = markets_data.get("markets", [])
            except Exception:
                continue

            if not markets:
                continue

            # Check complement arb (binary)
            if len(markets) == 1:
                m = markets[0]
                ya = m.get("yes_ask")
                na = m.get("no_ask")
                if ya and na and ya > 0 and na > 0:
                    combined = ya + na
                    if combined < 100:
                        gross = 100 - combined
                        fee = gross * FEE_RATE
                        net = gross - fee
                        if net > 0:
                            profitable_opps.append({
                                "type": "complement",
                                "event": event.get("title", event_ticker)[:80],
                                "event_ticker": event_ticker,
                                "tickers": [m.get("ticker", "")],
                                "cost_cents": combined,
                                "net_profit_cents": round(net, 1),
                                "net_profit_pct": round(net / combined * 100, 2),
                            })

            # Check partition arb (multi-outcome)
            elif len(markets) >= 2:
                yes_asks = []
                illiquid = False
                for m in markets:
                    ya = m.get("yes_ask")
                    if ya is None or ya <= 0:
                        illiquid = True
                        break
                    yes_asks.append(ya)

                if not illiquid and yes_asks:
                    combined = sum(yes_asks)
                    if combined < 100:
                        gross = 100 - combined
                        fee = gross * FEE_RATE
                        net = gross - fee
                        if net > 0:
                            profitable_opps.append({
                                "type": "partition",
                                "event": event.get("title", event_ticker)[:80],
                                "event_ticker": event_ticker,
                                "tickers": [m.get("ticker", "") for m in markets],
                                "cost_cents": combined,
                                "net_profit_cents": round(net, 1),
                                "net_profit_pct": round(net / combined * 100, 2),
                                "num_outcomes": len(markets),
                            })

            # Rate limit
            time.sleep(0.05)

        profitable_opps.sort(key=lambda x: x.get("net_profit_pct", 0), reverse=True)
        result["opportunities"] = profitable_opps[:20]
        result["profitable_count"] = len(profitable_opps)

    except Exception as e:
        result["error"] = str(e)

    return result


# ── Trade Journal Helpers (v2) ──────────────────────────────────────────────────

JOURNAL_FILE = BOT_DIR / "trade_journal.json"
CALIBRATION_FILE = BOT_DIR / "calibration_stats.json"


def get_journal_data():
    """Read trade journal and calibration stats from disk."""
    result = {"ts": time.time()}

    # Read predictions
    if JOURNAL_FILE.exists():
        try:
            predictions = json.loads(JOURNAL_FILE.read_text())
            result["total_predictions"] = len(predictions)

            # Recent predictions
            recent = predictions[-20:] if predictions else []
            result["recent_predictions"] = [
                {
                    "prediction_id": p.get("prediction_id", ""),
                    "market_ticker": p.get("market_ticker", ""),
                    "market_title": p.get("market_title", "")[:60],
                    "category": p.get("category", ""),
                    "model_prob": round(p.get("model_prob", 0), 3),
                    "market_price": round(p.get("market_price", 0), 3),
                    "edge": round(p.get("edge", 0), 3),
                    "traded": p.get("traded", False),
                    "side": p.get("side", ""),
                    "resolved": p.get("resolved", False),
                    "resolution": p.get("resolution"),
                    "brier_score": round(p.get("brier_score", 0), 4) if p.get("brier_score") is not None else None,
                    "pnl_dollars": round(p.get("pnl_dollars", 0), 2) if p.get("pnl_dollars") is not None else None,
                    "model_source": p.get("model_source", ""),
                    "timestamp": p.get("timestamp", 0),
                }
                for p in recent
            ]
        except Exception as e:
            result["journal_error"] = str(e)
            result["total_predictions"] = 0
            result["recent_predictions"] = []
    else:
        result["total_predictions"] = 0
        result["recent_predictions"] = []

    # Read calibration stats
    if CALIBRATION_FILE.exists():
        try:
            stats = json.loads(CALIBRATION_FILE.read_text())
            result["calibration"] = {
                "mean_brier_score": stats.get("mean_brier_score", 0),
                "win_rate": stats.get("win_rate", 0),
                "total_pnl": stats.get("total_pnl", 0),
                "profit_factor": stats.get("profit_factor", 0),
                "resolved_predictions": stats.get("resolved_predictions", 0),
                "traded_count": stats.get("traded_count", 0),
                "category_stats": stats.get("category_stats", {}),
                "model_stats": stats.get("model_stats", {}),
                "calibration_bins": stats.get("calibration_bins", {}),
            }
        except Exception as e:
            result["calibration_error"] = str(e)
            result["calibration"] = {}
    else:
        result["calibration"] = {}

    return result


# ── Main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), FreyjaHandler)
    print(f"[Freyja API] Listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"[Freyja API] Bot dir: {BOT_DIR}")
    print(f"[Freyja API] Dashboard: {DASHBOARD_DIR}")
    print(f"[Freyja API] Auth token: Bearer {AUTH_TOKEN}")
    print(f"[Freyja API] Open http://localhost:{LISTEN_PORT} for dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Freyja API] Shutting down.")
        server.server_close()