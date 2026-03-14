"""
sports_strategy.py — ESPN Live Data Client + Kalshi Sports Trading Logic

Freyja Sports Module v1.1 — Momentum/Mean-Reversion Trading on NBA + NCAA Spreads & Totals

Strategy Overview:
  1. ESPN API provides free real-time play-by-play, scores, and win probability
  2. Kalshi spread/total markets have MASSIVE volume during live games (100K+ contracts)
  3. We detect momentum swings (runs, scoring droughts) that create temporary mispricings
  4. Entry on momentum overreaction → Exit on mean reversion or profit target

Market Types Traded:
  - KXNBASPREAD: "Team X wins by over N.5 Points?" — spread markets
  - KXNBATOTAL: "Over N.5 total points scored?" — total points markets

Data Flow:
  ESPN Scoreboard → Detect live games → ESPN Game Summary → 
  Win prob + play-by-play → Momentum detection → 
  Kalshi market scan → Price comparison → Trade signal
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

# ── ESPN API Endpoints ─────────────────────────────────────────────
ESPN_NBA_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_NBA_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={game_id}"
ESPN_NCAAB_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
ESPN_NCAAB_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary?event={game_id}"
# Backward compat aliases
ESPN_SCOREBOARD = ESPN_NBA_SCOREBOARD
ESPN_SUMMARY = ESPN_NBA_SUMMARY

# ── Kalshi Sports Series ───────────────────────────────────────────
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
# NBA series
SPREAD_SERIES = "KXNBASPREAD"
TOTAL_SERIES = "KXNBATOTAL"
# NCAA Men's Basketball series (March Madness + regular season)
NCAA_SPREAD_SERIES = "KXNCAAMBSPREAD"
NCAA_TOTAL_SERIES = "KXNCAAMBTOTAL"
NCAA_GAME_SERIES = "KXNCAAMBGAME"

# ── League Configuration ───────────────────────────────────────────
LEAGUE_CONFIG = {
    "nba": {
        "scoreboard_url": ESPN_NBA_SCOREBOARD,
        "summary_url": ESPN_NBA_SUMMARY,
        "spread_series": SPREAD_SERIES,
        "total_series": TOTAL_SERIES,
        "game_series": None,  # NBA doesn't use a single game series
        "label": "NBA",
    },
    "ncaab": {
        "scoreboard_url": ESPN_NCAAB_SCOREBOARD,
        "summary_url": ESPN_NCAAB_SUMMARY,
        "spread_series": NCAA_SPREAD_SERIES,
        "total_series": NCAA_TOTAL_SERIES,
        "game_series": NCAA_GAME_SERIES,
        "label": "NCAAB",
    },
}

# ── Strategy Parameters ────────────────────────────────────────────
# These are tunable via the dashboard config

@dataclass
class SportsConfig:
    """Sports trading configuration — tunable via dashboard."""
    enabled: bool = True
    paper_mode: bool = True
    
    # Leagues to scan (paper mode = scan everything for learning)
    leagues: list = field(default_factory=lambda: ["nba", "ncaab"])
    
    # Scan timing
    scan_interval_seconds: float = 30.0  # Check every 30s during live games
    
    # Momentum detection
    momentum_window_plays: int = 8       # Look at last N plays for momentum
    momentum_threshold: float = 0.05     # Win prob must shift >5% in window (loosened for learning)
    scoring_run_threshold: int = 8       # Points scored by one team in a run (lowered for NCAAB pace)
    scoring_drought_minutes: float = 2.5 # Minutes without scoring = drought
    
    # Entry criteria (loosened — paper mode is for learning, take more swings)
    min_edge: float = 0.02              # 2% minimum edge (was 5% — too conservative for paper)
    min_volume: int = 0                 # No volume filter in paper mode (college markets are thin)
    min_game_elapsed_pct: float = 0.10  # Trade from 10% onward
    max_game_elapsed_pct: float = 0.95  # Trade through 95% of game
    
    # Position sizing (paper — be aggressive for data collection)
    max_position_dollars: float = 50.0  # Max per trade
    max_concurrent_sports: int = 20     # Lots of March Madness games = lots of trades
    kelly_fraction: float = 0.25        # More aggressive Kelly for paper
    
    # Exit criteria (disabled — hold to settlement for learning)
    profit_target_pct: float = 1.00     # Effectively disabled — let it ride
    stop_loss_pct: float = 1.00         # No stop loss in paper mode
    max_hold_minutes: float = 180.0     # Hold through full game
    
    # Risk (generous for paper mode)
    max_total_exposure_dollars: float = 500.0


# ── Data Classes ───────────────────────────────────────────────────

@dataclass
class ESPNGame:
    """Live game data from ESPN (NBA or NCAAB)."""
    game_id: str
    status: str              # "in", "pre", "post"
    period: int              # Current quarter/half (1-4 NBA, 1-2 NCAAB, 3+ OT)
    clock: str               # Game clock "5:32"
    home_team: str           # "LAL" or "DUKE"
    away_team: str           # "DEN" or "UVA"
    home_team_full: str      # "Los Angeles Lakers" or "Duke Blue Devils"
    away_team_full: str      # "Denver Nuggets" or "Virginia Cavaliers"
    home_score: int
    away_score: int
    home_win_prob: float     # ESPN's win probability (0-1)
    away_win_prob: float
    total_points: int
    spread: int              # home_score - away_score (positive = home leading)
    elapsed_pct: float       # 0.0 to 1.0 how far through the game
    venue: str = ""
    broadcast: str = ""
    league: str = "nba"       # "nba" or "ncaab"

@dataclass
class MomentumSignal:
    """Detected momentum shift in a live game."""
    game_id: str
    signal_type: str         # "scoring_run", "win_prob_shift", "drought_break"
    direction: str           # "home" or "away" — who has momentum
    magnitude: float         # How strong (0-1 scale)
    win_prob_shift: float    # Win prob change over window
    scoring_run: int         # Points in the run
    time_remaining_pct: float
    details: str             # Human-readable description
    timestamp: float = field(default_factory=time.time)

@dataclass
class SportsOpportunity:
    """A tradeable sports market opportunity."""
    game: ESPNGame
    signal: MomentumSignal
    market_type: str         # "spread" or "total"
    market_ticker: str       # Kalshi ticker
    market_title: str
    strike: float            # The strike (e.g., 5.5 for "wins by over 5.5")
    
    # Pricing
    yes_bid: float           # Best bid for Yes
    yes_ask: float           # Best ask for Yes
    no_bid: float
    no_ask: float
    volume: int
    
    # Model
    model_prob: float        # Our estimated probability
    market_prob: float       # Market-implied prob (midpoint)
    edge: float              # model_prob - market_prob
    
    # Trade decision
    side: str = ""           # "yes" or "no"
    contracts: int = 0
    limit_price: int = 0     # In cents
    ev_per_contract: float = 0.0
    should_trade: bool = False
    reason: str = ""


# ── ESPN Client ────────────────────────────────────────────────────

class ESPNClient:
    """Fetches real-time NBA + NCAAB data from ESPN's free API."""
    
    def __init__(self):
        self._cache: Dict[str, Tuple[float, dict]] = {}  # game_id -> (timestamp, data)
        self._cache_ttl = 10.0  # Cache for 10 seconds
        self._win_prob_history: Dict[str, List[Tuple[float, float, float]]] = {}  # game_id -> [(time, home_wp, away_wp)]
        self._score_history: Dict[str, List[Tuple[float, int, int]]] = {}  # game_id -> [(time, home, away)]
    
    def get_live_games(self, leagues: List[str] = None) -> List[ESPNGame]:
        """Fetch all live games from ESPN scoreboard (NBA + NCAAB)."""
        if leagues is None:
            leagues = ["nba", "ncaab"]
        
        all_games = []
        for league in leagues:
            cfg = LEAGUE_CONFIG.get(league)
            if not cfg:
                continue
            try:
                data = self._fetch_json(cfg["scoreboard_url"])
            except Exception as e:
                logger.error(f"ESPN {cfg['label']} scoreboard fetch failed: {e}")
                continue
            
            events = data.get("events", [])
            for event in events:
                try:
                    game = self._parse_scoreboard_event(event, league=league)
                    if game:
                        all_games.append(game)
                except Exception as e:
                    logger.debug(f"Failed to parse ESPN {cfg['label']} event: {e}")
        
        return all_games
    
    def get_game_details(self, game_id: str, league: str = "nba") -> Optional[dict]:
        """Fetch detailed game summary including play-by-play and win probability."""
        now = time.time()
        if game_id in self._cache:
            cached_time, cached_data = self._cache[game_id]
            if now - cached_time < self._cache_ttl:
                return cached_data
        
        try:
            cfg = LEAGUE_CONFIG.get(league, LEAGUE_CONFIG["nba"])
            url = cfg["summary_url"].format(game_id=game_id)
            data = self._fetch_json(url)
            self._cache[game_id] = (now, data)
            return data
        except Exception as e:
            logger.error(f"ESPN {league.upper()} game details fetch failed for {game_id}: {e}")
            return None
    
    def get_win_probability(self, game_id: str, league: str = "nba") -> Optional[List[dict]]:
        """Extract win probability data from game details."""
        details = self.get_game_details(game_id)
        if not details:
            return None
        
        # Win probability is in the winprobability array
        wp_data = details.get("winprobability", [])
        if not wp_data:
            # Try alternate location
            for item in details.get("plays", []):
                if "probability" in item:
                    wp_data.append(item)
        
        return wp_data if wp_data else None
    
    def update_history(self, game: ESPNGame) -> None:
        """Track win probability and score history for momentum detection."""
        now = time.time()
        
        # Win prob history
        if game.game_id not in self._win_prob_history:
            self._win_prob_history[game.game_id] = []
        self._win_prob_history[game.game_id].append(
            (now, game.home_win_prob, game.away_win_prob)
        )
        # Keep last 100 data points
        self._win_prob_history[game.game_id] = self._win_prob_history[game.game_id][-100:]
        
        # Score history
        if game.game_id not in self._score_history:
            self._score_history[game.game_id] = []
        self._score_history[game.game_id].append(
            (now, game.home_score, game.away_score)
        )
        self._score_history[game.game_id] = self._score_history[game.game_id][-100:]
    
    def get_momentum_data(self, game_id: str) -> dict:
        """Get momentum analysis data for a game."""
        wp_hist = self._win_prob_history.get(game_id, [])
        score_hist = self._score_history.get(game_id, [])
        
        return {
            "win_prob_history": wp_hist,
            "score_history": score_hist,
            "data_points": len(wp_hist),
        }
    
    def _parse_scoreboard_event(self, event: dict, league: str = "nba") -> Optional[ESPNGame]:
        """Parse a single ESPN scoreboard event into ESPNGame."""
        competitions = event.get("competitions", [{}])
        if not competitions:
            return None
        comp = competitions[0]
        
        status_obj = event.get("status", {})
        status_type = status_obj.get("type", {})
        state = status_type.get("state", "pre")  # "pre", "in", "post"
        
        period = status_obj.get("period", 0)
        clock = status_obj.get("displayClock", "0:00")
        
        # Get competitors
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None
        
        home = None
        away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            else:
                away = c
        
        if not home or not away:
            return None
        
        home_score = int(home.get("score", 0))
        away_score = int(away.get("score", 0))
        home_team = home.get("team", {}).get("abbreviation", "???")
        away_team = away.get("team", {}).get("abbreviation", "???")
        home_team_full = home.get("team", {}).get("displayName", home_team)
        away_team_full = away.get("team", {}).get("displayName", away_team)
        
        # Win probability from situation
        situation = comp.get("situation", {})
        # ESPN provides lastPlay.probability or situation
        home_wp = 0.5
        away_wp = 0.5
        
        if situation:
            # Some events have probability data
            last_play = situation.get("lastPlay", {})
            if last_play and "probability" in last_play:
                prob = last_play["probability"]
                home_wp = prob.get("homeWinPercentage", 0.5)
                away_wp = prob.get("awayWinPercentage", 0.5)
        
        # Also check for probability at the event level
        for pred in event.get("predictor", {}).get("homeTeam", [{}]):
            if isinstance(pred, dict):
                gp = pred.get("gameProjection")
                if gp:
                    home_wp = float(gp) / 100.0
                    away_wp = 1.0 - home_wp
        
        # More reliable: check competitions[0].odds
        odds = comp.get("odds", [])
        if odds:
            for o in odds:
                ht = o.get("homeTeamOdds", {})
                at = o.get("awayTeamOdds", {})
                if "winPercentage" in ht:
                    home_wp = float(ht["winPercentage"]) / 100.0
                    away_wp = float(at.get("winPercentage", (1.0 - home_wp) * 100)) / 100.0
        
        # Calculate elapsed percentage
        # NBA: 4 quarters × 12 min = 48 min | NCAAB: 2 halves × 20 min = 40 min
        if league == "ncaab":
            period_minutes = 20.0   # College halves are 20 min
            total_periods = 2
            regulation_minutes = 40.0
        else:
            period_minutes = 12.0   # NBA quarters are 12 min
            total_periods = 4
            regulation_minutes = 48.0
        
        elapsed_pct = 0.0
        if state == "in":
            try:
                clock_parts = clock.replace(".", ":").split(":")
                if len(clock_parts) >= 2:
                    mins = float(clock_parts[0])
                    secs = float(clock_parts[1]) if len(clock_parts) > 1 else 0
                    remaining_in_period = mins + secs / 60.0
                else:
                    remaining_in_period = float(clock_parts[0])
                
                periods_complete = max(0, period - 1)
                period_elapsed = period_minutes - remaining_in_period
                total_elapsed = (periods_complete * period_minutes) + period_elapsed
                elapsed_pct = min(1.0, total_elapsed / regulation_minutes)
            except (ValueError, IndexError):
                elapsed_pct = (period - 1) / float(total_periods)
        elif state == "post":
            elapsed_pct = 1.0
        
        venue = comp.get("venue", {}).get("fullName", "")
        broadcast_list = comp.get("broadcasts", [])
        broadcast = ""
        if broadcast_list:
            names = []
            for b in broadcast_list:
                for n in b.get("names", []):
                    names.append(n)
            broadcast = ", ".join(names[:2])
        
        return ESPNGame(
            game_id=event.get("id", ""),
            status=state,
            period=period,
            clock=clock,
            home_team=home_team,
            away_team=away_team,
            home_team_full=home_team_full,
            away_team_full=away_team_full,
            home_score=home_score,
            away_score=away_score,
            home_win_prob=home_wp,
            away_win_prob=away_wp,
            total_points=home_score + away_score,
            spread=home_score - away_score,
            elapsed_pct=elapsed_pct,
            venue=venue,
            broadcast=broadcast,
            league=league,
        )
    
    def _fetch_json(self, url: str) -> dict:
        """Fetch JSON from URL with timeout and error handling."""
        req = Request(url, headers={
            "User-Agent": "Freyja-Sports/1.0",
            "Accept": "application/json",
        })
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            logger.error(f"HTTP error fetching {url}: {e.code}")
            raise
        except URLError as e:
            logger.error(f"URL error fetching {url}: {e.reason}")
            raise


