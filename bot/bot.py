"""
bot.py — Main loop orchestrator for the Kalshi crypto trading bot.

Architecture:
  Every LOOP_INTERVAL seconds:
    1. Check kill switch + risk gates
    2. Refresh price feeds (Binance)
    3. Monitor open positions → evaluate exits
    4. Scan for new markets (every SCAN_INTERVAL)
    5. For each candidate market → compute signals → evaluate entry
    6. Place orders via Executor (or PaperTrader)
    7. Log status

The bot uses a single-threaded event loop for simplicity and predictability.
All blocking calls (API, sleep) happen sequentially.
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
from indicators import compute_composite_signal
from market_scanner import MarketScanner, MarketCandidate
from paper_trader import PaperTrader
from price_feed import PriceFeedManager
from risk_manager import RiskManager
from state import BotState
from strategy import should_enter, should_exit, OpenPosition

# IYKYK Markets integration (optional — graceful degradation)
try:
    from iykyk_client import IYKYKClient
    from iykyk_signals import IYKYKSignalProvider
    _IYKYK_AVAILABLE = True
except ImportError:
    _IYKYK_AVAILABLE = False
    IYKYKClient = None
    IYKYKSignalProvider = None

# Weather module integration (optional — graceful degradation)
try:
    from weather_config import WEATHER, CITIES
    from weather_scanner import WeatherScanner, WeatherOpportunity
    from weather_strategy import WeatherTradeDecision
    _WEATHER_AVAILABLE = True
except ImportError:
    _WEATHER_AVAILABLE = False
    WEATHER = None
    WeatherScanner = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with console + file handlers."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt=config.LOG_FORMAT,
        datefmt=config.LOG_DATE_FORMAT,
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(log_level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # File handler
    fh = logging.FileHandler(config.LOG_FILE, mode="a", encoding="utf-8")
    fh.setLevel(log_level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # Quiet down noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

BANNER = r"""
 ██╗  ██╗ █████╗ ██╗     ██████╗██╗  ██╗██╗    ██████╗  ██████╗ ███████╗
 ██║ ██╔╝██╔══██╗██║     ██╔════╝██║  ██║██║    ██╔══██╗██╔═══██╗╚══██╔══╝
 █████╔╝ ███████║██║     █████╗ ███████║██║    ██████╔╝██║   ██║   ██║
 ██╔═██╗ ██╔══██║██║     ╚════██╗██╔══██║██║    ██╔══██╗██║   ██║   ██║
 ██║  ██╗██║  ██║██████╗███████║██║  ██║██║    ██████╔╝╚██████╔╝   ██║
 ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝    ╚════╝  ╚════╝    ╚═╝
 Kalshi BTC/ETH/SOL/XRP Momentum Trader + IYKYK + VPIN + Weather v3.0
