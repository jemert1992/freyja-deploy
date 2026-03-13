"""
weather_strategy.py — Enhanced forecast-based pricing model for Kalshi weather markets.

v2.0 Upgrades:
  - ECMWF ensemble-driven sigma (data-driven uncertainty) replaces hardcoded values
  - Spread-aware edge: accounts for Kalshi's ~7% fee on net winnings
  - Multi-model blending: ECMWF ensemble + GFS + ECMWF deterministic
  - NWS kept as fallback only

Core Model:
    P(T > strike) = 1 - Φ((strike - μ_blended) / σ_blended)

Where:
    μ_blended = weighted mean of ECMWF ensemble, GFS, ECMWF det
    σ_blended = sqrt(ensemble_var + model_disagreement_var)
    Φ = standard normal CDF

Data Pipeline:
    1. ECMWF ensemble (primary) → get forecast mean + data-driven sigma
    2. GFS + ECMWF det (secondary) → blend for robustness
    3. NWS (fallback) → only if ECMWF/GFS fail
    4. For each market bracket, compute model probability via normal CDF
    5. Spread-aware edge: edge must exceed fee drag + min threshold
    6. Size via Kelly criterion with fee-adjusted odds
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

# ECMWF ensemble forecaster (new in v2)
try:
    from ecmwf_forecast import ECMWFForecastFetcher, EnsembleForecast
    _ECMWF_AVAILABLE = True
except ImportError:
    _ECMWF_AVAILABLE = False
    ECMWFForecastFetcher = None

logger = logging.getLogger(__name__)

# Kalshi fee rate on net winnings
KALSHI_FEE_RATE = 0.07


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
# NWS Forecast Fetcher (kept as fallback)
# ---------------------------------------------------------------------------

@dataclass
class ForecastResult:
    """Parsed forecast for a specific city."""
    city_code: str
    city_name: str
    forecast_high_f: float         # Predicted high temp in °F
    forecast_date: str             # "2026-02-28"
    lead_time_hours: float         # Hours until settlement
    lead_time_days: float          # Days until settlement
    fetched_at: float              # Unix timestamp
    raw_period_name: str = ""      # e.g. "Saturday"
    raw_detail: str = ""           # Full NWS detail text
    # v2: ECMWF ensemble data (if available)
    sigma_from_ensemble: Optional[float] = None  # Data-driven sigma
    forecast_source: str = "nws"   # "nws", "ecmwf_ensemble", "blended"


class NWSForecastFetcher:
    """
    Fetches high temperature forecasts from the National Weather Service API.
    Now serves as FALLBACK when ECMWF ensemble is unavailable.
    """

    def __init__(self):
        self._grid_cache: Dict[str, dict] = {}
        self._forecast_cache: Dict[str, Tuple[float, List[ForecastResult]]] = {}
        self._headers = {
            "User-Agent": NWS_USER_AGENT,
            "Accept": "application/json",
        }
        # ECMWF ensemble fetcher (primary source)
        self._ecmwf_fetcher = None
        if _ECMWF_AVAILABLE:
            self._ecmwf_fetcher = ECMWFForecastFetcher()
            logger.info("ECMWF ensemble fetcher initialized (primary forecast source)")
        else:
            logger.warning("ECMWF module not available — using NWS only")

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

        city.nws_office = grid["office"]
        city.nws_gridX = grid["gridX"]
        city.nws_gridY = grid["gridY"]
        city._grid_resolved = True

        self._grid_cache[city.kalshi_code] = grid
        return grid

    def get_forecast(self, city_code: str) -> List[ForecastResult]:
        """
        Fetch forecast for a city.

        v2: Tries ECMWF ensemble FIRST, falls back to NWS.
        When ECMWF is available, each ForecastResult includes the
        data-driven sigma from the ensemble spread.
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

        now_dt = datetime.now(timezone.utc)
        results = []

        # --- Try ECMWF ensemble first (primary source) ---
        if self._ecmwf_fetcher is not None:
            try:
                loc_forecast = self._ecmwf_fetcher.get_forecast(
                    city_code, city.name, city.lat, city.lon
                )
                if loc_forecast and loc_forecast.forecasts:
                    for ef in loc_forecast.forecasts:
                        # Parse date to compute lead time
                        try:
                            forecast_dt = datetime.strptime(ef.date, "%Y-%m-%d").replace(
                                tzinfo=timezone.utc, hour=12
                            )
                            settlement_dt = forecast_dt.replace(hour=6) + timedelta(days=1)
                            lead_hours = (settlement_dt - now_dt).total_seconds() / 3600
                            lead_days = lead_hours / 24
                        except (ValueError, TypeError):
                            continue

                        if lead_hours < WEATHER.min_hours_to_settlement:
                            continue
                        if lead_days > WEATHER.max_days_out:
                            continue

                        results.append(ForecastResult(
                            city_code=city_code,
                            city_name=city.name,
                            forecast_high_f=ef.blended_mean,
                            forecast_date=ef.date,
                            lead_time_hours=lead_hours,
                            lead_time_days=lead_days,
                            fetched_at=now,
                            raw_period_name="",
                            raw_detail=f"ECMWF ens: mean={ef.ensemble_mean:.1f} σ={ef.ensemble_stdev:.2f} n={ef.member_count}",
                            sigma_from_ensemble=ef.blended_sigma,
                            forecast_source="ecmwf_ensemble",
                        ))

                    if results:
                        logger.info(
                            "ECMWF ensemble forecast for %s: %d days — %s",
                            city.name, len(results),
                            ", ".join(f"{r.forecast_date}={r.forecast_high_f:.0f}°F σ={r.sigma_from_ensemble:.1f}" for r in results)
                        )
                        self._forecast_cache[city_code] = (now, results)
                        return results

            except Exception as e:
                logger.warning(f"ECMWF ensemble failed for {city.name}: {e}")

        # --- Fallback: NWS forecast ---
        logger.info(f"Falling back to NWS forecast for {city.name}")
        try:
            grid = self._resolve_grid(city)
        except Exception as e:
            logger.error(f"Failed to resolve NWS grid for {city.name}: {e}")
            return []

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

        for period in periods:
            if not period.get("isDaytime", False):
                continue

            temp = period.get("temperature")
            temp_unit = period.get("temperatureUnit", "F")
            if temp is None:
                continue

            if temp_unit == "C":
                temp = temp * 9 / 5 + 32

            start_str = period.get("startTime", "")
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                forecast_date = start_dt.strftime("%Y-%m-%d")
                settlement_dt = start_dt.replace(
                    hour=6, minute=0, second=0
                ) + timedelta(days=1)
                lead_hours = (settlement_dt - now_dt).total_seconds() / 3600
                lead_days = lead_hours / 24
            except (ValueError, TypeError):
                continue

            if lead_hours < WEATHER.min_hours_to_settlement:
                continue
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
                sigma_from_ensemble=None,  # No ensemble data
                forecast_source="nws",
            ))

        self._forecast_cache[city_code] = (now, results)
        logger.info(
            f"NWS forecast for {city.name}: {len(results)} day(s) — "
            + ", ".join(f"{r.forecast_date}={r.forecast_high_f:.0f}°F" for r in results)
        )
        return results

    def get_ecmwf_cache_summary(self) -> dict:
        """Return ECMWF cache status for dashboard."""
        if self._ecmwf_fetcher:
            return self._ecmwf_fetcher.get_cache_summary()
        return {}


