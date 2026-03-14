"""
sports_strategy.py — ESPN Live Data Client + Kalshi Sports Trading Logic

Freyja Sports Module v1.0 — Momentum/Mean-Reversion Trading on NBA Spreads & Totals

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

# ── ESPN API Endpoints ──────────────────────────────────
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={game_id}"

# ── Kalshi Sports Series ──────────────────────────────────
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
SPREAD_SERIES = "KXNBASPREAD"
TOTAL_SERIES = "KXNBATOTAL"

# ── Strategy Parameters ──────────────────────────────────
# These are tunable via the dashboard config

@dataclass
class SportsConfig:
    """Sports trading configuration — tunable via dashboard."""
    enabled: bool = True
    paper_mode: bool = True
    
    # Scan timing
    scan_interval_seconds: float = 30.0  # Check every 30s during live games
    
    # Momentum detection
    momentum_window_plays: int = 8       # Look at last N plays for momentum
    momentum_threshold: float = 0.08     # Win prob must shift >8% in window
    scoring_run_threshold: int = 10      # Points scored by one team in a run
    scoring_drought_minutes: float = 3.0 # Minutes without scoring = drought
    
    # Entry criteria
    min_edge: float = 0.05              # 5% minimum edge to enter
    min_volume: int = 500               # Market must have 500+ contracts traded
    min_game_elapsed_pct: float = 0.15  # Don't trade first 15% of game
    max_game_elapsed_pct: float = 0.90  # Don't trade last 10% (too volatile)
    
    # Position sizing (paper)
    max_position_dollars: float = 25.0  # Max per trade
    max_concurrent_sports: int = 6      # Max concurrent sports positions
    kelly_fraction: float = 0.15        # Quarter-Kelly for sports
    
    # Exit criteria
    profit_target_pct: float = 0.20     # Take profit at 20%
    stop_loss_pct: float = 0.40         # Stop loss at 40%
    max_hold_minutes: float = 30.0      # Don't hold longer than 30 min
    
    # Risk
    max_total_exposure_dollars: float = 100.0


# ── Data Classes ───────────────────────────────────────────

@dataclass
class ESPNGame:
    """Live NBA game data from ESPN."""
    game_id: str
    status: str              # "in", "pre", "post"
    period: int              # Current quarter (1-4, 5+ for OT)
    clock: str               # Game clock "5:32"
    home_team: str           # "LAL"
    away_team: str           # "DEN"
    home_team_full: str      # "Los Angeles Lakers"
    away_team_full: str      # "Denver Nuggets"
    home_score: int
    away_score: int
    home_win_prob: float     # ESPN's win probability (0-1)
    away_win_prob: float
    total_points: int
    spread: int              # home_score - away_score (positive = home leading)
    elapsed_pct: float       # 0.0 to 1.0 how far through the game
    venue: str = ""
    broadcast: str = ""

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


# ── ESPN Client ───────────────────────────────────────────

class ESPNClient:
    """Fetches real-time NBA data from ESPN's free API."""
    
    def __init__(self):
        self._cache: Dict[str, Tuple[float, dict]] = {}  # game_id -> (timestamp, data)
        self._cache_ttl = 10.0  # Cache for 10 seconds
        self._win_prob_history: Dict[str, List[Tuple[float, float, float]]] = {}  # game_id -> [(time, home_wp, away_wp)]
        self._score_history: Dict[str, List[Tuple[float, int, int]]] = {}  # game_id -> [(time, home, away)]
    
    def get_live_games(self) -> List[ESPNGame]:
        """Fetch all live NBA games from ESPN scoreboard."""
        try:
            data = self._fetch_json(ESPN_SCOREBOARD)
        except Exception as e:
            logger.error(f"ESPN scoreboard fetch failed: {e}")
            return []
        
        games = []
        events = data.get("events", [])
        
        for event in events:
            try:
                game = self._parse_scoreboard_event(event)
                if game:
                    games.append(game)
            except Exception as e:
                logger.debug(f"Failed to parse ESPN event: {e}")
        
        return games
    
    def get_game_details(self, game_id: str) -> Optional[dict]:
        """Fetch detailed game summary including play-by-play and win probability."""
        now = time.time()
        if game_id in self._cache:
            cached_time, cached_data = self._cache[game_id]
            if now - cached_time < self._cache_ttl:
                return cached_data
        
        try:
            url = ESPN_SUMMARY.format(game_id=game_id)
            data = self._fetch_json(url)
            self._cache[game_id] = (now, data)
            return data
        except Exception as e:
            logger.error(f"ESPN game details fetch failed for {game_id}: {e}")
            return None
    
    def get_win_probability(self, game_id: str) -> Optional[List[dict]]:
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
    
    def _parse_scoreboard_event(self, event: dict) -> Optional[ESPNGame]:
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
        # NBA game: 4 quarters × 12 min = 48 min regulation
        elapsed_pct = 0.0
        if state == "in":
            try:
                clock_parts = clock.replace(".", ":").split(":")
                if len(clock_parts) >= 2:
                    mins = float(clock_parts[0])
                    secs = float(clock_parts[1]) if len(clock_parts) > 1 else 0
                    remaining_in_quarter = mins + secs / 60.0
                else:
                    remaining_in_quarter = float(clock_parts[0])
                
                quarters_complete = max(0, period - 1)
                quarter_elapsed = 12.0 - remaining_in_quarter
                total_elapsed = (quarters_complete * 12.0) + quarter_elapsed
                elapsed_pct = min(1.0, total_elapsed / 48.0)
            except (ValueError, IndexError):
                elapsed_pct = (period - 1) / 4.0
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