"""


def print_startup_banner(mode: str, balance: Optional[float]) -> None:
    """Print startup information."""
    print(BANNER)
    print("=" * 72)
    print(f"  Mode          : {mode}")
    print(f"  API URL       : {config.get_base_url()}")
    print(f"  Balance       : ${balance:.2f}" if balance else "  Balance       : N/A")
    print(f"  Series        : {', '.join(config.SUPPORTED_SERIES)}")
    print(f"  Min Confidence: {config.STRATEGY.min_confidence:.0%}")
    print(f"  Contract Range: {config.STRATEGY.min_contract_price}–{config.STRATEGY.max_contract_price}¢")
    print(f"  Kelly Fraction: {config.STRATEGY.kelly_fraction:.0%}")
    print(f"  Stop Loss     : {config.STRATEGY.stop_loss_pct:.0%}")
    print(f"  Profit Target : {config.STRATEGY.profit_target_pct:.0%}")
    print(f"  Max Positions : {config.RISK.max_concurrent_positions}")
    print(f"  Daily Loss Lim: ${config.RISK.daily_loss_limit_dollars:.2f}")
    print(f"  VPIN Exit Thr : {config.STRATEGY.vpin_exit_threshold:.2f}")
    print(f"  Log File      : {config.LOG_FILE}")
    print(f"  State File    : {config.STATE_FILE}")
    print(f"  IYKYK Markets : {'ENABLED' if config.IYKYK_ENABLED else 'DISABLED'}")
    print(f"  Kill Switch   : delete {config.STOP_FILE} to resume, create to stop")
    if _WEATHER_AVAILABLE and WEATHER and WEATHER.enabled:
        print(f"  Weather Module: ENABLED ({len(WEATHER.get_active_city_codes())} cities)")
        print(f"  Weather Mode  : {'PAPER' if WEATHER.paper_mode else 'LIVE'}")
        print(f"  Weather Edge  : {WEATHER.min_edge:.0%} min / Kelly {WEATHER.kelly_fraction:.0%}")
    else:
        print(f"  Weather Module: DISABLED")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class KalshiBot:
    """
    Main bot orchestrator.

    Lifecycle:
      bot = KalshiBot(use_live=False)
      bot.run()          # blocking loop
    """

    def __init__(self, use_live: bool = False, paper_mode: bool = True):
        self.use_live = use_live
        self.paper_mode = paper_mode and not use_live
        self.mode_str = "LIVE" if use_live else ("PAPER" if self.paper_mode else "DEMO")

        # Validate credentials
        if not config.API_KEY_ID and not self.paper_mode:
            # In pure paper mode we still need the scanner/price feed
            # but can skip Kalshi auth for position sizing
            pass

        # Initialize components
        self.state = BotState()

        # Weather module
        self.weather_scanner = None
        self._last_weather_scan = 0.0
        self._weather_positions: dict = {}  # ticker → trade decision

        # Auth + Kalshi client (even in paper mode — for market scanning)
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

        # Subsystems
        self.price_feeds = PriceFeedManager()
        self.risk = RiskManager(self.state)

        # IYKYK Markets intelligence layer (optional)
        self.iykyk_provider = None
        if config.IYKYK_ENABLED and _IYKYK_AVAILABLE:
            try:
                self.iykyk_provider = IYKYKSignalProvider(IYKYKClient())
                logger.info("IYKYK Markets intelligence layer ENABLED")
            except Exception as e:
                logger.warning(f"IYKYK Markets init failed: {e} — running without IYKYK")

        if self._api_available:
            self.scanner = MarketScanner(self.client)
        else:
            self.scanner = None

        # Weather scanner (doesn't need Kalshi auth — uses public API)
        if _WEATHER_AVAILABLE and WEATHER and WEATHER.enabled:
            try:
                self.weather_scanner = WeatherScanner()
                logger.info("Weather module ENABLED — scanning %d cities",
                            len(WEATHER.get_active_city_codes()))
            except Exception as e:
                logger.warning(f"Weather module init failed: {e}")
                self.weather_scanner = None

        # Order execution: paper or real
        if self.paper_mode:
            self.executor: Union[PaperTrader, Executor] = PaperTrader(self.state)
        else:
            self.executor = Executor(self.client, self.state)

        # Timing
        self._last_scan_time = 0.0
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

        # Get initial balance
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

                # Sleep for the remainder of the loop interval
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
            # Still monitor positions even if new entries are blocked
            self._monitor_positions()
            return

        # --- 4. Refresh price feeds (Binance) ---
        try:
            self.price_feeds.fetch_all()
        except Exception as e:
            logger.warning(f"Price feed update error: {e}")

        # --- 5. Monitor open positions for exits ---
        self._monitor_positions()

        # --- 6. Scan for new markets (rate limited) ---
        if (now - self._last_scan_time) >= config.TIMING.scan_interval_seconds:
            self._scan_and_enter(balance or 0.0)
            self._last_scan_time = now

        # --- 7. Weather market scan (slower cadence) ---
        if self.weather_scanner is not None:
            weather_interval = WEATHER.scan_interval_seconds if WEATHER else 300.0
            if (now - self._last_weather_scan) >= weather_interval:
                try:
                    self._weather_scan_and_enter(balance or 0.0)
                except Exception as e:
                    logger.error(f"Weather scan error: {e}", exc_info=True)
                self._last_weather_scan = now

        # --- 8. Periodic status log ---
        if (now - self._last_status_log) >= 60.0:
            logger.info(self.risk.status_string())
            if self.paper_mode:
                logger.info(self.executor.stats_string())
            self._last_status_log = now

    # ------------------------------------------------------------------
    # Position Monitoring
    # ------------------------------------------------------------------

    def _monitor_positions(self) -> None:
        """
        Check all open positions for exit conditions.
        Fetches fresh market data for each position.
        """
        positions = self.state.get_all_positions()
        if not positions:
            return

        for pos in positions:
            self._evaluate_position(pos)

    def _evaluate_position(self, pos: OpenPosition) -> None:
        """Evaluate a single position for exit."""
        # Fetch fresh market data
        market: Optional[MarketCandidate] = None
        if self._api_available and self.scanner:
            try:
                market = self.scanner.refresh_single(pos.ticker)
            except Exception as e:
                logger.warning(f"Failed to refresh market {pos.ticker}: {e}")

        # Update mark-to-market price in state
        if market is not None:
            current_bid = market.yes_bid if pos.side == "yes" else market.no_bid
            if current_bid is not None:
                self.state.update_position_price(pos.ticker, current_bid)
                pos.current_price = current_bid

        # Get latest signal for VPIN
        series_map = {
            "BTC": "KXBTC15M", "ETH": "KXETH15M",
            "SOL": "KXSOL15M", "XRP": "KXXRP15M",
        }
        ticker_upper = pos.ticker.upper()
        if "ETH" in ticker_upper:
            pos_series = "KXETH15M"
        elif "SOL" in ticker_upper:
            pos_series = "KXSOL15M"
        elif "XRP" in ticker_upper:
            pos_series = "KXXRP15M"
        else:
            pos_series = "KXBTC15M"

        feed = self.price_feeds.get_for_series(pos_series)
        signal = None
        if feed:
            try:
                signal = compute_composite_signal(feed)
            except Exception as e:
                logger.debug(f"Signal compute error for {pos.ticker}: {e}")

        # Evaluate exit conditions (with IYKYK whale anomaly detection if available)
        exit_dec = should_exit(pos, market, signal, iykyk_provider=self.iykyk_provider)

        if exit_dec.should_exit:
            logger.info(f"EXIT TRIGGERED: {pos.ticker} — {exit_dec}")
            self.executor.exit_position(pos, exit_dec)
        else:
            # Log current P&L every 5 iterations (for monitoring)
            pnl_pct = pos.unrealized_pnl_pct() * 100
            logger.debug(
                f"HOLD {pos.ticker} | {pos.side.upper()} x{pos.contracts} | "
                f"entry={pos.entry_price}¢ current={pos.current_price}¢ | "
                f"P&L={pnl_pct:+.1f}% | "
                f"close_in={market.seconds_to_close:.0f}s" if market else
                f"HOLD {pos.ticker} | {pos.side.upper()} x{pos.contracts}"
            )

    # ------------------------------------------------------------------
    # Market Scanning + Entry
    # ------------------------------------------------------------------

    def _scan_and_enter(self, balance: float) -> None:
        """Scan for new market opportunities and enter if conditions are met."""
        if not self._api_available or self.scanner is None:
            return

        try:
            candidates = self.scanner.scan(force=False)
        except Exception as e:
            logger.error(f"Market scan failed: {e}")
            return

        for market in candidates:
            # Skip if we already have a position in this market
            if self.state.has_position(market.ticker):
                continue

            # Skip if max positions reached
            if self.state.position_count() >= config.RISK.max_concurrent_positions:
                logger.debug("Max concurrent positions reached, stopping scan")
                break

            self._evaluate_entry(market, balance)

    def _evaluate_entry(self, market: MarketCandidate, balance: float) -> None:
        """Evaluate whether to enter a specific market."""
        # Get price feed for this market's asset
        feed = self.price_feeds.get_for_series(market.series)
        if feed is None or feed.current_price is None:
            logger.debug(f"No price data for {market.series}, skipping {market.ticker}")
            return

        if feed.is_stale():
            logger.warning(f"Price feed for {market.series} is stale, skipping {market.ticker}")
            return

        # Compute composite signal
        try:
            signal = compute_composite_signal(feed)
        except Exception as e:
            logger.error(f"Signal computation failed for {market.ticker}: {e}")
            return

        if not signal.is_valid:
            logger.debug(f"Invalid signal for {market.ticker}: insufficient data")
            return

        # Log signal quality for monitoring
        logger.info(
            f"Signal for {market.ticker}: composite={signal.composite_prob_up:.3f} "
            f"conf={signal.confidence:.4f} mom={signal.momentum_signal:.3f} "
            f"rsi={signal.rsi_value:.1f} roc5={signal.roc_5:.4f}%" if signal.roc_5 else
            f"Signal for {market.ticker}: composite={signal.composite_prob_up:.3f} "
            f"conf={signal.confidence:.4f}"
        )

        # Compute available capital (risk-adjusted)
        available = min(
            balance - config.RISK.min_balance_dollars,
            config.RISK.max_position_size_dollars,
            self.risk.exposure_remaining(),
        )

        if available <= 0:
            logger.debug(f"No available capital for entry on {market.ticker}")
            return

        # Get entry decision from strategy (with IYKYK if available)
        entry = should_enter(
            market=market,
            signal=signal,
            available_dollars=available,
            existing_position_count=self.state.position_count(),
            iykyk_provider=self.iykyk_provider,
        )

        if not entry.should_enter:
            logger.debug(f"No entry on {market.ticker}: {entry.reason}")
            return

        # Final risk check
        approved, risk_reason = self.risk.pre_trade_check(
            contracts=entry.contracts,
            price_cents=entry.limit_price,
            balance_dollars=balance,
        )
        if not approved:
            logger.info(f"Trade blocked by risk manager: {risk_reason}")
            return

        # Place the order
        logger.info(
            f"ENTERING: {market.ticker} | {entry}"
        )
        pos = self.executor.enter_position(
            ticker=market.ticker,
            decision=entry,
            vpin_at_entry=signal.vpin,
        )

        if pos:
            logger.info(
                f"Position opened: {pos.ticker} | {pos.side.upper()} x{pos.contracts} "
                f"@ {pos.entry_price}¢ | cost=${pos.entry_cost_dollars():.2f}"
            )

    # ------------------------------------------------------------------
    # Weather Market Scanning + Entry
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

        # Calculate available capital for weather trades
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

            # Skip if already positioned on this ticker
            if dec.ticker in self._weather_positions:
                continue
            if self.state.has_position(dec.ticker):
                continue

            # Record prediction for calibration
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

            # Paper trade execution
            logger.info(f"WEATHER ENTRY: {dec}")

            if WEATHER.paper_mode:
                # Use PaperTrader-style execution
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

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """Graceful shutdown — save state, log summary."""
        self._running = False
        logger.info("Shutting down bot...")

        # Save final state
        self.state.save()

        # Log summary
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
        """Scan markets and print results. Used by --scan CLI arg."""
        setup_logging(config.LOG_LEVEL)
        if not self._api_available:
            print("ERROR: Kalshi API not configured. Check .env file.")
            return

        print("\nScanning Kalshi 15-minute crypto markets...\n")
        candidates = self.scanner.scan(force=True)

        if not candidates:
            print("No tradable markets found.")
            return

        print(f"{'TICKER':<30} {'ASSET':<5} {'CLOSE IN':>9} {'YES_ASK':>8} {'NO_ASK':>7} {'VOL':>7} {'SCORE':>7}")
        print("-" * 75)
        for c in candidates:
            mins = c.seconds_to_close / 60
            print(
                f"{c.ticker:<30} {c.asset:<5} {mins:>7.1f}m "
                f"{c.yes_ask or '?':>8} "
                f"{c.no_ask or '?':>7} "
                f"{c.volume:>7} "
                f"{c.score:>7.1f}"
            )

    def show_status(self) -> None:
        """Print current positions and P&L. Used by --status CLI arg."""
        setup_logging("WARNING")  # Quiet for status display
        print(self.state.summary())

    def test_connection(self) -> bool:
        """Test API connectivity. Used by --test CLI arg."""
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
