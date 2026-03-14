"""
btc_strategy.py — BTC Hourly Price Farmer for Kalshi KXBTCD markets.

Strategy:
  "High-probability farming" — buy YES on strikes well below BTC price,
  or buy NO on strikes well above BTC price. The farther BTC is from the
  strike, the higher the probability of the contract settling in our favor.

Market structure:
  - Series: KXBTCD (Bitcoin price above/below at a specific hour)
  - Events: KXBTCD-26MAR1407 = "Bitcoin price on Mar 14 at 7am EDT?"
  - Markets: KXBTCD-26MAR1407-T70499.99 = "$70,500 or above"
  - Settlement: Coinbase BTC/USD spot price at the stated time
  - Hourly events settle every hour (6am, 7am, 8am, ... 5pm EDT)
  - $250 strike intervals for hourly, $500 for daily

Model:
  Uses log-normal model of BTC price at settlement time to estimate
  probability that BTC finishes above/below each strike. Compares model
  probability to market price to find edge.

  Key insight: For a contract settling in <2 hours, if BTC is $2,000+ above
  the strike, the probability of YES is ~95%+. The market often prices these
  at 90-93c, giving 2-5c of edge per contract.
"""

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BTCConfig:
    """Configuration for BTC hourly price farming."""
    enabled: bool = True
    paper_mode: bool = True

    # Series to scan
    series_ticker: str = "KXBTCD"

    # Scan interval (seconds) — check every 60s for new hourly markets
    scan_interval_seconds: float = 60.0

    # Only trade markets settling within this window (seconds)
    # 24 hours covers hourly + daily events
    max_time_to_expiry_seconds: float = 24 * 3600

    # Minimum time to expiry — don't trade markets about to expire
    min_time_to_expiry_seconds: float = 60.0

    # Log-normal model parameters
    # Annualized volatility of BTC (approximate, used for short-term estimates)
    annual_volatility: float = 0.80  # 80% annualized vol

    # Minimum edge to place a trade (model prob - market price)
    min_edge: float = 0.03  # 3 cents minimum edge

    # Maximum position size per contract (in dollars)
    max_position_dollars: float = 50.0

    # Maximum contracts per market
    max_contracts_per_market: int = 10

    # Maximum total exposure in BTC markets
    max_total_exposure: float = 500.0

    # Coinbase API for BTC price
    coinbase_price_url: str = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

    # Price cache TTL (seconds)
    price_cache_ttl: float = 5.0


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class BTCMarket:
    """Represents a single KXBTCD market (one strike)."""
    ticker: str
    event_ticker: str
    strike: float          # The BTC price threshold (e.g., 70499.99 means above $70,500)
    yes_price: float       # Current YES price in cents (0-100)
    no_price: float        # Current NO price in cents (0-100)
    expiry_ts: float       # Unix timestamp of settlement
    volume: int            # Total volume
    open_interest: int     # Open interest

    @property
    def time_to_expiry(self) -> float:
        """Seconds until settlement."""
        return self.expiry_ts - time.time()

    @property
    def mid_yes(self) -> float:
        """Mid price for YES in probability (0-1)."""
        return self.yes_price / 100.0


@dataclass
class BTCOpportunity:
    """A trading opportunity identified by the model."""
    market: BTCMarket
    side: str              # 'YES' or 'NO'
    model_prob: float      # Model's probability estimate
    market_prob: float     # Market's implied probability
    edge: float            # model_prob - market_prob
    btc_price: float       # BTC price at time of evaluation
    suggested_contracts: int
    suggested_price: int   # In cents


@dataclass
class BTCPosition:
    """An open position in a BTC market."""
    ticker: str
    side: str
    contracts: int
    entry_price: float     # Cents
    entry_time: float
    order_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Log-normal Price Model
# ---------------------------------------------------------------------------

