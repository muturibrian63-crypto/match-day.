#!/usr/bin/env python3
"""Build today's Match Day page from API-Football.

Setup (one time):
  1. Create a free account at https://www.api-football.com and copy your API key.
  2. Put the key in an environment variable, so it never sits in a file:
       macOS/Linux:  export API_FOOTBALL_KEY="your-key"
       Windows:      setx API_FOOTBALL_KEY "your-key"   (then reopen the terminal)
  3. Keep this script and match-day-template.html in the same folder.

Usage:
  python3 api_football_daily.py                  # every game today; predictions for as many as the request budget allows
  python3 api_football_daily.py --leagues 39,140  # only these leagues
  python3 api_football_daily.py --corners 5      # also estimate corners from last 5 games
  python3 api_football_daily.py --date 2026-10-24
  python3 api_football_daily.py --demo           # no key needed, sample data

Then open match-day.html in your browser.

Request cost (matters on the free plan):
  1 request for the day's fixtures + 2 per match for team stats.
  --corners N adds about N+1 requests per team, so use it only for a few leagues.
  Results are cached for the day in .api_football_cache.json, so re-running is cheap.

Check your plan: the free plan may limit which seasons you can query.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

BASE = "https://v3.football.api-sports.io"
HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / ".api_football_cache.json"
# 39 Premier League, 140 La Liga, 135 Serie A, 78 Bundesliga, 61 Ligue 1, 2 Champions League
DEFAULT_LEAGUES = "39,140,135,78,61,2"
NOT_STARTED = {"NS", "TBD"}
FINISHED = {"FT", "AET", "PEN"}


class ApiError(Exception):
    """An error reported by API-Football itself (plan limits and so on)."""


class Api:
    def __init__(self, key, delay):
        self.key = key
        self.delay = delay
        self.today = date.today().isoformat()
        self.cache = {}
        self.calls = 0
        if CACHE_FILE.exists():
            try:
                self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except ValueError:
                self.cache = {}

    def save(self):
        CACHE_FILE.write_text(json.dumps(self.cache), encoding="utf-8")

    def get(self, path, params, soft=False):
        """soft=True raises ApiError for API-reported errors so the caller can fall back."""
        stamp = "" if path == "/fixtures/statistics" else self.today
        ck = "%s|%s|%s" % (stamp, path, json.dumps(params, sort_keys=True))
        if ck in self.cache:
            return self.cache[ck]
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"x-apisports-key": self.key})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as err:
                sys.exit("API-Football returned HTTP %s. Check your key and plan." % err.code)
            except urllib.error.URLError as err:
                sys.exit("Could not reach API-Football: %s" % err.reason)
            self.calls += 1
            errors = body.get("errors")
            if errors:
                text = json.dumps(errors)
                if "rate" in text.lower() and attempt < 3:
                    self.calls -= 1  # a refused request does not count against the daily budget
                    print("Rate limit reached, waiting 61 seconds...")
                    time.sleep(61)
                    continue
                if soft:
                    raise ApiError(text)
                sys.exit("API-Football error: " + text)
            self.cache[ck] = body["response"]
            time.sleep(self.delay)
            return body["response"]


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def goal_avg(stats, kind, where):
    """Average goals scored/conceded ('for'/'against') at home/away, falling back to the total."""
    stats = stats or {}
    played = ((stats.get("fixtures") or {}).get("played") or {}).get("total")
    if played == 0:
        return None  # the team has no games in this competition and season
    goals = stats.get("goals") or {}
    avg = (goals.get(kind) or {}).get("average") or {}
    return to_float(avg.get(where)) or to_float(avg.get("total"))


def corner_avgs(api, team_id, n):
    """Average corners won and conceded over the team's last n finished games."""
    won, conceded = [], []
    for f in api.get("/fixtures", {"team": team_id, "last": n}):
        if f["fixture"]["status"]["short"] not in FINISHED:
            continue
        mine = opp = None
        for block in api.get("/fixtures/statistics", {"fixture": f["fixture"]["id"]}):
            value = next((s["value"] for s in block["statistics"] if s["type"] == "Corner Kicks"), None)
            if block["team"]["id"] == team_id:
                mine = value
            else:
                opp = value
        if mine is not None and opp is not None:
            won.append(mine)
            conceded.append(opp)
    if not won:
        return None
    return sum(won) / len(won), sum(conceded) / len(conceded)