# ---------------------------------------------------------------------------
# σ (Forecast Error) Calculator — v2: prefers ensemble-derived sigma
# ---------------------------------------------------------------------------

def compute_sigma(
    city_code: str,
    lead_time_days: float,
    month: int,
    ensemble_sigma: Optional[float] = None,
) -> float:
    """
    Compute the forecast error standard deviation (σ).

    v2: If ensemble_sigma is provided (from ECMWF), use it directly
    with a small inflation factor. Otherwise fall back to the old
    hardcoded city-based estimation.
    """
    if ensemble_sigma is not None and ensemble_sigma > 0.5:
        # Ensemble sigma is data-driven. Apply mild inflation (1.2x)
        # because ensemble spread is typically underdispersed even for ECMWF
        inflated = ensemble_sigma * 1.2
        logger.debug(
            "σ for %s: ensemble_sigma=%.2f → inflated=%.2f°F",
            city_code, ensemble_sigma, inflated,
        )
        return max(1.5, inflated)

    # --- Fallback: old hardcoded estimation ---
    city = CITIES.get(city_code)
    if not city:
        return 3.0

    is_winter = month in (12, 1, 2)
    base = city.sigma_base
    if is_winter:
        base += city.sigma_winter_add

    lead_bonus = max(0.0, (lead_time_days - 1.0)) * WEATHER.sigma_per_day
    sigma = (base + lead_bonus) * WEATHER.sigma_inflation

    logger.debug(
        f"σ for {city_code}: base={city.sigma_base:.1f} "
        f"winter_add={city.sigma_winter_add if is_winter else 0:.1f} "
        f"lead_bonus={lead_bonus:.1f} inflation={WEATHER.sigma_inflation:.1f} "
        f"→ σ={sigma:.2f}°F (fallback)"
    )
    return sigma


