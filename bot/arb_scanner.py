"""
arb_scanner.py — Scans ALL Kalshi events for mathematical arbitrage opportunities.

Detects structural price violations that represent risk-free profit:
  1. Complement Arbitrage (Binary): YES_ask + NO_ask < $1.00 (post-fees)
  2. Partition Arbitrage (Multi-outcome): Sum of all YES_asks < $1.00 (post-fees)
  3. Cross-market Subset/Implication: price(A) > price(B) when A implies B

Uses only the public Kalshi API — no authentication required for market reads.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KALSHI_FEE_RATE = 0.07  # 7% of net winnings


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class ArbOpportunity:
    """A detected arbitrage opportunity."""
    arb_type: str  # "complement", "partition", "cross_market"
    event_ticker: str
    event_title: str
    market_tickers: List[str]
    combined_cost_cents: int  # Total cost in cents to buy all sides
    guaranteed_payout_cents: int  # Always 100 for binary/partition
    gross_profit_cents: float  # payout - cost
    fee_cents: float  # Fee on net winnings
    net_profit_cents: float  # gross - fee
    net_profit_pct: float  # net_profit / cost as percentage
    detected_at: float  # Unix timestamp
    details: str  # Human-readable description

    @property
    def is_profitable(self) -> bool:
        return self.net_profit_cents > 0


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class ArbScanner:
    """
    Scans Kalshi markets for mathematical arbitrage opportunities.

    Uses the Kalshi API to fetch events and their markets, then checks
    for structural price violations.
    """

    def __init__(
        self,
        kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2",
        fee_rate: float = KALSHI_FEE_RATE,
        scan_limit: int = 200,
    ):
        self._base_url = kalshi_base_url.rstrip("/")
        self._fee_rate = fee_rate
        self._scan_limit = scan_limit
        self._opportunities: List[ArbOpportunity] = []
        self._last_scan_time: float = 0.0
        self._scan_count: int = 0
        # Tracks the last request timestamp for rate limiting
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        """Enforce max ~10 requests/second by sleeping if needed."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        self._last_request_time = time.monotonic()

    def _kalshi_get(self, url: str) -> dict:
        """Fetch JSON from Kalshi public API."""
        self._rate_limit()
        headers = {
            "Accept": "application/json",
            "User-Agent": "FreyjaQuantEngine/1.0",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # 404s are expected — expired/delisted events still appear in the events list
                logger.debug("Kalshi API 404 (expired event) for %s", url)
            else:
                logger.error("Kalshi API HTTP %d for %s", exc.code, url)
            raise
        except Exception as exc:
            logger.error("Kalshi API error for %s: %s", url, exc)
            raise

    # ------------------------------------------------------------------
    # API fetchers
    # ------------------------------------------------------------------

    def _fetch_events(self) -> List[dict]:
        """
        Fetch all active events from Kalshi API (public, no auth).

        Paginates with cursor until exhausted or scan_limit reached.
        """
        all_events: List[dict] = []
        cursor: Optional[str] = None

        while True:
            url = f"{self._base_url}/events?status=open&limit=200"
            if cursor:
                url += f"&cursor={cursor}"

            try:
                data = self._kalshi_get(url)
            except Exception as exc:
                logger.error("Failed fetching events page (cursor=%s): %s", cursor, exc)
                break

            events = data.get("events", [])
            if not events:
                break

            all_events.extend(events)
            logger.debug("Fetched %d events (total so far: %d)", len(events), len(all_events))

            if len(all_events) >= self._scan_limit:
                all_events = all_events[: self._scan_limit]
                break

            cursor = data.get("cursor")
            if not cursor:
                break

        logger.info("Fetched %d active events from Kalshi", len(all_events))
        return all_events

    def _fetch_markets_for_event(self, event_ticker: str) -> List[dict]:
        """Fetch all markets for a specific event."""
        url = f"{self._base_url}/events/{event_ticker}/markets"
        try:
            data = self._kalshi_get(url)
            markets = data.get("markets", [])
            return markets
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.debug("Skipping expired event %s (404)", event_ticker)
            else:
                logger.warning("Failed fetching markets for event %s: %s", event_ticker, exc)
            return []
        except Exception as exc:
            logger.warning("Failed fetching markets for event %s: %s", event_ticker, exc)
            return []

    # ------------------------------------------------------------------
    # Profit math
    # ------------------------------------------------------------------

    def _compute_profit(
        self, combined_cost_cents: int, payout_cents: int
    ) -> tuple:
        """
        Compute gross profit, fee, net profit, and net profit pct.

        Returns (gross_profit, fee, net_profit, net_profit_pct).
        """
        gross = payout_cents - combined_cost_cents
        if gross <= 0:
            fee = 0.0
            net = float(gross)
        else:
            fee = gross * self._fee_rate
            net = gross - fee
        pct = (net / combined_cost_cents * 100) if combined_cost_cents > 0 else 0.0
        return float(gross), fee, net, pct

    # ------------------------------------------------------------------
    # Arbitrage checks
    # ------------------------------------------------------------------

    def _check_complement_arb(
        self, event: dict, markets: List[dict]
    ) -> Optional[ArbOpportunity]:
        """
        Check binary YES/NO markets for complement violation.

        For a single binary market: if yes_ask + no_ask < 100 cents,
        buying both YES and NO locks in a guaranteed $1.00 payout.
        """
        # Complement arb only applies to single-market binary events
        if len(markets) != 1:
            return None

        m = markets[0]
        yes_ask = m.get("yes_ask")
        no_ask = m.get("no_ask")

        # Need both ask prices available and valid
        if yes_ask is None or no_ask is None:
            return None
        if yes_ask <= 0 or no_ask <= 0:
            return None

        combined = yes_ask + no_ask
        if combined >= 100:
            return None  # No arb — combined cost meets or exceeds payout

        payout = 100
        gross, fee, net, pct = self._compute_profit(combined, payout)
        event_ticker = event.get("event_ticker", "")
        event_title = event.get("title", event_ticker)
        ticker = m.get("ticker", "")

        details = (
            f"Binary complement: YES_ask={yes_ask}¢ + NO_ask={no_ask}¢ = {combined}¢ < 100¢ | "
            f"gross={gross:.1f}¢ fee={fee:.1f}¢ net={net:.1f}¢ ({pct:+.2f}%)"
        )

        opp = ArbOpportunity(
            arb_type="complement",
            event_ticker=event_ticker,
            event_title=event_title,
            market_tickers=[ticker],
            combined_cost_cents=combined,
            guaranteed_payout_cents=payout,
            gross_profit_cents=gross,
            fee_cents=fee,
            net_profit_cents=net,
            net_profit_pct=pct,
            detected_at=time.time(),
            details=details,
        )

        if opp.is_profitable:
            logger.info("COMPLEMENT ARB: %s | %s", event_ticker, details)
        else:
            logger.debug("Complement sub-fee: %s | %s", event_ticker, details)

        return opp

    def _check_partition_arb(
        self, event: dict, markets: List[dict]
    ) -> Optional[ArbOpportunity]:
        """
        Check multi-outcome events for partition violation.

        For a categorical event with N mutually-exclusive outcomes, exactly
        one will settle YES.  If the sum of all YES_ask prices < 100 cents,
        buying YES on every outcome guarantees profit.
        """
        if len(markets) < 2:
            return None

        # Gather YES ask prices; skip if any market is missing a quote
        yes_asks: List[int] = []
        tickers: List[str] = []
        illiquid = False

        for m in markets:
            ya = m.get("yes_ask")
            if ya is None or ya <= 0:
                illiquid = True
                break
            yes_asks.append(ya)
            tickers.append(m.get("ticker", ""))

        if illiquid:
            return None

        combined = sum(yes_asks)
        payout = 100  # Exactly one outcome settles YES → $1.00

        if combined >= payout:
            return None  # No arb

        gross, fee, net, pct = self._compute_profit(combined, payout)
        event_ticker = event.get("event_ticker", "")
        event_title = event.get("title", event_ticker)

        price_breakdown = " + ".join(f"{a}¢" for a in yes_asks)
        details = (
            f"Partition ({len(markets)} outcomes): {price_breakdown} = {combined}¢ < 100¢ | "
            f"gross={gross:.1f}¢ fee={fee:.1f}¢ net={net:.1f}¢ ({pct:+.2f}%)"
        )

        opp = ArbOpportunity(
            arb_type="partition",
            event_ticker=event_ticker,
            event_title=event_title,
            market_tickers=tickers,
            combined_cost_cents=combined,
            guaranteed_payout_cents=payout,
            gross_profit_cents=gross,
            fee_cents=fee,
            net_profit_cents=net,
            net_profit_pct=pct,
            detected_at=time.time(),
            details=details,
        )

        if opp.is_profitable:
            logger.info("PARTITION ARB: %s | %s", event_ticker, details)
        else:
            logger.debug("Partition sub-fee: %s | %s", event_ticker, details)

        return opp

    def _check_cross_market_arb(
        self, events_markets: Dict[str, tuple]
    ) -> List[ArbOpportunity]:
        """
        Check for cross-market subset/implication violations.

        If event A implies event B (A is a strict subset), then
        price(A) should be <= price(B).  A violation means we can
        sell the overpriced side and buy the underpriced side.

        We detect implication relationships via event_ticker naming
        conventions — e.g., a more specific bracket implying a broader one.
        This is heuristic and conservative; it flags only clear violations.
        """
        opps: List[ArbOpportunity] = []

        # Build a lookup: ticker → (event, market_data)
        ticker_map: Dict[str, tuple] = {}
        for event_ticker, (event, markets) in events_markets.items():
            for m in markets:
                t = m.get("ticker", "")
                if t:
                    ticker_map[t] = (event, m)

        # Group markets by series_ticker — implication relationships are
        # only meaningful within the same series (e.g., same underlying).
        series_groups: Dict[str, List[tuple]] = {}
        for t, (event, m) in ticker_map.items():
            series = m.get("series_ticker", "")
            if series:
                series_groups.setdefault(series, []).append((event, m))

        for series, entries in series_groups.items():
            if len(entries) < 2:
                continue

            # Sort by yes_ask ascending — cheaper outcomes first
            priced = [
                (ev, m)
                for ev, m in entries
                if m.get("yes_ask") is not None and m.get("yes_ask", 0) > 0
            ]
            if len(priced) < 2:
                continue

            priced.sort(key=lambda x: x[1]["yes_ask"])

            # For bracket-style markets (e.g., "above 80°F" vs "above 90°F"),
            # a broader bracket should always cost more.  The broader bracket
            # is the one whose ticker substring matches a "wider" condition.
            # We look for pairs where a *narrower* condition is priced *higher*
            # than a broader one — a structural impossibility.
            #
            # This is intentionally conservative: we only flag when two markets
            # in the same series have yes_ask values that violate monotonicity
            # AND the ticker structure suggests a subset relationship.
            # For now, we skip this unless we find a reliable implication signal.
            # Placeholder for future heuristic expansion.

        return opps

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    def scan(self) -> List[ArbOpportunity]:
        """
        Scan all active Kalshi events for arbitrage opportunities.

        Returns list of opportunities sorted by net_profit_pct descending.
        Includes both profitable and sub-fee opportunities for monitoring.
        """
        logger.info("Starting arbitrage scan...")
        scan_start = time.time()
        all_opps: List[ArbOpportunity] = []
        events_markets: Dict[str, tuple] = {}
        events_scanned = 0
        markets_checked = 0

        events = self._fetch_events()

        for event in events:
            event_ticker = event.get("event_ticker", "")
            if not event_ticker:
                continue

            markets = self._fetch_markets_for_event(event_ticker)
            if not markets:
                continue

            events_scanned += 1
            markets_checked += len(markets)
            events_markets[event_ticker] = (event, markets)

            # --- Complement check (single binary market) ---
            opp = self._check_complement_arb(event, markets)
            if opp is not None:
                all_opps.append(opp)

            # --- Partition check (multi-outcome event) ---
            opp = self._check_partition_arb(event, markets)
            if opp is not None:
                all_opps.append(opp)

        # --- Cross-market checks across all fetched events ---
        cross_opps = self._check_cross_market_arb(events_markets)
        all_opps.extend(cross_opps)

        # Sort: profitable first, then by net_profit_pct descending
        all_opps.sort(key=lambda o: (o.is_profitable, o.net_profit_pct), reverse=True)

        elapsed = time.time() - scan_start
        profitable = [o for o in all_opps if o.is_profitable]
        self._opportunities = all_opps
        self._last_scan_time = time.time()
        self._scan_count += 1

        logger.info(
            "Arb scan #%d complete in %.1fs: %d events, %d markets checked, "
            "%d opportunities found (%d profitable)",
            self._scan_count,
            elapsed,
            events_scanned,
            markets_checked,
            len(all_opps),
            len(profitable),
        )

        for opp in profitable[:5]:
            logger.info(
                "  %s | %s | cost=%d¢ net=%.1f¢ (%.2f%%) | %s",
                opp.arb_type.upper(),
                opp.event_ticker,
                opp.combined_cost_cents,
                opp.net_profit_cents,
                opp.net_profit_pct,
                opp.event_title[:60],
            )

        return all_opps

    # ------------------------------------------------------------------
    # Summary / dashboard
    # ------------------------------------------------------------------

    def get_scan_summary(self) -> dict:
        """Return summary for dashboard/API."""
        return {
            "last_scan": self._last_scan_time,
            "scan_count": self._scan_count,
            "opportunities_found": len(self._opportunities),
            "profitable_count": sum(1 for o in self._opportunities if o.is_profitable),
            "opportunities": [
                {
                    "type": o.arb_type,
                    "event": o.event_title,
                    "event_ticker": o.event_ticker,
                    "market_tickers": o.market_tickers,
                    "cost_cents": o.combined_cost_cents,
                    "net_profit_cents": round(o.net_profit_cents, 1),
                    "net_profit_pct": round(o.net_profit_pct, 2),
                    "is_profitable": o.is_profitable,
                    "details": o.details,
                    "detected_at": o.detected_at,
                }
                for o in self._opportunities[:10]
            ],
        }