def clamp(x):
    return max(0.2, min(4.5, x))


def get_team_stats(api, f, state):
    """Return (home_stats, away_stats, source) for a fixture.

    Order of attempts:
      1. /teams/statistics for the fixture's season (needs a plan that includes that season).
      2. /predictions for the fixture, which includes each team's season averages and is
         not tied to a season.
      3. /teams/statistics for the newest season your plan allows (older, less accurate).
    """
    lg, season = f["league"], f["league"]["season"]
    home, away = f["teams"]["home"], f["teams"]["away"]

    def season_stats(s):
        return (api.get("/teams/statistics", {"league": lg["id"], "season": s, "team": home["id"]}, soft=True),
                api.get("/teams/statistics", {"league": lg["id"], "season": s, "team": away["id"]}, soft=True))

    if not state["limited"]:
        try:
            hs, as_ = season_stats(season)
            return hs, as_, "season"
        except ApiError as err:
            text = str(err).lower()
            if "season" not in text or "plan" not in text:
                raise
            state["limited"] = True
            years = re.findall(r"20\d\d", str(err))
            state["old_season"] = int(years[-1]) if years else season - 2
            print("Your plan does not include the %s season. Using other sources." % season)

    try:
        pred = api.get("/predictions", {"fixture": f["fixture"]["id"]}, soft=True)
        teams = pred[0]["teams"]
        hs, as_ = teams["home"]["league"], teams["away"]["league"]
        if goal_avg(hs, "for", "home") is not None and goal_avg(as_, "for", "away") is not None:
            return hs, as_, "predictions"
    except (ApiError, KeyError, IndexError, TypeError):
        pass

    old = state["old_season"] or season - 2
    hs, as_ = season_stats(old)
    return hs, as_, "old season %s" % old


def build_matches(args):
    api = Api(os.environ.get("API_FOOTBALL_KEY", ""), args.delay)
    if not api.key:
        sys.exit("Set the API_FOOTBALL_KEY environment variable first (see the top of this file).")
    everything = args.leagues.strip().lower() == "all"
    wanted = set() if everything else {int(x) for x in args.leagues.split(",") if x.strip()}
    priority = [int(x) for x in DEFAULT_LEAGUES.split(",")]
    fixtures = api.get("/fixtures", {"date": args.date, "timezone": args.timezone})
    fixtures = [f for f in fixtures
                if (everything or f["league"]["id"] in wanted) and f["fixture"]["status"]["short"] in NOT_STARTED]
    print("%d matches to show." % len(fixtures))

    # Top leagues first, so they get predictions before the request budget runs out.
    def rank(f):
        lid = f["league"]["id"]
        return (priority.index(lid) if lid in priority else len(priority), f["fixture"]["date"])
    fixtures.sort(key=rank)

    matches, sources = [], set()
    state = {"limited": False, "old_season": None, "stop": False}
    without = 0
    for f in fixtures:
        lg = f["league"]
        home, away = f["teams"]["home"], f["teams"]["away"]
        label = lg["name"] + (" · " + lg["country"] if lg.get("country") else "")
        item = {
            "lg": label,
            "ko": datetime.fromisoformat(f["fixture"]["date"]).strftime("%H:%M"),
            "h": home["name"],
            "a": away["name"],
        }
        matches.append(item)
        if state["stop"] or api.calls >= args.max_requests:
            without += 1
            continue
        try:
            hs, as_, source = get_team_stats(api, f, state)
        except ApiError as err:
            print("No stats for %s v %s: %s" % (home["name"], away["name"], err))
            if "request" in str(err).lower() and "limit" in str(err).lower():
                state["stop"] = True  # daily limit reached: list the rest without predictions
            without += 1
            continue
        h_for, h_against = goal_avg(hs, "for", "home"), goal_avg(hs, "against", "home")
        a_for, a_against = goal_avg(as_, "for", "away"), goal_avg(as_, "against", "away")
        if None in (h_for, h_against, a_for, a_against):
            print("No usable stats for %s v %s (%s)." % (home["name"], away["name"], source))
            without += 1
            continue
        sources.add(source)
        item["xg"] = [round(clamp(h_for * a_against / args.league_home), 2),
                      round(clamp(a_for * h_against / args.league_away), 2)]
        if args.corners:
            hc = corner_avgs(api, home["id"], args.corners)
            ac = corner_avgs(api, away["id"], args.corners)
            if hc and ac:
                item["c"] = round((hc[0] + ac[1]) / 2 + (ac[0] + hc[1]) / 2, 1)
                item["cs"] = 1
    api.save()
    print("Used %d new API requests (cached ones are free)." % api.calls)
    notes = []
    if without:
        notes.append("%d of %d matches are listed without predictions, because the daily request limit was "
                     "reached or no stats were available." % (without, len(matches)))
    if any(x.startswith("old season") for x in sources):
        notes.append("Your plan blocks the current season, so some goal averages come from an earlier season "
                     "and may be out of date.")
    if "predictions" in sources:
        notes.append("Some team averages come from API-Football's prediction data.")
    return sorted(matches, key=lambda m: m["ko"]), notes


