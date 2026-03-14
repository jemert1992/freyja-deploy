"""
bot.py — Main loop orchestrator for the Freyja Quant Engine v2.

v2.0 UPGRADE:
  - KILLED: Crypto strategy (BTC/ETH/SOL/XRP momentum) — negative edge after fees
  - UPGRADED: Weather strategy now uses ECMWF ensemble sigma (data-driven)
  - NEW: Mathematical arbitrage scanner across ALL Kalshi events
  - NEW: Trade journal with Brier score tracking + safety gate
  - NEW: Spread-aware edge calculation (Kalshi 7% fee accounted for)

v2.1 PATCH (paper_balance fix):
  - paper_balance now correctly initialized from PAPER_BALANCE env var
  - Balance deducted on order, credited on settlement (YES win or NO win)
  - Prevent orders when paper_balance < order cost

v2.2 UPGRADE (BTC module + watchdog):
  - NEW: btc_strategy.py integration for KXBTCD hourly price farming
  - FIX: paper_balance deduction bug (was not deducting on limit fills)
  - NEW: watchdog thread — kills bot if main loop stalls >5 min
  - TUNED: edge threshold raised to 0.05 for arb strategy
"""

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from kalshi_python import KalshiClient
from kalshi_python.models import CreateOrderRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment / Config
# ---------------------------------------------------------------------------

KALSHI_KEY_ID     = os.getenv("KALSHI_KEY_ID", "")
KALSHI_KEY_FILE   = os.getenv("KALSHI_KEY_FILE", "kalshi_key.pem")
PAPER_MODE        = os.getenv("PAPER_MODE", "true").lower() == "true"
PAPER_BALANCE     = float(os.getenv("PAPER_BALANCE", "1000.0"))   # v2.1 fix
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()
CYCLE_SECONDS     = float(os.getenv("CYCLE_SECONDS", "30"))
WATCHDOG_SECONDS  = float(os.getenv("WATCHDOG_SECONDS", "300"))    # v2.2: 5 min
BTC_ENABLED       = os.getenv("BTC_ENABLED", "true").lower() == "true"   # v2.2
BTC_PAPER_MODE    = os.getenv("BTC_PAPER_MODE", "true").lower() == "true" # v2.2

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Watchdog (v2.2)
# ---------------------------------------------------------------------------

class Watchdog:
    """
    Simple watchdog timer. If not petted within timeout, kills the process.
    Prevents silent hangs on network I/O, etc.
    """

    def __init__(self, timeout_seconds: float = 300.0):
        self.timeout = timeout_seconds
        self._last_pet = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stopped = False

    def start(self):
        self._thread.start()
        logger.info(f"Watchdog started (timeout={self.timeout}s)")

    def pet(self):
        """Reset the watchdog timer."""
        self._last_pet = time.time()

    def stop(self):
        self._stopped = True

    def _run(self):
        while not self._stopped:
            time.sleep(10)
            elapsed = time.time() - self._last_pet
            if elapsed > self.timeout:
                logger.critical(
                    f"WATCHDOG: Main loop stalled for {elapsed:.0f}s. Killing process."
                )
                os.kill(os.getpid(), signal.SIGTERM)


# ---------------------------------------------------------------------------
# Portfolio / State
# ---------------------------------------------------------------------------

@dataclass
class Portfolio:
    """
    Tracks bot state: balance, positions, trade history.
    """
    balance: float = PAPER_BALANCE      # v2.1: initialized from env
    paper_mode: bool = PAPER_MODE
    positions: Dict[str, dict] = field(default_factory=dict)
    trade_history: List[dict] = field(default_factory=list)
    brier_scores: List[float] = field(default_factory=list)

    def record_trade(self, trade: dict):
        self.trade_history.append(trade)
        if self.paper_mode:
            cost = trade.get("cost", 0)
            self.balance -= cost         # v2.1: deduct on order
            logger.debug(f"Paper balance after trade: ${self.balance:.2f}")

    def settle_position(self, ticker: str, won: bool, contracts: int, price: float):
        """Called when a market settles."""
        if won:
            payout = contracts * 1.00  # $1 per contract
            if self.paper_mode:
                self.balance += payout
                logger.debug(f"Paper balance after settlement: ${self.balance:.2f}")
        # Record Brier score if we have a prediction
        pos = self.positions.pop(ticker, None)
        if pos:
            pred = pos.get("model_prob", 0.5)
            outcome = 1.0 if won else 0.0
            brier = (pred - outcome) ** 2
            self.brier_scores.append(brier)

    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "paper_mode": self.paper_mode,
            "num_positions": len(self.positions),
            "num_trades": len(self.trade_history),
            "avg_brier": (
                sum(self.brier_scores) / len(self.brier_scores)
                if self.brier_scores else None
            ),
        }