# ── Momentum Detector ──────────────────────────────────────────────

class MomentumDetector:
    """Detects momentum swings in live NBA games."""
    
    def __init__(self, config: SportsConfig):
        self.config = config
        self._last_signals: Dict[str, float] = {}  # game_id -> last signal time
        self._signal_cooldown = 60.0  # Min 60s between signals for same game
    
    def detect(self, game: ESPNGame, espn_client: ESPNClient) -> List[MomentumSignal]:
        """Analyze a live game for momentum signals."""
        signals = []
        
        if game.status != "in":
            return signals
        
        # Check cooldown
        now = time.time()
        last = self._last_signals.get(game.game_id, 0)
        if now - last < self._signal_cooldown:
            return signals
        
        momentum_data = espn_client.get_momentum_data(game.game_id)
        wp_hist = momentum_data["win_prob_history"]
        score_hist = momentum_data["score_history"]
        
        # Need at least a few data points
        if len(wp_hist) < 3:
            return signals
        
        # --- Signal 1: Win probability shift ---
        wp_signal = self._detect_win_prob_shift(game, wp_hist)
        if wp_signal:
            signals.append(wp_signal)
        
        # --- Signal 2: Scoring run ---
        run_signal = self._detect_scoring_run(game, score_hist)
        if run_signal:
            signals.append(run_signal)
        
        # --- Signal 3: Drought break ---
        drought_signal = self._detect_drought_break(game, score_hist)
        if drought_signal:
            signals.append(drought_signal)
        
        if signals:
            self._last_signals[game.game_id] = now
        
        return signals
    
    def _detect_win_prob_shift(self, game: ESPNGame, wp_hist: list) -> Optional[MomentumSignal]:
        """Detect significant win probability shifts."""
        if len(wp_hist) < 3:
            return None
        
        # Look at recent window
        window = wp_hist[-min(len(wp_hist), 10):]
        first_wp = window[0][1]  # home win prob at start of window
        current_wp = window[-1][1]
        
        shift = current_wp - first_wp
        
        if abs(shift) >= self.config.momentum_threshold:
            direction = "home" if shift > 0 else "away"
            team = game.home_team if direction == "home" else game.away_team
            
            return MomentumSignal(
                game_id=game.game_id,
                signal_type="win_prob_shift",
                direction=direction,
                magnitude=min(1.0, abs(shift) / 0.20),  # Normalize to 0-1
                win_prob_shift=shift,
                scoring_run=0,
                time_remaining_pct=1.0 - game.elapsed_pct,
                details=f"{team} win prob shifted {shift:+.1%} in recent plays",
            )
        
        return None
    
    def _detect_scoring_run(self, game: ESPNGame, score_hist: list) -> Optional[MomentumSignal]:
        """Detect a scoring run (one team scoring without the other responding)."""
        if len(score_hist) < 3:
            return None
        
        # Look at recent window
        window = score_hist[-min(len(score_hist), 8):]
        first = window[0]
        last = window[-1]
        
        home_run = last[1] - first[1]
        away_run = last[2] - first[2]
        
        # Home team on a run
        if home_run >= self.config.scoring_run_threshold and away_run <= 2:
            return MomentumSignal(
                game_id=game.game_id,
                signal_type="scoring_run",
                direction="home",
                magnitude=min(1.0, home_run / 15.0),
                win_prob_shift=game.home_win_prob - 0.5,
                scoring_run=home_run,
                time_remaining_pct=1.0 - game.elapsed_pct,
                details=f"{game.home_team} on {home_run}-{away_run} run",
            )
        
        # Away team on a run
        if away_run >= self.config.scoring_run_threshold and home_run <= 2:
            return MomentumSignal(
                game_id=game.game_id,
                signal_type="scoring_run",
                direction="away",
                magnitude=min(1.0, away_run / 15.0),
                win_prob_shift=game.away_win_prob - 0.5,
                scoring_run=away_run,
                time_remaining_pct=1.0 - game.elapsed_pct,
                details=f"{game.away_team} on {away_run}-{home_run} run",
            )
        
        return None
    
    def _detect_drought_break(self, game: ESPNGame, score_hist: list) -> Optional[MomentumSignal]:
        """Detect when a scoring drought ends (mean reversion signal)."""
        if len(score_hist) < 5:
            return None
        
        # Look for a period where total scoring was flat, then a burst
        recent = score_hist[-5:]
        older = score_hist[-min(len(score_hist), 10):-5] if len(score_hist) > 5 else []
        
        if not older:
            return None
        
        # Calculate scoring rate in recent vs older window
        recent_duration = recent[-1][0] - recent[0][0]
        older_duration = older[-1][0] - older[0][0]
        
        if recent_duration < 30 or older_duration < 30:  # Need at least 30s windows
            return None
        
        recent_total = (recent[-1][1] + recent[-1][2]) - (recent[0][1] + recent[0][2])
        older_total = (older[-1][1] + older[-1][2]) - (older[0][1] + older[0][2])
        
        recent_rate = recent_total / (recent_duration / 60.0)  # pts per minute
        older_rate = older_total / (older_duration / 60.0)
        
        # Drought break: low scoring followed by burst
        if older_rate < 1.5 and recent_rate > 4.0:  # NBA avg is ~2.3 pts/min per team
            # Who is scoring in the burst?
            home_recent = recent[-1][1] - recent[0][1]
            away_recent = recent[-1][2] - recent[0][2]
            direction = "home" if home_recent > away_recent else "away"
            
            return MomentumSignal(
                game_id=game.game_id,
                signal_type="drought_break",
                direction=direction,
                magnitude=min(1.0, recent_rate / 6.0),
                win_prob_shift=0.0,
                scoring_run=max(home_recent, away_recent),
                time_remaining_pct=1.0 - game.elapsed_pct,
                details=f"Scoring drought broken — rate jumped from {older_rate:.1f} to {recent_rate:.1f} pts/min",
            )
        
        return None