# ── Momentum Detector ───────────────────────────────────────

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


# ── Kalshi Sports Market Client ─────────────────────────────────

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
        
        Returns dict with keys 'spread' and 'total' mapping to event tickers.
        """
        matches = {}
        
        # Build search date string (format: 26MAR14 for 2026-03-14)
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%y%b%d").upper()  # "26MAR14"
        
        # Team abbreviation mapping (ESPN → Kalshi)
        team_map = {
            "LAL": "LAL", "LAC": "LAC", "GSW": "GSW", "PHX": "PHX", "SAC": "SAC",
            "DEN": "DEN", "MIN": "MIN", "OKC": "OKC", "POR": "POR", "UTA": "UTA",
            "BOS": "BOS", "MIL": "MIL", "PHI": "PHI", "CLE": "CLE", "NYK": "NYK",
            "BKN": "BKN", "MIA": "MIA", "ATL": "ATL", "CHI": "CHI", "IND": "IND",
            "DET": "DET", "ORL": "ORL", "TOR": "TOR", "CHA": "CHA", "WAS": "WAS",
            "MEM": "MEM", "NOP": "NOP", "HOU": "HOU", "SAS": "SAS", "DAL": "DAL",
            "NO": "NOP",  # ESPN sometimes uses NO vs NOP
        }
        
        away_abbr = team_map.get(game.away_team, game.away_team)
        home_abbr = team_map.get(game.home_team, game.home_team)
        
        # Kalshi format: KXNBASPREAD-26MAR14DENLAL (away+home)
        game_suffix = f"{date_str}{away_abbr}{home_abbr}"
        
        # Try exact match first
        spread_ticker = f"{SPREAD_SERIES}-{game_suffix}"
        total_ticker = f"{TOTAL_SERIES}-{game_suffix}"
        
        matches["spread"] = spread_ticker
        matches["total"] = total_ticker
        
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


# ── Sports Trading Engine ───────────────────────────────────────

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
        """
        Full scan cycle:
        1. Get live games from ESPN
        2. Detect momentum signals
        3. Match to Kalshi markets
        4. Calculate edge and generate trade signals
        """
        self._scan_count += 1
        self._last_scan_time = time.time()
        self._opportunities = []
        
        # Step 1: Get live games
        games = self.espn.get_live_games()
        live_games = [g for g in games if g.status == "in"]
        
        if not live_games:
            logger.debug("No live NBA games currently")
            # Also track upcoming games for the dashboard
            self._active_games = {g.game_id: g for g in games}
            return []
        
        logger.info(f"Sports scan: {len(live_games)} live NBA games")
        self._active_games = {g.game_id: g for g in games}
        
        # Step 2: For each live game, update history and detect momentum
        all_signals = []
        for game in live_games:
            # Update history tracking
            self.espn.update_history(game)
            
            # Check game timing filters
            if game.elapsed_pct < self.config.min_game_elapsed_pct:
                logger.debug(f"Game {game.away_team}@{game.home_team} too early ({game.elapsed_pct:.0%})")
                continue
            if game.elapsed_pct > self.config.max_game_elapsed_pct:
                logger.debug(f"Game {game.away_team}@{game.home_team} too late ({game.elapsed_pct:.0%})")
                continue
            
            # Detect momentum signals
            signals = self.momentum.detect(game, self.espn)
            if signals:
                self._signals_detected += len(signals)
                for sig in signals:
                    logger.info(
                        f"MOMENTUM: {sig.signal_type} — {sig.details} "
                        f"(magnitude={sig.magnitude:.2f})"
                    )
            all_signals.extend([(game, sig) for sig in signals])
        
        # Step 3: Match signals to Kalshi markets and find opportunities
        for game, signal in all_signals:
            opps = self._find_opportunities(game, signal)
            self._opportunities.extend(opps)
        
        # Step 4: Score and filter opportunities
        tradeable = self._score_opportunities(self._opportunities)
        
        logger.info(
            f"Sports scan complete: {len(live_games)} games, "
            f"{len(all_signals)} signals, {len(tradeable)} tradeable"
        )
        
        return tradeable
    
    def _find_opportunities(self, game: ESPNGame, signal: MomentumSignal) -> List[SportsOpportunity]:
        """Find Kalshi market opportunities based on a momentum signal."""
        opportunities = []
        
        # Match game to Kalshi events
        event_tickers = self.kalshi.match_game_to_events(game)
        
        # For spread markets
        if signal.signal_type in ("win_prob_shift", "scoring_run"):
            spread_event = event_tickers.get("spread")
            if spread_event:
                spread_opps = self._evaluate_spread_markets(game, signal, spread_event)
                opportunities.extend(spread_opps)
        
        # For total markets (all signal types can affect totals)
        total_event = event_tickers.get("total")
        if total_event:
            total_opps = self._evaluate_total_markets(game, signal, total_event)
            opportunities.extend(total_opps)
        
        return opportunities
    
    def _evaluate_spread_markets(self, game: ESPNGame, signal: MomentumSignal, event_ticker: str) -> List[SportsOpportunity]:
        """Find spread market opportunities based on signal."""
        opportunities = []
        
        # Get markets for this event
        markets = self.kalshi.get_markets_for_event(event_ticker)
        if not markets:
            # Try fetching from series directly
            all_spread = self.kalshi.get_spread_markets()
            # Filter by event ticker
            markets = [m for m in all_spread if event_ticker.lower() in m.get("event_ticker", "").lower()]
        
        for market in markets:
            opp = self._evaluate_single_market(
                game=game,
                signal=signal,
                market=market,
                market_type="spread",
            )
            if opp:
                opportunities.append(opp)
        
        return opportunities
    
    def _evaluate_total_markets(self, game: ESPNGame, signal: MomentumSignal, event_ticker: str) -> List[SportsOpportunity]:
        """Find total points market opportunities."""
        opportunities = []
        
        markets = self.kalshi.get_markets_for_event(event_ticker)
        if not markets:
            all_total = self.kalshi.get_total_markets()
            markets = [m for m in all_total if event_ticker.lower() in m.get("event_ticker", "").lower()]
        
        for market in markets:
            opp = self._evaluate_single_market(
                game=game,
                signal=signal,
                market=market,
                market_type="total",
            )
            if opp:
                opportunities.append(opp)
        
        return opportunities
    
    def _evaluate_single_market(self, game: ESPNGame, signal: MomentumSignal, market: dict, market_type: str) -> Optional[SportsOpportunity]:
        """Evaluate a single Kalshi market for trading opportunity."""
        yes_bid = market.get("yes_bid", 0) or 0
        yes_ask = market.get("yes_ask", 0) or 0
        no_bid = market.get("no_bid", 0) or 0
        no_ask = market.get("no_ask", 0) or 0
        volume = market.get("volume", 0) or 0
        title = market.get("title", "")
        ticker = market.get("ticker", "")
        
        # Filter by volume
        if volume < self.config.min_volume:
            return None
        
        # Need valid prices
        if yes_ask <= 0 or no_ask <= 0:
            return None
        
        # Market midpoint
        market_prob = (yes_bid + yes_ask) / 2.0 / 100.0
        if market_prob <= 0 or market_prob >= 1:
            return None
        
        # Extract strike from title
        strike = self._extract_strike(title, market_type)
        if strike is None:
            return None
        
        # Model the probability based on current game state and signal
        model_prob = self._model_probability(
            game=game,
            signal=signal,
            market_type=market_type,
            strike=strike,
            market_prob=market_prob,
        )
        
        if model_prob is None:
            return None
        
        edge = model_prob - market_prob
        
        # Determine trade direction
        if edge > self.config.min_edge:
            side = "yes"
            entry_price = yes_ask
            market_prob_entry = yes_ask / 100.0
        elif -edge > self.config.min_edge:  # Short the yes = buy no
            side = "no"
            entry_price = no_ask
            model_prob = 1.0 - model_prob
            market_prob_entry = no_ask / 100.0
            edge = model_prob - market_prob_entry
        else:
            return None  # No edge
        
        # Fee-adjusted edge (Kalshi 7% fee on winnings)
        FEE = 0.07
        ev_per_contract = (
            model_prob * (100 - entry_price) * (1 - FEE)
            - (1 - model_prob) * entry_price
        )
        
        if ev_per_contract <= 0:
            return None
        
        # Kelly sizing
        kelly_p = model_prob
        kelly_q = 1 - kelly_p
        kelly_b = (100 - entry_price) / entry_price * (1 - FEE)
        kelly_fraction = (kelly_p * kelly_b - kelly_q) / kelly_b
        kelly_fraction = max(0, kelly_fraction)
        
        position_size = min(
            self.config.max_position_dollars,
            kelly_fraction * self.config.kelly_fraction * self.config.max_position_dollars * 10,
        )
        contracts = max(1, int(position_size / (entry_price / 100.0)))
        
        should_trade = True
        reason = f"edge={edge:.1%} ev={ev_per_contract:.1f}¢"
        
        return SportsOpportunity(
            game=game,
            signal=signal,
            market_type=market_type,
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
            limit_price=entry_price,
            ev_per_contract=ev_per_contract,
            should_trade=should_trade,
            reason=reason,
        )
    
    def _extract_strike(self, title: str, market_type: str) -> Optional[float]:
        """Extract the numeric strike from a market title."""
        import re
        
        if market_type == "spread":
            # "LAL wins by over 5.5" or "LAL -5.5"
            m = re.search(r'(?:by\s+)?(?:over|more\s+than)?\s*([+-]?\d+(?:\.\d+)?)\s*(?:points?|pts?)?', title, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        elif market_type == "total":
            # "Over 225.5 total points" or "225.5+"
            m = re.search(r'(?:over|under|o/u)?\s*(\d+(?:\.\d+))\s*(?:total|points?|pts?)?', title, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        
        return None
    
    def _model_probability(self, game: ESPNGame, signal: MomentumSignal, market_type: str, strike: float, market_prob: float) -> Optional[float]:
        """Model the probability of a market outcome given current game state and momentum."""
        
        if market_type == "spread":
            return self._model_spread_prob(game, signal, strike, market_prob)
        elif market_type == "total":
            return self._model_total_prob(game, signal, strike, market_prob)
        
        return None
    
    def _model_spread_prob(self, game: ESPNGame, signal: MomentumSignal, strike: float, market_prob: float) -> float:
        """
        Model spread probability using current game state + momentum.
        
        Strategy: Momentum signals create temporary overreactions in spread markets.
        When team A goes on a run, the spread market overprices them temporarily.
        We fade the momentum (bet against the run continuing at current prices).
        """
        current_spread = game.spread  # home - away (positive = home leading)
        home_wp = game.home_win_prob
        time_remaining = 1.0 - game.elapsed_pct
        
        # Base probability from win percentage (roughly correlates to spread cover)
        # If home has 70% win prob and spread is +5.5 for home, estimate cover prob
        # Simple heuristic: win prob > 0.5 means team is favored, spread likely covered
        
        if strike > 0:  # Positive strike = home team covers
            # Estimate probability that home team wins by more than strike
            # Scale based on current spread and time remaining
            spread_margin = current_spread - strike
            prob_base = 0.5 + (spread_margin * 0.03 * (1.0 + time_remaining))
            
            # Adjust for momentum signal
            if signal.direction == "home" and signal.signal_type == "scoring_run":
                # Home team just went on a run — market overprices them
                # We fade: bet that the run ends (mean reversion)
                momentum_fade = -signal.magnitude * 0.10
                prob_base += momentum_fade
            elif signal.direction == "away" and signal.signal_type == "scoring_run":
                # Away team running — might flip spread
                momentum_boost = signal.magnitude * 0.10
                prob_base += momentum_boost
            elif signal.signal_type == "win_prob_shift":
                # Win prob shifted — adjust accordingly
                prob_base += signal.win_prob_shift * 0.5
        else:  # Negative strike = away team covers
            spread_margin = -(current_spread - strike)
            prob_base = 0.5 + (spread_margin * 0.03 * (1.0 + time_remaining))
            
            if signal.direction == "away" and signal.signal_type == "scoring_run":
                momentum_fade = -signal.magnitude * 0.10
                prob_base += momentum_fade
            elif signal.direction == "home":
                momentum_fade = signal.magnitude * 0.10
                prob_base += momentum_fade
        
        # Blend with market probability (don't stray too far)
        MAX_DEVIATION = 0.20
        prob_final = market_prob + min(MAX_DEVIATION, max(-MAX_DEVIATION, prob_base - market_prob))
        
        return max(0.05, min(0.95, prob_final))
    
    def _model_total_prob(self, game: ESPNGame, signal: MomentumSignal, strike: float, market_prob: float) -> float:
        """
        Model total points probability.
        
        Strategy: Scoring bursts and droughts are mean-reverting in totals markets.
        After a scoring burst, pace likely slows → fade the over.
        After a drought, expect more scoring → fade the under.
        """
        current_total = game.total_points
        time_remaining = 1.0 - game.elapsed_pct
        
        # Project final total based on current pace
        if game.elapsed_pct > 0.05:
            pace = current_total / game.elapsed_pct
            projected_total = pace  # Linear projection
        else:
            projected_total = 220.0  # NBA average
        
        # Base probability
        total_margin = projected_total - strike
        prob_base = 0.5 + (total_margin * 0.015)
        
        # Adjust for momentum
        if signal.signal_type == "scoring_run":
            # Scoring burst — expect mean reversion (slower pace ahead)
            # Fade the over if currently over-pacing
            if projected_total > strike:
                prob_base -= signal.magnitude * 0.08  # Fade the over
            else:
                prob_base += signal.magnitude * 0.08  # Help the over
        
        elif signal.signal_type == "drought_break":
            # After drought, scoring typically picks up
            # If currently under-pacing strike, this helps the over
            if projected_total < strike:
                prob_base += signal.magnitude * 0.08
            else:
                prob_base -= signal.magnitude * 0.05
        
        elif signal.signal_type == "win_prob_shift":
            # Big win prob shifts often accompany scoring (affects total)
            scoring_adjustment = abs(signal.win_prob_shift) * 0.10
            if projected_total > strike:
                prob_base -= scoring_adjustment * 0.5  # Regression
            else:
                prob_base += scoring_adjustment * 0.5
        
        # Blend with market
        MAX_DEVIATION = 0.20
        prob_final = market_prob + min(MAX_DEVIATION, max(-MAX_DEVIATION, prob_base - market_prob))
        
        return max(0.05, min(0.95, prob_final))
    
    def _score_opportunities(self, opportunities: List[SportsOpportunity]) -> List[SportsOpportunity]:
        """Score and rank opportunities, returning only tradeable ones."""
        if not opportunities:
            return []
        
        # Filter: must have positive edge
        tradeable = [o for o in opportunities if o.should_trade and o.edge >= self.config.min_edge]
        
        # Score by EV
        def score(opp):
            return opp.ev_per_contract * opp.contracts * min(1.0, opp.volume / 5000.0)
        
        tradeable.sort(key=score, reverse=True)
        
        self._trades_generated += len(tradeable)
        
        # Cap at 3 opportunities per scan to avoid over-trading
        return tradeable[:3]
    
    def get_scan_summary(self) -> dict:
        """Return summary data for the API/dashboard."""
        live_games = [g for g in self._active_games.values() if g.status == "in"]
        upcoming_games = [g for g in self._active_games.values() if g.status == "pre"]
        
        games_data = []
        for g in list(self._active_games.values())[:10]:
            games_data.append({
                "game_id": g.game_id,
                "status": g.status,
                "away_team": g.away_team,
                "home_team": g.home_team,
                "away_team_full": g.away_team_full,
                "home_team_full": g.home_team_full,
                "away_score": g.away_score,
                "home_score": g.home_score,
                "period": g.period,
                "clock": g.clock,
                "home_win_prob": round(g.home_win_prob, 3),
                "away_win_prob": round(g.away_win_prob, 3),
                "spread": g.spread,
                "total_points": g.total_points,
                "elapsed_pct": round(g.elapsed_pct, 3),
                "venue": g.venue,
                "broadcast": g.broadcast,
            })
        
        opps_data = []
        for o in self._opportunities[:10]:
            opps_data.append({
                "ticker": o.market_ticker,
                "title": o.market_title,
                "type": o.market_type,
                "game": f"{o.game.away_team}@{o.game.home_team}",
                "signal": o.signal.signal_type,
                "signal_details": o.signal.details,
                "edge": round(o.edge, 4),
                "model_prob": round(o.model_prob, 4),
                "market_prob": round(o.market_prob, 4),
                "side": o.side,
                "contracts": o.contracts,
                "volume": o.volume,
                "should_trade": o.should_trade,
            })
        
        return {
            "enabled": self.config.enabled,
            "paper_mode": self.config.paper_mode,
            "scan_count": self._scan_count,
            "last_scan": self._last_scan_time,
            "signals_detected": self._signals_detected,
            "trades_generated": self._trades_generated,
            "live_games": len(live_games),
            "upcoming_games": len(upcoming_games),
            "total_games": len(self._active_games),
            "games": games_data,
            "opportunities": opps_data,
            "config": {
                "scan_interval": self.config.scan_interval_seconds,
                "momentum_threshold": self.config.momentum_threshold,
                "min_edge": self.config.min_edge,
                "min_volume": self.config.min_volume,
                "max_position_dollars": self.config.max_position_dollars,
                "kelly_fraction": self.config.kelly_fraction,
                "profit_target_pct": self.config.profit_target_pct,
                "stop_loss_pct": self.config.stop_loss_pct,
            },
        }


# ── Module-level config instance ───────────────────────────────

SPORTS = SportsConfig()