"""
sports_scanner.py — Live Game Monitor + Market Matcher for Freyja Sports Module

This scanner runs as part of the bot's main loop:
  1. Checks ESPN for live NBA games
  2. Runs momentum detection
  3. Matches signals to Kalshi spread/total markets
  4. Returns tradeable opportunities to bot.py for execution

Integration:
  bot.py calls sports_scanner.scan() every SCAN_INTERVAL seconds
  Results are SportsOpportunity objects with trade decisions
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sports_strategy import (
    SportsTrader,
    SportsConfig,
    SportsOpportunity,
    ESPNGame,
    SPORTS,
)

logger = logging.getLogger(__name__)


@dataclass
class SportsTrade:
    """A resolved sports trade decision ready for execution."""
    ticker: str
    side: str           # "yes" or "no"
    contracts: int
    limit_price: int    # In cents (1-99)
    
    # Context
    game_id: str
    game_label: str     # "DEN@LAL"
    market_type: str    # "spread" or "total"
    signal_type: str    # "scoring_run", "win_prob_shift", "drought_break"
    signal_details: str
    
    # Analytics
    model_prob: float
    market_prob: float
    edge: float
    ev_per_contract: float
    volume: int
    
    # Metadata
    should_trade: bool = True
    reason: str = ""
    timestamp: float = field(default_factory=time.time)


class SportsScanner:
    """
    Orchestrates sports scanning for the bot's main loop.
    Wraps SportsTrader with position tracking and cooldown logic.
    """
    
    def __init__(self, config: SportsConfig = None):
        self.config = config or SPORTS
        self.trader = SportsTrader(self.config)
        
        # Position tracking
        self._active_positions: Dict[str, SportsTrade] = {}
        self._completed_trades: List[Dict] = []
        self._total_sports_pnl: float = 0.0
        
        # Scan state
        self._last_scan_time: float = 0.0
        self._scan_count: int = 0
        self._opportunities_found: int = 0
        self._trades_executed: int = 0
        
        # Cooldown: don't re-enter same market within N minutes
        self._ticker_cooldown: Dict[str, float] = {}
        self._cooldown_minutes: float = 15.0
        
        logger.info(
            "Sports scanner initialized | "
            f"interval={self.config.scan_interval_seconds}s | "
            f"min_edge={self.config.min_edge:.0%} | "
            f"paper={'ON' if self.config.paper_mode else 'OFF'}"
        )
    
    def scan(self, force: bool = False) -> List[SportsTrade]:
        """
        Run a full sports scan cycle. Returns list of SportsTrade decisions.
        
        Called by bot.py every scan_interval seconds.
        """
        now = time.time()
        
        # Respect scan interval unless forced
        if not force and (now - self._last_scan_time) < self.config.scan_interval_seconds:
            return []
        
        self._scan_count += 1
        self._last_scan_time = now
        
        try:
            # Run the full ESPN → Kalshi scan
            opportunities = self.trader.scan()
        except Exception as e:
            logger.error(f"Sports scan failed: {e}", exc_info=True)
            return []
        
        if not opportunities:
            return []
        
        self._opportunities_found += len(opportunities)
        
        # Convert to SportsTrade objects with filtering
        trades = []
        for opp in opportunities:
            trade = self._evaluate_opportunity(opp)
            if trade and trade.should_trade:
                trades.append(trade)
        
        if trades:
            logger.info(
                f"Sports scan found {len(trades)} tradeable opportunities"
            )
        
        return trades
    
    def _evaluate_opportunity(self, opp: SportsOpportunity) -> Optional[SportsTrade]:
        """Convert a SportsOpportunity into a SportsTrade with additional filtering."""
        
        # Check cooldown
        now = time.time()
        if opp.market_ticker in self._ticker_cooldown:
            cooldown_until = self._ticker_cooldown[opp.market_ticker]
            if now < cooldown_until:
                logger.debug(f"Cooldown active for {opp.market_ticker}")
                return None
        
        # Check if we already have a position in this ticker
        if opp.market_ticker in self._active_positions:
            logger.debug(f"Already have position in {opp.market_ticker}")
            return None
        
        # Check concurrent position limit
        if len(self._active_positions) >= self.config.max_concurrent_sports:
            logger.debug("Max concurrent sports positions reached")
            return None
        
        # Check total exposure
        current_exposure = sum(
            (t.limit_price / 100.0) * t.contracts
            for t in self._active_positions.values()
        )
        trade_cost = (opp.limit_price / 100.0) * opp.contracts
        if current_exposure + trade_cost > self.config.max_total_exposure_dollars:
            logger.debug("Sports exposure limit reached")
            return None
        
        game_label = f"{opp.game.away_team}@{opp.game.home_team}"
        
        return SportsTrade(
            ticker=opp.market_ticker,
            side=opp.side,
            contracts=opp.contracts,
            limit_price=opp.limit_price,
            game_id=opp.game.game_id,
            game_label=game_label,
            market_type=opp.market_type,
            signal_type=opp.signal.signal_type,
            signal_details=opp.signal.details,
            model_prob=opp.model_prob,
            market_prob=opp.market_prob,
            edge=opp.edge,
            ev_per_contract=opp.ev_per_contract,
            volume=opp.volume,
            should_trade=opp.should_trade,
            reason=opp.reason,
        )
    
    def record_entry(self, trade: SportsTrade) -> None:
        """Record that a trade was executed."""
        self._active_positions[trade.ticker] = trade
        self._trades_executed += 1
        logger.info(
            f"SPORTS ENTRY: {trade.side.upper()} {trade.ticker} "
            f"x{trade.contracts} @ {trade.limit_price}¢ | "
            f"{trade.game_label} {trade.market_type} | "
            f"edge={trade.edge:.1%} | {trade.signal_type}"
        )
    
    def record_exit(self, ticker: str, exit_price: float, pnl: float) -> None:
        """Record that a position was exited."""
        trade = self._active_positions.pop(ticker, None)
        if trade:
            self._total_sports_pnl += pnl
            self._completed_trades.append({
                "ticker": ticker,
                "side": trade.side,
                "entry_price": trade.limit_price,
                "exit_price": exit_price,
                "contracts": trade.contracts,
                "pnl": pnl,
                "game_label": trade.game_label,
                "market_type": trade.market_type,
                "signal_type": trade.signal_type,
                "timestamp": time.time(),
            })
            # Set cooldown
            self._ticker_cooldown[ticker] = time.time() + self._cooldown_minutes * 60
            
            logger.info(
                f"SPORTS EXIT: {ticker} | PnL=${pnl:+.2f} | "
                f"entry={trade.limit_price}¢ exit={exit_price:.0f}¢"
            )
    
    def get_scan_summary(self) -> dict:
        """Return summary for the API/dashboard."""
        trader_summary = self.trader.get_scan_summary()
        
        return {
            **trader_summary,
            "scanner_stats": {
                "scan_count": self._scan_count,
                "last_scan": self._last_scan_time,
                "opportunities_found": self._opportunities_found,
                "trades_executed": self._trades_executed,
                "active_positions": len(self._active_positions),
                "total_pnl": round(self._total_sports_pnl, 2),
                "completed_trades": len(self._completed_trades),
            },
            "active_positions": [
                {
                    "ticker": t.ticker,
                    "side": t.side,
                    "contracts": t.contracts,
                    "entry_price": t.limit_price,
                    "game": t.game_label,
                    "type": t.market_type,
                    "signal": t.signal_type,
                    "edge": round(t.edge, 4),
                }
                for t in self._active_positions.values()
            ],
            "recent_trades": self._completed_trades[-10:],
        }
    
    def get_live_games(self) -> List[dict]:
        """Quick accessor for live games data for the dashboard."""
        summary = self.trader.get_scan_summary()
        return summary.get("games", [])