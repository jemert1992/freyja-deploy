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
    GET  /              — Serves dashboard HTML (auto-refreshes every 5s)
    GET  /api/status    — JSON: full bot state
    GET  /api/portfolio — JSON: portfolio summary
    GET  /api/trades    — JSON: recent trades (last 50)
    GET  /api/btc       — JSON: BTC strategy status (v2.2 NEW)
    POST /api/pause     — Pause trading
    POST /api/resume    — Resume trading
    POST /api/shutdown  — Graceful shutdown
"""

import http.server
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time
import traceback
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

API_PORT   = int(os.getenv("API_PORT", "8080"))
API_HOST   = os.getenv("API_HOST", "0.0.0.0")
SECRET_KEY = os.getenv("API_SECRET", "")  # Optional auth token


# ---------------------------------------------------------------------------
# Shared State (injected by bot.py)
# ---------------------------------------------------------------------------

class BotState:
    """
    Shared mutable state between api_server and bot threads.
    Thread-safe via lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "status": "initializing",
            "ts": time.time(),
            "cycle_count": 0,
            "last_cycle_ts": 0,
            "portfolio": {},
            "recent_trades": [],
            "btc": {},        # v2.2: BTC strategy state
            "paused": False,
        }

    def update(self, data: dict):
        with self._lock:
            self._data.update(data)
            self._data["ts"] = time.time()

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    def set_paused(self, paused: bool):
        with self._lock:
            self._data["paused"] = paused

    def is_paused(self) -> bool:
        with self._lock:
            return self._data.get("paused", False)


