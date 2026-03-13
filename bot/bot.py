"""
bot.py — Main loop orchestrator for the Freyja Quant Engine v2.

v2.0 UPGRADE:
  - KILLED: Crypto strategy (BTC/ETH/SOL/XRP momentum) — negative edge after fees
  - UPGRADED: Weather strategy now uses ECMWF ensemble sigma (data-driven)
  - NEW: Mathematical arbitrage scanner across ALL Kalshi events
  - NEW: Trade journal with Brier score tracking + safety gate
  - NEW: Spread-aware edge calculation (Kalshi 7% fee accounted for)

Architecture:
  Every LOOP_INTERVAL seconds:
    1. Check kill switch + risk gates
    2. Check trade journal safety gate (stop if Brier > 0.25 after 30 trades)
    3. Scan weather markets → ECMWF ensemble pricing → enter if edge
    4. Scan all events for mathematical arbitrage (every ARB_SCAN_INTERVAL)
    5. Monitor open positions → evaluate exits
    6. Log status + record predictions in trade journal
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Union

import config
from auth import KalshiAuth
from client import KalshiClient
from executor import Executor
from paper_trader import PaperTrader
from risk_manager import RiskManager
from state import BotState
from strategy import should_exit, OpenPosition

# Weather module integration
try:
    from weather_config import WEATHER, CITIES
    from weather_scanner import WeatherScanner, WeatherOpportunity
    from weather_strategy import WeatherTradeDecision
    _WEATHER_AVAILABLE = True
except ImportError:
    _WEATHER_AVAILABLE = False
    WEATHER = None
    WeatherScanner = None

# Arbitrage scanner (v2)
try:
    from arb_scanner import ArbScanner
    _ARB_AVAILABLE = True
except ImportError:
    _ARB_AVAILABLE = False
    ArbScanner = None

# Trade journal (v2)
try:
    from trade_journal import TradeJournal
    _JOURNAL_AVAILABLE = True
except ImportError:
    _JOURNAL_AVAILABLE = False
    TradeJournal = None

logger = logging.getLogger(__name__)

# Arb scanner runs less frequently (every 10 minutes)
ARB_SCAN_INTERVAL = 600.0

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with console + file handlers."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt=config.LOG_FORMAT,
        datefmt=config.LOG_DATE_FORMAT,
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(log_level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    fh = logging.FileHandler(config.LOG_FILE, mode="a", encoding="utf-8")
    fh.setLevel(log_level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

BANNER = r"""
 ███████╗██████╗ ███████╗██╗   ██╗     ██╗ █████╗     ██╗   ██╗██████╗
 ██╔════╝██╔══██╗██╔════╝╚██╗ ██╔╝     ██║██╔══██╗    ██║   ██║╚════██╗
 █████╗  ██████╔╝█████╗   ╚████╔╝      ██║███████║    ██║   ██║ █████╔╝
 ██╔══╝  ██╔══██╗██╔══╝    ╚██╔╝  ██   ██║██╔══██║    ╚██╗ ██╔╝██╔═══╝
 ██║     ██║  ██║███████╗   ██║   ╚█████╔╝██║  ██║     ╚████╔╝ ███████╗
 ╚═╝     ╚═╝  ╚═╝╚══════╝   ╚═╝    ╚════╝ ╚═╝  ╚═╝      ╚═══╝  ╚══════╝
 Freyja Quant Engine v2 — Weather + Arb Scanner + Trade Journal
 ECMWF Ensemble | Fee-Aware Kelly | Brier Score Gate
