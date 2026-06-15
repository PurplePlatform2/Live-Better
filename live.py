#!/usr/bin/env python3
"""
Betway GT League Bot – In‑Play 1X2 (Goal Difference / Draw on Equaliser)
Version 3. Live.py by Sanne Karibo
- Places a bet on the winning team when elapsed >= 11 min and goal diff >= 2.
- Places a bet on the draw ONLY when a goal makes the score level after minute 11
  (i.e. the match was not a draw before, and becomes a draw after 11’).
- Retries on transient errors, prevents duplicate bets.
"""

import os
import time
import json
import base64
import uuid
import logging
import traceback
import sys
import argparse
import threading
from typing import Dict, Optional, Any, Tuple, List

import requests

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
USERNAME: str = "08109995000"          # Betway username
PASSWORD: str = "password"             # Betway password
WAGER_AMOUNT: int = 100               # Stake in NGN
IS_LIVE: bool = True                  # Actually place bets (False = dry run)
ONE_TIME: bool = False                # Exit after first successful bet
LOG_LEVEL: str = "INFO"               # DEBUG / INFO / WARNING / ERROR
MAX_RETRIES: int = 3                  # Max retries on hidden errors

# ------------------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------------------
AUTH_URL: str = "https://www.betway.com.ng/appsynapse/auth/users/authenticate"
LIVE_URL: str = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/"
STRIKE_URL: str = "https://www.betway.com.ng/appsynapse/bet-api-sr02/v2/Betting/Strike"

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.betway.com.ng",
}

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
log = logging.getLogger("betway_bot")

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

placed_bets: set[int] = set()          # matches already handled
betting_in_progress: set[int] = set()  # matches currently being bet on
progress_lock = threading.Lock()

shutdown_event = threading.Event()

# Previous scores for draw‑equaliser detection
prev_scores: Dict[int, Tuple[int, int]] = {}
prev_scores_lock = threading.Lock()

# Local auth file
AUTH_FILE = "auth.txt"

# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------
def generate_uuid() -> str:
    return str(uuid.uuid4())

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

