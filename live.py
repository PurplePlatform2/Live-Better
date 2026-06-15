#!/usr/bin/env python3
"""
GT League - Double Goal in 1 Minute Detector (NO BETTING)
Focus:
- Detect 2 goals within 60 seconds in same match
- Track odds reaction around event
- Print live signal alerts
"""

import time
import requests
import threading
from collections import defaultdict, deque

# ---------------- CONFIG ----------------
USERNAME = "08109995000"
PASSWORD = "password"

AUTH_URL = "https://www.betway.com.ng/appsynapse/auth/users/authenticate"
LIVE_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/LiveInPlay/"

POLL_INTERVAL = 1.0

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

REGION_ID = "esoccer"
LEAGUE_ID = "gt-leagues"

# ---------------- STATE ----------------
auth_token = ""
shutdown = threading.Event()

# event_id -> deque of goal timestamps
goal_times = defaultdict(lambda: deque(maxlen=10))

# event_id -> last odds snapshot
last_odds = {}

# ---------------- AUTH ----------------
def authenticate():
    global auth_token
    r = requests.post(
        AUTH_URL,
        json={
            "username": USERNAME,
            "password": PASSWORD,
            "countryCode": "NG",
            "sessionMetadata": {},
        },
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    auth_token = r.json()["access_token"]
    print("✅ Authenticated")

# ---------------- FETCH ----------------
def fetch_live():
    headers = dict(HEADERS)
    headers["Authorization"] = f"Bearer {auth_token}"

    params = {
        "countryCode": "NG",
        "sportId": "soccer",
        "Skip": 0,
        "Take": 200,
        "cultureCode": "en-US",
        "isEsport": True,
    }

    r = requests.get(LIVE_URL, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

# ---------------- UTIL ----------------
def get_score(gs):
    s = gs.get("score")
    if isinstance(s, list) and len(s) >= 2:
        return int(s[0]), int(s[1])
    return None, None

def extract_odds(raw, event_id):
    odds = {"home": None, "draw": None, "away": None}

    prices = {p["outcomeId"]: p["priceDecimal"] for p in raw.get("prices", [])}
    events = {e["eventId"]: e for e in raw.get("events", [])}
    event = events.get(event_id)

    if not event:
        return odds

    for market in raw.get("markets", []):
        if market["eventId"] != event_id:
            continue

        if market.get("marketTypeCName") not in ("win-draw-win", "1X2"):
            continue

        for o in raw.get("outcomes", []):
            if o["marketId"] != market["marketId"]:
                continue

            price = prices.get(o["outcomeId"])
            if not price:
                continue

            if o["name"] == "Draw":
                odds["draw"] = price
            elif o["name"] == event["homeTeam"]:
                odds["home"] = price
            elif o["name"] == event["awayTeam"]:
                odds["away"] = price

    return odds

# ---------------- CORE LOGIC ----------------
def process():
    global last_odds

    while not shutdown.is_set():
        try:
            raw = fetch_live()

            events = [
                e for e in raw.get("events", [])
                if e.get("regionId") == REGION_ID
                and e.get("leagueId") == LEAGUE_ID
                and e.get("isActive")
            ]

            for e in events:
                eid = e["eventId"]
                gs = e.get("gameStateTimeScore", {})
                home, away = get_score(gs)

                if home is None:
                    continue

                current_score = home + away

                # init odds snapshot
                if eid not in last_odds:
                    last_odds[eid] = extract_odds(raw, eid)
                    continue

                prev_score = last_odds[eid].get("score", 0)
                odds_now = extract_odds(raw, eid)

                # detect goal
                if current_score > prev_score:
                    now = time.time()

                    dq = goal_times[eid]
                    dq.append(now)

                    # keep only last 60 seconds
                    while dq and now - dq[0] > 60:
                        dq.popleft()

                    # 🎯 DOUBLE GOAL DETECTION
                    if len(dq) >= 2:
                        print("\n🔥🔥 DOUBLE GOAL DETECTED (UNDER 60s)")
                        print(f"Match: {e['homeTeam']} vs {e['awayTeam']}")
                        print(f"Time window: {now - dq[0]:.1f}s")
                        print(f"Score now: {home}-{away}")

                        # odds movement (simple snapshot)
                        print("📊 Odds snapshot:")
                        print(odds_now)

                # update snapshot score
                last_odds[eid] = odds_now
                last_odds[eid]["score"] = current_score

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            print("Error:", e)
            time.sleep(2)

# ---------------- RUN ----------------
if __name__ == "__main__":
    authenticate()
    process()
