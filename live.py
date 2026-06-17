#!/usr/bin/env python3
"""
GT League Match Logger – JSON Output
- Saves initial match snapshot to firstMatches.json
- Logs all goal, minute-11 detail, and match-end events to Matches.json
- Runs for a configurable duration (default 2 hours)
Based on Live.py by Sanne Karibo.
"""

import os
import time
import json
import base64
import logging
import traceback
import sys
import argparse
import threading
from datetime import datetime
from typing import Dict, Optional, Any, Tuple, List

import requests

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
USERNAME: str = "08109995000"          # Betway username
PASSWORD: str = "password"             # Betway password
LOG_LEVEL: str = "INFO"                # DEBUG / INFO / WARNING / ERROR
DURATION_SECONDS: int = 7200           # 2 hours

# ------------------------------------------------------------------------------
# API endpoints (unchanged)
# ------------------------------------------------------------------------------
AUTH_URL: str = "https://www.betway.com.ng/appsynapse/auth/users/authenticate"
LIVE_URL: str = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/"

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.betway.com.ng",
}

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
log = logging.getLogger("match_logger")

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        force=True,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

# ------------------------------------------------------------------------------
# Shared state
# ------------------------------------------------------------------------------
latest_raw: Dict[str, Any] = {}
data_lock = threading.Lock()

auth_token: str = ""
brand_id: str = ""
auth_lock = threading.Lock()
token_updated = threading.Event()

shutdown_event = threading.Event()

# Match state tracking (goal detection, minute-11 dedup)
match_state: Dict[int, Dict[str, Any]] = {}
state_lock = threading.Lock()

# JSON accumulators
initial_matches: List[Dict[str, Any]] = []   # snapshot at first data
all_events: List[Dict[str, Any]] = []        # ongoing events
first_snapshot_taken = False

# Local auth file
AUTH_FILE = "auth.txt"

# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------
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