class LogNormalModel:
    """
    Log-normal model for BTC price probability estimation.

    P(BTC_T > K) = N(d2)
    where:
      d2 = (ln(S/K) + (mu - 0.5*sigma^2)*T) / (sigma * sqrt(T))
      S = current BTC price
      K = strike
      T = time to expiry in years
      sigma = annual volatility
      mu = drift (assume 0 for short horizons)
    """

    def __init__(self, annual_vol: float = 0.80):
        self.annual_vol = annual_vol

    def prob_above(self, btc_price: float, strike: float, time_to_expiry_seconds: float) -> float:
        """
        Probability that BTC price will be ABOVE strike at expiry.

        Args:
            btc_price: Current BTC/USD price
            strike: The strike price (K)
            time_to_expiry_seconds: Seconds until settlement

        Returns:
            Probability (0.0 to 1.0)
        """
        if time_to_expiry_seconds <= 0:
            return 1.0 if btc_price > strike else 0.0

        T = time_to_expiry_seconds / (365.25 * 24 * 3600)
        sigma = self.annual_vol

        if btc_price <= 0 or strike <= 0:
            return 0.0

        # d2 = (ln(S/K) + (mu - 0.5*sigma^2)*T) / (sigma * sqrt(T))
        # Using mu=0 (risk-neutral drift for short horizon)
        ln_ratio = math.log(btc_price / strike)
        drift_term = -0.5 * sigma * sigma * T
        denominator = sigma * math.sqrt(T)

        if denominator == 0:
            return 1.0 if btc_price > strike else 0.0

        d2 = (ln_ratio + drift_term) / denominator
        return self._norm_cdf(d2)

    def prob_below(self, btc_price: float, strike: float, time_to_expiry_seconds: float) -> float:
        """Probability that BTC will be BELOW strike at expiry."""
        return 1.0 - self.prob_above(btc_price, strike, time_to_expiry_seconds)

    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal CDF using math.erf."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


# ---------------------------------------------------------------------------
# Coinbase Price Feed
# ---------------------------------------------------------------------------

