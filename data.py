import os
import time
import json
import threading
import logging
import traceback
import sys
import argparse
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

import requests

# ============================================================
# CONFIG (can be overridden via command line)
# ============================================================
USERNAME: str = "Demo"
PASSWORD: str = "swords"
LOG_LEVEL: str = "INFO"

# API endpoints
LIVE_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.betway.com.ng",
}

FETCH_INTERVAL = 0.5
MATCHES_PER_FILE = 100
LIVE_LOG_DIR = "data"
LIVE_LOG_PREFIX = "live"

# ============================================================
# LOGGING
# ============================================================
log = logging.getLogger("gt_collector")

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

def now_wall() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
# SHARED STATE
# ============================================================
latest_raw: Dict[str, Any] = {}
data_lock = threading.Lock()
shutdown_event = threading.Event()

# ============================================================
# LIVE FILE WRITER
# ============================================================
class RollingLiveWriter:
    def __init__(self, base_dir: str, prefix: str, max_matches: int = 100):
        self.base_dir = base_dir
        self.prefix = prefix
        self.max_matches = max_matches
        self.lock = threading.Lock()
        self.file_index = 1
        self.match_count = 0
        os.makedirs(self.base_dir, exist_ok=True)
        self._resume_or_create()

    def _path(self, index: Optional[int] = None) -> str:
        idx = self.file_index if index is None else index
        return os.path.join(self.base_dir, f"{self.prefix}{idx}.txt")

    def _ensure_file(self, index: Optional[int] = None) -> None:
        path = self._path(index)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf8") as f:
                f.write("wall_time|event|match_time|score|home_team|away_team|home_odds|draw_odds|away_odds|extra\n")

    def _count_starts(self, path: str) -> int:
        try:
            with open(path, "r", encoding="utf8") as f:
                return sum(1 for line in f if "|START|" in line)
        except Exception:
            return 0

    def _resume_or_create(self) -> None:
        index = 1
        while os.path.exists(self._path(index)):
            index += 1

        if index == 1:
            self.file_index = 1
            self.match_count = 0
            self._ensure_file(1)
            return

        last_index = index - 1
        last_path = self._path(last_index)
        count = self._count_starts(last_path)
        if count >= self.max_matches:
            self.file_index = index
            self.match_count = 0
            self._ensure_file(index)
        else:
            self.file_index = last_index
            self.match_count = count
            self._ensure_file(last_index)

    def _rotate_if_needed(self) -> None:
        if self.match_count >= self.max_matches:
            self.file_index += 1
            self.match_count = 0
            self._ensure_file(self.file_index)

    def start_match(self) -> None:
        with self.lock:
            self._rotate_if_needed()
            self.match_count += 1
            self._ensure_file(self.file_index)

    def append(self, line: str) -> None:
        with self.lock:
            self._ensure_file(self.file_index)
            with open(self._path(), "a", encoding="utf8") as f:
                f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

    def current_path(self) -> str:
        return self._path()

live_writer = RollingLiveWriter(LIVE_LOG_DIR, LIVE_LOG_PREFIX, MATCHES_PER_FILE)

# ============================================================
# DATA WRITER (kept for completed match snapshots)
# ============================================================
class DatasetWriter:
    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self.file_index = 1
        self.match_count = 0
        self.lock = threading.Lock()
        self._discover_last_file()

    def _discover_last_file(self):
        while True:
            path = f"data/data{self.file_index}.txt"
            if not os.path.exists(path):
                break
            try:
                with open(path, "r", encoding="utf8") as f:
                    count = sum(1 for _ in f)
                if count < MATCHES_PER_FILE:
                    self.match_count = count
                    return
                self.file_index += 1
            except Exception:
                break

    def save_match(self, line: str):
        with self.lock:
            if self.match_count >= MATCHES_PER_FILE:
                self.file_index += 1
                self.match_count = 0
            path = f"data/data{self.file_index}.txt"
            with open(path, "a", encoding="utf8") as f:
                f.write(line + "\n")
            self.match_count += 1

writer = DatasetWriter()

