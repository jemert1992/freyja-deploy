"""
weather_config.py — Configuration for the Kalshi weather market module.

Covers:
  - NWS API settings
  - Kalshi weather series definitions
  - City → NWS grid mappings
  - Forecast error (σ) calibration parameters
  - Risk/sizing parameters specific to weather trades
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Tuple


# ---------------------------------------------------------------------------
# NWS API
# ---------------------------------------------------------------------------
NWS_API_BASE = "https://api.weather.gov"
NWS_USER_AGENT = os.getenv(
    "NWS_USER_AGENT",
    "FreyjaQuantEngine/1.0 (tech@freyjafinancialgroup.net)"
)
NWS_REQUEST_TIMEOUT = float(os.getenv("NWS_REQUEST_TIMEOUT", "10.0"))

# ---------------------------------------------------------------------------
# Kalshi Weather Series
# ---------------------------------------------------------------------------
KALSHI_WEATHER_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Series ticker prefix for daily high temperature markets
KXHIGH_PREFIX = "KXHIGH"

# ---------------------------------------------------------------------------
# City Definitions
# Each city maps to:
#   - kalshi_code: suffix used in the series ticker (e.g. KXHIGHNY)
#   - lat, lon: coordinates for NWS point lookup
#   - name: human-readable name
#   - nws_office: NWS forecast office (resolved dynamically, but cached)
#   - sigma_base: base forecast error std dev in °F for 1-day forecasts
#   - sigma_scale: multiplier per additional day of lead time
# ---------------------------------------------------------------------------

@dataclass
class CityConfig:
    kalshi_code: str
    name: str
    lat: float
    lon: float
    sigma_base: float = 2.5      # Base σ for day-1 forecast (°F)
    sigma_winter_add: float = 1.0 # Additional σ in winter months (Dec-Feb)
    # NWS grid info (populated at runtime after first /points lookup)
    nws_office: str = ""
    nws_gridX: int = 0
    nws_gridY: int = 0
    _grid_resolved: bool = False


# Master city registry
# σ values tuned from NWS forecast verification studies:
#   - Southern/coastal cities: tighter (2.0-2.5°F)
#   - Northern/inland cities: wider (2.5-3.5°F)
#   - Desert cities (Phoenix, Vegas): wider due to extreme swings
CITIES: Dict[str, CityConfig] = {
    "NY": CityConfig(
        kalshi_code="NY", name="New York City",
        lat=40.7128, lon=-74.0060, sigma_base=2.5,
    ),
    "CHI": CityConfig(
        kalshi_code="CHI", name="Chicago",
        lat=41.8781, lon=-87.6298, sigma_base=3.0, sigma_winter_add=1.5,
    ),
    "MIA": CityConfig(
        kalshi_code="MIA", name="Miami",
        lat=25.7617, lon=-80.1918, sigma_base=2.0, sigma_winter_add=0.5,
    ),
    "AUS": CityConfig(
        kalshi_code="AUS", name="Austin",
        lat=30.2672, lon=-97.7431, sigma_base=2.5,
    ),
    "LA": CityConfig(
        kalshi_code="LA", name="Los Angeles",
        lat=34.0522, lon=-118.2437, sigma_base=2.0, sigma_winter_add=0.5,
    ),
    "PHX": CityConfig(
        kalshi_code="PHX", name="Phoenix",
        lat=33.4484, lon=-112.0740, sigma_base=3.0, sigma_winter_add=0.5,
    ),
    "MSP": CityConfig(
        kalshi_code="MSP", name="Minneapolis",
        lat=44.9778, lon=-93.2650, sigma_base=3.5, sigma_winter_add=2.0,
    ),
    "SF": CityConfig(
        kalshi_code="SF", name="San Francisco",
        lat=37.7749, lon=-122.4194, sigma_base=2.0, sigma_winter_add=0.5,
    ),
    "LV": CityConfig(
        kalshi_code="LV", name="Las Vegas",
        lat=36.1699, lon=-115.1398, sigma_base=3.0,
    ),
    "SEA": CityConfig(
        kalshi_code="SEA", name="Seattle",
        lat=47.6062, lon=-122.3321, sigma_base=2.5, sigma_winter_add=1.0,
    ),
    "ATL": CityConfig(
        kalshi_code="ATL", name="Atlanta",
        lat=33.7490, lon=-84.3880, sigma_base=2.5,
    ),
    "BOS": CityConfig(
        kalshi_code="BOS", name="Boston",
        lat=42.3601, lon=-71.0589, sigma_base=2.8, sigma_winter_add=1.5,
    ),
    "DAL": CityConfig(
        kalshi_code="DAL", name="Dallas",
        lat=32.7767, lon=-96.7970, sigma_base=2.5,
    ),
    "DC": CityConfig(
        kalshi_code="DC", name="Washington DC",
        lat=38.9072, lon=-77.0369, sigma_base=2.5,
    ),
    "NO": CityConfig(
        kalshi_code="NO", name="New Orleans",
        lat=29.9511, lon=-90.0715, sigma_base=2.2,
    ),
    "SA": CityConfig(
        kalshi_code="SA", name="San Antonio",
        lat=29.4241, lon=-98.4936, sigma_base=2.5,
    ),
    "HOU": CityConfig(
        kalshi_code="HOU", name="Houston",
        lat=29.7604, lon=-95.3698, sigma_base=2.2,
    ),
    "OKC": CityConfig(
        kalshi_code="OKC", name="Oklahoma City",
        lat=35.4676, lon=-97.5164, sigma_base=3.0, sigma_winter_add=1.5,
    ),
}


# ---------------------------------------------------------------------------
# Weather Strategy Parameters
# ---------------------------------------------------------------------------
@dataclass
class WeatherStrategyConfig:
    # Enable/disable weather module
    enabled: bool = os.getenv("WEATHER_ENABLED", "true").lower() in ("1", "true", "yes")

    # Paper mode for weather (independent of crypto paper mode)
    paper_mode: bool = os.getenv("WEATHER_PAPER_MODE", "true").lower() in ("1", "true", "yes")

    # Minimum edge (model_prob - market_implied_prob) to enter
    min_edge: float = float(os.getenv("WEATHER_MIN_EDGE", "0.08"))

    # Fractional Kelly for weather trades (conservative: 0.25)
    kelly_fraction: float = float(os.getenv("WEATHER_KELLY_FRACTION", "0.25"))

    # Maximum $ per weather trade
    max_position_dollars: float = float(os.getenv("WEATHER_MAX_POSITION_DOLLARS", "5.0"))

    # Maximum total weather exposure
    max_total_exposure_dollars: float = float(os.getenv("WEATHER_MAX_TOTAL_EXPOSURE", "15.0"))

    # Maximum concurrent weather positions
    max_positions: int = int(os.getenv("WEATHER_MAX_POSITIONS", "4"))

    # Minimum hours before settlement to enter (skip same-day)
    min_hours_to_settlement: float = float(os.getenv("WEATHER_MIN_HOURS", "18.0"))

    # Maximum days out to trade (1-3 day forecasts are best)
    max_days_out: int = int(os.getenv("WEATHER_MAX_DAYS_OUT", "3"))

    # σ inflation factor — multiply NWS ensemble spread by this
    # NWS ensembles are underdispersed; need 1.5-2.5x correction
    sigma_inflation: float = float(os.getenv("WEATHER_SIGMA_INFLATION", "1.8"))

    # Σ per additional day of lead time (compounding forecast uncertainty)
    sigma_per_day: float = float(os.getenv("WEATHER_SIGMA_PER_DAY", "0.8"))

    # Contract price range — skip extreme-priced contracts
    min_contract_price: int = int(os.getenv("WEATHER_MIN_CONTRACT_PRICE", "8"))
    max_contract_price: int = int(os.getenv("WEATHER_MAX_CONTRACT_PRICE", "92"))

    # Scan interval for weather markets (seconds) — less frequent than crypto
    scan_interval_seconds: float = float(os.getenv("WEATHER_SCAN_INTERVAL", "300.0"))

    # NWS forecast cache TTL (seconds) — forecasts update ~hourly
    forecast_cache_ttl: float = float(os.getenv("WEATHER_FORECAST_CACHE_TTL", "1800.0"))

    # Which cities to trade (comma-separated codes, or "ALL")
    active_cities: str = os.getenv("WEATHER_ACTIVE_CITIES", "ALL")

    def get_active_city_codes(self):
        """Return list of active city codes based on config."""
        if self.active_cities.upper() == "ALL":
            return list(CITIES.keys())
        return [c.strip().upper() for c in self.active_cities.split(",") if c.strip()]


# Singleton
WEATHER = WeatherStrategyConfig()
