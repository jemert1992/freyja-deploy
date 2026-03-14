"""
weather_scanner.py — Discovers and monitors Kalshi weather (KXHIGH) markets.

Responsibilities:
  1. Scan all active KXHIGH series across configured cities
  2. Fetch NWS forecasts for each city
  3. Price each bracket using the normal CDF model
  4. Return ranked trade opportunities sorted by edge * EV
  5. Track weather positions and settlements for calibration

Architecture:
  The scanner runs on a slower cadence than crypto (every 5 min vs every 45s)
  because weather markets move slowly and forecasts update hourly.
"""

import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from weather_config import (
    KALSHI_WEATHER_API_BASE,
    KXHIGH_PREFIX,
    CITIES,
    CityConfig,
    WEATHER,
)
from weather_strategy import (
    NWSForecastFetcher,
    ForecastResult,
    BracketPricing,
    WeatherTradeDecision,
    price_brackets,
    evaluate_bracket,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kalshi Weather Market Data Structures
# ---------------------------------------------------------------------------

@dataclass
class WeatherMarket:
    """A single Kalshi weather market (one bracket of a daily high temp event)."""
    ticker: str
    series_ticker: str
    event_ticker: str
    title: str
    city_code: str
    city_name: str
    status: str
    close_time: Optional[datetime]
    settlement_date: str           # "2026-02-28"

    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last_price: Optional[int] = None
    volume: int = 0
    open_interest: int = 0

    # Derived from strategy
    model_prob: float = 0.0
    edge: float = 0.0
    ev_per_contract: float = 0.0


@dataclass
class WeatherOpportunity:
    """A ranked trading opportunity combining market data + model pricing."""
    market: WeatherMarket
    pricing: BracketPricing
    decision: WeatherTradeDecision
    score: float = 0.0            # Ranking score (edge * EV * confidence)

    @property
    def ticker(self) -> str:
        return self.market.ticker

    @property
    def city_code(self) -> str:
        return self.market.city_code


# ---------------------------------------------------------------------------
# Weather Market Scanner
# ---------------------------------------------------------------------------

class WeatherScanner:
    """
    Scans Kalshi for weather (KXHIGH) trading opportunities.

    Workflow per scan cycle:
      1. For each active city, fetch open KXHIGH markets from Kalshi
      2. Fetch NWS forecast for each city
      3. Run the pricing model on each bracket
      4. Rank opportunities by edge quality
      5. Return top opportunities for the bot to execute
    """

    def __init__(self):
        self.forecast_fetcher = NWSForecastFetcher()
        self._last_scan_time: float = 0.0
        self._cached_opportunities: List[WeatherOpportunity] = []
        self._market_cache: Dict[str, List[dict]] = {}  # city_code → raw markets

        # Calibration tracking
        self._predictions: List[dict] = []  # Historical predictions for accuracy tracking

    def _kalshi_get(self, url: str) -> dict:
        """Fetch from Kalshi public API (no auth needed for market data)."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "FreyjaQuantEngine/1.0",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.error(f"Kalshi API HTTP {e.code} for {url}")
            raise
        except Exception as e:
            logger.error(f"Kalshi API error for {url}: {e}")
            raise

    def _fetch_city_markets(self, city_code: str) -> List[dict]:
        """Fetch all open KXHIGH markets for a specific city from Kalshi."""
        series_ticker = f"{KXHIGH_PREFIX}{city_code}"
        url = (
            f"{KALSHI_WEATHER_API_BASE}/markets"
            f"?series_ticker={series_ticker}"
            f"&status=open"
            f"&limit=100"
        )

        try:
            data = self._kalshi_get(url)
            markets = data.get("markets", [])
            logger.debug(f"Kalshi {series_ticker}: {len(markets)} open markets")
            return markets
        except Exception as e:
            logger.warning(f"Failed to fetch {series_ticker} markets: {e}")
            return []

    def _parse_settlement_date(self, market: dict) -> str:
        """Extract the settlement date from a market's close_time or title."""
        close_time = market.get("close_time", "")
        if close_time:
            try:
                dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass

        # Fallback: try event_ticker which sometimes has the date encoded
        return ""

    def _parse_close_time(self, close_time_str: str) -> Optional[datetime]:
        """Parse Kalshi ISO-8601 close time."""
        if not close_time_str:
            return None
        try:
            return datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _group_markets_by_date(
        self, raw_markets: List[dict], city_code: str
    ) -> Dict[str, List[dict]]:
        """Group markets by settlement date (each date has ~6 brackets)."""
        by_date: Dict[str, List[dict]] = {}
        for m in raw_markets:
            date = self._parse_settlement_date(m)
            if date:
                by_date.setdefault(date, []).append(m)
        return by_date

    def scan(self, force: bool = False) -> List[WeatherOpportunity]:
        """
        Main scan method. Returns ranked weather trading opportunities.

        Rate-limited by WEATHER.scan_interval_seconds unless force=True.
        """
        now = time.time()
        if not force and (now - self._last_scan_time) < WEATHER.scan_interval_seconds:
            return self._cached_opportunities

        logger.info("Starting weather market scan...")
        active_cities = WEATHER.get_active_city_codes()
        all_opportunities: List[WeatherOpportunity] = []
        now_dt = datetime.now(timezone.utc)
        current_month = now_dt.month

        for city_code in active_cities:
            city = CITIES.get(city_code)
            if not city:
                continue

            # 1. Fetch Kalshi markets for this city
            raw_markets = self._fetch_city_markets(city_code)
            if not raw_markets:
                continue

            # 2. Group by settlement date
            by_date = self._group_markets_by_date(raw_markets, city_code)

            # 3. Fetch NWS forecast
            try:
                forecasts = self.forecast_fetcher.get_forecast(city_code)
            except Exception as e:
                logger.error(f"Forecast fetch failed for {city.name}: {e}")
                continue

            if not forecasts:
                logger.debug(f"No valid forecasts for {city.name}")
                continue

            # 4. Match forecasts to market dates and price
            forecast_by_date = {f.forecast_date: f for f in forecasts}

            for date, brackets_raw in by_date.items():
                forecast = forecast_by_date.get(date)
                if not forecast:
                    logger.debug(f"No forecast match for {city.name} {date}")
                    continue

                # Parse bracket market data
                brackets_data = []
                for m in brackets_raw:
                    # Kalshi API returns prices in dollar strings (e.g. "0.0400")
                    # Convert to integer cents for the pricing engine
                    def _dollars_to_cents(val):
                        if val is None:
                            return None
                        try:
                            return int(round(float(val) * 100))
                        except (ValueError, TypeError):
                            return None
                    yes_bid = _dollars_to_cents(m.get("yes_bid_dollars")) or m.get("yes_bid")
                    no_bid = _dollars_to_cents(m.get("no_bid_dollars")) or m.get("no_bid")
                    yes_ask = _dollars_to_cents(m.get("yes_ask_dollars")) or m.get("yes_ask")
                    no_ask = _dollars_to_cents(m.get("no_ask_dollars")) or m.get("no_ask")

                    # Derive missing prices
                    if yes_ask is None and no_bid is not None:
                        yes_ask = 100 - no_bid
                    if no_ask is None and yes_bid is not None:
                        no_ask = 100 - yes_bid

                    brackets_data.append({
                        "ticker": m.get("ticker", ""),
                        "title": m.get("title", ""),
                        "yes_bid": yes_bid,
                        "yes_ask": yes_ask,
                        "no_bid": no_bid,
                        "no_ask": no_ask,
                        "volume": m.get("volume", 0) or 0,
                        "open_interest": m.get("open_interest", 0) or 0,
                    })

                # 5. Price all brackets
                pricings = price_brackets(forecast, brackets_data, current_month)

                # 6. Create WeatherMarket and evaluate each bracket
                for pricing, raw in zip(pricings, brackets_raw):
                    wm = WeatherMarket(
                        ticker=pricing.ticker,
                        series_ticker=f"{KXHIGH_PREFIX}{city_code}",
                        event_ticker=raw.get("event_ticker", ""),
                        title=pricing.bracket_label,
                        city_code=city_code,
                        city_name=city.name,
                        status=raw.get("status", "open"),
                        close_time=self._parse_close_time(raw.get("close_time", "")),
                        settlement_date=date,
                        yes_bid=_dollars_to_cents(raw.get("yes_bid_dollars")) or raw.get("yes_bid"),
                        yes_ask=_dollars_to_cents(raw.get("yes_ask_dollars")) or raw.get("yes_ask"),
                        no_bid=_dollars_to_cents(raw.get("no_bid_dollars")) or raw.get("no_bid"),
                        no_ask=_dollars_to_cents(raw.get("no_ask_dollars")) or raw.get("no_ask"),
                        last_price=raw.get("last_price"),
                        volume=raw.get("volume", 0) or 0,
                        open_interest=raw.get("open_interest", 0) or 0,
                        model_prob=pricing.model_prob,
                        edge=pricing.edge,
                        ev_per_contract=pricing.ev_per_contract,
                    )

                    # Build opportunity (score later)
                    opp = WeatherOpportunity(
                        market=wm,
                        pricing=pricing,
                        decision=WeatherTradeDecision(should_trade=False),
                    )

                    # Compute ranking score: prioritize large edges with high EV
                    edge_abs = abs(pricing.edge)
                    ev_abs = max(0, pricing.ev_per_contract)
                    # Boost score for shorter lead times (more reliable forecasts)
                    lead_discount = 1.0 / (1.0 + forecast.lead_time_days * 0.3)
                    opp.score = edge_abs * ev_abs * lead_discount * 100

                    if opp.score > 0:
                        all_opportunities.append(opp)

        # Sort by score (best first)
        all_opportunities.sort(key=lambda o: o.score, reverse=True)

        # Log top opportunities
        logger.info(
            f"Weather scan complete: {len(all_opportunities)} opportunities "
            f"across {len(active_cities)} cities"
        )
        for opp in all_opportunities[:5]:
            m = opp.market
            p = opp.pricing
            logger.info(
                f"  {m.ticker} | {m.city_name} {m.settlement_date} | "
                f"forecast={p.forecast_high:.0f}°F σ={p.sigma:.1f} | "
                f"model={p.model_prob:.3f} mkt={p.market_implied_prob:.3f} | "
                f"edge={p.edge:+.3f} ev={p.ev_per_contract:+.1f}¢ | "
                f"score={opp.score:.1f}"
            )

        self._cached_opportunities = all_opportunities
        self._last_scan_time = now
        return all_opportunities

    def evaluate_opportunities(
        self,
        opportunities: List[WeatherOpportunity],
        available_dollars: float,
        existing_weather_positions: int,
    ) -> List[WeatherOpportunity]:
        """
        Run full trade evaluation on opportunities (Kelly sizing, risk checks).

        Returns only opportunities where decision.should_trade is True.
        """
        tradeable = []
        for opp in opportunities:
            decision = evaluate_bracket(
                opp.pricing,
                available_dollars,
                existing_weather_positions + len(tradeable),
            )
            opp.decision = decision
            if decision.should_trade:
                tradeable.append(opp)

        return tradeable

    def get_scan_summary(self) -> dict:
        """Return a JSON-serializable summary of the last scan for the dashboard."""
        summary = {
            "last_scan": self._last_scan_time,
            "total_opportunities": len(self._cached_opportunities),
            "cities_scanned": len(WEATHER.get_active_city_codes()),
            "opportunities": [],
        }

        for opp in self._cached_opportunities[:20]:
            m = opp.market
            p = opp.pricing
            summary["opportunities"].append({
                "ticker": m.ticker,
                "city": m.city_name,
                "city_code": m.city_code,
                "date": m.settlement_date,
                "bracket": m.title,
                "forecast_high": round(p.forecast_high, 1),
                "sigma": round(p.sigma, 1),
                "model_prob": round(p.model_prob, 3),
                "market_implied": round(p.market_implied_prob, 3),
                "edge": round(p.edge, 3),
                "ev_cents": round(p.ev_per_contract, 1),
                "score": round(opp.score, 1),
                "yes_price": m.yes_ask,
                "no_price": m.no_ask,
                "volume": m.volume,
            })

        # Forecast cache summary
        forecasts = {}
        for city_code, (ts, results) in self.forecast_fetcher._forecast_cache.items():
            city = CITIES.get(city_code)
            if results:
                forecasts[city_code] = {
                    "city": city.name if city else city_code,
                    "forecasts": [
                        {
                            "date": r.forecast_date,
                            "high_f": r.forecast_high_f,
                            "lead_days": round(r.lead_time_days, 1),
                            "period": r.raw_period_name,
                        }
                        for r in results
                    ],
                    "fetched_at": ts,
                }
        summary["forecasts"] = forecasts

        return summary

    def record_prediction(
        self,
        ticker: str,
        city_code: str,
        forecast_date: str,
        model_prob: float,
        market_prob: float,
        side: str,
        forecast_high: float,
        sigma: float,
    ):
        """Record a prediction for later calibration analysis."""
        self._predictions.append({
            "ticker": ticker,
            "city_code": city_code,
            "forecast_date": forecast_date,
            "model_prob": model_prob,
            "market_prob": market_prob,
            "side": side,
            "forecast_high": forecast_high,
            "sigma": sigma,
            "timestamp": time.time(),
            "outcome": None,  # Filled in after settlement
        })

    def get_calibration_data(self) -> List[dict]:
        """Return prediction history for calibration analysis."""
        return self._predictions[-100:]  # Last 100 predictions