# ============================================================
# HELPERS
# ============================================================
def get_score(game_state: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    score = game_state.get("score")
    if isinstance(score, list) and len(score) >= 2:
        try:
            return int(score[0]), int(score[1])
        except Exception:
            pass
    return None, None

def get_match_odds(raw: Dict[str, Any], event_id: int):
    events = {e["eventId"]: e for e in raw.get("events", [])}
    prices = {p["outcomeId"]: p for p in raw.get("prices", [])}
    odds = {"home": None, "draw": None, "away": None}
    event = events.get(event_id)
    if not event:
        return odds
    home_team = event.get("homeTeam")
    away_team = event.get("awayTeam")
    outcomes_by_market = {}
    for outcome in raw.get("outcomes", []):
        outcomes_by_market.setdefault(outcome["marketId"], []).append(outcome)
    for market in raw.get("markets", []):
        if market.get("eventId") != event_id:
            continue
        market_name = market.get("marketTypeCName", "")
        if market_name not in ("win-draw-win", "1X2"):
            continue
        market_id = market["marketId"]
        for outcome in outcomes_by_market.get(market_id, []):
            price_obj = prices.get(outcome["outcomeId"])
            if not price_obj:
                continue
            price = price_obj.get("priceDecimal")
            if outcome["name"] == "Draw":
                odds["draw"] = price
            elif outcome["name"] == home_team:
                odds["home"] = price
            elif outcome["name"] == away_team:
                odds["away"] = price
        break
    return odds

def odds_str(odds: Dict[str, Any]) -> str:
    def fmt(v):
        return "NA" if v is None else str(v)
    return f"{fmt(odds.get('home'))},{fmt(odds.get('draw'))},{fmt(odds.get('away'))}"

def odds_changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    return (
        before.get("home") != after.get("home") or
        before.get("draw") != after.get("draw") or
        before.get("away") != after.get("away")
    )

def log_live_line(event: Dict[str, Any], event_type: str,
                  minute: Any, home_score: Any, away_score: Any,
                  odds: Dict[str, Any], extra: str = "") -> None:
    line = "|".join([
        now_wall(),
        event_type,
        str(minute if minute is not None else "NA"),
        f"{home_score}-{away_score}",
        str(event.get("homeTeam", "HOME")),
        str(event.get("awayTeam", "AWAY")),
        odds_str(odds),
        extra,
    ])
    live_writer.append(line)

# ============================================================
# FETCHER
# ============================================================
def fetch_live_data() -> Optional[Dict[str, Any]]:
    params = {
        "countryCode": "NG",
        "sportId": "soccer",
        "Skip": 0,
        "Take": 10000,
        "cultureCode": "en-US",
        "isEsport": False,
        "boostedOnly": False,
        "marketTypes": [
            "[Win/Draw/Win]",
            "1X2 (1Up)",
            "1X2 (2Up)",
            "[Double Chance]",
        ],
    }
    resp = requests.get(LIVE_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()

def background_fetcher() -> None:
    while not shutdown_event.is_set():
        try:
            raw = fetch_live_data()
            filtered_events = [
                e for e in raw.get("events", [])
                if e.get("regionId") == "esoccer" and e.get("leagueId") == "gt-leagues"
            ]
            raw["events"] = filtered_events
            with data_lock:
                latest_raw.clear()
                latest_raw.update(raw)
        except requests.exceptions.HTTPError as e:
            log.error("Fetcher HTTP error: %s", e)
            time.sleep(1)
        except Exception as e:
            log.error("Fetcher error: %s", e)
            time.sleep(1)
        time.sleep(FETCH_INTERVAL)

# ============================================================
# MATCH TRACKER
# ============================================================
active_matches: Dict[int, Dict[str, Any]] = {}

def compact_goal(minute, score, before, after):
    return (
        f"{minute},"
        f"{score},"
        f"{before['home']},"
        f"{before['draw']},"
        f"{before['away']},"
        f"{after['home']},"
        f"{after['draw']},"
        f"{after['away']}"
    )

def finalize_match(event_id, final_home, final_away):
    state = active_matches.get(event_id)
    if not state:
        return
    line = (
        f"{state['homeTeam']}|"
        f"{state['awayTeam']}|"
        f"{final_home}-{final_away}"
    )
    if state["goals"]:
        line += "|" + "|".join(state["goals"])
    writer.save_match(line)
    log.info("saved %s vs %s", state['homeTeam'], state['awayTeam'])

def tracker_loop():
    while not shutdown_event.is_set():
        with data_lock:
            raw = dict(latest_raw)

        events = raw.get("events", [])
        current_ids = set()

        for event in events:
            if not event.get("isActive", True):
                continue

            event_id = event["eventId"]
            current_ids.add(event_id)

            game_state = event.get("gameStateTimeScore", {})
            minute = game_state.get("time", 0)

            home_score, away_score = get_score(game_state)
            if home_score is None:
                continue

            current_odds = get_match_odds(raw, event_id)

            if event_id not in active_matches:
                active_matches[event_id] = {
                    "homeTeam": event.get("homeTeam", "HOME"),
                    "awayTeam": event.get("awayTeam", "AWAY"),
                    "lastHome": home_score,
                    "lastAway": away_score,
                    "lastOdds": current_odds,
                    "lastMinute": minute,
                    "goals": []
                }
                live_writer.start_match()
                log.info("new match %s vs %s %s-%s minute %s",
                         active_matches[event_id]["homeTeam"],
                         active_matches[event_id]["awayTeam"],
                         home_score, away_score, minute)
                log_live_line(event, "START", minute, home_score, away_score, current_odds)
                continue

            state = active_matches[event_id]
            old_home = state["lastHome"]
            old_away = state["lastAway"]
            old_odds = state.get("lastOdds", {})

            score_changed = (old_home != home_score or old_away != away_score)
            odds_changed_flag = odds_changed(old_odds, current_odds)

            if score_changed or odds_changed_flag:
                if score_changed:
                    record = compact_goal(
                        minute,
                        f"{home_score}-{away_score}",
                        state["lastOdds"],
                        current_odds
                    )
                    state["goals"].append(record)
                    log.info("goal %s vs %s %s", state["homeTeam"], state["awayTeam"], record)

                event_type = (
                    "GOAL" if score_changed and not odds_changed_flag else
                    "ODDS" if odds_changed_flag and not score_changed else
                    "GOAL_ODDS"
                )
                extra_parts = []
                if score_changed:
                    extra_parts.append(f"score_change={old_home}-{old_away}->{home_score}-{away_score}")
                if odds_changed_flag:
                    extra_parts.append(f"odds_change={odds_str(old_odds)}->{odds_str(current_odds)}")
                log_live_line(event, event_type, minute, home_score, away_score, current_odds, ";".join(extra_parts))

            state["lastHome"] = home_score
            state["lastAway"] = away_score
            state["lastOdds"] = current_odds
            state["lastMinute"] = minute

        finished = [eid for eid in active_matches if eid not in current_ids]
        for event_id in finished:
            state = active_matches[event_id]
            finalize_match(event_id, state["lastHome"], state["lastAway"])
            del active_matches[event_id]

        time.sleep(0.5)

# ============================================================
# MAIN
# ============================================================
def main():
    log.info("GT League collector started")
    log.info("Live log file: %s", live_writer.current_path())

    fetcher_thread = threading.Thread(target=background_fetcher, daemon=True)
    fetcher_thread.start()

    tracker_loop()

# ============================================================
# COMMAND-LINE OVERRIDES
# ============================================================
def parse_overrides():
    global USERNAME, PASSWORD, LOG_LEVEL
    parser = argparse.ArgumentParser(description="GT League goal data collector")
    parser.add_argument("--username", type=str, default=None, help="Betway username")
    parser.add_argument("--password", type=str, default=None, help="Betway password")
    parser.add_argument("--debug", action="store_true", help="Debug logging")
    args, _ = parser.parse_known_args()
    if args.username:
        USERNAME = args.username
    if args.password:
        PASSWORD = args.password
    if args.debug:
        LOG_LEVEL = "DEBUG"

if __name__ == "__main__":
    parse_overrides()
    setup_logging(LOG_LEVEL)
    try:
        main()
    except KeyboardInterrupt:
        shutdown_event.set()
        log.info("Collector stopped by user.")
    except Exception:
        log.critical("Fatal error:\n%s", traceback.format_exc())