# ---------------------------------------------------------------------------
# Spread-Aware Edge Calculation (v2)
# ---------------------------------------------------------------------------

def compute_fee_adjusted_edge(
    model_prob: float,
    market_price_cents: int,
    side: str,
) -> Tuple[float, float, float]:
    """
    Compute edge adjusted for Kalshi's fee structure.

    Kalshi charges ~7% of NET WINNINGS (not on the cost).
    So if you buy YES at 40¢ and win, you get $1.00 - 0.07*(100-40)¢ = 95.8¢
    Net payout: 95.8 - 40 = 55.8¢ instead of 60¢.

    Returns:
        (raw_edge, fee_adjusted_ev_cents, fee_adjusted_edge)
    """
    if market_price_cents <= 0 or market_price_cents >= 100:
        return (0.0, 0.0, 0.0)

    cost = market_price_cents
    gross_win = 100 - cost
    fee_on_win = gross_win * KALSHI_FEE_RATE
    net_win = gross_win - fee_on_win  # What you actually get if you win

    if side == "yes":
        p = model_prob
    else:
        p = 1.0 - model_prob

    # Raw edge (ignoring fees)
    raw_edge = p - (cost / 100.0)

    # Fee-adjusted EV per contract (cents)
    ev = p * net_win - (1.0 - p) * cost

    # Fee-adjusted edge: what the true breakeven probability is
    # breakeven: p_be * net_win = (1 - p_be) * cost
    # p_be = cost / (cost + net_win)
    breakeven_prob = cost / (cost + net_win)
    fee_adjusted_edge = p - breakeven_prob

    return (raw_edge, ev, fee_adjusted_edge)


# ---------------------------------------------------------------------------
# Market Bracket Pricer
# ---------------------------------------------------------------------------

@dataclass
class BracketPricing:
    """Pricing result for a single Kalshi weather market bracket."""
    ticker: str
    city_code: str
    forecast_date: str
    bracket_label: str
    strike_low: Optional[float]
    strike_high: Optional[float]
    model_prob: float
    market_yes_price: Optional[int]
    market_no_price: Optional[int]
    edge: float = 0.0
    fee_adjusted_edge: float = 0.0  # v2: edge accounting for fees
    ev_per_contract: float = 0.0
    forecast_high: float = 0.0
    sigma: float = 0.0
    sigma_source: str = "hardcoded"  # "ensemble" or "hardcoded"
    lead_days: float = 0.0

    @property
    def market_implied_prob(self) -> float:
        if self.market_yes_price is not None:
            return self.market_yes_price / 100.0
        return 0.5

    @property
    def has_edge(self) -> bool:
        # v2: use fee-adjusted edge for the threshold check
        return abs(self.fee_adjusted_edge) >= WEATHER.min_edge

    def best_side(self) -> Optional[str]:
        """Return 'yes' or 'no' based on where the fee-adjusted edge is."""
        if self.fee_adjusted_edge >= WEATHER.min_edge:
            return "yes"
        elif self.fee_adjusted_edge <= -WEATHER.min_edge:
            return "no"
        return None