def get_score_and_time(game_state: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Return (home_score, away_score, elapsed_seconds) from gameStateTimeScore."""
    score_list = game_state.get("score")
    home_score = away_score = None
    if isinstance(score_list, list) and len(score_list) >= 2:
        try:
            home_score = int(score_list[0])
            away_score = int(score_list[1])
        except (ValueError, TypeError):
            pass

    elapsed = game_state.get("time")
    elapsed_sec = None
    if isinstance(elapsed, (int, float)):
        elapsed_sec = int(elapsed)
    elif isinstance(elapsed, str):
        try:
            elapsed_sec = int(float(elapsed))
        except ValueError:
            pass
    return home_score, away_score, elapsed_sec

def get_match_odds(raw: Dict[str, Any], event_id: int) -> Dict[str, Optional[float]]:
    """Return dict with 'home', 'draw', 'away' decimal odds for 1X2 market."""
    events = {e["eventId"]: e for e in raw.get("events", [])}
    prices_map = {p["outcomeId"]: p for p in raw.get("prices", [])}
    outcomes_by_market: Dict[int, list] = {}
    for o in raw.get("outcomes", []):
        outcomes_by_market.setdefault(o["marketId"], []).append(o)

    odds = {"home": None, "draw": None, "away": None}
    event = events.get(event_id)
    if not event:
        return odds

    for market in raw.get("markets", []):
        if market["eventId"] != event_id:
            continue
        if market.get("marketTypeCName") not in ("win-draw-win", "1X2"):
            continue
        m_id = market["marketId"]
        for outcome in outcomes_by_market.get(m_id, []):
            name = outcome["name"]
            if name == "Draw":
                key = "draw"
            elif name == event["homeTeam"]:
                key = "home"
            elif name == event["awayTeam"]:
                key = "away"
            else:
                continue
            price_obj = prices_map.get(outcome["outcomeId"])
            if price_obj and "priceDecimal" in price_obj:
                odds[key] = price_obj["priceDecimal"]
        break
    return odds

# ------------------------------------------------------------------------------
# Authentication (same as original)
# ------------------------------------------------------------------------------
def _save_auth(token: str, brand: str) -> None:
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump({"token": token, "brand": brand}, f)
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
    saved = _load_auth()
    if saved:
        token, brand = saved
        log.info("Using token from auth.txt")
        with auth_lock:
            auth_token = token
            brand_id = brand
            token_updated.set()
        return token, brand

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

# ------------------------------------------------------------------------------
# Background data fetcher (unchanged)
# ------------------------------------------------------------------------------
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
    while True:
        with auth_lock:
            token = auth_token
        if not token:
            token_updated.wait()
            token_updated.clear()
            continue
        try:
            raw = fetch_live_data(token)
            with data_lock:
                latest_raw.clear()
                latest_raw.update(raw)
            time.sleep(0.5)
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

# ------------------------------------------------------------------------------
# Event logging helpers (JSON)
# ------------------------------------------------------------------------------
def create_event_dict(event_id: int, home_team: str, away_team: str,
                      elapsed_sec: Optional[int], home_score: Optional[int],
                      away_score: Optional[int], odds: Dict[str, Optional[float]],
                      event_type: str, notes: str = "") -> Dict[str, Any]:
    return {
        "system_time": datetime.now().isoformat(),
        "match_id": event_id,
        "home_team": home_team,
        "away_team": away_team,
        "elapsed_seconds": elapsed_sec,
        "home_score": home_score,
        "away_score": away_score,
        "home_odds": odds.get("home"),
        "draw_odds": odds.get("draw"),
        "away_odds": odds.get("away"),
        "event_type": event_type,
        "notes": notes,
    }

# ------------------------------------------------------------------------------
# Main processing loop
# ------------------------------------------------------------------------------
def process_matches() -> None:
    global first_snapshot_taken, initial_matches, all_events, match_state

    with data_lock:
        raw = dict(latest_raw)

    gt_events = [
        e for e in raw.get("events", [])
        if e.get("regionId") == "esoccer"
        and e.get("leagueId") == "gt-leagues"
    ]
    current_ids = {ev["eventId"] for ev in gt_events}

    # --- First snapshot: all active matches at this moment ---
    if not first_snapshot_taken and gt_events:
        log.info("Taking initial snapshot of %d match(es).", len(gt_events))
        for event in gt_events:
            eid = event["eventId"]
            home_team = event.get("homeTeam", "?")
            away_team = event.get("awayTeam", "?")
            gs = event.get("gameStateTimeScore", {})
            home_score, away_score, elapsed_sec = get_score_and_time(gs)
            odds = get_match_odds(raw, eid)
            initial_matches.append({
                "match_id": eid,
                "home_team": home_team,
                "away_team": away_team,
                "snapshot_time": datetime.now().isoformat(),
                "initial_score": {"home": home_score, "away": away_score},
                "initial_elapsed_seconds": elapsed_sec,
                "initial_odds": odds,
            })
        # Save immediately (overwrite each time, but it's static now)
        with open("firstMatches.json", "w", encoding="utf-8") as f:
            json.dump(initial_matches, f, indent=2, default=str)
        log.info("firstMatches.json written with %d entries.", len(initial_matches))
        first_snapshot_taken = True

    # --- Process event detection for each match ---
    for event in gt_events:
        eid = event["eventId"]
        home_team = event.get("homeTeam", "?")
        away_team = event.get("awayTeam", "?")
        gs = event.get("gameStateTimeScore", {})
        home_score, away_score, elapsed_sec = get_score_and_time(gs)

        if home_score is None or away_score is None:
            continue

        odds = get_match_odds(raw, eid)

        with state_lock:
            if eid not in match_state:
                # First time we see this match (after snapshot, or if it appeared later)
                match_state[eid] = {
                    "prev_score": (home_score, away_score),
                    "logged_minute11": set(),
                    "home_team": home_team,
                    "away_team": away_team,
                }
                # We do not log a "match_start" event here because the snapshot already captured it.
                # If a match appears after snapshot, you could optionally log it; skipping for simplicity.

            state = match_state[eid]
            prev_home, prev_away = state["prev_score"]

            # Goal detection
            if home_score != prev_home or away_score != prev_away:
                goal_scorer = ""
                if home_score > prev_home:
                    goal_scorer = f"{home_team} scored (now {home_score}-{away_score})"
                elif away_score > prev_away:
                    goal_scorer = f"{away_team} scored (now {home_score}-{away_score})"
                all_events.append(create_event_dict(
                    eid, home_team, away_team, elapsed_sec,
                    home_score, away_score, odds,
                    "goal", goal_scorer
                ))
                state["prev_score"] = (home_score, away_score)
                state["logged_minute11"].clear()
                log.info("Goal: %s vs %s %d-%d at %d sec",
                         home_team, away_team, home_score, away_score, elapsed_sec)

            # Minute-11 detailed logging
            if elapsed_sec is not None and 660 <= elapsed_sec <= 719:
                if elapsed_sec not in state["logged_minute11"]:
                    all_events.append(create_event_dict(
                        eid, home_team, away_team, elapsed_sec,
                        home_score, away_score, odds,
                        "minute11_second", f"minute 11, second {elapsed_sec-660}"
                    ))
                    state["logged_minute11"].add(elapsed_sec)
            else:
                state["logged_minute11"].clear()

            # Update stored score
            state["prev_score"] = (home_score, away_score)

    # Match end detection (matches that disappeared from active list)
    with state_lock:
        for eid in list(match_state.keys()):
            if eid not in current_ids:
                state = match_state[eid]
                last_home, last_away = state["prev_score"]
                # We cannot know the exact elapsed time at end, so leave it None
                all_events.append(create_event_dict(
                    eid, state["home_team"], state["away_team"],
                    None, last_home, last_away,
                    {"home": None, "draw": None, "away": None},
                    "match_ended", "No longer in active list"
                ))
                log.info("Match ended: %s vs %s (%d-%d)", state["home_team"],
                         state["away_team"], last_home, last_away)
                # Remove from state so we don't log end repeatedly
                del match_state[eid]

# ------------------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------------------
def main(duration: int) -> None:
    log.info("Logger starting. Duration = %d seconds.", duration)

    #authenticate()
    fetcher_thread = threading.Thread(target=background_fetcher, daemon=True)
    fetcher_thread.start()

    # Wait until we have at least some data
    log.info("Waiting for first data poll...")
    while not latest_raw and not shutdown_event.is_set():
        time.sleep(0.5)
    start_time = time.time()

    log.info("Logging active. Will run for %.0f seconds.", duration)
    try:
        while not shutdown_event.is_set() and (time.time() - start_time) < duration:
            process_matches()
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown_event.set()
    finally:
        # Save the full event log to Matches.json
        log.info("Writing Matches.json with %d events...", len(all_events))
        with open("Matches.json", "w", encoding="utf-8") as f:
            json.dump(all_events, f, indent=2, default=str)
        log.info("Matches.json saved.")
        log.info("Logger stopped cleanly.")

# ------------------------------------------------------------------------------
# Command‑line overrides
# ------------------------------------------------------------------------------
def parse_overrides() -> None:
    global LOG_LEVEL, DURATION_SECONDS
    parser = argparse.ArgumentParser(description="GT League Match Logger (JSON)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG logging")
    parser.add_argument("--duration", type=int, default=DURATION_SECONDS,
                        help=f"Total run time in seconds (default {DURATION_SECONDS})")
    args, _ = parser.parse_known_args()
    if args.debug:
        LOG_LEVEL = "DEBUG"
    DURATION_SECONDS = args.duration

if __name__ == "__main__":
    parse_overrides()
    setup_logging(LOG_LEVEL)
    try:
        main(DURATION_SECONDS)
    except Exception:
        log.critical("Fatal error:\n%s", traceback.format_exc())
