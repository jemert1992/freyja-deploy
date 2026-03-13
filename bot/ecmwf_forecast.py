"""
ecmwf_forecast.py — ECMWF + GFS ensemble forecast fetcher via Open-Meteo API.

Fetches ensemble weather forecasts from TWO sources for model-blending:
  1. ECMWF IFS 0.25° (51-member ensemble) — European gold-standard
  2. GFS (deterministic) — NOAA backup

The ensemble spread gives us a DATA-DRIVEN sigma (uncertainty) instead of
the hardcoded per-city guesses we had before. This is the single biggest
upgrade for weather prediction accuracy.

Open-Meteo APIs used (free, no key required):
  - Ensemble: https://ensemble-api.open-meteo.com/v1/ensemble
  - GFS: https://api.open-meteo.com/v1/gfs
  - ECMWF: https://api.open-meteo.com/v1/ecmwf

All temps returned in °F (temperature_unit=fahrenheit).
"""

import json
import logging
import math
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ECMWF_ENSEMBLE_URL = (
    "https://ensemble-api.open-meteo.com/v1/ensemble"
    "?latitude={lat}&longitude={lon}"
    "&daily=temperature_2m_max"
    "&models=ecmwf_ifs025"
    "&forecast_days=7"
    "&temperature_unit=fahrenheit"
)

GFS_URL = (
    "https://api.open-meteo.com/v1/gfs"
    "?latitude={lat}&longitude={lon}"
    "&daily=temperature_2m_max"
    "&forecast_days=7"
    "&temperature_unit=fahrenheit"
)

ECMWF_DETERMINISTIC_URL = (
    "https://api.open-meteo.com/v1/ecmwf"
    "?latitude={lat}&longitude={lon}"
    "&daily=temperature_2m_max"
    "&forecast_days=7"
    "&temperature_unit=fahrenheit"
)

# Cache TTL: 1 hour (ECMWF updates every 6h, GFS every 6h)
CACHE_TTL = 3600.0

# Minimum ensemble members to trust the spread
MIN_ENSEMBLE_MEMBERS = 10

# Fallback sigma if ensemble data unavailable
FALLBACK_SIGMA = 3.5


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class EnsembleForecast:
    """Forecast result from ECMWF ensemble for one day at one location."""
    date: str                        # "2026-03-14"
    ensemble_mean: float             # Mean of all ensemble members (°F)
    ensemble_median: float           # Median of members
    ensemble_stdev: float            # Std dev of members (= data-driven sigma!)
    ensemble_min: float              # Min member
    ensemble_max: float              # Max member
    ensemble_p10: float              # 10th percentile
    ensemble_p90: float              # 90th percentile
    member_count: int                # Number of ensemble members
    gfs_forecast: Optional[float]    # GFS deterministic forecast (°F)
    ecmwf_det: Optional[float]       # ECMWF deterministic forecast (°F)

    # Blended values (computed after fetching all sources)
    blended_mean: float = 0.0        # Weighted mean of all sources
    blended_sigma: float = 0.0       # Best uncertainty estimate

    @property
    def ensemble_spread(self) -> float:
        """Full spread (max - min) of ensemble members."""
        return self.ensemble_max - self.ensemble_min

    @property
    def iqr(self) -> float:
        """Interquartile-ish range (p90 - p10)."""
        return self.ensemble_p90 - self.ensemble_p10


