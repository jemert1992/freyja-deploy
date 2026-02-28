"""
weather_strategy.py — NWS forecast-based pricing model for Kalshi weather markets.

Core Model:
    P(T > strike) = 1 - Φ((strike - forecast_high) / σ)

Where:
    T = actual high temperature
    forecast_high = NWS point forecast high
    Φ = standard normal CDF
    σ = forecast error std deviation (calibrated per city/season/lead-time)

The model prices each bracket of a KXHIGH market by computing the probability
that the actual temp falls above each strike boundary, then derives bracket
probabilities as the difference between adjacent cumulative probabilities.

Data Pipeline:
    1. Fetch NWS point forecast for city → get forecast high temp
    2. Compute σ based on city, season, and days-to-settlement
    3. For each market bracket, compute model probability via normal CDF
    4. Compare to Kalshi market price → find edge → size via Kelly
"""

import json
import logging
import math
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from weather_config import (
    NWS_API_BASE,
    NWS_USER_AGENT,
    NWS_REQUEST_TIMEOUT,
    CITIES,
    CityConfig,
    WEATHER,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normal Distribution CDF (pure Python — no scipy needed)
# ---------------------------------------------------------------------------

def _erf(x: float) -> float:
    """Approximation of the error function (Abramowitz & Stegun 7.1.26)."""
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (
        ((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
         - 0.284496736) * t + 0.254829592
    ) * t * math.exp(-x * x)
    return sign * y


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + _erf(x / math.sqrt(2.0)))


def prob_above(strike: float, forecast: float, sigma: float) -> float:
    """P(T > strike) using normal CDF model."""
    if sigma <= 0:
        return 1.0 if forecast > strike else 0.0
    z = (strike - forecast) / sigma
    return 1.0 - norm_cdf(z)


def prob_in_range(low: float, high: float, forecast: float, sigma: float) -> float:
    """P(low <= T < high) = P(T >= low) - P(T >= high)."""
    return prob_above(low, forecast, sigma) - prob_above(high, forecast, sigma)


# ---------------------------------------------------------------------------
# NWS Forecast Fetcher
# ---------------------------------------------------------------------------

@dataclass
class ForecastResult:
    """Parsed NWS forecast for a specific city."""
    city_code: str
    city_name: str
    forecast_high_f: float         # Predicted high temp in °F
    forecast_date: str             # "2026-02-28"
    lead_time_hours: float         # Hours until settlement
    lead_time_days: float          # Days until settlement
    fetched_at: float              # Unix timestamp
    raw_period_name: str = ""      # e.g. "Saturday"
    raw_detail: str = ""           # Full NWS detail text


class NWSForecastFetcher:
    """
    Fetches high temperature forecasts from the National Weather Service API.

    Uses the two-step NWS flow:
        1. GET /points/{lat},{lon} → grid coordinates + forecast URL
        2. GET /gridpoints/{office}/{gridX},{gridY}/forecast → 7-day forecast

    Caches both grid lookups and forecasts.
    """

    def __init__(self):
        self._grid_cache: Dict[str, dict] = {}   # city_code → grid info
        self._forecast_cache: Dict[str, Tuple[float, List[ForecastResult]]] = {}
        self._headers = {
            "User-Agent": NWS_USER_AGENT,
            "Accept": "application/json",
        }

    def _nws_get(self, url: str) -> dict:
        """Make a GET request to NWS API with proper headers."""
        req = urllib.request.Request(url, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=NWS_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.error(f"NWS API HTTP error {e.code} for {url}: {e.reason}")
            raise
        except urllib.error.URLError as e:
            logger.error(f"NWS API connection error for {url}: {e.reason}")
            raise
        except Exception as e:
            logger.error(f"NWS API error for {url}: {e}")
            raise

    def _resolve_grid(self, city: CityConfig) -> dict:
        """Resolve lat/lon to NWS grid coordinates. Caches permanently."""
        if city.kalshi_code in self._grid_cache:
            return self._grid_cache[city.kalshi_code]

        if city._grid_resolved and city.nws_office:
            grid = {
                "office": city.nws_office,
                "gridX": city.nws_gridX,
                "gridY": city.nws_gridY,
            }
            self._grid_cache[city.kalshi_code] = grid
            return grid

        url = f"{NWS_API_BASE}/points/{city.lat:.4f},{city.lon:.4f}"
        logger.info(f"Resolving NWS grid for {city.name} ({city.lat}, {city.lon})")

        data = self._nws_get(url)
        props = data.get("properties", {})

        grid = {
            "office": props.get("gridId", ""),
            "gridX": props.get("gridX", 0),
            "gridY": props.get("gridY", 0),
            "forecast_url": props.get("forecast", ""),
        }

        # Cache on the city object too
        city.nws_office = grid["office"]
        city.nws_gridX = grid["gridX"]
        city.nws_gridY = grid["gridY"]
        city._grid_resolved = True

        self._grid_cache[city.kalshi_code] = grid
        logger.info(
            f"NWS grid resolved: {city.name} → {grid['office']} "
            f"({grid['gridX']},{grid['gridY']})"
        )
        return grid

    def get_forecast(self, city_code: str) -> List[ForecastResult]:
        """
        Fetch the 7-day forecast for a city and extract daily high temperatures.

        Returns a list of ForecastResult objects, one per day with a daytime
        high temperature forecast (typically 3-7 results covering the next week).

        Results are cached for WEATHER.forecast_cache_ttl seconds.
        """
        now = time.time()

        # Check cache
        if city_code in self._forecast_cache:
            cached_time, cached_results = self._forecast_cache[city_code]
            if (now - cached_time) < WEATHER.forecast_cache_ttl:
                logger.debug(f"Using cached forecast for {city_code}")
                return cached_results

        city = CITIES.get(city_code)
        if not city:
            logger.error(f"Unknown city code: {city_code}")
            return []

        try:
            grid = self._resolve_grid(city)
        except Exception as e:
            logger.error(f"Failed to resolve NWS grid for {city.name}: {e}")
            return []

        # Fetch forecast
        forecast_url = grid.get("forecast_url")
        if not forecast_url:
            forecast_url = (
                f"{NWS_API_BASE}/gridpoints/{grid['office']}/"
                f"{grid['gridX']},{grid['gridY']}/forecast"
            )

        try:
            data = self._nws_get(forecast_url)
        except Exception as e:
            logger.error(f"Failed to fetch NWS forecast for {city.name}: {e}")
            return []

        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            logger.warning(f"No forecast periods returned for {city.name}")
            return []

        now_dt = datetime.now(timezone.utc)
        results = []

        for period in periods:
            # We only want daytime periods (isDaytime=True) for high temps
            if not period.get("isDaytime", False):
                continue

            temp = period.get("temperature")
            temp_unit = period.get("temperatureUnit", "F")
            if temp is None:
                continue

            # Convert to °F if needed
            if temp_unit == "C":
                temp = temp * 9 / 5 + 32

            # Parse the period's date from startTime
            start_str = period.get("startTime", "")
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                forecast_date = start_dt.strftime("%Y-%m-%d")
                # Lead time = hours until midnight of the forecast day (settlement)
                # Kalshi settles next morning, so we use 6 AM next day as proxy
                settlement_dt = start_dt.replace(
                    hour=6, minute=0, second=0
                ) + timedelta(days=1)
                lead_hours = (settlement_dt - now_dt).total_seconds() / 3600
                lead_days = lead_hours / 24
            except (ValueError, TypeError):
                continue

            # Skip same-day and already-passed forecasts
            if lead_hours < WEATHER.min_hours_to_settlement:
                continue

            # Skip forecasts too far out
            if lead_days > WEATHER.max_days_out:
                continue

            results.append(ForecastResult(
                city_code=city_code,
                city_name=city.name,
                forecast_high_f=float(temp),
                forecast_date=forecast_date,
                lead_time_hours=lead_hours,
                lead_time_days=lead_days,
                fetched_at=now,
                raw_period_name=period.get("name", ""),
                raw_detail=period.get("detailedForecast", ""),
            ))

        self._forecast_cache[city_code] = (now, results)
        logger.info(
            f"NWS forecast for {city.name}: {len(results)} day(s) — "
            + ", ".join(f"{r.forecast_date}={r.forecast_high_f:.0f}°F" for r in results)
        )
        return results


# ---------------------------------------------------------------------------
# σ (Forecast Error) Calculator
# ---------------------------------------------------------------------------

def compute_sigma(city_code: str, lead_time_days: float, month: int) -> float:
    """
    Compute the forecast error standard deviation (σ) for a given city,
    lead time, and month.

    σ increases with:
      - Lead time (forecast uncertainty grows)
      - Winter months in northern cities (more volatile weather)
      - Inland/desert locations (less maritime moderation)

    Formula:
        σ = (sigma_base + sigma_winter_add * is_winter) * inflation
            + sigma_per_day * (lead_days - 1)
    """
    city = CITIES.get(city_code)
    if not city:
        return 3.0  # Conservative default

    is_winter = month in (12, 1, 2)
    base = city.sigma_base
    if is_winter:
        base += city.sigma_winter_add

    # Scale with lead time: each additional day adds uncertainty
    lead_bonus = max(0.0, (lead_time_days - 1.0)) * WEATHER.sigma_per_day

    # Apply ensemble inflation factor
    sigma = (base + lead_bonus) * WEATHER.sigma_inflation

    logger.debug(
        f"σ for {city_code}: base={city.sigma_base:.1f} "
        f"winter_add={city.sigma_winter_add if is_winter else 0:.1f} "
        f"lead_bonus={lead_bonus:.1f} inflation={WEATHER.sigma_inflation:.1f} "
        f"→ σ={sigma:.2f}°F"
    )
    return sigma


# ---------------------------------------------------------------------------
# Market Bracket Pricer
# ---------------------------------------------------------------------------

@dataclass
class BracketPricing:
    """Pricing result for a single Kalshi weather market bracket."""
    ticker: str
    city_code: str
    forecast_date: str
    bracket_label: str           # e.g. "62°F or below", "63°F to 64°F"
    strike_low: Optional[float]  # Lower bound (None for catch-all low)
    strike_high: Optional[float] # Upper bound (None for catch-all high)
    model_prob: float            # Our model's probability for this bracket
    market_yes_price: Optional[int]  # Kalshi YES price in cents
    market_no_price: Optional[int]
    edge: float = 0.0           # model_prob - market_implied
    ev_per_contract: float = 0.0
    forecast_high: float = 0.0
    sigma: float = 0.0
    lead_days: float = 0.0

    @property
    def market_implied_prob(self) -> float:
        if self.market_yes_price is not None:
            return self.market_yes_price / 100.0
        return 0.5

    @property
    def has_edge(self) -> bool:
        return abs(self.edge) >= WEATHER.min_edge

    def best_side(self) -> Optional[str]:
        """Return 'yes' or 'no' based on where the edge is, or None."""
        if self.edge >= WEATHER.min_edge:
            return "yes"   # Model says more likely than market
        elif self.edge <= -WEATHER.min_edge:
            return "no"    # Model says less likely than market
        return None


def price_brackets(
    forecast: ForecastResult,
    brackets: list,
    month: int,
) -> List[BracketPricing]:
    """
    Price a set of Kalshi weather brackets using the normal CDF model.

    Args:
        forecast: NWS forecast result with high temp prediction
        brackets: List of dicts from Kalshi API with bracket info:
            [{ticker, title, yes_bid, yes_ask, no_bid, no_ask, ...}]
        month: Current month (1-12) for seasonal σ adjustment

    Returns:
        List of BracketPricing objects with model probabilities and edges.
    """
    sigma = compute_sigma(
        forecast.city_code,
        forecast.lead_time_days,
        month,
    )
    high = forecast.forecast_high_f

    results = []

    for bracket in brackets:
        ticker = bracket.get("ticker", "")
        title = bracket.get("title", "")
        yes_ask = bracket.get("yes_ask")
        yes_bid = bracket.get("yes_bid")
        no_ask = bracket.get("no_ask")
        no_bid = bracket.get("no_bid")

        # Parse bracket boundaries from title
        # Kalshi titles look like:
        #   "High temperature in NYC on Feb 28 62°F or below"
        #   "High temperature in NYC on Feb 28 between 63°F and 64°F"
        #   "High temperature in NYC on Feb 28 65°F or above"
        strike_low, strike_high = _parse_bracket_bounds(title)

        # Compute model probability for this bracket
        if strike_low is None and strike_high is not None:
            # Catch-all low: P(T <= high)
            model_prob = 1.0 - prob_above(strike_high, high, sigma)
        elif strike_low is not None and strike_high is None:
            # Catch-all high: P(T >= low)
            model_prob = prob_above(strike_low, high, sigma)
        elif strike_low is not None and strike_high is not None:
            # Regular bracket: P(low <= T < high)
            model_prob = prob_in_range(strike_low, high, sigma) if strike_low < strike_high else 0.0
            # Actually: P(T >= strike_low) - P(T >= strike_high + 1)
            # Since brackets are inclusive, e.g., "63 to 64" means 63 <= T <= 64
            model_prob = prob_above(strike_low, high, sigma) - prob_above(strike_high + 1, high, sigma)
        else:
            model_prob = 0.0

        # Clamp probability
        model_prob = max(0.001, min(0.999, model_prob))

        # Compute edge vs market
        # Use YES ask as market implied probability for buying YES
        market_yes = yes_ask if yes_ask else (100 - no_bid if no_bid else None)
        market_no = no_ask if no_ask else (100 - yes_bid if yes_bid else None)

        edge = 0.0
        ev = 0.0
        if market_yes is not None:
            market_implied = market_yes / 100.0
            edge = model_prob - market_implied
            # EV for YES side
            ev = model_prob * (100 - market_yes) - (1 - model_prob) * market_yes

        bp = BracketPricing(
            ticker=ticker,
            city_code=forecast.city_code,
            forecast_date=forecast.forecast_date,
            bracket_label=title,
            strike_low=strike_low,
            strike_high=strike_high,
            model_prob=model_prob,
            market_yes_price=market_yes,
            market_no_price=market_no,
            edge=edge,
            ev_per_contract=ev,
            forecast_high=high,
            sigma=sigma,
            lead_days=forecast.lead_time_days,
        )

        # Also check NO side edge
        if market_no is not None:
            no_model_prob = 1.0 - model_prob
            no_market_implied = market_no / 100.0
            no_edge = no_model_prob - no_market_implied
            no_ev = no_model_prob * (100 - market_no) - (1 - no_model_prob) * market_no

            # Use whichever side has better edge
            if abs(no_edge) > abs(edge) and no_ev > ev:
                bp.edge = -no_edge  # Negative edge means NO side is better
                bp.ev_per_contract = no_ev

        results.append(bp)

    return results


# ---------------------------------------------------------------------------
# Bracket Title Parser
# ---------------------------------------------------------------------------

def _parse_bracket_bounds(title: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Parse bracket boundaries from Kalshi market title.

    Examples:
        "... 62°F or below"         → (None, 62)
        "... between 63°F and 64°F" → (63, 64)
        "... 65°F or above"         → (65, None)
        "... 62° or lower"          → (None, 62)
        "... 65° or higher"         → (65, None)

    Returns (low_bound, high_bound) — None means catch-all.
    """
    import re

    title_lower = title.lower()

    # Pattern: "X°F or below" or "X° or lower" or "X°F or less"
    m = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(?:below|lower|less)', title_lower)
    if m:
        return (None, float(m.group(1)))

    # Pattern: "X°F or above" or "X° or higher" or "X°F or more"
    m = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(?:above|higher|more)', title_lower)
    if m:
        return (float(m.group(1)), None)

    # Pattern: "between X°F and Y°F" or "X°F to Y°F"
    m = re.search(r'(?:between\s+)?(\d+)\s*°?\s*f?\s+(?:and|to)\s+(\d+)\s*°?\s*f?', title_lower)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    # Fallback: try to find any numbers
    nums = re.findall(r'(\d+)\s*°', title)
    if len(nums) == 2:
        return (float(nums[0]), float(nums[1]))
    elif len(nums) == 1:
        # Ambiguous — check context
        if 'below' in title_lower or 'lower' in title_lower or 'less' in title_lower:
            return (None, float(nums[0]))
        elif 'above' in title_lower or 'higher' in title_lower or 'more' in title_lower:
            return (float(nums[0]), None)

    logger.warning(f"Could not parse bracket bounds from title: {title}")
    return (None, None)


# ---------------------------------------------------------------------------
# Kelly Criterion for Weather Trades
# ---------------------------------------------------------------------------

def weather_kelly(model_prob: float, market_price_cents: int) -> float:
    """
    Compute fractional Kelly bet size for a weather trade.

    For a YES contract at price c cents:
        b = (100 - c) / c    (payout odds)
        f* = (p * b - q) / b  (full Kelly)
        fraction = f* * WEATHER.kelly_fraction
    """
    if market_price_cents <= 0 or market_price_cents >= 100:
        return 0.0

    p = model_prob
    q = 1.0 - p
    b = (100 - market_price_cents) / market_price_cents

    if b <= 0:
        return 0.0

    f_star = (p * b - q) / b
    return max(0.0, f_star * WEATHER.kelly_fraction)


# ---------------------------------------------------------------------------
# Trade Decision
# ---------------------------------------------------------------------------

@dataclass
class WeatherTradeDecision:
    """Decision output from the weather strategy."""
    should_trade: bool
    ticker: str = ""
    side: str = ""              # "yes" or "no"
    contracts: int = 0
    limit_price: int = 0        # cents
    model_prob: float = 0.0
    market_implied: float = 0.0
    edge: float = 0.0
    ev_per_contract: float = 0.0
    kelly_fraction: float = 0.0
    city_code: str = ""
    city_name: str = ""
    forecast_high: float = 0.0
    sigma: float = 0.0
    forecast_date: str = ""
    lead_days: float = 0.0
    bracket_label: str = ""
    reason: str = ""

    def __str__(self):
        if not self.should_trade:
            return f"SKIP ({self.reason})"
        return (
            f"WEATHER TRADE: {self.side.upper()} {self.ticker} "
            f"x{self.contracts} @ {self.limit_price}¢ | "
            f"{self.city_name} {self.forecast_date} | "
            f"forecast={self.forecast_high:.0f}°F σ={self.sigma:.1f} | "
            f"model={self.model_prob:.3f} mkt={self.market_implied:.3f} "
            f"edge={self.edge:+.3f} ev={self.ev_per_contract:+.1f}¢"
        )


def evaluate_bracket(
    pricing: BracketPricing,
    available_dollars: float,
    existing_weather_positions: int,
) -> WeatherTradeDecision:
    """
    Evaluate a single bracket for trading.

    Returns a WeatherTradeDecision indicating whether to trade.
    """
    dec = WeatherTradeDecision(should_trade=False, ticker=pricing.ticker)
    dec.city_code = pricing.city_code
    dec.forecast_high = pricing.forecast_high
    dec.sigma = pricing.sigma
    dec.forecast_date = pricing.forecast_date
    dec.lead_days = pricing.lead_days
    dec.bracket_label = pricing.bracket_label
    dec.model_prob = pricing.model_prob

    city = CITIES.get(pricing.city_code)
    dec.city_name = city.name if city else pricing.city_code

    # Max positions check
    if existing_weather_positions >= WEATHER.max_positions:
        dec.reason = f"Max weather positions ({WEATHER.max_positions}) reached"
        return dec

    # Determine best side
    best_side = pricing.best_side()
    if best_side is None:
        dec.reason = f"No edge: {pricing.edge:+.3f} < min {WEATHER.min_edge}"
        return dec

    if best_side == "yes":
        ask_price = pricing.market_yes_price
        model_p = pricing.model_prob
    else:  # "no"
        ask_price = pricing.market_no_price
        model_p = 1.0 - pricing.model_prob

    if ask_price is None:
        dec.reason = "No ask price available"
        return dec

    # Price range check
    if not (WEATHER.min_contract_price <= ask_price <= WEATHER.max_contract_price):
        dec.reason = f"Price {ask_price}¢ outside range [{WEATHER.min_contract_price}-{WEATHER.max_contract_price}]"
        return dec

    dec.side = best_side
    dec.market_implied = ask_price / 100.0
    dec.edge = model_p - dec.market_implied

    # Edge check (with the correct side's edge)
    if dec.edge < WEATHER.min_edge:
        dec.reason = f"Edge {dec.edge:.3f} < min {WEATHER.min_edge}"
        return dec

    # EV check
    ev = model_p * (100 - ask_price) - (1 - model_p) * ask_price
    dec.ev_per_contract = ev
    if ev <= 0:
        dec.reason = f"Negative EV: {ev:.2f}¢"
        return dec

    # Kelly sizing
    b = (100 - ask_price) / ask_price
    kelly_f = weather_kelly(model_p, ask_price)
    dec.kelly_fraction = kelly_f

    if kelly_f <= 0:
        dec.reason = "Kelly says no bet"
        return dec

    # Dollar sizing
    dollar_risk = min(
        available_dollars * kelly_f,
        WEATHER.max_position_dollars,
    )
    dollar_risk = max(dollar_risk, 0.01)

    # Contracts
    cost_per = ask_price / 100.0
    contracts = int(dollar_risk / cost_per)
    contracts = max(contracts, 1)
    contracts = min(contracts, 25)  # Hard cap

    dec.contracts = contracts
    dec.limit_price = ask_price
    dec.should_trade = True

    logger.info(f"Weather trade decision: {dec}")
    return dec