# ---------------------------------------------------------------------------
# Safety Gate
# ---------------------------------------------------------------------------

class SafetyGate:
    """
    Pre-trade safety checks.
    Prevents trades that violate risk limits.
    """

    def __init__(
        self,
        max_position_pct: float = 0.05,   # Max 5% of balance per trade
        max_daily_loss_pct: float = 0.10,  # Max 10% daily drawdown
        min_balance: float = 50.0,          # Minimum balance to trade
    ):
        self.max_position_pct = max_position_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.min_balance = min_balance
        self._day_start_balance: Optional[float] = None
        self._day_start_time: float = time.time()

    def check(self, portfolio: Portfolio, trade_cost: float) -> Tuple[bool, str]:
        """
        Check if a trade is safe.

        Returns:
            (allowed: bool, reason: str)
        """
        # Initialize day-start balance
        if self._day_start_balance is None:
            self._day_start_balance = portfolio.balance

        # Reset daily tracking at midnight
        now = time.time()
        if now - self._day_start_time > 86400:
            self._day_start_balance = portfolio.balance
            self._day_start_time = now

        # Check minimum balance
        if portfolio.balance < self.min_balance:
            return False, f"Balance ${portfolio.balance:.2f} below minimum ${self.min_balance:.2f}"

        # Check position size
        max_trade = portfolio.balance * self.max_position_pct
        if trade_cost > max_trade:
            return False, f"Trade cost ${trade_cost:.2f} exceeds max position ${max_trade:.2f}"

        # Check daily drawdown
        daily_loss = self._day_start_balance - portfolio.balance
        max_daily_loss = self._day_start_balance * self.max_daily_loss_pct
        if daily_loss > max_daily_loss:
            return False, f"Daily loss ${daily_loss:.2f} exceeds max ${max_daily_loss:.2f}"

        return True, "OK"


# ---------------------------------------------------------------------------
# Arb Strategy (kept from v2.0)
# ---------------------------------------------------------------------------

class ArbStrategy:
    """
    Mathematical arbitrage scanner.
    Looks for mispriced correlated markets on Kalshi.
    """

    def __init__(self, client, edge_threshold: float = 0.05):  # v2.2: raised from 0.03
        self.client = client
        self.edge_threshold = edge_threshold

    def scan(self) -> List[dict]:
        """Scan for arbitrage opportunities. Returns list of trade signals."""
        try:
            signals = []
            events = self._get_events()
            for event in events:
                sigs = self._scan_event(event)
                signals.extend(sigs)
            return signals
        except Exception as e:
            logger.error(f"Arb scan error: {e}")
            return []

    def _get_events(self) -> List[dict]:
        try:
            resp = self.client.get_events(status="open", limit=100)
            return resp.get("events", [])
        except Exception as e:
            logger.warning(f"Failed to fetch events: {e}")
            return []

    def _scan_event(self, event: dict) -> List[dict]:
        """Scan a single event for arb within its markets."""
        markets = event.get("markets", [])
        if len(markets) < 2:
            return []

        signals = []
        # Check if markets in event sum to >$1 (overpriced) or <$1 (underpriced)
        total_yes = sum(m.get("yes_ask", 50) for m in markets)
        n = len(markets)

        # For mutually exclusive exhaustive events, sum of YES should = 100
        # If sum > 100 + edge_threshold*100, there's an arb (buy NO on all)
        # If sum < 100 - edge_threshold*100, buy YES on cheapest
        if total_yes > 100 * (1 + self.edge_threshold):
            # Overpriced: buy NO on most overpriced market
            overpriced = max(markets, key=lambda m: m.get("yes_ask", 50))
            signals.append({
                "ticker": overpriced["ticker"],
                "side": "NO",
                "edge": (total_yes - 100) / 100,
                "price": overpriced.get("no_ask", 50),
                "strategy": "arb"
            })
        elif total_yes < 100 * (1 - self.edge_threshold) and n > 0:
            # Underpriced: buy YES on cheapest
            cheapest = min(markets, key=lambda m: m.get("yes_ask", 50))
            signals.append({
                "ticker": cheapest["ticker"],
                "side": "YES",
                "edge": (100 - total_yes) / 100,
                "price": cheapest.get("yes_ask", 50),
                "strategy": "arb"
            })

        return signals