@dataclass
class LocationForecast:
    """Full multi-day forecast for a single location."""
    city_code: str
    city_name: str
    lat: float
    lon: float
    forecasts: List[EnsembleForecast]
    fetched_at: float
    source_status: Dict[str, str]    # {"ecmwf_ens": "ok", "gfs": "ok", ...}


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class ECMWFForecastFetcher:
    """
    Fetches and blends ensemble forecasts from ECMWF + GFS.

    The key output is a data-driven sigma for each day/location instead
    of the hardcoded values in the old weather_config.py.
    """

    def __init__(self):
        self._cache: Dict[str, Tuple[float, LocationForecast]] = {}
        self._last_request_time: float = 0.0

    def _rate_limit(self) -> None:
        """Open-Meteo allows 10,000 req/day. Be conservative: 1 req/0.5s."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)
        self._last_request_time = time.monotonic()

    def _fetch_json(self, url: str) -> Optional[dict]:
        """Fetch JSON from Open-Meteo API."""
        self._rate_limit()
        headers = {
            "Accept": "application/json",
            "User-Agent": "FreyjaQuantEngine/2.0",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning("Open-Meteo HTTP %d for %s", e.code, url[:80])
            return None
        except Exception as e:
            logger.warning("Open-Meteo error for %s: %s", url[:80], e)
            return None

    # ------------------------------------------------------------------
    # ECMWF Ensemble fetch
    # ------------------------------------------------------------------

    def _fetch_ecmwf_ensemble(
        self, lat: float, lon: float
    ) -> Tuple[Dict[str, dict], str]:
        """
        Fetch ECMWF IFS 51-member ensemble.

        The API returns daily.temperature_2m_max as an array of arrays:
        each inner array is one ensemble member's forecast across days.

        Returns: (dict of date → member values, status string)
        """
        url = ECMWF_ENSEMBLE_URL.format(lat=lat, lon=lon)
        data = self._fetch_json(url)

        if data is None:
            return {}, "error"

        daily = data.get("daily", {})
        dates = daily.get("time", [])

        # Ensemble members come as temperature_2m_max_member01, _member02, etc.
        # OR as a 2D array if using a single model
        member_keys = [k for k in daily.keys() if "temperature_2m_max" in k and k != "time"]

        if not dates or not member_keys:
            # Try alternative: the data might be in a flat structure
            temps = daily.get("temperature_2m_max", [])
            if dates and temps:
                # Deterministic only — no ensemble spread
                result = {}
                for i, d in enumerate(dates):
                    if i < len(temps) and temps[i] is not None:
                        result[d] = {"members": [temps[i]], "mean": temps[i]}
                return result, "ok_det_only"
            return {}, "no_data"

        # Build per-day member arrays
        result: Dict[str, dict] = {}
        n_days = len(dates)

        for i in range(n_days):
            date = dates[i]
            members = []
            for key in member_keys:
                vals = daily.get(key, [])
                if i < len(vals) and vals[i] is not None:
                    members.append(float(vals[i]))

            if members:
                result[date] = {
                    "members": members,
                    "mean": statistics.mean(members),
                    "stdev": statistics.stdev(members) if len(members) > 1 else FALLBACK_SIGMA,
                    "median": statistics.median(members),
                    "min": min(members),
                    "max": max(members),
                }

        status = "ok" if len(member_keys) >= MIN_ENSEMBLE_MEMBERS else "low_members"
        logger.info(
            "ECMWF ensemble: %d days, %d members per day",
            len(result), len(member_keys),
        )
        return result, status

    # ------------------------------------------------------------------
    # GFS fetch
    # ------------------------------------------------------------------

    def _fetch_gfs(self, lat: float, lon: float) -> Dict[str, float]:
        """Fetch GFS deterministic daily max temp. Returns date → temp_f."""
        url = GFS_URL.format(lat=lat, lon=lon)
        data = self._fetch_json(url)
        if data is None:
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])

        result = {}
        for i, d in enumerate(dates):
            if i < len(temps) and temps[i] is not None:
                result[d] = float(temps[i])

        logger.info("GFS forecast: %d days fetched", len(result))
        return result

    # ------------------------------------------------------------------
    # ECMWF Deterministic fetch
    # ------------------------------------------------------------------

    def _fetch_ecmwf_det(self, lat: float, lon: float) -> Dict[str, float]:
        """Fetch ECMWF deterministic daily max temp. Returns date → temp_f."""
        url = ECMWF_DETERMINISTIC_URL.format(lat=lat, lon=lon)
        data = self._fetch_json(url)
        if data is None:
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])

        result = {}
        for i, d in enumerate(dates):
            if i < len(temps) and temps[i] is not None:
                result[d] = float(temps[i])

        logger.info("ECMWF det: %d days fetched", len(result))
        return result

    # ------------------------------------------------------------------
    # Blend forecasts
    # ------------------------------------------------------------------

    def _blend(self, ens: EnsembleForecast) -> None:
        """
        Compute blended mean and sigma from all available sources.

        Blending logic:
          - Mean: 60% ECMWF ensemble, 20% ECMWF det, 20% GFS
          - Sigma: primarily from ensemble spread, with floor
        """
        sources = [(ens.ensemble_mean, 0.6)]

        if ens.ecmwf_det is not None:
            sources.append((ens.ecmwf_det, 0.2))
        else:
            # Redistribute weight to ensemble
            sources[0] = (ens.ensemble_mean, 0.8)

        if ens.gfs_forecast is not None:
            sources.append((ens.gfs_forecast, 0.2))
        else:
            # Redistribute weight
            total_w = sum(w for _, w in sources)
            sources = [(v, w / total_w) for v, w in sources]

        # Normalize weights
        total_w = sum(w for _, w in sources)
        ens.blended_mean = sum(v * (w / total_w) for v, w in sources)

        # Sigma: use ensemble stdev as base, boost with model disagreement
        base_sigma = ens.ensemble_stdev if ens.ensemble_stdev > 0.5 else FALLBACK_SIGMA

        # Add model disagreement term: if GFS and ECMWF differ a lot,
        # increase uncertainty
        model_temps = [ens.ensemble_mean]
        if ens.ecmwf_det is not None:
            model_temps.append(ens.ecmwf_det)
        if ens.gfs_forecast is not None:
            model_temps.append(ens.gfs_forecast)

        if len(model_temps) > 1:
            model_disagree = statistics.stdev(model_temps)
        else:
            model_disagree = 0.0

        # Combined sigma: sqrt(ensemble_var + model_disagree_var)
        # This accounts for both within-model and between-model uncertainty
        ens.blended_sigma = math.sqrt(base_sigma ** 2 + model_disagree ** 2)

        # Floor: sigma should never be below 1.5°F for daily max temp
        ens.blended_sigma = max(1.5, ens.blended_sigma)

    # ------------------------------------------------------------------
    # Main fetch method
    # ------------------------------------------------------------------

    def get_forecast(
        self, city_code: str, city_name: str, lat: float, lon: float
    ) -> Optional[LocationForecast]:
        """
        Fetch blended ensemble forecast for a city.

        Returns cached result if fresh enough. Otherwise fetches from
        ECMWF ensemble + GFS + ECMWF deterministic in parallel-ish fashion,
        blends them, and caches.
        """
        now = time.time()

        # Check cache
        if city_code in self._cache:
            cached_time, cached_result = self._cache[city_code]
            if (now - cached_time) < CACHE_TTL:
                logger.debug("Using cached ECMWF forecast for %s", city_code)
                return cached_result

        logger.info("Fetching ensemble forecasts for %s (%s)", city_name, city_code)
        source_status: Dict[str, str] = {}

        # 1. ECMWF Ensemble (primary)
        ens_data, ens_status = self._fetch_ecmwf_ensemble(lat, lon)
        source_status["ecmwf_ensemble"] = ens_status

        # 2. GFS (backup / blending)
        gfs_data = self._fetch_gfs(lat, lon)
        source_status["gfs"] = "ok" if gfs_data else "error"

        # 3. ECMWF Deterministic (extra data point)
        ecmwf_det = self._fetch_ecmwf_det(lat, lon)
        source_status["ecmwf_det"] = "ok" if ecmwf_det else "error"

        if not ens_data:
            logger.warning(
                "No ECMWF ensemble data for %s — falling back to GFS/ECMWF det",
                city_code,
            )
            # Build minimal forecasts from whatever we have
            all_dates = set(gfs_data.keys()) | set(ecmwf_det.keys())
            if not all_dates:
                logger.error("No forecast data at all for %s", city_code)
                return None

            forecasts = []
            for date in sorted(all_dates):
                gfs_t = gfs_data.get(date)
                det_t = ecmwf_det.get(date)
                best_t = gfs_t if gfs_t is not None else det_t
                if best_t is None:
                    continue

                ef = EnsembleForecast(
                    date=date,
                    ensemble_mean=best_t,
                    ensemble_median=best_t,
                    ensemble_stdev=FALLBACK_SIGMA,
                    ensemble_min=best_t - FALLBACK_SIGMA * 2,
                    ensemble_max=best_t + FALLBACK_SIGMA * 2,
                    ensemble_p10=best_t - FALLBACK_SIGMA * 1.28,
                    ensemble_p90=best_t + FALLBACK_SIGMA * 1.28,
                    member_count=1,
                    gfs_forecast=gfs_t,
                    ecmwf_det=det_t,
                )
                self._blend(ef)
                forecasts.append(ef)

        else:
            # Build full ensemble forecasts
            forecasts = []
            for date in sorted(ens_data.keys()):
                d = ens_data[date]
                members = d["members"]

                # Compute percentiles
                sorted_m = sorted(members)
                n = len(sorted_m)
                p10_idx = max(0, int(n * 0.10))
                p90_idx = min(n - 1, int(n * 0.90))

                ef = EnsembleForecast(
                    date=date,
                    ensemble_mean=d["mean"],
                    ensemble_median=d.get("median", d["mean"]),
                    ensemble_stdev=d.get("stdev", FALLBACK_SIGMA),
                    ensemble_min=d.get("min", sorted_m[0]),
                    ensemble_max=d.get("max", sorted_m[-1]),
                    ensemble_p10=sorted_m[p10_idx],
                    ensemble_p90=sorted_m[p90_idx],
                    member_count=n,
                    gfs_forecast=gfs_data.get(date),
                    ecmwf_det=ecmwf_det.get(date),
                )
                self._blend(ef)
                forecasts.append(ef)

        if not forecasts:
            logger.error("No valid forecasts assembled for %s", city_code)
            return None

        result = LocationForecast(
            city_code=city_code,
            city_name=city_name,
            lat=lat,
            lon=lon,
            forecasts=forecasts,
            fetched_at=now,
            source_status=source_status,
        )

        # Cache
        self._cache[city_code] = (now, result)

        # Log summary
        for ef in forecasts[:3]:
            logger.info(
                "  %s %s: ens_mean=%.1f°F ens_σ=%.2f°F (n=%d) | "
                "gfs=%s ecmwf_det=%s | blended_mean=%.1f°F blended_σ=%.2f°F",
                city_code, ef.date, ef.ensemble_mean, ef.ensemble_stdev,
                ef.member_count,
                f"{ef.gfs_forecast:.1f}" if ef.gfs_forecast else "N/A",
                f"{ef.ecmwf_det:.1f}" if ef.ecmwf_det else "N/A",
                ef.blended_mean, ef.blended_sigma,
            )

        return result

    def get_forecast_for_date(
        self, city_code: str, city_name: str, lat: float, lon: float, target_date: str
    ) -> Optional[EnsembleForecast]:
        """Get the ensemble forecast for a specific date. Convenience method."""
        loc = self.get_forecast(city_code, city_name, lat, lon)
        if loc is None:
            return None

        for ef in loc.forecasts:
            if ef.date == target_date:
                return ef
        return None

    def get_cache_summary(self) -> dict:
        """Return cache status for dashboard/API."""
        summary = {}
        now = time.time()
        for code, (ts, loc) in self._cache.items():
            age_min = (now - ts) / 60
            summary[code] = {
                "city": loc.city_name,
                "forecasts": len(loc.forecasts),
                "cache_age_min": round(age_min, 1),
                "source_status": loc.source_status,
                "days": [
                    {
                        "date": ef.date,
                        "blended_mean": round(ef.blended_mean, 1),
                        "blended_sigma": round(ef.blended_sigma, 2),
                        "ensemble_stdev": round(ef.ensemble_stdev, 2),
                        "member_count": ef.member_count,
                        "gfs": round(ef.gfs_forecast, 1) if ef.gfs_forecast else None,
                    }
                    for ef in loc.forecasts[:5]
                ],
            }
        return summary