# ── Kalshi Sports Market Client ────────────────────────────────────

class KalshiSportsClient:
    """Fetches and manages Kalshi sports market data."""
    
    def __init__(self):
        self._market_cache: Dict[str, Tuple[float, list]] = {}
        self._cache_ttl = 15.0  # 15 second cache
    
    def get_spread_markets(self, game_date: str = None) -> List[dict]:
        """Get all open NBA spread markets."""
        return self._get_sports_markets(SPREAD_SERIES, game_date)
    
    def get_total_markets(self, game_date: str = None) -> List[dict]:
        """Get all open NBA total points markets."""
        return self._get_sports_markets(TOTAL_SERIES, game_date)
    
    def get_markets_for_event(self, event_ticker: str) -> List[dict]:
        """Get all markets for a specific event (game)."""
        cache_key = f"event_{event_ticker}"
        now = time.time()
        
        if cache_key in self._market_cache:
            cached_time, cached_data = self._market_cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data
        
        url = f"{KALSHI_API}/markets?event_ticker={event_ticker}&limit=50"
        try:
            data = self._fetch_json(url)
            markets = data.get("markets", [])
            self._market_cache[cache_key] = (now, markets)
            return markets
        except Exception as e:
            logger.error(f"Failed to fetch markets for {event_ticker}: {e}")
            return []
    
    def get_events_for_series(self, series_ticker: str, status: str = "open") -> List[dict]:
        """Get all events (games) for a series."""
        url = f"{KALSHI_API}/events?series_ticker={series_ticker}&status={status}&limit=50"
        try:
            data = self._fetch_json(url)
            return data.get("events", [])
        except Exception as e:
            logger.error(f"Failed to fetch events for {series_ticker}: {e}")
            return []
    
    def match_game_to_events(self, game: ESPNGame) -> Dict[str, str]:
        """Match an ESPN game to Kalshi event tickers.
        
        Returns dict with keys 'spread', 'total', and optionally 'game' mapping to event tickers.
        """
        matches = {}
        
        # Build search date string (format: 26MAR14 for 2026-03-14)
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%y%b%d").upper()  # "26MAR14"
        
        # Team abbreviation mapping (ESPN → Kalshi)
        # NBA teams
        team_map = {
            "LAL": "LAL", "LAC": "LAC", "GSW": "GSW", "PHX": "PHX", "SAC": "SAC",
            "DEN": "DEN", "MIN": "MIN", "OKC": "OKC", "POR": "POR", "UTA": "UTA",
            "BOS": "BOS", "MIL": "MIL", "PHI": "PHI", "CLE": "CLE", "NYK": "NYK",
            "BKN": "BKN", "MIA": "MIA", "ATL": "ATL", "CHI": "CHI", "IND": "IND",
            "DET": "DET", "ORL": "ORL", "TOR": "TOR", "CHA": "CHA", "WAS": "WAS",
            "MEM": "MEM", "NOP": "NOP", "HOU": "HOU", "SAS": "SAS", "DAL": "DAL",
            "NO": "NOP",  # ESPN sometimes uses NO vs NOP
        }
        # NCAA: ESPN abbreviations generally match Kalshi (DUKE, UVA, ARK, etc.)
        # No special mapping needed — pass through directly
        
        away_abbr = team_map.get(game.away_team, game.away_team)
        home_abbr = team_map.get(game.home_team, game.home_team)
        
        # Pick the right Kalshi series based on league
        cfg = LEAGUE_CONFIG.get(game.league, LEAGUE_CONFIG["nba"])
        spread_series = cfg["spread_series"]
        total_series = cfg["total_series"]
        game_series = cfg.get("game_series")
        
        # Kalshi format: {SERIES}-{YY}{MON}{DD}{AWAY}{HOME}
        game_suffix = f"{date_str}{away_abbr}{home_abbr}"
        
        matches["spread"] = f"{spread_series}-{game_suffix}"
        matches["total"] = f"{total_series}-{game_suffix}"
        
        # NCAA also has game winner markets
        if game_series:
            matches["game"] = f"{game_series}-{game_suffix}"
        
        return matches
    
    def _get_sports_markets(self, series_ticker: str, game_date: str = None) -> List[dict]:
        """Fetch markets for a sports series."""
        cache_key = f"series_{series_ticker}"
        now = time.time()
        
        if cache_key in self._market_cache:
            cached_time, cached_data = self._market_cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data
        
        url = f"{KALSHI_API}/markets?series_ticker={series_ticker}&status=open&limit=200"
        try:
            data = self._fetch_json(url)
            markets = data.get("markets", [])
            self._market_cache[cache_key] = (now, markets)
            return markets
        except Exception as e:
            logger.error(f"Failed to fetch {series_ticker} markets: {e}")
            return []
    
    def _fetch_json(self, url: str) -> dict:
        """Fetch JSON from Kalshi public API."""
        req = Request(url, headers={
            "Accept": "application/json",
        })
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                logger.warning("Kalshi rate limit hit — backing off")
                time.sleep(5)
            raise
        except URLError as e:
            logger.error(f"Kalshi API error: {e.reason}")
            raise


