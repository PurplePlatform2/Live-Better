import os
import time
import json
import base64
import uuid
import threading
import logging
import traceback
import sys
import argparse
from typing import Dict, Any, Optional, Tuple, List

import requests

# ============================================================
# CONFIG (can be overridden via command line)
# ============================================================
USERNAME: str = "Demo"          # Default Betway username
PASSWORD: str = "swords"            # Default Betway password
LOG_LEVEL: str = "INFO"

# API endpoints
AUTH_URL = "https://www.betway.com.ng/appsynapse/auth/users/authenticate"
LIVE_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.betway.com.ng",
}

AUTH_FILE = "auth.txt"

FETCH_INTERVAL = 0.5
MATCHES_PER_FILE = 100

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

# ============================================================
# SHARED STATE
# ============================================================
latest_raw: Dict[str, Any] = {}
data_lock = threading.Lock()

auth_token: str = ""
brand_id: str = ""
auth_lock = threading.Lock()
token_updated = threading.Event()

shutdown_event = threading.Event()

# ============================================================
# AUTH HELPERS
# ============================================================
def decode_jwt(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1].replace("-", "+").replace("_", "/")
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        decoded = base64.b64decode(payload_b64).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}

def _save_auth(token: str, brand: str) -> None:
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump({"token": token, "brand": brand}, f)
        log.info("Authentication saved to %s", AUTH_FILE)
    except Exception as e:
        log.warning("Could not save auth file: %s", e)

def _load_auth() -> Optional[Tuple[str, str]]:
    if not os.path.exists(AUTH_FILE):
        return None
    try:
        with open(AUTH_FILE, "r") as f:
            data = json.load(f)
        token = data.get("token")
        brand = data.get("brand")
        if token and brand and decode_jwt(token):
            return token, brand
    except Exception:
        pass
    return None

def authenticate() -> Tuple[str, str]:
    global auth_token, brand_id

    # 1. Try local file
    saved = _load_auth()
    if saved:
        token, brand = saved
        log.info("Using token from auth.txt")
        with auth_lock:
            auth_token = token
            brand_id = brand
            token_updated.set()
        return token, brand

    # 2. API login
    body = json.dumps({
        "username": USERNAME,
        "password": PASSWORD,
        "countryCode": "NG",
        "sessionMetadata": {},
    })
    resp = requests.post(
        AUTH_URL,
        headers={**HEADERS, "Content-Type": "application/json"},
        data=body,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError("Invalid login response – missing access_token")
    claims = decode_jwt(token)
    brand = claims.get(
        "http://schemas.ragingriver.io/ws/2021/05/identity/claims/brand",
        "f8a8d16a-d619-4b49-aa8c-f21211403c92",
    )
    _save_auth(token, brand)

    with auth_lock:
        auth_token = token
        brand_id = brand
        token_updated.set()
    return token, brand

# ============================================================
# DATA WRITER (unchanged)
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

# ============================================================
# FETCHER (with auth & GT‑league filter)
# ============================================================
def fetch_live_data(token: str) -> Optional[Dict[str, Any]]:
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
    headers = {**HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(LIVE_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()

def background_fetcher() -> None:
    while not shutdown_event.is_set():
        with auth_lock:
            token = auth_token
        if not token:
            token_updated.wait()
            token_updated.clear()
            continue
        try:
            raw = fetch_live_data(token)
            # Filter to only GT leagues before storing
            filtered_events = [
                e for e in raw.get("events", [])
                if e.get("regionId") == "esoccer" and e.get("leagueId") == "gt-leagues"
            ]
            raw["events"] = filtered_events
            with data_lock:
                latest_raw.clear()
                latest_raw.update(raw)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                log.warning("Fetcher got 401 – re‑authenticating…")
                try:
                    authenticate()
                except Exception as auth_err:
                    log.error("Fetcher re‑auth failed: %s", auth_err)
                    time.sleep(5)
            else:
                log.error("Fetcher HTTP error: %s", e)
                time.sleep(1)
        except Exception as e:
            log.error("Fetcher error: %s", e)
            time.sleep(1)
        time.sleep(FETCH_INTERVAL)

# ============================================================
# MATCH TRACKER (unchanged logic)
# ============================================================
active_matches = {}

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
    log.info("saved %s vs %s", state["homeTeam"], state["awayTeam"])

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
                    "goals": []
                }
                continue

            state = active_matches[event_id]
            old_home = state["lastHome"]
            old_away = state["lastAway"]

            if old_home != home_score or old_away != away_score:
                record = compact_goal(
                    minute,
                    f"{home_score}-{away_score}",
                    state["lastOdds"],
                    current_odds
                )
                state["goals"].append(record)
                log.info("goal %s vs %s %s", state["homeTeam"], state["awayTeam"], record)

            state["lastHome"] = home_score
            state["lastAway"] = away_score
            state["lastOdds"] = current_odds

        # finished matches
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
    #authenticate()
    log.info("GT League collector started")

    fetcher_thread = threading.Thread(target=background_fetcher, daemon=True)
    fetcher_thread.start()

    tracker_loop()

# ============================================================
# COMMAND‑LINE OVERRIDES
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