class CoinbasePriceFeed:
    """Fetches BTC/USD spot price from Coinbase API."""

    def __init__(self, url: str, cache_ttl: float = 5.0):
        self.url = url
        self.cache_ttl = cache_ttl
        self._cached_price: Optional[float] = None
        self._cache_time: float = 0.0

    def get_price(self) -> Optional[float]:
        """
        Returns current BTC/USD price.
        Uses cache if fresh enough.
        """
        now = time.time()
        if self._cached_price and (now - self._cache_time) < self.cache_ttl:
            return self._cached_price

        price = self._fetch_price()
        if price:
            self._cached_price = price
            self._cache_time = now
        return price

    def _fetch_price(self) -> Optional[float]:
        """Fetch price from Coinbase API."""
        try:
            import urllib.request
            import urllib.error

            req = urllib.request.Request(
                self.url,
                headers={"User-Agent": "FreyjaBot/2.2"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                price = float(data["data"]["amount"])
                logger.debug(f"BTC price from Coinbase: ${price:,.0f}")
                return price
        except Exception as e:
            logger.warning(f"Failed to fetch BTC price from Coinbase: {e}")
            return None


import json  # needed for CoinbasePriceFeed._fetch_price


# ---------------------------------------------------------------------------
# Orderbook Scanner
# ---------------------------------------------------------------------------

class OrderbookScanner:
    """
    Scans the Kalshi orderbook for KXBTCD markets.
    Identifies markets with favorable prices relative to model.
    """

    def __init__(self, kalshi_client, config: BTCConfig):
        self.client = kalshi_client
        self.config = config
        self.model = LogNormalModel(annual_vol=config.annual_volatility)
        self.price_feed = CoinbasePriceFeed(
            url=config.coinbase_price_url,
            cache_ttl=config.price_cache_ttl
        )

    def scan_markets(self) -> List[BTCOpportunity]:
        """
        Scan all active KXBTCD markets and return opportunities.

        Returns list of BTCOpportunity sorted by edge (descending).
        """
        btc_price = self.price_feed.get_price()
        if btc_price is None:
            logger.warning("Cannot scan BTC markets: no BTC price available")
            return []

        logger.info(f"Scanning KXBTCD markets. BTC=${btc_price:,.0f}")

        markets = self._fetch_active_markets()
        if not markets:
            logger.info("No active KXBTCD markets found")
            return []

        opportunities = []
        for market in markets:
            opp = self._evaluate_market(market, btc_price)
            if opp and opp.edge >= self.config.min_edge:
                opportunities.append(opp)

        # Sort by edge descending
        opportunities.sort(key=lambda o: o.edge, reverse=True)

        if opportunities:
            logger.info(f"Found {len(opportunities)} BTC opportunities")
            for opp in opportunities[:5]:  # Log top 5
                logger.info(
                    f"  {opp.market.ticker} {opp.side}: "
                    f"model={opp.model_prob:.3f} market={opp.market_prob:.3f} "
                    f"edge={opp.edge:.3f} BTC=${opp.btc_price:,.0f}"
                )

        return opportunities

    def _fetch_active_markets(self) -> List[BTCMarket]:
        """Fetch all active KXBTCD markets from Kalshi."""
        try:
            resp = self.client.get_markets(
                series_ticker=self.config.series_ticker,
                status="open"
            )

            markets = []
            for m in resp.get("markets", []):
                try:
                    btc_market = self._parse_market(m)
                    if btc_market:
                        markets.append(btc_market)
                except Exception as e:
                    logger.debug(f"Failed to parse market {m.get('ticker', '?')}: {e}")

            logger.debug(f"Fetched {len(markets)} active KXBTCD markets")
            return markets

        except Exception as e:
            logger.error(f"Failed to fetch KXBTCD markets: {e}")
            return []

    def _parse_market(self, m: dict) -> Optional[BTCMarket]:
        """
        Parse a Kalshi market response into BTCMarket.

        Market ticker format: KXBTCD-26MAR1407-T70499.99
        The '-T' prefix + number = the strike threshold.
        """
        ticker = m.get("ticker", "")
        if not ticker.startswith(self.config.series_ticker):
            return None

        # Parse strike from ticker
        # Format: KXBTCD-26MAR1407-T70499.99
        parts = ticker.split("-")
        if len(parts) < 3:
            return None

        strike_part = parts[-1]  # e.g., "T70499.99"
        if not strike_part.startswith("T"):
            return None

        try:
            strike = float(strike_part[1:])  # Remove 'T', parse float
        except ValueError:
            return None

        # Parse expiry
        close_time = m.get("close_time") or m.get("expiration_time")
        if not close_time:
            return None

        try:
            # Parse ISO 8601 format
            import datetime
            expiry_ts = datetime.datetime.fromisoformat(
                close_time.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return None

        # Check time constraints
        now = time.time()
        tte = expiry_ts - now
        if tte < self.config.min_time_to_expiry_seconds:
            return None
        if tte > self.config.max_time_to_expiry_seconds:
            return None

        # Parse prices
        yes_price = m.get("yes_ask") or m.get("last_price") or 50
        no_price = m.get("no_ask") or (100 - yes_price)

        return BTCMarket(
            ticker=ticker,
            event_ticker=m.get("event_ticker", ""),
            strike=strike,
            yes_price=float(yes_price),
            no_price=float(no_price),
            expiry_ts=expiry_ts,
            volume=m.get("volume", 0),
            open_interest=m.get("open_interest", 0)
        )

    def _evaluate_market(
        self, market: BTCMarket, btc_price: float
    ) -> Optional[BTCOpportunity]:
        """
        Evaluate a single market for edge.

        Considers:
        - YES: buy if model says prob > market price (BTC likely to stay above)
        - NO: buy if model says 1-prob > market NO price (BTC likely to stay below)
        """
        tte = market.time_to_expiry
        if tte <= 0:
            return None

        model_prob_above = self.model.prob_above(btc_price, market.strike, tte)

        # Evaluate YES side
        yes_market_prob = market.yes_price / 100.0
        yes_edge = model_prob_above - yes_market_prob

        # Evaluate NO side
        no_market_prob = market.no_price / 100.0
        no_edge = (1.0 - model_prob_above) - no_market_prob

        # Pick best side
        if yes_edge >= no_edge and yes_edge >= self.config.min_edge:
            side = "YES"
            edge = yes_edge
            market_prob = yes_market_prob
            model_prob = model_prob_above
            suggested_price = market.yes_price
        elif no_edge >= self.config.min_edge:
            side = "NO"
            edge = no_edge
            market_prob = no_market_prob
            model_prob = 1.0 - model_prob_above
            suggested_price = market.no_price
        else:
            return None

        # Size the position
        # Simple: risk (100 - price) per contract, target $max_position_dollars
        cost_per_contract = suggested_price  # cents
        if cost_per_contract <= 0:
            return None

        max_contracts = min(
            self.config.max_contracts_per_market,
            int(self.config.max_position_dollars / cost_per_contract)
        )
        max_contracts = max(1, max_contracts)

        return BTCOpportunity(
            market=market,
            side=side,
            model_prob=model_prob,
            market_prob=market_prob,
            edge=edge,
            btc_price=btc_price,
            suggested_contracts=max_contracts,
            suggested_price=int(suggested_price)
        )


# ---------------------------------------------------------------------------
# BTC Strategy Module
# ---------------------------------------------------------------------------

class BTCStrategy:
    """
    Main BTC strategy module. Integrates with bot.py.

    Usage:
        config = BTCConfig(paper_mode=True)
        strategy = BTCStrategy(kalshi_client, config)

        # In main loop:
        trades = strategy.run_cycle(portfolio)
    """

    def __init__(self, kalshi_client, config: BTCConfig):
        self.client = kalshi_client
        self.config = config
        self.scanner = OrderbookScanner(kalshi_client, config)
        self.positions: List[BTCPosition] = []
        self._last_scan: float = 0.0
        self._total_exposure: float = 0.0

    def run_cycle(self, portfolio: dict) -> List[dict]:
        """
        Run one cycle of the BTC strategy.

        Args:
            portfolio: Current portfolio state from bot.py

        Returns:
            List of trade dicts executed this cycle.
        """
        if not self.config.enabled:
            return []

        now = time.time()
        if now - self._last_scan < self.config.scan_interval_seconds:
            return []  # Not time to scan yet

        self._last_scan = now

        try:
            return self._execute_cycle(portfolio)
        except Exception as e:
            logger.error(f"BTC strategy cycle error: {e}", exc_info=True)
            return []

    def _execute_cycle(self, portfolio: dict) -> List[dict]:
        """Internal cycle execution."""
        # Check total exposure
        if self._total_exposure >= self.config.max_total_exposure:
            logger.info(f"BTC: max exposure reached (${self._total_exposure:.0f})")
            return []

        # Get balance
        balance = portfolio.get("balance", 0)
        if balance < 10:  # Need at least $10
            logger.info("BTC: insufficient balance")
            return []

        # Scan for opportunities
        opportunities = self.scanner.scan_markets()
        if not opportunities:
            return []

        trades_executed = []
        remaining_exposure = self.config.max_total_exposure - self._total_exposure

        for opp in opportunities:
            if remaining_exposure <= 0:
                break

            # Scale contracts by remaining exposure
            max_cost = min(
                self.config.max_position_dollars,
                remaining_exposure
            )
            contracts = min(
                opp.suggested_contracts,
                int(max_cost / opp.suggested_price) if opp.suggested_price > 0 else 0
            )

            if contracts <= 0:
                continue

            trade = self._place_trade(opp, contracts)
            if trade:
                trades_executed.append(trade)
                remaining_exposure -= trade.get("cost", 0)
                self._total_exposure += trade.get("cost", 0)

        return trades_executed

    def _place_trade(self, opp: BTCOpportunity, contracts: int) -> Optional[dict]:
        """
        Place a trade for an opportunity.

        Returns trade dict if successful, None otherwise.
        """
        ticker = opp.market.ticker
        side = opp.side
        price = opp.suggested_price
        cost = contracts * price  # In cents

        logger.info(
            f"BTC TRADE: {ticker} {side} x{contracts} @ {price}c "
            f"(edge={opp.edge:.3f}, model={opp.model_prob:.3f})"
        )

        if self.config.paper_mode:
            # Paper trade — just log and return
            trade = {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "price": price,
                "cost": cost / 100.0,  # Convert to dollars
                "model_prob": opp.model_prob,
                "market_prob": opp.market_prob,
                "edge": opp.edge,
                "btc_price": opp.btc_price,
                "paper": True,
                "ts": time.time()
            }
            logger.info(f"[PAPER] BTC trade logged: {trade}")
            return trade

        # Live trading
        try:
            resp = self.client.create_order(
                ticker=ticker,
                side=side.lower(),
                count=contracts,
                type="limit",
                yes_price=price if side == "YES" else None,
                no_price=price if side == "NO" else None
            )
            order_id = resp.get("order", {}).get("order_id")
            logger.info(f"BTC order placed: {order_id}")

            # Track position
            pos = BTCPosition(
                ticker=ticker,
                side=side,
                contracts=contracts,
                entry_price=price,
                entry_time=time.time(),
                order_id=order_id
            )
            self.positions.append(pos)

            return {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "price": price,
                "cost": cost / 100.0,
                "order_id": order_id,
                "model_prob": opp.model_prob,
                "edge": opp.edge,
                "btc_price": opp.btc_price,
                "paper": False,
                "ts": time.time()
            }

        except Exception as e:
            logger.error(f"Failed to place BTC order {ticker} {side}: {e}")
            return None

    def get_status(self) -> dict:
        """Return current BTC strategy status."""
        return {
            "enabled": self.config.enabled,
            "paper_mode": self.config.paper_mode,
            "positions": len(self.positions),
            "total_exposure": self._total_exposure,
            "max_exposure": self.config.max_total_exposure,
            "last_scan": self._last_scan,
            "btc_price": self.scanner.price_feed.get_price()
        }