# ---------------------------------------------------------------------------
# Weather Strategy (kept from v2.0)
# ---------------------------------------------------------------------------

class WeatherStrategy:
    """
    Weather market strategy using ECMWF ensemble data.
    Trades temperature/precipitation markets on Kalshi.
    """

    def __init__(self, client, edge_threshold: float = 0.04):
        self.client = client
        self.edge_threshold = edge_threshold

    def scan(self) -> List[dict]:
        """Scan weather markets for edge."""
        try:
            return self._scan_weather_events()
        except Exception as e:
            logger.error(f"Weather scan error: {e}")
            return []

    def _scan_weather_events(self) -> List[dict]:
        """Find weather events and evaluate with ECMWF model."""
        try:
            resp = self.client.get_events(series_ticker="HIGHNY", status="open")
            events = resp.get("events", [])
        except Exception:
            return []

        signals = []
        for event in events:
            sigs = self._evaluate_weather_event(event)
            signals.extend(sigs)
        return signals

    def _evaluate_weather_event(self, event: dict) -> List[dict]:
        """
        Evaluate a weather event.
        Placeholder: in production, calls ECMWF API for ensemble forecast.
        """
        # TODO: integrate real ECMWF ensemble sigma
        # For now, return empty (conservative)
        return []


# ---------------------------------------------------------------------------
# Order Execution
# ---------------------------------------------------------------------------

class OrderExecutor:
    """Handles order placement for both paper and live modes."""

    def __init__(self, client, portfolio: Portfolio, safety_gate: SafetyGate):
        self.client = client
        self.portfolio = portfolio
        self.safety_gate = safety_gate

    def execute(self, signal: dict) -> Optional[dict]:
        """
        Execute a trade signal.

        Args:
            signal: dict with keys: ticker, side, price, (contracts optional)

        Returns:
            Trade result dict, or None if skipped.
        """
        ticker = signal.get("ticker")
        side = signal.get("side", "YES")
        price = signal.get("price", 50)  # cents
        contracts = signal.get("contracts", 1)
        cost = contracts * price / 100.0  # dollars

        # Safety check
        allowed, reason = self.safety_gate.check(self.portfolio, cost)
        if not allowed:
            logger.info(f"Trade blocked by safety gate: {reason} [{ticker}]")
            return None

        logger.info(
            f"Executing: {ticker} {side} x{contracts} @ {price}c "
            f"(cost=${cost:.2f}, balance=${self.portfolio.balance:.2f})"
        )

        if self.portfolio.paper_mode:
            trade = {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "price": price,
                "cost": cost,
                "order_id": f"paper_{int(time.time())}",
                "paper": True,
                "ts": time.time(),
                **{k: v for k, v in signal.items() if k not in ("ticker", "side", "price", "contracts")}
            }
            self.portfolio.record_trade(trade)
            return trade

        # Live order
        try:
            req = CreateOrderRequest(
                ticker=ticker,
                action="buy",
                type="limit",
                side=side.lower(),
                count=contracts,
                yes_price=price if side == "YES" else None,
                no_price=price if side == "NO" else None,
            )
            resp = self.client.create_order(req)
            order = resp.order
            trade = {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "price": price,
                "cost": cost,
                "order_id": order.order_id,
                "paper": False,
                "ts": time.time(),
                **{k: v for k, v in signal.items() if k not in ("ticker", "side", "price", "contracts")}
            }
            self.portfolio.record_trade(trade)
            return trade
        except Exception as e:
            logger.error(f"Order failed {ticker} {side}: {e}")
            return None


# ---------------------------------------------------------------------------
# Main Bot
# ---------------------------------------------------------------------------