# Global state singleton
bot_state = BotState()


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="5">
  <title>Freyja Quant Engine v2.2</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Courier New', monospace;
      background: #0a0e1a;
      color: #c8d6e5;
      padding: 20px;
    }
    h1 { color: #00d2ff; font-size: 1.4em; margin-bottom: 16px; }
    h2 { color: #48dbfb; font-size: 1.0em; margin: 12px 0 6px; text-transform: uppercase; letter-spacing: 1px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .card {
      background: #111827;
      border: 1px solid #1f2d40;
      border-radius: 8px;
      padding: 16px;
    }
    .card.btc { border-color: #f7931a55; }  /* BTC orange accent v2.2 */
    .stat { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #1f2d40; }
    .stat:last-child { border-bottom: none; }
    .label { color: #8899aa; font-size: 0.85em; }
    .value { color: #e2e8f0; font-weight: bold; }
    .value.green { color: #00d2a0; }
    .value.red { color: #ff6b6b; }
    .value.orange { color: #f7931a; }
    .trades-table { width: 100%; border-collapse: collapse; font-size: 0.78em; }
    .trades-table th { color: #8899aa; text-align: left; padding: 4px 8px; border-bottom: 1px solid #1f2d40; }
    .trades-table td { padding: 4px 8px; border-bottom: 1px solid #0d1626; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75em; font-weight: bold; }
    .badge.yes { background: #00d2a022; color: #00d2a0; }
    .badge.no  { background: #ff6b6b22; color: #ff6b6b; }
    .badge.paper { background: #48dbfb22; color: #48dbfb; }
    .controls { margin-top: 20px; }
    .btn { padding: 8px 20px; margin-right: 10px; border: none; border-radius: 4px; cursor: pointer; font-family: inherit; font-size: 0.9em; }
    .btn-pause  { background: #f7931a; color: #000; }
    .btn-resume { background: #00d2a0; color: #000; }
    .btn-stop   { background: #ff6b6b; color: #fff; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
    .status-dot.running { background: #00d2a0; animation: pulse 1s infinite; }
    .status-dot.paused  { background: #f7931a; }
    .status-dot.stopped { background: #ff6b6b; }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
  </style>
</head>
<body>
  <h1>
    <span id="status-dot" class="status-dot"></span>
    Freyja Quant Engine <span style="color:#48dbfb">v2.2</span>
  </h1>

  <div class="grid" id="dashboard">Loading...</div>

  <div class="controls">
    <button class="btn btn-pause"  onclick="apiPost('/api/pause')">⏸ Pause</button>
    <button class="btn btn-resume" onclick="apiPost('/api/resume')">▶ Resume</button>
    <button class="btn btn-stop"   onclick="if(confirm('Shutdown bot?')) apiPost('/api/shutdown')">⏹ Shutdown</button>
  </div>

  <script>
    async function loadStatus() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();
        renderDashboard(d);
      } catch(e) {
        document.getElementById('dashboard').innerHTML = '<p style="color:#ff6b6b">Failed to load status</p>';
      }
    }

    function val(v, cls='') {
      return `<span class="value ${cls}">${v}</span>`;
    }

    function stat(label, value, cls='') {
      return `<div class="stat"><span class="label">${label}</span>${val(value, cls)}</div>`;
    }

    function renderDashboard(d) {
      const p = d.portfolio || {};
      const btc = d.btc || {};
      const trades = d.recent_trades || [];
      const paused = d.paused;
      const statusText = paused ? 'PAUSED' : 'RUNNING';
      const statusClass = paused ? 'paused' : 'running';

      document.getElementById('status-dot').className = `status-dot ${statusClass}`;

      const age = d.last_cycle_ts ? Math.round(Date.now()/1000 - d.last_cycle_ts) : '?';
      const balanceVal = p.balance != null ? `$${p.balance.toFixed(2)}` : '?';
      const balanceCls = p.balance > 900 ? 'green' : p.balance > 500 ? '' : 'red';

      let html = `
        <div class="card">
          <h2>Bot Status</h2>
          ${stat('Status', statusText, paused ? 'orange' : 'green')}
          ${stat('Cycles', d.cycle_count || 0)}
          ${stat('Last cycle', age + 's ago')}
          ${stat('Mode', p.paper_mode ? 'PAPER' : 'LIVE', p.paper_mode ? '' : 'red')}
        </div>
        <div class="card">
          <h2>Portfolio</h2>
          ${stat('Balance', balanceVal, balanceCls)}
          ${stat('Positions', p.num_positions || 0)}
          ${stat('Trades', p.num_trades || 0)}
          ${stat('Avg Brier', p.avg_brier != null ? p.avg_brier.toFixed(4) : 'N/A')}
        </div>
      `;

      // BTC card (v2.2)
      if (btc && Object.keys(btc).length > 0) {
        const btcPrice = btc.btc_price ? `$${btc.btc_price.toLocaleString()}` : 'N/A';
        const btcExp = btc.total_exposure != null ? `$${btc.total_exposure.toFixed(2)}` : 'N/A';
        html += `
          <div class="card btc">
            <h2>&#8383; BTC Strategy</h2>
            ${stat('Enabled', btc.enabled ? 'YES' : 'NO', btc.enabled ? 'green' : 'red')}
            ${stat('Mode', btc.paper_mode ? 'PAPER' : 'LIVE', btc.paper_mode ? '' : 'red')}
            ${stat('BTC Price', btcPrice, 'orange')}
            ${stat('Exposure', btcExp)}
            ${stat('Positions', btc.positions || 0)}
          </div>
        `;
      }

      // Trades table
      if (trades.length > 0) {
        const rows = trades.slice(-10).reverse().map(t => `
          <tr>
            <td>${new Date(t.ts*1000).toLocaleTimeString()}</td>
            <td>${t.ticker || ''}</td>
            <td><span class="badge ${(t.side||'').toLowerCase()}">${t.side || ''}</span></td>
            <td>${t.contracts || 1}</td>
            <td>${t.price || ''}c</td>
            <td>$${(t.cost||0).toFixed(2)}</td>
            <td>${t.strategy || (t.btc_price ? 'btc' : 'arb')}</td>
            ${t.paper ? '<td><span class="badge paper">PAPER</span></td>' : '<td></td>'}
          </tr>
        `).join('');
        html += `
          <div class="card" style="grid-column: 1/-1">
            <h2>Recent Trades</h2>
            <table class="trades-table">
              <thead><tr><th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Price</th><th>Cost</th><th>Strategy</th><th>Mode</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        `;
      }

      document.getElementById('dashboard').innerHTML = html;
    }

    async function apiPost(endpoint) {
      try {
        const r = await fetch(endpoint, {method: 'POST'});
        const d = await r.json();
        alert(d.message || 'OK');
        loadStatus();
      } catch(e) {
        alert('Request failed: ' + e);
      }
    }

    loadStatus();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Request Handler
# ---------------------------------------------------------------------------

class APIHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the control API."""

    # Suppress default request logging (we do our own)
    def log_message(self, format, *args):
        pass

    def _auth_check(self) -> bool:
        """Check API secret if configured."""
        if not SECRET_KEY:
            return True  # No auth configured
        auth = self.headers.get("X-API-Key", "")
        return auth == SECRET_KEY

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]  # Strip query params

        if path == "/":
            self._send_html(DASHBOARD_HTML)

        elif path == "/api/status":
            state = bot_state.get()
            self._send_json(state)

        elif path == "/api/portfolio":
            state = bot_state.get()
            self._send_json(state.get("portfolio", {}))

        elif path == "/api/trades":
            state = bot_state.get()
            trades = state.get("recent_trades", [])[-50:]
            self._send_json({"trades": trades, "count": len(trades)})

        elif path == "/api/btc":   # v2.2 NEW
            state = bot_state.get()
            btc_status = state.get("btc", {})
            self._send_json(btc_status)

        elif path == "/api/health":
            self._send_json({"status": "ok", "ts": time.time()})

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self._auth_check():
            self._send_json({"error": "Unauthorized"}, 401)
            return

        path = self.path.split("?")[0]

        if path == "/api/pause":
            bot_state.set_paused(True)
            logger.info("Bot paused via API")
            self._send_json({"message": "Bot paused"})

        elif path == "/api/resume":
            bot_state.set_paused(False)
            logger.info("Bot resumed via API")
            self._send_json({"message": "Bot resumed"})

        elif path == "/api/shutdown":
            logger.info("Shutdown requested via API")
            self._send_json({"message": "Shutting down..."})
            threading.Thread(target=self._do_shutdown, daemon=True).start()

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-API-Key, Content-Type")
        self.end_headers()

    def _do_shutdown(self):
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Thread-per-request HTTP server."""
    daemon_threads = True
    allow_reuse_address = True


def start_server(
    bot_ref=None,
    host: str = API_HOST,
    port: int = API_PORT,
    state_update_interval: float = 2.0,
) -> threading.Thread:
    """
    Start the API server in a background thread.

    Args:
        bot_ref: Reference to the FreyjaBot instance (for state polling)
        host: Host to bind to
        port: Port to listen on
        state_update_interval: Seconds between bot state polls

    Returns:
        The server thread (daemon, so it stops with the main process)
    """
    server = ThreadedHTTPServer((host, port), APIHandler)

    def state_updater():
        """Polls bot state and updates shared BotState."""
        while True:
            try:
                if bot_ref is not None and hasattr(bot_ref, 'get_state'):
                    state = bot_ref.get_state()
                    state["status"] = "paused" if bot_state.is_paused() else "running"
                    bot_state.update(state)
            except Exception as e:
                logger.debug(f"State update error: {e}")
            time.sleep(state_update_interval)

    def run_server():
        logger.info(f"API server listening on {host}:{port}")
        server.serve_forever()

    # Start state updater
    state_thread = threading.Thread(target=state_updater, daemon=True)
    state_thread.start()

    # Start server
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    return server_thread


# ---------------------------------------------------------------------------
# Standalone Mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    logger.info(f"Starting API server in standalone mode on {API_HOST}:{API_PORT}")
    logger.info("No bot connected — serving static state only")

    # Update state with dummy data for testing
    bot_state.update({
        "status": "running",
        "cycle_count": 42,
        "last_cycle_ts": time.time(),
        "portfolio": {
            "balance": 1000.0,
            "paper_mode": True,
            "num_positions": 3,
            "num_trades": 17,
            "avg_brier": 0.0821,
        },
        "recent_trades": [
            {
                "ticker": "KXBTCD-26MAR1407-T70499.99",
                "side": "YES",
                "contracts": 5,
                "price": 92,
                "cost": 4.60,
                "strategy": "btc",
                "paper": True,
                "ts": time.time() - 120,
            },
            {
                "ticker": "HIGHNY-26MAR14-T72",
                "side": "YES",
                "contracts": 2,
                "price": 65,
                "cost": 1.30,
                "strategy": "weather",
                "paper": True,
                "ts": time.time() - 300,
            },
        ],
        "btc": {
            "enabled": True,
            "paper_mode": True,
            "btc_price": 83421.50,
            "positions": 2,
            "total_exposure": 46.00,
            "max_exposure": 500.0,
        },
    })

    try:
        server = ThreadedHTTPServer((API_HOST, API_PORT), APIHandler)
        logger.info(f"Dashboard: http://localhost:{API_PORT}/")
        logger.info("Press Ctrl+C to stop")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped")
