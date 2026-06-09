#!/usr/bin/env python3
# cython: language_level=3
"""
Betway GT League Auto‑Bot (Cython‑friendly)

Periodically checks for live eSoccer (GT League) matches.
When the current period clock reaches 12 minutes (720 sec), it identifies
the lowest odds outcome (home/draw/away) and automatically places a bet
using the exact same API calls from your code.

Set IS_LIVE to False to only log bets without actually placing them (dry‑run).
Set it to True to go live.

Cython‑friendly: type annotations added, compiles without errors.
If a bet placement returns a 401 (expired token), the bot automatically
re‑authenticates and retries the bet once.
"""

import os
import time
import json
import base64
import random
import string
import logging
from typing import Tuple, Dict, Optional, Any

import requests

# ------------------------------------------------------------------------------
# Configuration – change these variables directly
# ------------------------------------------------------------------------------
USERNAME: str = "08109995000"          # your Betway account username
PASSWORD: str = "password"             # your Betway account password
WAGER_AMOUNT: int = 100                # in NGN
IS_LIVE: bool = False                  # True: place real bets; False: log only

# ------------------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log: logging.Logger = logging.getLogger("betway_bot")

# ------------------------------------------------------------------------------
# API endpoints (exactly as in your code)
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
# Utility functions (mirroring your original code)
# ------------------------------------------------------------------------------
def generate_uuid() -> str:
    """Generate a unique ID similar to the JS _uuid() method."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8)) \
           + '-' + str(int(time.time() * 1000))

def decode_jwt(token: str) -> Dict[str, Any]:
    """Decode a JWT payload (same as _decodeJWT)."""
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

# ------------------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------------------
def authenticate() -> Tuple[str, str]:
    """Returns (access_token, brand_id)."""
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
    brand_id: str = claims.get(
        "http://schemas.ragingriver.io/ws/2021/05/identity/claims/brand",
        "f8a8d16a-d619-4b49-aa8c-f21211403c92",
    )
    log.info("Authenticated. Brand ID: %s", brand_id)
    return token, brand_id

# ------------------------------------------------------------------------------
# Fetch live data
# ------------------------------------------------------------------------------
def fetch_live_data(token: str) -> Dict[str, Any]:
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

# ------------------------------------------------------------------------------
# Build a selection (mirrors buildSelection)
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

# ------------------------------------------------------------------------------
# Build bet payload (mirrors _buildBetPayload)
# ------------------------------------------------------------------------------
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
# Place real bet
# ------------------------------------------------------------------------------
def place_bet(token: str, brand_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        **HEADERS,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Brand-Id": brand_id,
    }
    resp = requests.post(STRIKE_URL, headers=headers, data=json.dumps(payload), timeout=20)
    resp.raise_for_status()
    return resp.json()

# ------------------------------------------------------------------------------
# Determine lowest odds outcome for a match
# ------------------------------------------------------------------------------
def lowest_odds_pick(raw: Dict[str, Any], event: Dict[str, Any]) -> Optional[str]:
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
            if odd is None or odd <= 0:      # skip suspended / zero odds
                continue
            if outcome["name"] == "Draw":
                draw_odd = odd
            elif outcome["name"] == event["homeTeam"]:
                home_odd = odd
            elif outcome["name"] == event["awayTeam"]:
                away_odd = odd

    picks: list = [
        ("home", home_odd),
        ("draw", draw_odd),
        ("away", away_odd),
    ]
    valid: list = [(p, o) for p, o in picks if o is not None]
    if not valid:
        return None
    # Return the pick with the smallest decimal odds
    return min(valid, key=lambda x: x[1])[0]

# ------------------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------------------
def main() -> None:
    log.info("Bot starting. IS_LIVE = %s, Wager = %d NGN", IS_LIVE, WAGER_AMOUNT)
    token, brand_id = authenticate()
    placed_bets: set = set()

    while True:
        # Clear console for fresh logging (Cython‑safe)
        os.system('cls' if os.name == 'nt' else 'clear')

        try:
            raw = fetch_live_data(token)
        except Exception as e:
            log.error("Error fetching live data: %s", e)
            time.sleep(1)
            continue

        gt_events: list = [e for e in raw.get("events", [])
                     if e.get("regionId") == "esoccer" and e.get("leagueId") == "gt-leagues"]

        for event in gt_events:
            eid: int = event["eventId"]
            if eid in placed_bets:
                continue

            gs = event.get("gameStateTimeScore", {})
            elapsed_sec = gs.get("time")
            if not isinstance(elapsed_sec, int):
                continue
            if elapsed_sec < 720:        # 12 minutes
                continue

            match_name: str = f"{event['homeTeam']} vs {event['awayTeam']}"
            log.info("Match %d (%s) reached %d sec in current period", eid, match_name, elapsed_sec)

            pick: Optional[str] = lowest_odds_pick(raw, event)
            if not pick:
                log.warning("No valid odds found for match %d", eid)
                continue

            selection: Optional[Dict[str, Any]] = build_selection(raw, eid, pick)
            if not selection:
                log.warning("Could not build selection for match %d, pick=%s", eid, pick)
                continue

            payload: Dict[str, Any] = build_bet_payload(selection, WAGER_AMOUNT)
            log.info("Bet candidate: %s – %s @ %.2f (amount: %d)",
                     match_name, pick, selection["price"], WAGER_AMOUNT)

            if IS_LIVE:
                # Attempt to place the bet, re‑login on auth error
                try:
                    response = place_bet(token, brand_id, payload)
                    log.info("✅ Bet placed. Response: %s", response)
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 401:
                        log.warning("Got 401 – token expired. Re‑authenticating…")
                        try:
                            token, brand_id = authenticate()
                            # Retry once with new token
                            response = place_bet(token, brand_id, payload)
                            log.info("✅ Bet placed after re‑login. Response: %s", response)
                        except Exception as re_login_err:
                            log.error("Re‑login or retry failed: %s", re_login_err)
                    else:
                        log.error("Failed to place bet for event %d: %s", eid, e)
                except Exception as e:
                    log.error("Failed to place bet for event %d: %s", eid, e)
            else:
                log.info("❌ Dry run – bet NOT placed (IS_LIVE=False).")

            # Mark as processed so we don't bet again on this match
            placed_bets.add(eid)

        time.sleep(1)

if __name__ == "__main__":
    main()
