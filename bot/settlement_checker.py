"""
settlement_checker.py — Settles expired KXBTCD paper positions.

KXBTCD ticker format:
  KXBTCD-26MAR1408-T74499.99
    26MAR14 = 2026 March 14
    08      = settles at 08:00 UTC
    T74499.99 = strike $74,499.99 ("$74,500 or above?")

Settlement logic:
  - If we bought YES on "above $X": we WIN if BTC >= X at settlement
  - If we bought NO  on "above $X": we WIN if BTC <  X at settlement
  - Winner gets $1.00 per contract; loser gets $0

BTC price at settlement hour is fetched from Coinbase.
"""

import json
import logging
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Months for ticker parsing
MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
}

# Regex: KXBTCD-26MAR1408-T74499.99
KXBTCD_PATTERN = re.compile(
    r'KXBTCD-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T([\d.]+)'
)


def parse_btc_ticker(ticker: str) -> Optional[dict]:
    """
    Parse a KXBTCD ticker to extract settlement time and strike.

    Returns dict with keys: settle_dt (datetime UTC), strike (float), or None.
    """
    m = KXBTCD_PATTERN.match(ticker)
    if not m:
        return None

    year_2d = int(m.group(1))       # 26
    month_str = m.group(2)           # MAR
    day = int(m.group(3))            # 14
    hour = int(m.group(4))           # 08
    strike = float(m.group(5))       # 74499.99

    month = MONTHS.get(month_str)
    if month is None:
        return None

    year = 2000 + year_2d
    try:
        settle_dt = datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
    except ValueError:
        return None

    return {
        'settle_dt': settle_dt,
        'strike': strike,
    }


def get_btc_price_at(target_dt: datetime) -> Optional[float]:
    """
    Fetch BTC price at a specific time using Coinbase candles API.

    Uses 1-hour candles around the target time.
    Returns the close price of the candle containing target_dt.
    """
    start = target_dt - timedelta(hours=1)
    end = target_dt + timedelta(hours=1)

    url = (
        f"https://api.exchange.coinbase.com/products/BTC-USD/candles"
        f"?start={start.isoformat()}"
        f"&end={end.isoformat()}"
        f"&granularity=3600"
    )

    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "FreyjaSettlementChecker/1.0",
        })
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            candles = json.loads(resp.read().decode("utf-8"))

        if not candles:
            logger.warning(f"No candles returned for {target_dt.isoformat()}")
            return None

        # Coinbase candles: [timestamp, low, high, open, close, volume]
        target_ts = target_dt.timestamp()
        best = None
        best_diff = float('inf')
        for candle in candles:
            ts = candle[0]
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = candle

        if best:
            close_price = float(best[4])
            logger.debug(
                f"BTC price at {target_dt.isoformat()}: ${close_price:,.2f} "
                f"(candle ts={best[0]})"
            )
            return close_price

    except Exception as e:
        logger.error(f"Failed to fetch BTC price at {target_dt.isoformat()}: {e}")

    return None


def get_current_btc_price() -> Optional[float]:
    """Get current BTC price from Coinbase."""
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "FreyjaSettlementChecker/1.0",
        })
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return float(data["data"]["amount"])
    except Exception as e:
        logger.error(f"Failed to fetch current BTC price: {e}")
        return None


class SettlementChecker:
    """
    Checks open KXBTCD positions for settlement and resolves them.

    Call check_settlements() each cycle. It will:
      1. Parse each position's ticker for settlement time
      2. If settlement time has passed (+ 5 min buffer), fetch BTC price
      3. Determine win/loss and call the settlement callback
    """

    def __init__(self):
        self._btc_price_cache: Dict[str, float] = {}
        self._settlement_buffer_seconds = 300

    def check_settlements(
        self,
        positions: Dict[str, dict],
        settle_callback,
    ) -> List[dict]:
        """
        Check all positions for settlement.

        Args:
            positions: dict of ticker -> position data from state.json
            settle_callback: function(ticker, won, contracts, entry_price, pnl_dollars, details)

        Returns:
            List of settlement result dicts.
        """
        now = datetime.now(timezone.utc)
        results = []

        for ticker, pos in list(positions.items()):
            if not ticker.startswith("KXBTCD"):
                continue

            parsed = parse_btc_ticker(ticker)
            if not parsed:
                logger.debug(f"Cannot parse ticker: {ticker}")
                continue

            settle_dt = parsed['settle_dt']
            strike = parsed['strike']

            if now < settle_dt + timedelta(seconds=self._settlement_buffer_seconds):
                continue

            cache_key = settle_dt.strftime("%Y-%m-%d %H")
            btc_price = self._btc_price_cache.get(cache_key)
            if btc_price is None:
                btc_price = get_btc_price_at(settle_dt)
                if btc_price is not None:
                    self._btc_price_cache[cache_key] = btc_price

            if btc_price is None:
                hours_old = (now - settle_dt).total_seconds() / 3600
                if hours_old > 2:
                    btc_price = get_current_btc_price()
                    if btc_price:
                        logger.warning(
                            f"Using current BTC price ${btc_price:,.0f} for "
                            f"{ticker} (settled {hours_old:.1f}h ago)"
                        )

            if btc_price is None:
                logger.warning(f"Cannot determine BTC price for {ticker} settlement")
                continue

            side = pos.get("side", "").lower()
            contracts = pos.get("contracts", 0)
            entry_price = pos.get("entry_price", 0)

            btc_above_strike = btc_price >= strike

            if side == "yes":
                won = btc_above_strike
            elif side == "no":
                won = not btc_above_strike
            else:
                logger.warning(f"Unknown side '{side}' for {ticker}")
                continue

            if won:
                settlement_price = 100
                pnl_cents = (settlement_price - entry_price) * contracts
            else:
                settlement_price = 0
                pnl_cents = (0 - entry_price) * contracts

            pnl_dollars = pnl_cents / 100.0

            result = {
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "entry_price": entry_price,
                "settlement_price": settlement_price,
                "strike": strike,
                "btc_price_at_settle": round(btc_price, 2),
                "settle_time": settle_dt.isoformat(),
                "won": won,
                "pnl_dollars": round(pnl_dollars, 4),
            }

            logger.info(
                f"{'WIN' if won else 'LOSS'} {ticker} | "
                f"{side.upper()} x{contracts} @ {entry_price}c | "
                f"BTC=${btc_price:,.0f} vs strike=${strike:,.0f} | "
                f"P&L=${pnl_dollars:+.2f}"
            )

            try:
                settle_callback(
                    ticker=ticker,
                    won=won,
                    contracts=contracts,
                    entry_price=entry_price,
                    pnl_dollars=pnl_dollars,
                    details=result,
                )
            except Exception as e:
                logger.error(f"Settlement callback failed for {ticker}: {e}")
                continue

            results.append(result)

        return results