DEMO = [
    {"lg": "Premier League · England", "ko": "17:00", "h": "Home FC", "a": "Away FC", "xg": [1.9, 1.1], "c": 10.6, "cs": 1},
    {"lg": "La Liga · Spain", "ko": "19:30", "h": "Example United", "a": "Sample City", "xg": [1.2, 1.3]},
    {"lg": "Serie A · Italy", "ko": "21:45", "h": "Demo Calcio", "a": "Test Athletic", "xg": [0.9, 0.8]},
    {"lg": "Sample League · Kenya", "ko": "15:00", "h": "Sample FC", "a": "Test United"},
]


def main():
    ap = argparse.ArgumentParser(description="Build the Match Day page from API-Football.")
    ap.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD (default: today)")
    ap.add_argument("--timezone", default="Africa/Nairobi", help="Time zone for kickoff times")
    ap.add_argument("--leagues", default="all", help='"all" (default) or comma-separated API-Football league IDs')
    ap.add_argument("--max-requests", type=int, default=85, help="Stop making predictions after this many API requests (free plan allows about 100 a day)")
    ap.add_argument("--corners", type=int, default=0, help="Estimate corners from each team's last N games (costs requests)")
    ap.add_argument("--league-home", type=float, default=1.5, help="League average home goals per game")
    ap.add_argument("--league-away", type=float, default=1.2, help="League average away goals per game")
    ap.add_argument("--delay", type=float, default=0.0, help="Seconds to pause between requests")
    ap.add_argument("--template", default=str(HERE / "match-day-template.html"))
    ap.add_argument("--out", default="match-day.html")
    ap.add_argument("--demo", action="store_true", help="Use sample data, no API key needed")
    args = ap.parse_args()

    matches, notes = (DEMO, []) if args.demo else build_matches(args)
    pretty = datetime.strptime(args.date, "%Y-%m-%d").strftime("%A %d %B %Y")
    corner_note = ("Corners are estimated from each team's recent games where marked, otherwise assumed."
                   if args.corners else "Corners are an adjustable assumption.")
    note = ("<b>Sample data.</b> Not real fixtures." if args.demo else
            "<b>Source: API-Football.</b> Goal estimates combine each team's season home or away scoring and conceding "
            "averages with league averages. " + " ".join(notes) + " " + corner_note)
    meta = {"date": pretty + " · Times in " + args.timezone, "banner": note}

    html = Path(args.template).read_text(encoding="utf-8")

    def inject(text, name, payload):
        pattern = re.compile(r"/\*%s_START\*/.*?/\*%s_END\*/" % (name, name), re.S)
        data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        text, n = pattern.subn(lambda _m: data, text, count=1)
        if n != 1:
            sys.exit("Template is missing the %s markers." % name)
        return text

    html = inject(html, "META", meta)
    html = inject(html, "MATCHES", matches)
    Path(args.out).write_text(html, encoding="utf-8")
    print("Wrote %s with %d matches." % (args.out, len(matches)))


if __name__ == "__main__":
    main()
