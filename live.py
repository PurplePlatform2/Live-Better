#!/usr/bin/env python3
"""
Betway GT League Bot – In‑Play 1X2 (Lowest Odds < 2.0) – Multi‑threaded
- Checks per‑bet isSuccessful & placementStatus from API
- Retries up to 3 times on hidden errors (price/version change) instantly
- Prevents duplicate bets via thread‑safe in‑progress tracking
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
from typing import Dict, Optional, Any, Tuple, List, Set

import requests

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------
USERNAME: str = "08109995000"          # Betway username
PASSWORD: str = "password"             # Betway password
WAGER_AMOUNT: int = 100               # Stake in NGN
IS_LIVE: bool = True                 # Actually place bets (False = dry run)
ONE_TIME: bool = False               # Exit after first *successful* bet
LOG_LEVEL: str = "INFO"               # DEBUG / INFO / WARNING / ERROR
TIMER_SECONDS: int = 45               # Seconds to wait after 11th minute before betting
MAX_RETRIES: int = 3                 # Max retries on hidden errors

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

placed_bets: Set[int] = set()          # matches already handled
betting_in_progress: Set[int] = set()  # matches currently being bet on
progress_lock = threading.Lock()

shutdown_event = threading.Event()

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
    """
    Determine if the error is transient (price/version changed) and can be retried.
    Checks keywords AND structured error codes from the API response.
    """
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
    """
    Parse the full Strike response to determine:
      - success: True if ALL betResponses have isSuccessful == True and placementStatus == "Accepted"
      - is_hidden_error: True if any failed due to a transient (price/version) error
      - error_detail: human‑readable summary of the error(s)
    Returns (success, is_hidden_error, error_detail)
    """
    if not data_obj:
        return False, False, "Empty response"

    bet_responses = data_obj.get("betResponses", [])
    if not bet_responses:
        # Fallback to top‑level isSuccessful if no betResponses
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

            # Check for hidden error codes
            if error_code == 100001 or _is_hidden_error(msg):
                hidden = True
        else:
            # Double‑check placement status just in case
            if placement != "Accepted":
                success = False
                error_messages.append(f"placementStatus={placement}")

    if success:
        return True, False, None

    error_detail = "; ".join(error_messages)
    is_hidden = hidden  # True if at least one failed with a hidden error
    return False, is_hidden, error_detail

# ------------------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------------------
def authenticate() -> Tuple[str, str]:
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
    with auth_lock:
        global auth_token, brand_id
        auth_token = token
        brand_id = brand
        token_updated.set()
    log.info("Authenticated. Brand ID: %s", brand)
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
    global auth_token, brand_id
    while True:
        with auth_lock:
            token = auth_token
            bid = brand_id
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
# Bet building (unchanged from earlier)
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

# ------------------------------------------------------------------------------
# Low‑level bet post – now uses parse_bet_response
# ------------------------------------------------------------------------------
def post_bet(token: str, brand: str, payload: Dict[str, Any]) -> Tuple[bool, bool, Optional[Dict], Optional[str]]:
    """
    Returns (success, is_hidden_error, response_data, error_text)
    success = True only if ALL betResponses indicate success.
    """
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
        else:
            # non‑200 but still got JSON
            return False, hidden, data_obj, detail

    if resp.status_code == 400:
        hidden = _is_hidden_error(resp.text, data_obj)
        return False, hidden, data_obj, resp.text

    if resp.status_code == 401:
        return False, False, None, "401 Unauthorized"

    # Other errors
    try:
        resp.raise_for_status()
    except Exception as e:
        return False, False, None, str(e)
    return False, False, None, "Unknown error"

# ------------------------------------------------------------------------------
# Lowest odds pick
# ------------------------------------------------------------------------------
def lowest_odds_pick(raw: Dict[str, Any], event: Dict[str, Any]) -> Optional[Tuple[str, float]]:
    eid: int = event["eventId"]
    prices_map: Dict[int, float] = {p["outcomeId"]: p["priceDecimal"] for p in raw.get("prices", [])}
    outcomes_by_market: Dict[int, list] = {}
    for o in raw.get("outcomes", []):
        outcomes_by_market.setdefault(o["marketId"], []).append(o)

    home_odd: Optional[float] = None
    draw_odd: Optional[float] = None
    away_odd: Optional[float] = None
    for market in raw.get("markets", []):
        if market["eventId"] != eid:
            continue
        if market.get("marketTypeCName") not in ("win-draw-win", "1X2"):
            continue
        for outcome in outcomes_by_market.get(market["marketId"], []):
            odd = prices_map.get(outcome["outcomeId"])
            if odd is None or odd <= 0:
                continue
            if outcome["name"] == "Draw":
                draw_odd = odd
            elif outcome["name"] == event["homeTeam"]:
                home_odd = odd
            elif outcome["name"] == event["awayTeam"]:
                away_odd = odd

    picks = [("home", home_odd), ("draw", draw_odd), ("away", away_odd)]
    valid = [(p, o) for p, o in picks if o is not None]
    if not valid:
        return None
    return min(valid, key=lambda x: x[1])

# ------------------------------------------------------------------------------
# Bet worker (per match) – retries up to MAX_RETRIES on hidden errors
# ------------------------------------------------------------------------------
def bet_match_worker(event_id: int, event_dict: Dict[str, Any], pick: str, match_name: str) -> None:
    global placed_bets, betting_in_progress
    retries = 0
    current_pick = pick
    log.info("Thread for match %d (%s) started – initial pick: %s", event_id, match_name, current_pick)

    try:
        while retries <= MAX_RETRIES and not shutdown_event.is_set():
            # 1. Get freshest live data from background fetcher
            with data_lock:
                raw_bet = dict(latest_raw)

            # 2. Check match still active
            event_bet = None
            for ev in raw_bet.get("events", []):
                if ev["eventId"] == event_id:
                    event_bet = ev
                    break
            if not event_bet or not event_bet.get("isActive", False):
                log.warning("Match %d (%s) gone/inactive – giving up", event_id, match_name)
                break

            # 3. Re‑evaluate lowest odds & pick (may have changed)
            result = lowest_odds_pick(raw_bet, event_bet)
            if not result:
                log.warning("No valid odds for %d – giving up", event_id)
                break
            new_pick, new_odds = result
            if new_pick != current_pick:
                log.info("Pick changed from %s to %s (%.2f) – adjusting", current_pick, new_pick, new_odds)
                current_pick = new_pick

            if new_odds >= 2.0:
                log.info("Lowest odds now %.2f (>= 2.0) – stopping attempts for %s", new_odds, match_name)
                break

            # 4. Build selection & payload with latest versions
            selection = build_selection(raw_bet, event_id, current_pick)
            if not selection:
                log.warning("Could not build selection for %d – retrying…", event_id)
                retries += 1
                continue

            payload = build_bet_payload(selection, WAGER_AMOUNT)

            # 5. Dry run mode
            if not IS_LIVE:
                log.info("❌ Dry run – bet NOT placed for %s (ONE_TIME=%s).", match_name, ONE_TIME)
                break

            # 6. Send bet
            with auth_lock:
                tok = auth_token
                bid = brand_id
            success, hidden_error, resp_data, err_text = post_bet(tok, bid, payload)

            if success:
                # Log detailed success info
                try:
                    first_resp = resp_data.get("betResponses", [{}])[0]
                    betslip = first_resp.get("betslipId")
                    booking = first_resp.get("bookingCode")
                    log.info("✅ Bet placed successfully! Betslip: %s, Booking: %s", betslip, booking)
                except Exception:
                    log.info("✅ Bet placed successfully! Response: %s", resp_data)
                if ONE_TIME:
                    log.info("ONE_TIME set – requesting bot shutdown")
                    shutdown_bot()
                break

            if hidden_error:
                retries += 1
                if retries <= MAX_RETRIES:
                    log.info("Hidden error (price/version change) – retry %d/%d instantly with new price",
                             retries, MAX_RETRIES)
                    # loop immediately, fresh data will be picked up
                    continue
                else:
                    log.error("Max retries (%d) reached for match %d – giving up", MAX_RETRIES, event_id)
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
        # Clean up: remove from in‑progress and add to placed (prevent re‑entry)
        with progress_lock:
            betting_in_progress.discard(event_id)
            placed_bets.add(event_id)

# ------------------------------------------------------------------------------
# Shutdown helper
# ------------------------------------------------------------------------------
def shutdown_bot() -> None:
    shutdown_event.set()

# ------------------------------------------------------------------------------
# Main loop – scans matches, launches bet threads with dual‑bet prevention
# ------------------------------------------------------------------------------
def main() -> None:
    global placed_bets, betting_in_progress
    log.info("Bot starting. IS_LIVE = %s, Wager = %d NGN, ONE_TIME = %s, Timer = %ds, Max retries = %d",
             IS_LIVE, WAGER_AMOUNT, ONE_TIME, TIMER_SECONDS, MAX_RETRIES)

    authenticate()
    fetcher_thread = threading.Thread(target=background_fetcher, daemon=True)
    fetcher_thread.start()

    eleven_min_start: Dict[int, float] = {}
    active_bet_threads: List[threading.Thread] = []

    while not shutdown_event.is_set():
        with data_lock:
            raw = dict(latest_raw)

        gt_events = [
            e for e in raw.get("events", [])
            if e.get("regionId") == "esoccer"
            and e.get("leagueId") == "gt-leagues"
            and e.get("isActive", False)
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

        # --- Timer & bet trigger (dual‑bet protected) ---
        for event in gt_events:
            eid = event["eventId"]

            # Atomically check if already handled or in progress
            with progress_lock:
                if eid in placed_bets or eid in betting_in_progress:
                    continue
                # Mark as in‑progress BEFORE releasing the lock
                betting_in_progress.add(eid)

            gs = event.get("gameStateTimeScore", {})
            elapsed_min = gs.get("time")
            if not isinstance(elapsed_min, (int, float)) or elapsed_min < 11:
                with progress_lock:
                    betting_in_progress.discard(eid)
                continue

            match_name = f"{event['homeTeam']} vs {event['awayTeam']}"

            if eid not in eleven_min_start:
                eleven_min_start[eid] = time.time()
                log.info("Match %d (%s) entered 11 min window – starting %ds timer",
                         eid, match_name, TIMER_SECONDS)
                with progress_lock:
                    betting_in_progress.discard(eid)
                continue

            elapsed_in_window = time.time() - eleven_min_start[eid]
            if elapsed_in_window < TIMER_SECONDS:
                log.info("Match %d (%s) – waiting in 11 min window (%.1f/%ds)",
                         eid, match_name, elapsed_in_window, TIMER_SECONDS)
                with progress_lock:
                    betting_in_progress.discard(eid)
                continue

            # Timer expired – keep in‑progress marker, launch thread
            log.info("Match %d (%s) – %ds timer expired, launching bet thread", eid, match_name, TIMER_SECONDS)
            del eleven_min_start[eid]

            result = lowest_odds_pick(raw, event)
            if not result:
                log.warning("No valid odds for match %d – skipping", eid)
                with progress_lock:
                    betting_in_progress.discard(eid)
                    placed_bets.add(eid)
                continue

            pick, odds = result
            if odds >= 2.0:
                log.info("Lowest odds (%.2f) for %s (%s) >= 2.0 – skipping", odds, match_name, pick)
                with progress_lock:
                    betting_in_progress.discard(eid)
                    placed_bets.add(eid)
                continue

            log.info("Bet candidate: %s – %s @ %.2f", match_name, pick, odds)

            t = threading.Thread(
                target=bet_match_worker,
                args=(eid, event, pick, match_name),
                daemon=True
            )
            t.start()
            active_bet_threads.append(t)

        # Clean up finished threads
        active_bet_threads = [t for t in active_bet_threads if t.is_alive()]

        # Clean up timers for vanished matches
        for eid in list(eleven_min_start.keys()):
            if eid not in current_ids:
                log.info("Match %d vanished before %ds elapsed – timer discarded", eid, TIMER_SECONDS)
                del eleven_min_start[eid]

        time.sleep(0.5)

    log.info("Bot shutdown requested. Waiting for active bet threads to finish...")
    for t in active_bet_threads:
        t.join(timeout=5)
    log.info("Bot stopped cleanly.")

# ------------------------------------------------------------------------------
# Command‑line overrides
# ------------------------------------------------------------------------------
def parse_overrides() -> None:
    global IS_LIVE, ONE_TIME, LOG_LEVEL
    parser = argparse.ArgumentParser(description="Betway GT League Bot (multi‑threaded with retries)")
    parser.add_argument("--live", action="store_true", default=None,
                        help="Enable live betting (otherwise dry run)")
    parser.add_argument("--one-time", action="store_true", default=None,
                        help="Exit after first *successful* bet")
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