def price_brackets(
    forecast: ForecastResult,
    brackets: list,
    month: int,
) -> List[BracketPricing]:
    """
    Price a set of Kalshi weather brackets using the normal CDF model.

    v2: Uses ECMWF ensemble sigma when available, and computes
    fee-adjusted edges for all brackets.
    """
    # Determine sigma: ensemble-derived or fallback
    ensemble_sigma = getattr(forecast, 'sigma_from_ensemble', None)
    sigma = compute_sigma(
        forecast.city_code,
        forecast.lead_time_days,
        month,
        ensemble_sigma=ensemble_sigma,
    )
    sigma_source = "ensemble" if ensemble_sigma else "hardcoded"
    high = forecast.forecast_high_f

    results = []

    for bracket in brackets:
        ticker = bracket.get("ticker", "")
        title = bracket.get("title", "")
        yes_ask = bracket.get("yes_ask")
        yes_bid = bracket.get("yes_bid")
        no_ask = bracket.get("no_ask")
        no_bid = bracket.get("no_bid")

        strike_low, strike_high = _parse_bracket_bounds(title)

        # Compute model probability for this bracket
        if strike_low is None and strike_high is not None:
            model_prob = 1.0 - prob_above(strike_high, high, sigma)
        elif strike_low is not None and strike_high is None:
            model_prob = prob_above(strike_low, high, sigma)
        elif strike_low is not None and strike_high is not None:
            model_prob = prob_above(strike_low, high, sigma) - prob_above(strike_high + 1, high, sigma)
        else:
            model_prob = 0.0

        model_prob = max(0.001, min(0.999, model_prob))

        # Derive market prices
        market_yes = yes_ask if yes_ask else (100 - no_bid if no_bid else None)
        market_no = no_ask if no_ask else (100 - yes_bid if yes_bid else None)

        # Compute YES side edge (fee-adjusted)
        raw_edge_yes = 0.0
        ev_yes = 0.0
        fa_edge_yes = 0.0
        if market_yes is not None and market_yes > 0:
            raw_edge_yes, ev_yes, fa_edge_yes = compute_fee_adjusted_edge(
                model_prob, market_yes, "yes"
            )

        # Compute NO side edge (fee-adjusted)
        raw_edge_no = 0.0
        ev_no = 0.0
        fa_edge_no = 0.0
        if market_no is not None and market_no > 0:
            raw_edge_no, ev_no, fa_edge_no = compute_fee_adjusted_edge(
                model_prob, market_no, "no"
            )

        # Pick the better side
        if abs(fa_edge_yes) >= abs(fa_edge_no):
            edge = raw_edge_yes
            fee_adj_edge = fa_edge_yes
            ev = ev_yes
        else:
            edge = -raw_edge_no  # Negative edge means NO side
            fee_adj_edge = -fa_edge_no
            ev = ev_no

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
            fee_adjusted_edge=fee_adj_edge,
            ev_per_contract=ev,
            forecast_high=high,
            sigma=sigma,
            sigma_source=sigma_source,
            lead_days=forecast.lead_time_days,
        )

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
    """
    import re

    title_lower = title.lower()

    m = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(?:below|lower|less)', title_lower)
    if m:
        return (None, float(m.group(1)))

    m = re.search(r'(\d+)\s*°?\s*f?\s+or\s+(?:above|higher|more)', title_lower)
    if m:
        return (float(m.group(1)), None)

    m = re.search(r'(?:between\s+)?(\d+)\s*°?\s*f?\s+(?:and|to)\s+(\d+)\s*°?\s*f?', title_lower)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    nums = re.findall(r'(\d+)\s*°', title)
    if len(nums) == 2:
        return (float(nums[0]), float(nums[1]))
    elif len(nums) == 1:
        if 'below' in title_lower or 'lower' in title_lower or 'less' in title_lower:
            return (None, float(nums[0]))
        elif 'above' in title_lower or 'higher' in title_lower or 'more' in title_lower:
            return (float(nums[0]), None)

    logger.warning(f"Could not parse bracket bounds from title: {title}")
    return (None, None)


# ---------------------------------------------------------------------------
# Kelly Criterion for Weather Trades — v2: fee-adjusted
# ---------------------------------------------------------------------------

def weather_kelly(model_prob: float, market_price_cents: int) -> float:
    """
    Compute fractional Kelly bet size for a weather trade.

    v2: Accounts for Kalshi's 7% fee on net winnings.
    """
    if market_price_cents <= 0 or market_price_cents >= 100:
        return 0.0

    p = model_prob
    q = 1.0 - p
    cost = market_price_cents
    gross_win = 100 - cost
    fee = gross_win * KALSHI_FEE_RATE
    net_win = gross_win - fee

    # Odds after fees
    b = net_win / cost

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
    side: str = ""
    contracts: int = 0
    limit_price: int = 0
    model_prob: float = 0.0
    market_implied: float = 0.0
    edge: float = 0.0
    fee_adjusted_edge: float = 0.0  # v2
    ev_per_contract: float = 0.0
    kelly_fraction: float = 0.0
    city_code: str = ""
    city_name: str = ""
    forecast_high: float = 0.0
    sigma: float = 0.0
    sigma_source: str = ""  # v2: "ensemble" or "hardcoded"
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
            f"forecast={self.forecast_high:.0f}°F σ={self.sigma:.1f} ({self.sigma_source}) | "
            f"model={self.model_prob:.3f} mkt={self.market_implied:.3f} "
            f"edge={self.edge:+.3f} fee_adj={self.fee_adjusted_edge:+.3f} "
            f"ev={self.ev_per_contract:+.1f}¢"
        )


def evaluate_bracket(
    pricing: BracketPricing,
    available_dollars: float,
    existing_weather_positions: int,
) -> WeatherTradeDecision:
    """
    Evaluate a single bracket for trading.

    v2: Uses fee-adjusted edge for all decisions.
    """
    dec = WeatherTradeDecision(should_trade=False, ticker=pricing.ticker)
    dec.city_code = pricing.city_code
    dec.forecast_high = pricing.forecast_high
    dec.sigma = pricing.sigma
    dec.sigma_source = pricing.sigma_source
    dec.forecast_date = pricing.forecast_date
    dec.lead_days = pricing.lead_days
    dec.bracket_label = pricing.bracket_label
    dec.model_prob = pricing.model_prob

    city = CITIES.get(pricing.city_code)
    dec.city_name = city.name if city else pricing.city_code

    if existing_weather_positions >= WEATHER.max_positions:
        dec.reason = f"Max weather positions ({WEATHER.max_positions}) reached"
        return dec

    best_side = pricing.best_side()
    if best_side is None:
        dec.reason = f"No fee-adjusted edge: {pricing.fee_adjusted_edge:+.3f} < min {WEATHER.min_edge}"
        return dec

    if best_side == "yes":
        ask_price = pricing.market_yes_price
        model_p = pricing.model_prob
    else:
        ask_price = pricing.market_no_price
        model_p = 1.0 - pricing.model_prob

    if ask_price is None:
        dec.reason = "No ask price available"
        return dec

    if not (WEATHER.min_contract_price <= ask_price <= WEATHER.max_contract_price):
        dec.reason = f"Price {ask_price}¢ outside range [{WEATHER.min_contract_price}-{WEATHER.max_contract_price}]"
        return dec

    dec.side = best_side
    dec.market_implied = ask_price / 100.0

    # Fee-adjusted edge for the chosen side
    _, ev, fa_edge = compute_fee_adjusted_edge(model_p, ask_price, best_side)
    dec.edge = model_p - dec.market_implied
    dec.fee_adjusted_edge = fa_edge
    dec.ev_per_contract = ev

    if fa_edge < WEATHER.min_edge:
        dec.reason = f"Fee-adjusted edge {fa_edge:.3f} < min {WEATHER.min_edge}"
        return dec

    if ev <= 0:
        dec.reason = f"Negative fee-adjusted EV: {ev:.2f}¢"
        return dec

    # Kelly sizing (fee-adjusted)
    kelly_f = weather_kelly(model_p, ask_price)
    dec.kelly_fraction = kelly_f

    if kelly_f <= 0:
        dec.reason = "Kelly says no bet"
        return dec

    dollar_risk = min(
        available_dollars * kelly_f,
        WEATHER.max_position_dollars,
    )
    dollar_risk = max(dollar_risk, 0.01)

    cost_per = ask_price / 100.0
    contracts = int(dollar_risk / cost_per)
    contracts = max(contracts, 1)
    contracts = min(contracts, 25)

    dec.contracts = contracts
    dec.limit_price = ask_price
    dec.should_trade = True

    logger.info(f"Weather trade decision: {dec}")
    return dec