# ── Sports Trading Engine ──────────────────────────────────────────

class SportsTrader:
    """
    Main sports trading engine — combines ESPN data + Kalshi markets 
    to find and execute momentum trades.
    """
    
    def __init__(self, config: SportsConfig = None):
        self.config = config or SportsConfig()
        self.espn = ESPNClient()
        self.kalshi = KalshiSportsClient()
        self.momentum = MomentumDetector(self.config)
        
        # State tracking
        self._active_games: Dict[str, ESPNGame] = {}
        self._opportunities: List[SportsOpportunity] = []
        self._last_scan_time: float = 0.0
        self._scan_count: int = 0
        self._signals_detected: int = 0
        self._trades_generated: int = 0
    
    def scan(self) -> List[SportsOpportunity]:
        """Main scan loop — find all current trading opportunities."""
        self._last_scan_time = time.time()
        self._scan_count += 1
        
        try:
            # 1. Get live games
            live_games = self.espn.get_live_games(leagues=self.config.leagues)
            
            if not live_games:
                logger.debug("No live games found")
                return []
            
            logger.info(f"Found {len(live_games)} live games")
            
            opportunities = []
            
            for game in live_games:
                # Update history for momentum tracking
                self.espn.update_history(game)
                
                # Check game timing filters
                if game.elapsed_pct < self.config.min_game_elapsed_pct:
                    continue
                if game.elapsed_pct > self.config.max_game_elapsed_pct:
                    continue
                
                # Detect momentum signals
                signals = self.momentum.detect(game, self.espn)
                
                if not signals:
                    continue
                
                self._signals_detected += len(signals)
                
                # Match game to Kalshi markets
                event_tickers = self.kalshi.match_game_to_events(game)
                
                # Scan spread and total markets for each signal
                for signal in signals:
                    # Spread markets
                    spread_ticker = event_tickers.get("spread")
                    if spread_ticker:
                        spread_markets = self.kalshi.get_markets_for_event(spread_ticker)
                        for market in spread_markets:
                            opp = self._evaluate_spread_opportunity(game, signal, market)
                            if opp and opp.should_trade:
                                opportunities.append(opp)
                                self._trades_generated += 1
                    
                    # Total markets
                    total_ticker = event_tickers.get("total")
                    if total_ticker:
                        total_markets = self.kalshi.get_markets_for_event(total_ticker)
                        for market in total_markets:
                            opp = self._evaluate_total_opportunity(game, signal, market)
                            if opp and opp.should_trade:
                                opportunities.append(opp)
                                self._trades_generated += 1
            
            self._opportunities = opportunities
            return opportunities
            
        except Exception as e:
            logger.error(f"Sports scan error: {e}", exc_info=True)
            return []
    
    def _evaluate_spread_opportunity(
        self, game: ESPNGame, signal: MomentumSignal, market: dict
    ) -> Optional[SportsOpportunity]:
        """Evaluate a spread market for trading opportunity."""
        try:
            ticker = market.get("ticker", "")
            title = market.get("title", "")
            status = market.get("status", "")
            
            if status != "open":
                return None
            
            # Parse strike from title (e.g., "Lakers win by over 5.5?")
            strike = self._parse_strike(title)
            if strike is None:
                return None
            
            # Get pricing
            yes_bid = market.get("yes_bid", 0) / 100.0
            yes_ask = market.get("yes_ask", 100) / 100.0
            no_bid = market.get("no_bid", 0) / 100.0
            no_ask = market.get("no_ask", 100) / 100.0
            volume = market.get("volume", 0)
            
            # Volume filter
            if volume < self.config.min_volume:
                return None
            
            # Market mid-price
            market_prob = (yes_bid + yes_ask) / 2.0
            
            # Model the probability
            model_prob = self._model_spread_prob(game, signal, strike)
            
            edge = model_prob - market_prob
            
            # Determine trade direction
            side = ""
            limit_price = 0
            
            if edge >= self.config.min_edge:
                side = "yes"
                limit_price = int(yes_ask * 100)  # Lift the ask
            elif -edge >= self.config.min_edge:
                side = "no"
                limit_price = int(no_ask * 100)
            
            if not side:
                return None
            
            # Position sizing
            contracts, ev = self._size_position(model_prob, market_prob, side, limit_price)
            
            return SportsOpportunity(
                game=game,
                signal=signal,
                market_type="spread",
                market_ticker=ticker,
                market_title=title,
                strike=strike,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                volume=volume,
                model_prob=model_prob,
                market_prob=market_prob,
                edge=edge,
                side=side,
                contracts=contracts,
                limit_price=limit_price,
                ev_per_contract=ev,
                should_trade=contracts > 0,
                reason=f"{signal.signal_type} signal → {side} spread @ {limit_price}¢",
            )
        except Exception as e:
            logger.debug(f"Error evaluating spread opportunity: {e}")
            return None
    
    def _evaluate_total_opportunity(
        self, game: ESPNGame, signal: MomentumSignal, market: dict
    ) -> Optional[SportsOpportunity]:
        """Evaluate a total points market for trading opportunity."""
        try:
            ticker = market.get("ticker", "")
            title = market.get("title", "")
            status = market.get("status", "")
            
            if status != "open":
                return None
            
            strike = self._parse_strike(title)
            if strike is None:
                return None
            
            yes_bid = market.get("yes_bid", 0) / 100.0
            yes_ask = market.get("yes_ask", 100) / 100.0
            no_bid = market.get("no_bid", 0) / 100.0
            no_ask = market.get("no_ask", 100) / 100.0
            volume = market.get("volume", 0)
            
            if volume < self.config.min_volume:
                return None
            
            market_prob = (yes_bid + yes_ask) / 2.0
            model_prob = self._model_total_prob(game, signal, strike)
            
            edge = model_prob - market_prob
            
            side = ""
            limit_price = 0
            
            if edge >= self.config.min_edge:
                side = "yes"
                limit_price = int(yes_ask * 100)
            elif -edge >= self.config.min_edge:
                side = "no"
                limit_price = int(no_ask * 100)
            
            if not side:
                return None
            
            contracts, ev = self._size_position(model_prob, market_prob, side, limit_price)
            
            return SportsOpportunity(
                game=game,
                signal=signal,
                market_type="total",
                market_ticker=ticker,
                market_title=title,
                strike=strike,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                volume=volume,
                model_prob=model_prob,
                market_prob=market_prob,
                edge=edge,
                side=side,
                contracts=contracts,
                limit_price=limit_price,
                ev_per_contract=ev,
                should_trade=contracts > 0,
                reason=f"{signal.signal_type} signal → {side} total @ {limit_price}¢",
            )
        except Exception as e:
            logger.debug(f"Error evaluating total opportunity: {e}")
            return None
    
    def _model_spread_prob(self, game: ESPNGame, signal: MomentumSignal, strike: float) -> float:
        """
        Model the probability that the home team wins by more than `strike` points.
        
        Uses current spread + momentum signal + time remaining.
        """
        current_spread = game.spread  # home_score - away_score (positive = home leading)
        time_remaining = game.elapsed_pct  # how far through game
        time_left = 1.0 - time_remaining
        
        # Expected scoring rate (pts per minute, per team)
        # NBA: ~2.3 pts/min per team | NCAAB: ~1.8 pts/min per team (40 min game)
        if game.league == "ncaab":
            pts_per_min = 1.8
            regulation_minutes = 40.0
        else:
            pts_per_min = 2.3
            regulation_minutes = 48.0
        
        minutes_left = time_left * regulation_minutes
        
        # Expected net pts remaining (home - away)
        # Baseline: equal scoring
        expected_remaining_spread = 0.0
        
        # Momentum adjustment: team with momentum scores slightly more
        if signal.direction == "home":
            momentum_boost = signal.magnitude * pts_per_min * 0.15 * minutes_left
            expected_remaining_spread += momentum_boost
        else:
            momentum_boost = signal.magnitude * pts_per_min * 0.15 * minutes_left
            expected_remaining_spread -= momentum_boost
        
        # Final expected spread
        expected_final_spread = current_spread + expected_remaining_spread
        
        # Uncertainty grows with time remaining (more variance = more uncertainty)
        # Approximate std dev: sqrt(minutes_left) * ~1.5 pts
        import math
        spread_std = max(1.0, math.sqrt(minutes_left) * 1.5)
        
        # P(home wins by > strike)
        z_score = (expected_final_spread - strike) / spread_std
        prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
        
        return max(0.01, min(0.99, prob))
    
    def _model_total_prob(self, game: ESPNGame, signal: MomentumSignal, strike: float) -> float:
        """
        Model the probability that the total points exceeds `strike`.
        
        Uses current total + expected remaining scoring + momentum.
        """
        current_total = game.total_points
        time_left = 1.0 - game.elapsed_pct
        
        # Expected scoring per team per minute
        # NBA: ~2.3 pts/min | NCAAB: ~1.8 pts/min
        if game.league == "ncaab":
            pts_per_min_per_team = 1.8
            regulation_minutes = 40.0
        else:
            pts_per_min_per_team = 2.3
            regulation_minutes = 48.0
        
        minutes_left = time_left * regulation_minutes
        expected_remaining = pts_per_min_per_team * 2 * minutes_left
        
        # Momentum adjustment: scoring run = elevated pace
        if signal.signal_type == "scoring_run":
            pace_boost = signal.magnitude * 0.2  # +20% max pace boost
            expected_remaining *= (1.0 + pace_boost)
        elif signal.signal_type == "drought_break":
            # Recent surge after drought
            expected_remaining *= 1.1
        
        expected_final_total = current_total + expected_remaining
        
        # Uncertainty
        import math
        total_std = max(2.0, math.sqrt(minutes_left) * 2.0)
        
        # P(total > strike)
        z_score = (expected_final_total - strike) / total_std
        prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
        
        return max(0.01, min(0.99, prob))
    
    def _parse_strike(self, title: str) -> Optional[float]:
        """Parse the strike value from a market title."""
        import re
        # Match patterns like "5.5", "10", "220.5"
        match = re.search(r'([\d]+\.?[\d]*)\s*(?:points?|pts|\.|\?|$)', title, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        
        # Fallback: find any decimal number
        numbers = re.findall(r'\b(\d+\.5|\d+\.0|\d+)\b', title)
        if numbers:
            # Take the most reasonable one (typically 5-250 range for sports)
            candidates = [float(n) for n in numbers if 1 <= float(n) <= 300]
            if candidates:
                return candidates[-1]  # Take last number (usually the strike)
        
        return None
    
    def _size_position(
        self, model_prob: float, market_prob: float, side: str, limit_price: int
    ) -> Tuple[int, float]:
        """Kelly criterion position sizing."""
        if side == "yes":
            win_prob = model_prob
            price = limit_price / 100.0
        else:
            win_prob = 1.0 - model_prob
            price = limit_price / 100.0
        
        if price <= 0 or price >= 1:
            return 0, 0.0
        
        # Kelly fraction
        b = (1.0 - price) / price  # Odds ratio
        kelly = (win_prob * b - (1.0 - win_prob)) / b
        
        if kelly <= 0:
            return 0, 0.0
        
        # Fractional Kelly
        f = kelly * self.config.kelly_fraction
        
        # Dollar amount
        dollars = min(self.config.max_position_dollars, f * self.config.max_total_exposure_dollars)
        
        # Contracts (each contract costs `limit_price` cents)
        if limit_price <= 0:
            return 0, 0.0
        
        contracts = max(1, int(dollars / (limit_price / 100.0)))
        
        # EV per contract
        if side == "yes":
            ev = win_prob * (1.0 - price) - (1.0 - win_prob) * price
        else:
            ev = win_prob * (1.0 - price) - (1.0 - win_prob) * price
        
        return contracts, ev
    
    def get_scan_summary(self) -> dict:
        """Get a summary of current scan state."""
        live_games = self.espn.get_live_games(leagues=self.config.leagues)
        
        games_data = []
        for g in live_games:
            games_data.append({
                "game_id": g.game_id,
                "matchup": f"{g.away_team} @ {g.home_team}",
                "score": f"{g.away_score}-{g.home_score}",
                "period": g.period,
                "clock": g.clock,
                "elapsed_pct": g.elapsed_pct,
                "home_wp": g.home_win_prob,
                "status": g.status,
                "league": g.league,
            })
        
        return {
            "scan_count": self._scan_count,
            "last_scan": self._last_scan_time,
            "signals_detected": self._signals_detected,
            "trades_generated": self._trades_generated,
            "active_opportunities": len(self._opportunities),
            "live_games": len(live_games),
            "games_data": games_data,
        }


# ── Module-level config instance ───────────────────────────────────

SPORTS = SportsConfig()