"""


def print_startup_banner(mode: str, balance: Optional[float]) -> None:
    """Print startup information."""
    print(BANNER)
    print("=" * 72)
    print(f"  Mode          : {mode}")
    print(f"  API URL       : {config.get_base_url()}")
    print(f"  Balance       : ${balance:.2f}" if balance else "  Balance       : N/A")
    print(f"  Kill Switch   : delete {config.STOP_FILE} to resume, create to stop")
    if _WEATHER_AVAILABLE and WEATHER and WEATHER.enabled:
        print(f"  Weather Module: ENABLED ({len(WEATHER.get_active_city_codes())} cities)")
        print(f"  Weather Mode  : {'PAPER' if WEATHER.paper_mode else 'LIVE'}")
        print(f"  Weather Edge  : {WEATHER.min_edge:.0%} min / Kelly {WEATHER.kelly_fraction:.0%}")
    else:
        print(f"  Weather Module: DISABLED")
    if _ARB_AVAILABLE:
        print(f"  Arb Scanner   : ENABLED (scan every {ARB_SCAN_INTERVAL/60:.0f} min)")
    else:
        print(f"  Arb Scanner   : DISABLED")
    if _JOURNAL_AVAILABLE:
        print(f"  Trade Journal : ENABLED (Brier gate at 0.25 after 30 trades)")
    else:
        print(f"  Trade Journal : DISABLED")
    print(f"  Crypto Trading: DISABLED (v2 — negative edge after fees)")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class KalshiBot:
    """
    Main bot orchestrator v2.

    Lifecycle:
      bot = KalshiBot(use_live=False)
      bot.run()          # blocking loop
    """

    def __init__(self, use_live: bool = False, paper_mode: bool = True):
        self.use_live = use_live
        self.paper_mode = paper_mode and not use_live
        self.mode_str = "LIVE" if use_live else ("PAPER" if self.paper_mode else "DEMO")

        # Initialize components
        self.state = BotState()

        # Weather module
        self.weather_scanner = None
        self._last_weather_scan = 0.0
        self._weather_positions: dict = {}

        # Arb scanner (v2)
        self.arb_scanner = None
        self._last_arb_scan = 0.0

        # Trade journal (v2)
        self.journal = None
        if _JOURNAL_AVAILABLE:
            try:
                self.journal = TradeJournal()
                logger.info("Trade journal initialized")
            except Exception as e:
                logger.warning(f"Trade journal init failed: {e}")

        # Auth + Kalshi client
        try:
            self.auth = KalshiAuth(
                api_key_id=config.API_KEY_ID,
                private_key_path=config.PRIVATE_KEY_PATH,
            )
            self.client = KalshiClient(self.auth, use_demo=not use_live)
            self._api_available = True
        except (FileNotFoundError, ValueError) as e:
            logger.warning(f"Kalshi API not configured: {e}")
            logger.warning("Running in OFFLINE PAPER mode — no market scanning")
            self._api_available = False
            self.auth = None
            self.client = None

        # Risk manager
        self.risk = RiskManager(self.state)

        # Weather scanner
        if _WEATHER_AVAILABLE and WEATHER and WEATHER.enabled:
            try:
                self.weather_scanner = WeatherScanner()
                logger.info("Weather module ENABLED — scanning %d cities",
                            len(WEATHER.get_active_city_codes()))
            except Exception as e:
                logger.warning(f"Weather module init failed: {e}")
                self.weather_scanner = None

        # Arb scanner (v2) — uses public API, no auth needed
        if _ARB_AVAILABLE:
            try:
                self.arb_scanner = ArbScanner()
                logger.info("Arb scanner ENABLED")
            except Exception as e:
                logger.warning(f"Arb scanner init failed: {e}")

        # Order execution
        if self.paper_mode:
            self.executor: Union[PaperTrader, Executor] = PaperTrader(self.state)
        else:
            self.executor = Executor(self.client, self.state)

        # Timing
        self._last_status_log = 0.0
        self._iteration = 0
        self._running = False

    # ------------------------------------------------------------------
    # Main Run Loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Main blocking loop. Runs until:
          - STOP file appears
          - Daily loss limit exceeded
          - KeyboardInterrupt / SIGTERM
        """
        setup_logging(config.LOG_LEVEL)

        balance = None
        if self._api_available and self.client:
            try:
                balance = self.client.get_balance_dollars()
            except Exception as e:
                logger.warning(f"Could not fetch initial balance: {e}")

        print_startup_banner(self.mode_str, balance)
        logger.info(f"Bot starting — mode={self.mode_str}")
        logger.info(f"Loaded {self.state.position_count()} open positions from state")

        self._running = True

        try:
            while self._running:
                self._iteration += 1
                loop_start = time.time()

                try:
                    self._main_loop_iteration(balance)
                except Exception as e:
                    logger.error(f"Unhandled error in main loop (iteration {self._iteration}): {e}", exc_info=True)

                elapsed = time.time() - loop_start
                sleep_time = max(0.0, config.TIMING.loop_interval_seconds - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received — shutting down gracefully")
        finally:
            self._shutdown()

    def _main_loop_iteration(self, balance: Optional[float]) -> None:
        """Single iteration of the main loop."""
        now = time.time()

        # --- 1. Kill switch check ---
        if self.risk.check_kill_switch():
            logger.critical("KILL SWITCH IS ACTIVE. Halting all trading.")
            self._running = False
            return

        # --- 2. Refresh balance ---
        if self._api_available and self.client:
            try:
                balance = self.client.get_balance_dollars()
            except Exception as e:
                logger.warning(f"Balance fetch failed: {e}")

        # --- 3. Global risk check ---
        allowed, reason = self.risk.is_trading_allowed(balance)
        if not allowed:
            logger.warning(f"Trading not allowed: {reason}")
            self._monitor_positions()
            return

        # --- 4. Trade journal safety gate (v2) ---
        if self.journal and not self.journal.should_trade():
            logger.warning(
                "TRADE JOURNAL SAFETY GATE: Brier score > 0.25 — "
                "predictions are worse than random. Pausing new entries."
            )
            self._monitor_positions()
            return

        # --- 5. Monitor open positions for exits ---
        self._monitor_positions()

        # --- 6. Weather market scan (primary strategy) ---
        if self.weather_scanner is not None:
            weather_interval = WEATHER.scan_interval_seconds if WEATHER else 300.0
            if (now - self._last_weather_scan) >= weather_interval:
                try:
                    self._weather_scan_and_enter(balance or 0.0)
                except Exception as e:
                    logger.error(f"Weather scan error: {e}", exc_info=True)
                self._last_weather_scan = now

        # --- 7. Arb scanner (v2 — runs less frequently) ---
        if self.arb_scanner is not None:
            if (now - self._last_arb_scan) >= ARB_SCAN_INTERVAL:
                try:
                    self._arb_scan(balance or 0.0)
                except Exception as e:
                    logger.error(f"Arb scan error: {e}", exc_info=True)
                self._last_arb_scan = now

        # --- 8. Periodic status log ---
        if (now - self._last_status_log) >= 60.0:
            self._log_status()
            self._last_status_log = now

    # ------------------------------------------------------------------
    # Position Monitoring (simplified — weather only)
    # ------------------------------------------------------------------

    def _monitor_positions(self) -> None:
        """Check all open positions for exit conditions."""
        positions = self.state.get_all_positions()
        if not positions:
            return

        for pos in positions:
            self._evaluate_position(pos)

    def _evaluate_position(self, pos: OpenPosition) -> None:
        """Evaluate a single position for exit."""
        # For weather positions, we mostly hold to settlement
        # Check for time-based exit or manual stop-loss
        if hasattr(pos, 'current_price') and pos.current_price is not None:
            pnl_pct = pos.unrealized_pnl_pct() * 100
            logger.debug(
                f"HOLD {pos.ticker} | {pos.side.upper()} x{pos.contracts} | "
                f"entry={pos.entry_price}¢ current={pos.current_price}¢ | "
                f"P&L={pnl_pct:+.1f}%"
            )

    # ------------------------------------------------------------------
    # Weather Market Scanning + Entry (primary strategy)
    # ------------------------------------------------------------------

    def _weather_scan_and_enter(self, balance: float) -> None:
        """Scan weather markets and enter paper trades on best opportunities."""
        if self.weather_scanner is None:
            return

        try:
            opportunities = self.weather_scanner.scan(force=False)
        except Exception as e:
            logger.error(f"Weather market scan failed: {e}")
            return

        if not opportunities:
            logger.debug("No weather opportunities found")
            return

        # Calculate available capital
        weather_exposure = sum(
            (d.limit_price / 100.0) * d.contracts
            for d in self._weather_positions.values()
            if hasattr(d, 'limit_price')
        )
        available = min(
            balance - config.RISK.min_balance_dollars,
            WEATHER.max_total_exposure_dollars - weather_exposure,
        )

        if available <= 0:
            logger.debug("No available capital for weather trades")
            return

        # Evaluate and filter
        tradeable = self.weather_scanner.evaluate_opportunities(
            opportunities,
            available,
            len(self._weather_positions),
        )

        for opp in tradeable:
            dec = opp.decision
            if not dec.should_trade:
                continue

            if dec.ticker in self._weather_positions:
                continue
            if self.state.has_position(dec.ticker):
                continue

            # Record prediction in journal (v2)
            if self.journal:
                self.journal.record_prediction(
                    market_ticker=dec.ticker,
                    event_ticker="",
                    market_title=dec.bracket_label,
                    category="weather",
                    model_prob=dec.model_prob,
                    market_price=dec.market_implied,
                    model_source=getattr(dec, 'sigma_source', 'nws_cdf'),
                    forecast_details={
                        "city": dec.city_code,
                        "forecast_high": dec.forecast_high,
                        "sigma": dec.sigma,
                        "sigma_source": getattr(dec, 'sigma_source', 'unknown'),
                        "lead_days": dec.lead_days,
                    },
                    traded=True,
                    side=dec.side,
                    contracts=dec.contracts,
                    entry_price_cents=dec.limit_price,
                    cost_dollars=(dec.limit_price / 100.0) * dec.contracts,
                )

            # Record prediction for weather scanner calibration too
            self.weather_scanner.record_prediction(
                ticker=dec.ticker,
                city_code=dec.city_code,
                forecast_date=dec.forecast_date,
                model_prob=dec.model_prob,
                market_prob=dec.market_implied,
                side=dec.side,
                forecast_high=dec.forecast_high,
                sigma=dec.sigma,
            )

            # Execute trade
            logger.info(f"WEATHER ENTRY: {dec}")

            if WEATHER.paper_mode:
                from strategy import EntryDecision
                entry_dec = EntryDecision(
                    should_enter=True,
                    side=dec.side,
                    contracts=dec.contracts,
                    limit_price=dec.limit_price,
                    model_prob=dec.model_prob,
                    market_implied_prob=dec.market_implied,
                    edge=dec.edge,
                    ev_per_contract=dec.ev_per_contract,
                    kelly_fraction=dec.kelly_fraction,
                )
                pos = self.executor.enter_position(
                    ticker=dec.ticker,
                    decision=entry_dec,
                )
                if pos:
                    self._weather_positions[dec.ticker] = dec
                    logger.info(
                        f"Weather position opened: {pos.ticker} | "
                        f"{pos.side.upper()} x{pos.contracts} @ {pos.entry_price}¢ | "
                        f"{dec.city_name} {dec.forecast_date} | "
                        f"forecast={dec.forecast_high:.0f}°F σ={dec.sigma:.1f}"
                    )

    # ------------------------------------------------------------------
    # Arbitrage Scanner (v2 — monitoring & logging, trade execution TODO)
    # ------------------------------------------------------------------

    def _arb_scan(self, balance: float) -> None:
        """Scan all Kalshi events for mathematical arbitrage."""
        if self.arb_scanner is None:
            return

        try:
            opportunities = self.arb_scanner.scan()
        except Exception as e:
            logger.error(f"Arb scan failed: {e}")
            return

        profitable = [o for o in opportunities if o.is_profitable]

        if profitable:
            logger.info(
                "ARB ALERT: %d profitable arbitrage opportunities found!",
                len(profitable),
            )
            for opp in profitable[:3]:
                logger.info(
                    "  %s | %s | cost=%d¢ net=%.1f¢ (%.2f%%) | %s",
                    opp.arb_type.upper(),
                    opp.event_ticker,
                    opp.combined_cost_cents,
                    opp.net_profit_cents,
                    opp.net_profit_pct,
                    opp.event_title[:60],
                )

            # Record arb opportunities in journal
            if self.journal:
                for opp in profitable[:5]:
                    self.journal.record_prediction(
                        market_ticker=opp.market_tickers[0] if opp.market_tickers else opp.event_ticker,
                        event_ticker=opp.event_ticker,
                        market_title=opp.event_title,
                        category="arbitrage",
                        model_prob=1.0,  # Arb = guaranteed
                        market_price=opp.combined_cost_cents / 100.0,
                        model_source=f"arb_{opp.arb_type}",
                        forecast_details={
                            "arb_type": opp.arb_type,
                            "combined_cost": opp.combined_cost_cents,
                            "net_profit_cents": opp.net_profit_cents,
                        },
                        traded=False,  # Not auto-executing arbs yet
                    )

    # ------------------------------------------------------------------
    # Status Logging
    # ------------------------------------------------------------------

    def _log_status(self) -> None:
        """Log periodic status update."""
        parts = [self.risk.status_string()]

        if self.paper_mode:
            parts.append(self.executor.stats_string())

        if self.journal:
            stats = self.journal.get_stats()
            parts.append(
                f"Journal: {stats.total_predictions} predictions, "
                f"{stats.resolved_predictions} resolved, "
                f"Brier={stats.mean_brier_score:.4f}, "
                f"PnL=${stats.total_pnl:+.2f}"
            )

        if self.arb_scanner:
            summary = self.arb_scanner.get_scan_summary()
            parts.append(
                f"Arb: {summary['opportunities_found']} found, "
                f"{summary['profitable_count']} profitable"
            )

        logger.info(" | ".join(parts))

    # ------------------------------------------------------------------
    # Public accessors for dashboard/API
    # ------------------------------------------------------------------

    def get_weather_summary(self) -> dict:
        """Return weather module status for the API/dashboard."""
        result = {
            "enabled": _WEATHER_AVAILABLE and WEATHER is not None and WEATHER.enabled,
            "paper_mode": WEATHER.paper_mode if WEATHER else True,
            "weather_positions": len(self._weather_positions),
            "scanner_data": {},
        }
        if self.weather_scanner:
            result["scanner_data"] = self.weather_scanner.get_scan_summary()
        return result

    def get_arb_summary(self) -> dict:
        """Return arb scanner summary for dashboard."""
        if self.arb_scanner:
            return self.arb_scanner.get_scan_summary()
        return {"enabled": False}

    def get_journal_summary(self) -> dict:
        """Return trade journal summary for dashboard."""
        if self.journal:
            return self.journal.get_summary_for_api()
        return {"enabled": False}

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """Graceful shutdown — save state, log summary."""
        self._running = False
        logger.info("Shutting down bot...")

        self.state.save()

        print(self.state.summary())
        logger.info(
            f"Shutdown complete | "
            f"iterations={self._iteration} | "
            f"total_trades={self.state.total_trades()} | "
            f"total_pnl=${self.state.total_pnl():+.2f}"
        )

    # ------------------------------------------------------------------
    # Single-shot helpers (for run.py CLI commands)
    # ------------------------------------------------------------------

    def scan_once(self) -> None:
        """Scan markets and print results."""
        setup_logging(config.LOG_LEVEL)

        print("\nScanning Kalshi weather markets...\n")
        if self.weather_scanner:
            opportunities = self.weather_scanner.scan(force=True)
            if not opportunities:
                print("No weather opportunities found.")
            else:
                print(f"{'TICKER':<30} {'CITY':<15} {'DATE':<12} {'MODEL':>6} {'MKT':>5} {'EDGE':>6} {'EV':>6} {'SCORE':>7}")
                print("-" * 95)
                for opp in opportunities[:20]:
                    p = opp.pricing
                    m = opp.market
                    print(
                        f"{m.ticker:<30} {m.city_name:<15} {m.settlement_date:<12} "
                        f"{p.model_prob:>6.3f} {p.market_implied_prob:>5.3f} "
                        f"{p.fee_adjusted_edge:>+6.3f} {p.ev_per_contract:>+6.1f} "
                        f"{opp.score:>7.1f}"
                    )

        if self.arb_scanner:
            print("\nScanning for arbitrage...\n")
            opps = self.arb_scanner.scan()
            profitable = [o for o in opps if o.is_profitable]
            if profitable:
                print(f"FOUND {len(profitable)} PROFITABLE ARBITRAGE OPPORTUNITIES:")
                for o in profitable[:10]:
                    print(f"  {o.arb_type.upper()}: {o.event_title[:50]} | cost={o.combined_cost_cents}¢ net={o.net_profit_cents:.1f}¢")
            else:
                print("No profitable arbitrage found.")

    def show_status(self) -> None:
        """Print current positions and P&L."""
        setup_logging("WARNING")
        print(self.state.summary())
        if self.journal:
            stats = self.journal.get_stats()
            print(f"\nTrade Journal: {stats.total_predictions} predictions, Brier={stats.mean_brier_score:.4f}")

    def test_connection(self) -> bool:
        """Test API connectivity."""
        setup_logging(config.LOG_LEVEL)
        if not self._api_available:
            print("ERROR: API keys not configured. Check .env file and private key path.")
            return False
        result = self.client.test_connection()
        if result:
            print("✓ Kalshi API connection successful")
        else:
            print("✗ Kalshi API connection failed — check logs")
        return result
