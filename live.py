#!/usr/bin/env python3

import os
import time
import json
import base64
import uuid
import logging
import threading
import sys
import argparse
import traceback
from typing import Dict, Optional, Any, Tuple, List

import requests

USERNAME: str = "08035796220"
PASSWORD: str = "password"
WAGER_AMOUNT: int = 102
WINDOW_SECONDS: int = 51
MAX_RETRIES: int = 3

AUTH_URL: str = "https://www.betway.com.ng/appsynapse/auth/users/authenticate"
LIVE_URL: str = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/"
STRIKE_URL: str = "https://www.betway.com.ng/appsynapse/bet-api-sr02/v2/Betting/Strike"

HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.betway.com.ng",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("betway_bot")

latest_raw: Dict[str, Any] = {}
data_lock = threading.Lock()

auth_token: str = ""
brand_id: str = ""
auth_lock = threading.Lock()
token_updated = threading.Event()

placed_bets: set[int] = set()
betting_in_progress: set[int] = set()
progress_lock = threading.Lock()

shutdown_event = threading.Event()

window_state: Dict[int, Dict[str, Any]] = {}
window_lock = threading.Lock()

AUTH_FILE = "auth.txt"

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
        claims = decode_jwt(token) if token else {}
        exp = claims.get("exp")
        now = int(time.time())
        if token and brand and claims and (exp is None or int(exp) > now):
            return token, brand
    except Exception:
        pass
    return None

def authenticate(force_login: bool = False) -> Tuple[str, str]:
    global auth_token, brand_id
    if not force_login:
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
                    authenticate(force_login=True)
                except Exception as auth_err:
                    log.error("Fetcher re‑auth failed: %s", auth_err)
                    time.sleep(5)
            else:
                log.error("Fetcher HTTP error: %s", e)
                time.sleep(1)
        except Exception as e:
            log.error("Fetcher error: %s", e)
            time.sleep(1)

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

def bet_worker(event_id: int, pick: str, match_name: str, wager_amount: int) -> None:
    retries = 0
    log.info("Thread for match %d (%s) – betting on %s with stake %d NGN",
             event_id, match_name, pick, wager_amount)

    try:
        while retries <= MAX_RETRIES and not shutdown_event.is_set():
            with data_lock:
                raw_bet = dict(latest_raw)

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

            payload = build_bet_payload(selection, wager_amount)

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
                    log.info("One-time mode – stopping after this bet.")
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
                    authenticate(force_login=True)
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

def main() -> None:
    log.info("Bot starting. Wager = %d NGN, Window = %d seconds. One-time = %s",
             WAGER_AMOUNT, WINDOW_SECONDS, ONE_TIME)

    authenticate(force_login=False)
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
            and e.get("isActive", False)
        ]

        current_ids = {ev["eventId"] for ev in gt_events}

        with window_lock:
            for eid in list(window_state.keys()):
                if eid not in current_ids:
                    del window_state[eid]

        for event in gt_events:
            eid = event["eventId"]

            with progress_lock:
                if eid in placed_bets or eid in betting_in_progress:
                    continue

            gs = event.get("gameStateTimeScore", {})
            elapsed_min = gs.get("time")
            if not isinstance(elapsed_min, (int, float)):
                continue

            home_score, away_score = get_score(gs)
            if home_score is None or away_score is None:
                continue

            match_name = f"{event['homeTeam']} vs {event['awayTeam']}"

            if elapsed_min >= 11:
                with window_lock:
                    state = window_state.get(eid)

                    if state is None:
                        window_state[eid] = {
                            "start_time": time.time(),
                            "goal_seen": False,
                            "baseline_home": home_score,
                            "baseline_away": away_score,
                        }
                        log.info("⏱️  Started 51‑sec window for %s at game time %.2f (score %d-%d)",
                                 match_name, elapsed_min, home_score, away_score)
                        continue

                    elapsed_real = time.time() - state["start_time"]

                    if not state["goal_seen"]:
                        if home_score != state["baseline_home"] or away_score != state["baseline_away"]:
                            state["goal_seen"] = True
                            log.info("⚽ Goal detected in window! %s new score %d-%d",
                                     match_name, home_score, away_score)

                    if elapsed_real < WINDOW_SECONDS:
                        continue

                    if state["goal_seen"]:
                        if home_score > away_score:
                            pick = "home"
                        elif away_score > home_score:
                            pick = "away"
                        else:
                            pick = "draw"

                        del window_state[eid]

                        with progress_lock:
                            if eid in placed_bets or eid in betting_in_progress:
                                continue
                            betting_in_progress.add(eid)

                        t = threading.Thread(target=bet_worker,
                                             args=(eid, pick, match_name, WAGER_AMOUNT),
                                             daemon=True)
                        t.start()
                        active_bet_threads.append(t)
                        log.info("🚀 Bet dispatched for %s (pick=%s) after window expired",
                                 match_name, pick)
                    else:
                        del window_state[eid]
                        log.info("❌ Window expired for %s with no goal – no bet placed.", match_name)
                        with progress_lock:
                            placed_bets.add(eid)

        active_bet_threads = [t for t in active_bet_threads if t.is_alive()]
        time.sleep(0.5)

    log.info("Bot shutdown requested. Waiting for active bet threads...")
    for t in active_bet_threads:
        t.join(timeout=5)
    log.info("Bot stopped cleanly.")

ONE_TIME: bool = False

def parse_overrides() -> None:
    global WAGER_AMOUNT, USERNAME, PASSWORD, ONE_TIME
    parser = argparse.ArgumentParser(
        description="Betway GT League Goal‑In‑Window Bot"
    )
    parser.add_argument("--wager", type=int, default=None,
                        help="Stake amount in NGN")
    parser.add_argument("--username", type=str, default=None,
                        help="Betway username")
    parser.add_argument("--password", type=str, default=None,
                        help="Betway password")
    parser.add_argument("--one-time", action="store_true", default=False,
                        help="Stop after the first successful bet")
    args, _ = parser.parse_known_args()

    if args.wager is not None:
        WAGER_AMOUNT = args.wager
    if args.username is not None:
        USERNAME = args.username
    if args.password is not None:
        PASSWORD = args.password
    global ONE_TIME
    if args.one_time:
        ONE_TIME = True

if __name__ == "__main__":
    parse_overrides()
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception:
        log.critical("Fatal error:\n%s", traceback.format_exc())