def get_score(game_state: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    score_list = game_state.get("score")
    if isinstance(score_list, list) and len(score_list) >= 2:
        try:
            return int(score_list[0]), int(score_list[1])
        except (ValueError, TypeError):
            pass
    return None, None

def _is_hidden_error(error_text: str, data_obj: Optional[Dict[str, Any]] = None) -> bool:
    triggers = ["price", "version", "changed", "expired", "no longer available",
                "selection not found", "market suspended", "outcome changed"]
    if any(t in error_text.lower() for t in triggers):
        return True

    if data_obj:
        try:
            for bet_resp in data_obj.get("betResponses", []):
                if bet_resp.get("errorCode") == 100001:
                    return True
                err_meta = bet_resp.get("errorMetaData", {})
                if err_meta.get("code") == 100001:
                    return True
                for sel in err_meta.get("erroredSelections", []):
                    if sel.get("errorCode") == 100001:
                        return True
        except Exception:
            pass
    return False

def parse_bet_response(data_obj: Dict[str, Any]) -> Tuple[bool, bool, Optional[str]]:
    if not data_obj:
        return False, False, "Empty response"
    bet_responses = data_obj.get("betResponses", [])
    if not bet_responses:
        return data_obj.get("isSuccessful", False), False, "No bet responses"

    success = True
    hidden = False
    error_messages = []
    for resp in bet_responses:
        is_success = resp.get("isSuccessful", False)
        placement = resp.get("placementStatus", "")
        error_code = resp.get("errorCode", 0)
        if not is_success or placement == "Error":
            success = False
            error_meta = resp.get("errorMetaData", {})
            msg = error_meta.get("message") or resp.get("errorMessage") or "Unknown error"
            error_messages.append(f"[{error_code}] {msg}")
            if error_code == 100001 or _is_hidden_error(msg):
                hidden = True
        else:
            if placement != "Accepted":
                success = False
                error_messages.append(f"placementStatus={placement}")
    return (True, False, None) if success else (False, hidden, "; ".join(error_messages))

# ------------------------------------------------------------------------------
# Authentication – load from auth.txt, fallback to API login and save
# ------------------------------------------------------------------------------
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
        if token and brand and decode_jwt(token):  # quick sanity check
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

    # 2. Perform API login
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
    data: Dict[str, Any] = resp.json()
    token: Optional[str] = data.get("access_token")
    if not token:
        raise ValueError("Invalid login response – missing access_token")
    claims: Dict[str, Any] = decode_jwt(token)
    brand: str = claims.get(
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
# Background data fetcher
# ------------------------------------------------------------------------------
def fetch_live_data(token: str) -> Optional[Dict[str, Any]]:
    params = {
        "countryCode": "NG",
        "sportId": "soccer",
        "Skip": 0,
        "Take": 100,
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
# Selection & payload builders
# ------------------------------------------------------------------------------
def build_selection(raw: Dict[str, Any], event_id: int, pick: str) -> Optional[Dict[str, Any]]:
    events = {e["eventId"]: e for e in raw.get("events", [])}
    prices_map = {p["outcomeId"]: p for p in raw.get("prices", [])}
    outcomes_by_market: Dict[int, list] = {}
    for o in raw.get("outcomes", []):
        outcomes_by_market.setdefault(o["marketId"], []).append(o)

    event = events.get(event_id)
    if not event:
        return None

    for market in raw.get("markets", []):
        if market["eventId"] != event_id:
            continue
        if market.get("marketTypeCName") not in ("win-draw-win", "1X2"):
            continue
        m_id: int = market["marketId"]
        for outcome in outcomes_by_market.get(m_id, []):
            matched = False
            if pick == "draw" and outcome["name"] == "Draw":
                matched = True
            elif pick == "home" and outcome["name"] == event["homeTeam"]:
                matched = True
            elif pick == "away" and outcome["name"] == event["awayTeam"]:
                matched = True
            if not matched:
                continue

            price_obj = prices_map.get(outcome["outcomeId"])
            if not price_obj:
                continue

            return {
                "price": price_obj["priceDecimal"],
                "eventId": event["eventId"],
                "marketId": market["marketId"],
                "outcomeId": outcome["outcomeId"],
                "eventVersion": event["version"],
                "marketVersion": market["version"],
                "outcomeVersion": outcome["version"],
                "priceVersion": price_obj["version"],
                "priceNum": price_obj["numerator"],
                "priceDen": price_obj["denominator"],
                "publicHubPublishedTime": price_obj.get("publicHubPublishedTime"),
                "serverEmopSource": price_obj.get("emopSource", 1),
            }
    return None

def build_bet_payload(selection: Dict[str, Any], wager_amount: int) -> Dict[str, Any]:
    request_id = generate_uuid()
    return {
        "currencyCode": "NGN",
        "countryCode": "NG",
        "betRequests": [{
            "requestId": request_id,
            "paymentType": 1,
            "betSelectionType": "Normal",
            "numberOfLines": 1,
            "acceptPriceChange": "None",
            "isEachWay": False,
            "channel": "web",
            "handicap": 0,
            "priceNum": selection["priceNum"],
            "priceDen": selection["priceDen"],
            "referringBookingCode": "",
            "wagerAmount": wager_amount,
            "bets": [{
                "priceType": "Normal",
                "handicap": 0,
                "priceDen": selection["priceDen"],
                "priceNum": selection["priceNum"],
                "priceDec": selection["price"],
                "isEachWayActive": False,
                "eventId": selection["eventId"],
                "marketId": selection["marketId"],
                "displayMarketId": selection["marketId"],
                "outcomeId": [selection["outcomeId"]],
                "eventVersion": selection["eventVersion"],
                "marketVersion": selection["marketVersion"],
                "outcomeVersion": selection["outcomeVersion"],
                "priceVersion": selection["priceVersion"],
                "serverEmopSource": selection["serverEmopSource"],
                "publicHubPublishedTime": selection["publicHubPublishedTime"],
            }],
        }],
    }

def post_bet(token: str, brand: str, payload: Dict[str, Any]) -> Tuple[bool, bool, Optional[Dict], Optional[str]]:
    headers = {
        **HEADERS,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Brand-Id": brand,
    }
    body = json.dumps(payload)
    try:
        resp = requests.post(STRIKE_URL, headers=headers, data=body, timeout=20)
    except requests.exceptions.RequestException as e:
        return False, False, None, f"HTTP error: {e}"

    data_obj = None
    try:
        data_obj = resp.json() if resp.text else None
    except ValueError:
        pass

    if resp.status_code == 200 and data_obj:
        success, hidden, detail = parse_bet_response(data_obj)
        if success:
            return True, False, data_obj, None
        return False, hidden, data_obj, detail

    if resp.status_code == 400:
        hidden = _is_hidden_error(resp.text, data_obj)
        return False, hidden, data_obj, resp.text

    if resp.status_code == 401:
        return False, False, None, "401 Unauthorized"

    try:
        resp.raise_for_status()
    except Exception as e:
        return False, False, None, str(e)
    return False, False, None, "Unknown error"

# ------------------------------------------------------------------------------
# Bet worker (retries + marking)
# ------------------------------------------------------------------------------
def bet_worker(event_id: int, pick: str, match_name: str) -> None:
    global placed_bets, betting_in_progress
    retries = 0
    log.info("Thread for match %d (%s) – betting on %s", event_id, match_name, pick)

    try:
        while retries <= MAX_RETRIES and not shutdown_event.is_set():
            with data_lock:
                raw_bet = dict(latest_raw)

            # Verify event still active
            event_bet = None
            for ev in raw_bet.get("events", []):
                if ev["eventId"] == event_id:
                    event_bet = ev
                    break
            if not event_bet or not event_bet.get("isActive", False):
                log.warning("Match %d gone/inactive – giving up", event_id)
                break

            selection = build_selection(raw_bet, event_id, pick)
            if not selection:
                log.warning("Could not build selection for %d – retrying", event_id)
                retries += 1
                continue

            payload = build_bet_payload(selection, WAGER_AMOUNT)

            if not IS_LIVE:
                log.info("❌ Dry run – bet NOT placed for %s (pick=%s).", match_name, pick)
                break

            with auth_lock:
                tok = auth_token
                bid = brand_id
            success, hidden_error, resp_data, err_text = post_bet(tok, bid, payload)

            if success:
                try:
                    first_resp = resp_data.get("betResponses", [{}])[0]
                    betslip = first_resp.get("betslipId")
                    booking = first_resp.get("bookingCode")
                    log.info("✅ Bet placed successfully! Betslip: %s, Booking: %s", betslip, booking)
                except Exception:
                    log.info("✅ Bet placed successfully! Response: %s", resp_data)
                if ONE_TIME:
                    shutdown_event.set()
                break

            if hidden_error:
                retries += 1
                if retries <= MAX_RETRIES:
                    log.info("Hidden error – retry %d/%d instantly", retries, MAX_RETRIES)
                    continue
                else:
                    log.error("Max retries reached for match %d – giving up", event_id)
                    break
            elif "401" in str(err_text):
                log.warning("Got 401 – re‑authenticating…")
                try:
                    authenticate()
                except Exception as e:
                    log.error("Re‑auth failed: %s", e)
                retries += 1
                if retries > MAX_RETRIES:
                    break
                continue
            else:
                log.error("Non‑recoverable error: %s – giving up on %s", err_text, match_name)
                break
    finally:
        with progress_lock:
            betting_in_progress.discard(event_id)
            placed_bets.add(event_id)

# ------------------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------------------
def main() -> None:
    global placed_bets, betting_in_progress, prev_scores
    log.info("Bot starting. IS_LIVE = %s, Wager = %d NGN, ONE_TIME = %s",
             IS_LIVE, WAGER_AMOUNT, ONE_TIME)

    authenticate()
    fetcher_thread = threading.Thread(target=background_fetcher, daemon=True)
    fetcher_thread.start()

    active_bet_threads: List[threading.Thread] = []

    while not shutdown_event.is_set():
        with data_lock:
            raw = dict(latest_raw)

        gt_events = [
            e for e in raw.get("events", [])
            if e.get("regionId") == "esoccer"
            and e.get("leagueId") == "gt-leagues"
            and e.get("isActive", False)   # only active matches
        ]

        if gt_events:
            log.info("Found %d GT League match(es):", len(gt_events))
            for ev in gt_events:
                gs = ev.get("gameStateTimeScore", {})
                home_score, away_score = get_score(gs)
                score_str = f"{home_score}-{away_score}" if home_score is not None else "?-?"
                log.info("  %s vs %s  (%s, elapsed: %s min)", ev["homeTeam"], ev["awayTeam"],
                         score_str, gs.get("time", "?"))

        current_ids = {ev["eventId"] for ev in gt_events}

        for event in gt_events:
            eid = event["eventId"]
            gs = event.get("gameStateTimeScore", {})
            elapsed_min = gs.get("time")
            if not isinstance(elapsed_min, (int, float)):
                continue
            home_score, away_score = get_score(gs)
            if home_score is None or away_score is None:
                continue

            match_name = f"{event['homeTeam']} vs {event['awayTeam']}"
            goal_diff = abs(home_score - away_score)

            # Retrieve previous known score for this match
            with prev_scores_lock:
                prev = prev_scores.get(eid)

            # --- Condition 1: bet on winning team if elapsed >= 11 and diff >= 2 ---
            if elapsed_min >= 11 and goal_diff >= 2:
                pick = "home" if home_score > away_score else "away"
                with progress_lock:
                    if eid in placed_bets or eid in betting_in_progress:
                        # Update score anyway, then skip
                        with prev_scores_lock:
                            prev_scores[eid] = (home_score, away_score)
                        continue
                    betting_in_progress.add(eid)
                log.info("Condition WIN: %s (%d:%d) – betting on %s", match_name, home_score, away_score, pick)
                t = threading.Thread(target=bet_worker, args=(eid, pick, match_name), daemon=True)
                t.start()
                active_bet_threads.append(t)
                # Update score after betting attempt
                with prev_scores_lock:
                    prev_scores[eid] = (home_score, away_score)
                continue

            # --- Condition 2: bet on draw ONLY if score became a draw after minute 11 ---
            if elapsed_min >= 11:
                # Check if current score is a draw and previous known score was NOT a draw
                if home_score == away_score and prev is not None and prev[0] != prev[1]:
                    with progress_lock:
                        if eid not in placed_bets and eid not in betting_in_progress:
                            betting_in_progress.add(eid)
                            log.info("Condition DRAW (equaliser): %s (%d:%d) – betting on draw",
                                     match_name, home_score, away_score)
                            t = threading.Thread(target=bet_worker, args=(eid, "draw", match_name), daemon=True)
                            t.start()
                            active_bet_threads.append(t)
                # Always update the stored score for this match (even if no bet placed)
                with prev_scores_lock:
                    prev_scores[eid] = (home_score, away_score)
            else:
                # For matches before minute 11, just store current score for future comparisons
                with prev_scores_lock:
                    prev_scores[eid] = (home_score, away_score)

        # Clean up finished threads
        active_bet_threads = [t for t in active_bet_threads if t.is_alive()]

        # Remove previous scores for matches that are no longer active
        with prev_scores_lock:
            for eid in list(prev_scores.keys()):
                if eid not in current_ids:
                    del prev_scores[eid]

        time.sleep(0.5)

    log.info("Bot shutdown requested. Waiting for active bet threads...")
    for t in active_bet_threads:
        t.join(timeout=5)
    log.info("Bot stopped cleanly.")

# ------------------------------------------------------------------------------
# Command‑line overrides
# ------------------------------------------------------------------------------
def parse_overrides() -> None:
    global IS_LIVE, ONE_TIME, LOG_LEVEL
    parser = argparse.ArgumentParser(description="Betway GT League Bot (goal diff / equaliser draw)")
    parser.add_argument("--live", action="store_true", default=None,
                        help="Enable live betting (otherwise dry run)")
    parser.add_argument("--one-time", action="store_true", default=None,
                        help="Exit after first successful bet")
    parser.add_argument("--debug", action="store_true", default=False,
                        help="Shortcut: dry‑run + one‑time + DEBUG logging")
    args, _ = parser.parse_known_args()

    if args.debug:
        IS_LIVE = False
        ONE_TIME = True
        LOG_LEVEL = "DEBUG"
    else:
        if args.live is not None:
            IS_LIVE = args.live
        if args.one_time is not None:
            ONE_TIME = args.one_time

if __name__ == "__main__":
    parse_overrides()
    setup_logging(LOG_LEVEL)
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception:
        log.critical("Fatal error:\n%s", traceback.format_exc())
