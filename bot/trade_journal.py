"""
Trade journal for tracking predictions, trades, and outcomes.

The "learning loop" — every prediction the bot makes gets logged,
and when markets resolve, we compute Brier scores and calibration
metrics to know if the bot is actually good at predicting.
"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict
from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_FILE = "/root/kalshi-bot/trade_journal.json"
CALIBRATION_FILE = "/root/kalshi-bot/calibration_stats.json"


def _generate_id() -> str:
    """Generate a prediction ID from timestamp + random suffix."""
    ts = int(time.time() * 1000)
    suffix = random.randint(1000, 9999)
    return f"pred_{ts}_{suffix}"


@dataclass
class PredictionRecord:
    """A single prediction made by the bot."""
    prediction_id: str  # UUID
    timestamp: float  # When prediction was made
    market_ticker: str
    event_ticker: str
    market_title: str
    category: str  # "weather", "economic", "political", "crypto", "sports", "other"

    # Model outputs
    model_prob: float  # Our probability estimate (0-1)
    market_price: float  # Kalshi price at time of prediction (0-1)
    edge: float  # model_prob - market_price
    confidence: str  # "low", "medium", "high"

    # Trade details (None if we didn't trade)
    traded: bool = False
    side: Optional[str] = None  # "yes" or "no"
    contracts: int = 0
    entry_price_cents: int = 0
    cost_dollars: float = 0.0

    # Model metadata
    model_source: str = ""  # "nws_cdf", "ecmwf_ensemble", "llm_forecast", etc.
    forecast_details: Dict = field(default_factory=dict)  # Extra model data

    # Resolution (filled in later)
    resolved: bool = False
    resolution: Optional[int] = None  # 0 or 1 (YES=1, NO=0)
    resolution_time: Optional[float] = None
    pnl_dollars: Optional[float] = None
    brier_score: Optional[float] = None  # (model_prob - resolution)^2


@dataclass
class CalibrationStats:
    """Rolling calibration statistics."""
    total_predictions: int = 0
    resolved_predictions: int = 0
    traded_count: int = 0

    # Overall metrics
    mean_brier_score: float = 0.0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0

    # By category
    category_stats: Dict[str, Dict] = field(default_factory=dict)

    # By model source
    model_stats: Dict[str, Dict] = field(default_factory=dict)

    # Calibration bins (predicted prob range -> actual frequency)
    calibration_bins: Dict[str, Dict] = field(default_factory=dict)

    last_updated: float = 0.0


class TradeJournal:
    """
    Persistent trade journal that tracks every prediction and trade.
    Computes calibration metrics as markets resolve.
    """

    def __init__(self, journal_path: str = JOURNAL_FILE,
                 calibration_path: str = CALIBRATION_FILE):
        self._journal_path = journal_path
        self._calibration_path = calibration_path
        self._predictions: List[PredictionRecord] = []
        self._stats = CalibrationStats()
        self._load()

    def record_prediction(self,
                          market_ticker: str,
                          event_ticker: str,
                          market_title: str,
                          category: str,
                          model_prob: float,
                          market_price: float,
                          model_source: str = "",
                          forecast_details: Dict = None,
                          traded: bool = False,
                          side: str = None,
                          contracts: int = 0,
                          entry_price_cents: int = 0,
                          cost_dollars: float = 0.0) -> str:
        """Record a new prediction. Returns prediction_id."""
        prediction_id = _generate_id()

        # Determine confidence from edge magnitude
        abs_edge = abs(model_prob - market_price)
        if abs_edge >= 0.15:
            confidence = "high"
        elif abs_edge >= 0.08:
            confidence = "medium"
        else:
            confidence = "low"

        record = PredictionRecord(
            prediction_id=prediction_id,
            timestamp=time.time(),
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            market_title=market_title,
            category=category,
            model_prob=model_prob,
            market_price=market_price,
            edge=model_prob - market_price,
            confidence=confidence,
            traded=traded,
            side=side,
            contracts=contracts,
            entry_price_cents=entry_price_cents,
            cost_dollars=cost_dollars,
            model_source=model_source,
            forecast_details=forecast_details or {},
        )

        self._predictions.append(record)
        self._stats.total_predictions += 1
        if traded:
            self._stats.traded_count += 1

        logger.info(
            f"Prediction recorded: {prediction_id} | {market_ticker} | "
            f"model={model_prob:.3f} mkt={market_price:.3f} edge={record.edge:+.3f} | "
            f"traded={traded}"
        )

        self._save()
        return prediction_id

    def resolve_prediction(self, market_ticker: str, resolution: int,
                           pnl_dollars: float = 0.0) -> Optional[PredictionRecord]:
        """
        Mark a prediction as resolved and compute Brier score.

        Resolves the most recent unresolved prediction for the given ticker.
        Returns the resolved record, or None if no matching prediction found.
        """
        # Find the most recent unresolved prediction for this ticker
        target = None
        for pred in reversed(self._predictions):
            if pred.market_ticker == market_ticker and not pred.resolved:
                target = pred
                break

        if target is None:
            logger.warning(f"No unresolved prediction found for {market_ticker}")
            return None

        target.resolved = True
        target.resolution = resolution
        target.resolution_time = time.time()
        target.pnl_dollars = pnl_dollars
        target.brier_score = (target.model_prob - resolution) ** 2

        logger.info(
            f"Prediction resolved: {target.prediction_id} | {market_ticker} | "
            f"resolution={resolution} | brier={target.brier_score:.4f} | "
            f"pnl=${pnl_dollars:+.2f}"
        )

        # Recompute rolling stats
        self._stats = self._compute_stats()
        self._save()
        return target

    def get_stats(self) -> CalibrationStats:
        """Get current calibration statistics."""
        return self._stats

    def should_trade(self) -> bool:
        """
        Based on calibration history, should we be trading?
        Returns False if Brier score > 0.25 (worse than random) after 30+ resolved predictions.
        """
        if self._stats.resolved_predictions < 30:
            return True  # Not enough data to judge
        return self._stats.mean_brier_score < 0.25

    def get_category_edge(self, category: str) -> Optional[float]:
        """Get the average realized edge for a market category. None if insufficient data."""
        cat_stats = self._stats.category_stats.get(category)
        if cat_stats is None or cat_stats.get("resolved", 0) < 5:
            return None
        return cat_stats.get("avg_edge")

    def _compute_calibration_bins(self) -> Dict[str, Dict]:
        """
        Compute calibration bins for reliability diagram.

        Groups resolved predictions into 10 buckets by predicted probability.
        For each bucket, computes the actual resolution rate.
        """
        bins: Dict[str, Dict] = {}
        bin_edges = [i / 10.0 for i in range(11)]  # 0.0, 0.1, ..., 1.0

        for i in range(10):
            lo = bin_edges[i]
            hi = bin_edges[i + 1]
            label = f"{lo:.1f}-{hi:.1f}"
            bins[label] = {
                "low": lo,
                "high": hi,
                "count": 0,
                "sum_predicted": 0.0,
                "sum_resolved": 0,
                "avg_predicted": 0.0,
                "avg_resolved": 0.0,
            }

        for pred in self._predictions:
            if not pred.resolved or pred.resolution is None:
                continue
            # Determine which bin this prediction falls into
            prob = max(0.0, min(pred.model_prob, 1.0))
            bin_idx = min(int(prob * 10), 9)  # 0-9
            lo = bin_edges[bin_idx]
            hi = bin_edges[bin_idx + 1]
            label = f"{lo:.1f}-{hi:.1f}"

            bins[label]["count"] += 1
            bins[label]["sum_predicted"] += pred.model_prob
            bins[label]["sum_resolved"] += pred.resolution

        # Compute averages
        for label, b in bins.items():
            if b["count"] > 0:
                b["avg_predicted"] = b["sum_predicted"] / b["count"]
                b["avg_resolved"] = b["sum_resolved"] / b["count"]

        return bins

    def _compute_stats(self) -> CalibrationStats:
        """Recompute all calibration statistics from predictions."""
        resolved = [p for p in self._predictions if p.resolved and p.resolution is not None]
        traded = [p for p in self._predictions if p.traded]

        stats = CalibrationStats(
            total_predictions=len(self._predictions),
            resolved_predictions=len(resolved),
            traded_count=len(traded),
            last_updated=time.time(),
        )

        if resolved:
            # Mean Brier score
            brier_scores = [p.brier_score for p in resolved if p.brier_score is not None]
            if brier_scores:
                stats.mean_brier_score = sum(brier_scores) / len(brier_scores)

            # PnL and win rate for traded + resolved predictions
            traded_resolved = [p for p in resolved if p.traded]
            if traded_resolved:
                pnls = [p.pnl_dollars for p in traded_resolved if p.pnl_dollars is not None]
                if pnls:
                    stats.total_pnl = sum(pnls)
                    wins = sum(1 for x in pnls if x > 0)
                    stats.win_rate = wins / len(pnls)

                    # Profit factor: sum(wins) / sum(losses)
                    gross_wins = sum(x for x in pnls if x > 0)
                    gross_losses = abs(sum(x for x in pnls if x < 0))
                    if gross_losses > 0:
                        stats.profit_factor = gross_wins / gross_losses
                    elif gross_wins > 0:
                        stats.profit_factor = float("inf")
                    else:
                        stats.profit_factor = 0.0

        # Category stats
        stats.category_stats = self._compute_group_stats(
            lambda p: p.category
        )

        # Model source stats
        stats.model_stats = self._compute_group_stats(
            lambda p: p.model_source or "unknown"
        )

        # Calibration bins
        stats.calibration_bins = self._compute_calibration_bins()

        return stats

    def _compute_group_stats(self, key_fn) -> Dict[str, Dict]:
        """Compute stats grouped by an arbitrary key function."""
        groups: Dict[str, List[PredictionRecord]] = {}
        for pred in self._predictions:
            key = key_fn(pred)
            if key not in groups:
                groups[key] = []
            groups[key].append(pred)

        result: Dict[str, Dict] = {}
        for key, preds in groups.items():
            resolved = [p for p in preds if p.resolved and p.resolution is not None]
            traded_resolved = [p for p in resolved if p.traded]

            brier_scores = [p.brier_score for p in resolved if p.brier_score is not None]
            pnls = [p.pnl_dollars for p in traded_resolved if p.pnl_dollars is not None]

            # Average realized edge: for resolved predictions, edge = model_prob - market_price,
            # but realized edge compares against outcome
            realized_edges = []
            for p in resolved:
                # Realized edge = how much better our prob was vs market
                # Positive = we were closer to the truth than the market
                our_error = abs(p.model_prob - p.resolution)
                mkt_error = abs(p.market_price - p.resolution)
                realized_edges.append(mkt_error - our_error)

            group_stat = {
                "total": len(preds),
                "resolved": len(resolved),
                "traded": len(traded_resolved),
                "mean_brier": (sum(brier_scores) / len(brier_scores)) if brier_scores else 0.0,
                "total_pnl": sum(pnls) if pnls else 0.0,
                "win_rate": (sum(1 for x in pnls if x > 0) / len(pnls)) if pnls else 0.0,
                "avg_edge": (sum(realized_edges) / len(realized_edges)) if realized_edges else 0.0,
            }
            result[key] = group_stat

        return result

    def _load(self):
        """Load journal from disk."""
        # Load predictions
        if os.path.exists(self._journal_path):
            try:
                with open(self._journal_path, "r") as f:
                    data = json.load(f)
                self._predictions = []
                for rec in data:
                    # Handle forecast_details defaulting
                    if "forecast_details" not in rec or rec["forecast_details"] is None:
                        rec["forecast_details"] = {}
                    self._predictions.append(PredictionRecord(**rec))
                logger.info(f"Loaded {len(self._predictions)} predictions from {self._journal_path}")
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.error(f"Failed to load journal from {self._journal_path}: {e}")
                self._predictions = []

        # Load calibration stats
        if os.path.exists(self._calibration_path):
            try:
                with open(self._calibration_path, "r") as f:
                    data = json.load(f)
                self._stats = CalibrationStats(**data)
                logger.info(f"Loaded calibration stats from {self._calibration_path}")
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.error(f"Failed to load calibration stats from {self._calibration_path}: {e}")
                self._stats = CalibrationStats()
        else:
            # Recompute stats from loaded predictions if any
            if self._predictions:
                self._stats = self._compute_stats()

    def _save(self):
        """Save journal and calibration stats to disk."""
        # Ensure parent directories exist
        Path(self._journal_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._calibration_path).parent.mkdir(parents=True, exist_ok=True)

        # Save predictions
        try:
            records = [asdict(p) for p in self._predictions]
            tmp_path = self._journal_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(records, f, indent=2, default=str)
            os.replace(tmp_path, self._journal_path)
        except OSError as e:
            logger.error(f"Failed to save journal: {e}")

        # Save calibration stats
        try:
            stats_dict = asdict(self._stats)
            tmp_path = self._calibration_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(stats_dict, f, indent=2, default=str)
            os.replace(tmp_path, self._calibration_path)
        except OSError as e:
            logger.error(f"Failed to save calibration stats: {e}")

    def get_summary_for_api(self) -> dict:
        """Return summary dict for dashboard API."""
        s = self._stats
        recent = self._predictions[-10:] if self._predictions else []

        return {
            "total_predictions": s.total_predictions,
            "resolved_predictions": s.resolved_predictions,
            "traded_count": s.traded_count,
            "mean_brier_score": round(s.mean_brier_score, 4),
            "win_rate": round(s.win_rate, 4),
            "total_pnl": round(s.total_pnl, 2),
            "profit_factor": round(s.profit_factor, 4) if s.profit_factor != float("inf") else "inf",
            "should_trade": self.should_trade(),
            "category_stats": s.category_stats,
            "model_stats": s.model_stats,
            "calibration_bins": s.calibration_bins,
            "last_updated": s.last_updated,
            "recent_predictions": [
                {
                    "prediction_id": p.prediction_id,
                    "market_ticker": p.market_ticker,
                    "market_title": p.market_title,
                    "category": p.category,
                    "model_prob": round(p.model_prob, 3),
                    "market_price": round(p.market_price, 3),
                    "edge": round(p.edge, 3),
                    "traded": p.traded,
                    "resolved": p.resolved,
                    "resolution": p.resolution,
                    "brier_score": round(p.brier_score, 4) if p.brier_score is not None else None,
                    "pnl_dollars": round(p.pnl_dollars, 2) if p.pnl_dollars is not None else None,
                }
                for p in recent
            ],
        }