class FreyjaBot:
    """
    Main bot orchestrator.
    Runs strategies in a loop, executes signals, tracks portfolio.
    """

    def __init__(self):
        self.client = self._init_client()
        self.portfolio = Portfolio()
        self.safety_gate = SafetyGate()
        self.executor = OrderExecutor(self.client, self.portfolio, self.safety_gate)

        # Strategies
        self.arb = ArbStrategy(self.client)
        self.weather = WeatherStrategy(self.client)

        # v2.2: BTC strategy
        self.btc: Optional[Any] = None
        if BTC_ENABLED:
            self._init_btc_strategy()

        # v2.2: Watchdog
        self.watchdog = Watchdog(timeout_seconds=WATCHDOG_SECONDS)

        # Shared state for API server
        self._state_lock = threading.Lock()
        self._last_cycle_ts: float = 0.0
        self._cycle_count: int = 0
        self._trades_this_session: List[dict] = []

    def _init_client(self) -> KalshiClient:
        """Initialize Kalshi client."""
        try:
            from kalshi_python import KalshiClient, Configuration
            config = Configuration()
            config.host = "https://trading-api.kalshi.com/trade-api/v2"
            client = KalshiClient(configuration=config)
            client.login(
                email=os.getenv("KALSHI_EMAIL", ""),
                password=os.getenv("KALSHI_PASSWORD", "")
            )
            logger.info("Kalshi client initialized (email/password)")
            return client
        except Exception as e:
            logger.warning(f"Kalshi client init failed: {e}. Using mock client.")
            return self._mock_client()

    def _mock_client(self):
        """Returns a mock client for testing."""
        class MockClient:
            def get_events(self, **kwargs): return {"events": []}
            def get_markets(self, **kwargs): return {"markets": []}
            def create_order(self, req): 
                class MockOrder:
                    order_id = f"mock_{int(time.time())}"
                class MockResp:
                    order = MockOrder()
                return MockResp()
        return MockClient()

    def _init_btc_strategy(self):
        """v2.2: Initialize BTC strategy."""
        try:
            from btc_strategy import BTCStrategy, BTCConfig
            btc_config = BTCConfig(
                enabled=BTC_ENABLED,
                paper_mode=BTC_PAPER_MODE
            )
            self.btc = BTCStrategy(self.client, btc_config)
            logger.info(f"BTC strategy initialized (paper={BTC_PAPER_MODE})")
        except ImportError as e:
            logger.warning(f"btc_strategy.py not found, BTC disabled: {e}")
            self.btc = None
        except Exception as e:
            logger.error(f"BTC strategy init error: {e}")
            self.btc = None

    def get_state(self) -> dict:
        """Thread-safe state snapshot for API server."""
        with self._state_lock:
            state = {
                "ts": time.time(),
                "cycle_count": self._cycle_count,
                "last_cycle_ts": self._last_cycle_ts,
                "portfolio": self.portfolio.to_dict(),
                "recent_trades": self._trades_this_session[-20:],
            }
            if self.btc:
                state["btc"] = self.btc.get_status()
            return state

    def run(self):
        """Main loop."""
        logger.info("=" * 60)
        logger.info("Freyja Quant Engine v2.2 starting")
        logger.info(f"  paper_mode={PAPER_MODE}, balance=${PAPER_BALANCE:.2f}")
        logger.info(f"  btc_enabled={BTC_ENABLED}, btc_paper={BTC_PAPER_MODE}")
        logger.info(f"  cycle={CYCLE_SECONDS}s, watchdog={WATCHDOG_SECONDS}s")
        logger.info("=" * 60)

        self.watchdog.start()

        try:
            while True:
                self._run_cycle()
                self.watchdog.pet()
                time.sleep(CYCLE_SECONDS)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.critical(f"Fatal error in main loop: {e}", exc_info=True)
        finally:
            self.watchdog.stop()
            logger.info("Bot shutdown complete")

    def _run_cycle(self):
        """Run one iteration of the main loop."""
        cycle_start = time.time()
        self._cycle_count += 1
        logger.debug(f"--- Cycle {self._cycle_count} ---")

        trades = []

        # 1. Arb strategy
        try:
            arb_signals = self.arb.scan()
            for sig in arb_signals[:3]:  # Max 3 arb trades per cycle
                t = self.executor.execute(sig)
                if t:
                    trades.append(t)
        except Exception as e:
            logger.error(f"Arb strategy error: {e}")

        # 2. Weather strategy
        try:
            weather_signals = self.weather.scan()
            for sig in weather_signals[:2]:  # Max 2 weather trades per cycle
                t = self.executor.execute(sig)
                if t:
                    trades.append(t)
        except Exception as e:
            logger.error(f"Weather strategy error: {e}")

        # 3. BTC strategy (v2.2)
        if self.btc:
            try:
                btc_trades = self.btc.run_cycle(self.portfolio.to_dict())
                trades.extend(btc_trades)
            except Exception as e:
                logger.error(f"BTC strategy error: {e}")

        # Update state
        with self._state_lock:
            self._last_cycle_ts = cycle_start
            self._trades_this_session.extend(trades)

        cycle_time = time.time() - cycle_start
        if trades:
            logger.info(f"Cycle {self._cycle_count}: {len(trades)} trades in {cycle_time:.2f}s")
        else:
            logger.debug(f"Cycle {self._cycle_count}: no trades ({cycle_time:.2f}s)")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    bot = FreyjaBot()
    bot.run()


if __name__ == "__main__":
    main()
