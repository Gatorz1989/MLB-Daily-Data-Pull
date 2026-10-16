#!/usr/bin/env python3
"""
mlb_edge_data_pull.py  —  MLB Edge Report Data Companion v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pulls live game lines, player props, and season stats then writes
mlb_edge_data.json ready to drag-and-drop (or GitHub-load) into
MLB_Edge_Model_v1.2.html.

Data sources
  ● Odds API v4  — game lines (ML, total, run line) + player props
  ● BigDataBall  — season batter / pitcher stats (for edge calc)

Output  →  mlb_edge_data.json  (MODEL_DATA-compatible)

Usage
  python mlb_edge_data_pull.py                     # today, default output
  python mlb_edge_data_pull.py --date 2026-10-15   # specific date
  python mlb_edge_data_pull.py --out /path/data.json
  python mlb_edge_data_pull.py --dry-run            # no file write
  python mlb_edge_data_pull.py --verbose            # debug logging
  python mlb_edge_data_pull.py --no-props           # skip prop pull
  python mlb_edge_data_pull.py --no-stats           # skip BigDataBall

Config  (in priority order)
  1. .env file in same directory as this script
  2. Environment variables

Required env vars
  ODDS_API_KEY      Your Odds API key (https://the-odds-api.com)

Optional env vars
  BIGBALL_API_KEY   BigDataBall API key (stats/projections)
  ODDS_REGION       us | uk | au | eu  (default: us)
  ODDS_BOOKS        Comma-separated bookmakers  (default: draftkings,fanduel,betmgm,bet365_us)
  EDGE_MIN_PCT      Minimum edge % to include in output  (default: 0)
  OUTPUT_PATH       Output file path  (default: ./mlb_edge_data.json)
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── optional .env loader ────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed — use real env vars

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests not installed. Run:  pip install requests")

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

ODDS_API_BASE  = "https://api.the-odds-api.com/v4"
BIGBALL_BASE   = "https://api.bigdataball.com/v1"   # adjust if endpoint differs

SPORT          = "baseball_mlb"
DEFAULT_REGION = "us"
DEFAULT_BOOKS  = "draftkings,fanduel,betmgm,bet365_us"

# Prop market keys (Odds API) → display labels used in the model
PROP_MARKETS = {
    "pitcher_strikeouts":         "K",
    "batter_home_runs":           "HR",
    "batter_hits":                "H",
    "batter_runs_scored":         "R",
    "batter_rbis":                "RBI",
    "batter_total_bases":         "TB",
    "batter_walks":               "BB",
    "batter_strikeouts":          "K",
    "pitcher_hits_allowed":       "H-Allowed",
    "pitcher_walks":              "BB-Allowed",
    "pitcher_earned_runs":        "ER",
    "pitcher_outs":               "OUTS",
}

# Bookmaker priority order (mirrors BK_ORDER in the model)
BK_ORDER = {
    "draftkings": 0, "fanduel": 1, "betmgm": 2,
    "bet365_us": 3,  "betrivers": 3, "bet365": 3,
}

# MLB team name → slug mapping (for park factor lookups in model)
TEAM_SLUG = {
    "Arizona Diamondbacks": "arizona_diamondbacks",
    "Atlanta Braves": "atlanta_braves",
    "Baltimore Orioles": "baltimore_orioles",
    "Boston Red Sox": "boston_red_sox",
    "Chicago Cubs": "chicago_cubs",
    "Chicago White Sox": "chicago_white_sox",
    "Cincinnati Reds": "cincinnati_reds",
    "Cleveland Guardians": "cleveland_guardians",
    "Colorado Rockies": "colorado_rockies",
    "Detroit Tigers": "detroit_tigers",
    "Houston Astros": "houston_astros",
    "Kansas City Royals": "kansas_city_royals",
    "Los Angeles Angels": "los_angeles_angels",
    "Los Angeles Dodgers": "los_angeles_dodgers",
    "Miami Marlins": "miami_marlins",
    "Milwaukee Brewers": "milwaukee_brewers",
    "Minnesota Twins": "minnesota_twins",
    "New York Mets": "new_york_mets",
    "New York Yankees": "new_york_yankees",
    "Oakland Athletics": "oakland_athletics",
    "Philadelphia Phillies": "philadelphia_phillies",
    "Pittsburgh Pirates": "pittsburgh_pirates",
    "San Diego Padres": "san_diego_padres",
    "San Francisco Giants": "san_francisco_giants",
    "Seattle Mariners": "seattle_mariners",
    "St. Louis Cardinals": "st_louis_cardinals",
    "Tampa Bay Rays": "tampa_bay_rays",
    "Texas Rangers": "texas_rangers",
    "Toronto Blue Jays": "toronto_blue_jays",
    "Washington Nationals": "washington_nationals",
}

# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

log = logging.getLogger("mlb_pull")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def calc_edge(projection: float, line: float) -> float:
    """Edge % = (projection/line - 1) * 100, rounded to 1 decimal."""
    if line <= 0 or projection <= 0:
        return 0.0
    return round(((projection / line) - 1) * 100, 1)


def assign_tier(edge_pct: float) -> str:
    if edge_pct >= 8:   return "ELITE"
    if edge_pct >= 4:   return "STRONG"
    if edge_pct >= 1:   return "LEAN"
    return "SKIP"


def best_book_odds(outcomes: list, side: str) -> tuple[str, int]:
    """
    From a list of Odds API outcome dicts, pick the best (highest) odds
    for `side` ('Over' or 'Under' or team name) across bookmakers.
    Returns (book_name, american_odds).
    """
    best_price = -99999
    best_book  = "draftkings"
    for o in outcomes:
        if o.get("name", "").lower() == side.lower():
            price = o.get("price", -110)
            if price > best_price:
                best_price = price
                best_book  = o.get("source_book", "draftkings")
    return best_book, best_price if best_price != -99999 else -110


def decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1) * 100))
    elif decimal > 1.0:
        return int(round(-100 / (decimal - 1)))
    return -110


def fmt_time(iso_str: str) -> str:
    """Convert ISO timestamp to 'HH:MM ET' display string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # Convert UTC → ET (UTC-4 during EDT)
        from datetime import timedelta
        et = dt - timedelta(hours=4)
        return et.strftime("%-I:%M %p ET")
    except Exception:
        return iso_str


# ═══════════════════════════════════════════════════════════════════════════
# ODDS API  — GAMES (ML + TOTAL + RUN LINE)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_game_lines(api_key: str, date_str: str, region: str, books: str) -> list[dict]:
    """
    Fetch moneylines, totals, and run line odds for all MLB games
    on `date_str` (YYYY-MM-DD). Returns list of game dicts.
    """
    url = f"{ODDS_API_BASE}/sports/{SPORT}/odds/"
    params = {
        "apiKey":    api_key,
        "regions":   region,
        "markets":   "h2h,totals,spreads",     # h2h = ML, spreads = run line
        "oddsFormat": "american",
        "dateFormat": "iso",
        "commenceTimeFrom": f"{date_str}T00:00:00Z",
        "commenceTimeTo":   f"{date_str}T23:59:59Z",
        "bookmakers": books,
    }
    log.info("Fetching game lines from Odds API …")
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    log.info(f"  {len(raw)} games found  |  API calls remaining: {resp.headers.get('x-requests-remaining','?')}")
    return _parse_game_lines(raw)


def _parse_game_lines(raw: list) -> list[dict]:
    games = []
    for event in raw:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        commence = event.get("commence_time", "")

        game = {
            "eventId":     event.get("id", ""),
            "homeTeam":    home,
            "awayTeam":    away,
            "time":        fmt_time(commence),
            "park":        f"{home} Park",
            "homeTeamSlug": TEAM_SLUG.get(home, ""),
            # SP placeholders — filled if BigDataBall provides starters
            "homePitcher": "TBD", "awayPitcher": "TBD",
            "homeERA": 0, "awayERA": 0,
            "homeK9":  0, "awayK9":  0,
            "homeWHIP": 0, "awayWHIP": 0,
            # Lines
            "homeML": 0, "awayML": 0,
            "total":  0,
            "homeRL": 0, "awayRL": 0,
            "homeRLEdge": 0, "awayRLEdge": 0,
        }

        for bk in (event.get("bookmakers") or []):
            bk_name = bk.get("key", "unknown")
            bk_rank = BK_ORDER.get(bk_name, 99)

            for market in (bk.get("markets") or []):
                mkey = market.get("key", "")
                outcomes = market.get("outcomes", [])

                if mkey == "h2h":
                    for o in outcomes:
                        team = o.get("name", "")
                        price = o.get("price", 0)
                        if team == home:
                            if bk_rank < BK_ORDER.get(_source_of(game, "homeML"), 99) or game["homeML"] == 0:
                                game["homeML"] = price
                                game["_homeML_book"] = bk_name
                        elif team == away:
                            if bk_rank < BK_ORDER.get(_source_of(game, "awayML"), 99) or game["awayML"] == 0:
                                game["awayML"] = price
                                game["_awayML_book"] = bk_name

                elif mkey == "totals":
                    for o in outcomes:
                        if o.get("name") == "Over":
                            point = o.get("point", 0)
                            if point and (game["total"] == 0 or bk_rank == 0):
                                game["total"] = point

                elif mkey == "spreads":
                    for o in outcomes:
                        team  = o.get("name", "")
                        point = o.get("point", 0)
                        price = o.get("price", 0)
                        if team == away and abs(point - 1.5) < 0.1:
                            if game["awayRL"] == 0 or bk_rank == 0:
                                game["awayRL"] = price
                        elif team == home and abs(point - 1.5) < 0.1:
                            if game["homeRL"] == 0 or bk_rank == 0:
                                game["homeRL"] = price

        # Clean up internal book-source keys
        for k in list(game.keys()):
            if k.startswith("_"):
                del game[k]

        games.append(game)
        log.debug(f"  Game parsed: {away} @ {home}  ML:{game['awayML']}/{game['homeML']}  O/U:{game['total']}")

    return games


def _source_of(game: dict, field: str) -> str:
    return game.get(f"_{field}_book", "unknown")


# ═══════════════════════════════════════════════════════════════════════════
# ODDS API  — PLAYER PROPS
# ═══════════════════════════════════════════════════════════════════════════

def fetch_props(api_key: str, games: list[dict], region: str, books: str) -> list[dict]:
    """
    For each game, fetch all supported prop markets and return flat list
    of prop dicts compatible with MODEL_DATA.props.
    """
    all_props = []
    market_str = ",".join(PROP_MARKETS.keys())

    for game in games:
        event_id = game.get("eventId", "")
        if not event_id:
            continue

        url = f"{ODDS_API_BASE}/sports/{SPORT}/events/{event_id}/odds"
        params = {
            "apiKey":    api_key,
            "regions":   region,
            "markets":   market_str,
            "oddsFormat": "american",
            "bookmakers": books,
        }
        log.info(f"  Props: {game['awayTeam']} @ {game['homeTeam']} …")
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                log.debug("    No props available for this game yet.")
                continue
            resp.raise_for_status()
            raw = resp.json()
            props = _parse_props(raw, game)
            all_props.extend(props)
            log.debug(f"    {len(props)} prop lines parsed")
        except requests.HTTPError as e:
            log.warning(f"    Props fetch failed: {e}")

    log.info(f"Total prop lines: {len(all_props)}")
    return all_props


def _parse_props(event_data: dict, game: dict) -> list[dict]:
    props = []
    home = game.get("homeTeam", "")
    away = game.get("awayTeam", "")

    # Collect all outcomes across bookmakers, keyed by (player, market, direction)
    # We want Over outcomes only (the model tracks Over plays)
    collector: dict[tuple, dict] = {}

    for bk in (event_data.get("bookmakers") or []):
        bk_name = bk.get("key", "unknown")
        bk_rank = BK_ORDER.get(bk_name, 99)

        for market in (bk.get("markets") or []):
            mkey = market.get("key", "")
            prop_type = PROP_MARKETS.get(mkey)
            if not prop_type:
                continue

            for o in (market.get("outcomes") or []):
                direction = o.get("name", "")
                if direction != "Over":
                    continue

                player = o.get("description", "Unknown")
                line   = float(o.get("point", 0))
                price  = int(o.get("price", -110))
                key    = (player, mkey)

                existing = collector.get(key)
                if not existing or bk_rank < BK_ORDER.get(existing.get("book", ""), 99):
                    # Determine team from pitcher vs batter market
                    team = _guess_team(player, mkey, home, away)
                    collector[key] = {
                        "player":    player,
                        "team":      team,
                        "propType":  prop_type,
                        "line":      line,
                        "book":      bk_name,
                        "odds":      price,
                        "isManual":  False,
                    }

    for (player, mkey), prop in collector.items():
        # Projection and edge filled in by stats layer; placeholder here
        prop["projection"] = 0.0
        prop["edge"]       = 0.0
        prop["tier"]       = "SKIP"
        props.append(prop)

    return props


def _guess_team(player: str, market_key: str, home: str, away: str) -> str:
    """
    We don't know team from Odds API prop response — return empty string;
    the stats layer fills it in when matching player names.
    """
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# BIGDATABALL  — PLAYER STATS
# ═══════════════════════════════════════════════════════════════════════════

def fetch_bigball_stats(api_key: str, season: str = "2026") -> tuple[list[dict], list[dict]]:
    """
    Pull season batter and pitcher stats from BigDataBall.
    Returns (batters, pitchers).
    Falls back to empty lists if key not set or API unavailable.
    """
    if not api_key:
        log.info("BigDataBall key not set — skipping stats pull.")
        return [], []

    batters  = _fetch_bigball_batters(api_key, season)
    pitchers = _fetch_bigball_pitchers(api_key, season)
    return batters, pitchers


def _fetch_bigball_batters(api_key: str, season: str) -> list[dict]:
    url = f"{BIGBALL_BASE}/mlb/stats/batting"
    params = {"season": season, "api_key": api_key}
    try:
        log.info("Fetching batter stats from BigDataBall …")
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        players = raw if isinstance(raw, list) else raw.get("data", raw.get("players", []))
        result = []
        for p in players:
            result.append({
                "name": p.get("name") or p.get("player_name", ""),
                "team": p.get("team", ""),
                "avg":  float(p.get("avg") or p.get("batting_avg") or 0),
                "obp":  float(p.get("obp") or p.get("on_base_pct") or 0),
                "slg":  float(p.get("slg") or p.get("slugging_pct") or 0),
                "ops":  float(p.get("ops") or 0),
                "hr":   int(p.get("hr") or p.get("home_runs") or 0),
                "rbi":  int(p.get("rbi") or 0),
                # Per-game averages for edge calculation
                "_h_pg":  float(p.get("h_per_game") or p.get("hits_per_game") or 0),
                "_hr_pg": float(p.get("hr_per_game") or p.get("home_runs_per_game") or 0),
                "_rbi_pg":float(p.get("rbi_per_game") or 0),
                "_tb_pg": float(p.get("tb_per_game") or p.get("total_bases_per_game") or 0),
                "_bb_pg": float(p.get("bb_per_game") or p.get("walks_per_game") or 0),
                "_k_pg":  float(p.get("k_per_game") or p.get("strikeouts_per_game") or 0),
            })
        log.info(f"  {len(result)} batters loaded")
        return result
    except Exception as e:
        log.warning(f"BigDataBall batters failed: {e}")
        return []


def _fetch_bigball_pitchers(api_key: str, season: str) -> list[dict]:
    url = f"{BIGBALL_BASE}/mlb/stats/pitching"
    params = {"season": season, "api_key": api_key}
    try:
        log.info("Fetching pitcher stats from BigDataBall …")
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        players = raw if isinstance(raw, list) else raw.get("data", raw.get("players", []))
        result = []
        for p in players:
            result.append({
                "name":  p.get("name") or p.get("player_name", ""),
                "team":  p.get("team", ""),
                "era":   float(p.get("era") or 0),
                "whip":  float(p.get("whip") or 0),
                "k9":    float(p.get("k9") or p.get("strikeouts_per_9") or 0),
                "bb9":   float(p.get("bb9") or p.get("walks_per_9") or 0),
                "wins":  int(p.get("wins") or p.get("w") or 0),
                "ip":    float(p.get("ip") or p.get("innings_pitched") or 0),
                # Per-start averages for edge calculation
                "_k_per_start": float(p.get("k_per_start") or p.get("strikeouts_per_start") or 0),
            })
        log.info(f"  {len(result)} pitchers loaded")
        return result
    except Exception as e:
        log.warning(f"BigDataBall pitchers failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# EDGE CALCULATION
# ═══════════════════════════════════════════════════════════════════════════

# Map prop type labels → per-game stat key in batter/pitcher dicts
BATTER_STAT_KEY = {
    "H":   "_h_pg",
    "HR":  "_hr_pg",
    "RBI": "_rbi_pg",
    "TB":  "_tb_pg",
    "BB":  "_bb_pg",
    "K":   "_k_pg",    # batter K
    "R":   "_h_pg",    # proxy: use hits/game as R proxy
}
PITCHER_STAT_KEY = {
    "K":          "_k_per_start",
    "H-Allowed":  None,   # not always available
    "BB-Allowed": None,
    "ER":         None,
    "OUTS":       None,
}


def enrich_props_with_projections(
    props: list[dict],
    batters: list[dict],
    pitchers: list[dict],
    edge_min: float,
) -> list[dict]:
    """
    Match props to batter/pitcher stats by player name, compute a
    simple projection from per-game or per-start averages, then
    calculate edge % and tier. Drops props below edge_min.
    """
    # Build lookup dicts by lowercase name
    batter_map  = {p["name"].lower(): p for p in batters}
    pitcher_map = {p["name"].lower(): p for p in pitchers}

    enriched = []
    skipped  = 0

    for prop in props:
        player_lc = prop["player"].lower()
        ptype     = prop["propType"]
        line      = prop["line"]

        projection = 0.0
        matched    = False

        # --- Try pitcher markets first ---
        p_stat_key = PITCHER_STAT_KEY.get(ptype)
        if p_stat_key is not None:
            pitcher = pitcher_map.get(player_lc)
            if pitcher:
                prop["team"] = prop["team"] or pitcher["team"]
                raw_proj = pitcher.get(p_stat_key) or 0
                if raw_proj > 0:
                    projection = raw_proj
                    matched = True

        # --- Then try batter ---
        if not matched:
            b_stat_key = BATTER_STAT_KEY.get(ptype)
            if b_stat_key:
                batter = batter_map.get(player_lc)
                if batter:
                    prop["team"] = prop["team"] or batter["team"]
                    raw_proj = batter.get(b_stat_key) or 0
                    if raw_proj > 0:
                        projection = raw_proj
                        matched = True

        # Even if no stats match, include the line so it's visible in model
        edge = calc_edge(projection, line)

        if edge < edge_min and edge_min > 0:
            skipped += 1
            continue

        prop["projection"] = round(projection, 2)
        prop["edge"]       = edge
        prop["tier"]       = assign_tier(edge)
        enriched.append(prop)

    if skipped:
        log.debug(f"  {skipped} props skipped (below edge_min={edge_min}%)")
    return enriched


def attach_sp_stats(games: list[dict], pitchers: list[dict]) -> list[dict]:
    """
    Try to match starting pitcher names from game data (if BigDataBall
    provides them) to stats. If pitchers list is empty, games come back
    with TBD starters.
    """
    pitcher_map = {p["name"].lower(): p for p in pitchers}
    for game in games:
        for side in ("home", "away"):
            name = game.get(f"{side}Pitcher", "TBD")
            if name and name != "TBD":
                p = pitcher_map.get(name.lower())
                if p:
                    game[f"{side}ERA"]  = p.get("era",  0)
                    game[f"{side}K9"]   = p.get("k9",   0)
                    game[f"{side}WHIP"] = p.get("whip", 0)
    return games


# ═══════════════════════════════════════════════════════════════════════════
# ASSEMBLE OUTPUT
# ═══════════════════════════════════════════════════════════════════════════

def build_output(
    games: list[dict],
    props: list[dict],
    batters: list[dict],
    pitchers: list[dict],
    date_str: str,
) -> dict:
    """
    Build the MODEL_DATA-compatible JSON object.
    """
    # Strip internal helper keys from batters/pitchers
    clean_batters = []
    for b in batters:
        clean_batters.append({k: v for k, v in b.items() if not k.startswith("_")})

    clean_pitchers = []
    for p in pitchers:
        clean_pitchers.append({k: v for k, v in p.items() if not k.startswith("_")})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pull_date":    date_str,
        "version":      "mlb_edge_data_pull/v1.0",
        "games":        games,
        "props":        props,
        "batters":      clean_batters,
        "pitchers":     clean_pitchers,
        # Summary counts (convenience — model also computes these)
        "_counts": {
            "games":    len(games),
            "props":    len(props),
            "batters":  len(clean_batters),
            "pitchers": len(clean_pitchers),
            "elite":    sum(1 for p in props if p.get("tier") == "ELITE"),
            "strong":   sum(1 for p in props if p.get("tier") == "STRONG"),
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MLB Edge Report data pull — outputs mlb_edge_data.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--date",      default=None,         help="YYYY-MM-DD (default: today)")
    p.add_argument("--out",       default=None,         help="Output path (default: ./mlb_edge_data.json)")
    p.add_argument("--region",    default=None,         help="Odds API region (default: $ODDS_REGION or 'us')")
    p.add_argument("--books",     default=None,         help="Comma-separated bookmakers")
    p.add_argument("--edge-min",  type=float, default=0.0, help="Minimum edge %% to include (default: 0)")
    p.add_argument("--no-props",  action="store_true",  help="Skip player prop pull")
    p.add_argument("--no-stats",  action="store_true",  help="Skip BigDataBall stats pull")
    p.add_argument("--dry-run",   action="store_true",  help="Print output, don't write file")
    p.add_argument("--verbose",   action="store_true",  help="Debug logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    # ── API keys ────────────────────────────────────────────────────────
    odds_key    = get_env("ODDS_API_KEY")
    bigball_key = get_env("BIGBALL_API_KEY")

    if not odds_key:
        log.error("ODDS_API_KEY not set. Add it to .env or export it as an env var.")
        sys.exit(1)

    # ── Settings ────────────────────────────────────────────────────────
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    region   = args.region or get_env("ODDS_REGION", DEFAULT_REGION)
    books    = args.books  or get_env("ODDS_BOOKS",  DEFAULT_BOOKS)
    edge_min = args.edge_min or float(get_env("EDGE_MIN_PCT", "0"))
    out_path = args.out   or get_env("OUTPUT_PATH", "mlb_edge_data.json")

    log.info(f"━━━  MLB Edge Data Pull  ━━━  {date_str}  ━━━")
    log.info(f"Region: {region}  |  Books: {books}  |  Edge min: {edge_min}%")

    # ── Step 1: Game lines ──────────────────────────────────────────────
    games = fetch_game_lines(odds_key, date_str, region, books)
    if not games:
        log.warning(f"No MLB games found for {date_str}. Output will have empty games array.")

    # ── Step 2: Player props ────────────────────────────────────────────
    props: list[dict] = []
    if not args.no_props and games:
        props = fetch_props(odds_key, games, region, books)

    # ── Step 3: Player stats (BigDataBall) ─────────────────────────────
    batters:  list[dict] = []
    pitchers: list[dict] = []
    if not args.no_stats:
        season = str(datetime.now().year)
        batters, pitchers = fetch_bigball_stats(bigball_key, season)

    # ── Step 4: Enrich props with projections / edge calc ───────────────
    if props and (batters or pitchers):
        log.info("Calculating edges …")
        props = enrich_props_with_projections(props, batters, pitchers, edge_min)
    else:
        log.info("No stats available — props included without projections (edge=0).")

    # ── Step 5: Attach SP stats to games ───────────────────────────────
    if pitchers:
        games = attach_sp_stats(games, pitchers)

    # ── Step 6: Build output ────────────────────────────────────────────
    output = build_output(games, props, batters, pitchers, date_str)

    counts = output["_counts"]
    log.info(f"━━━  Summary  ━━━")
    log.info(f"  Games:    {counts['games']}")
    log.info(f"  Props:    {counts['props']}  (ELITE:{counts['elite']}  STRONG:{counts['strong']})")
    log.info(f"  Batters:  {counts['batters']}")
    log.info(f"  Pitchers: {counts['pitchers']}")

    # ── Step 7: Write / print ───────────────────────────────────────────
    json_str = json.dumps(output, indent=2)

    if args.dry_run:
        print(json_str)
        log.info("Dry-run — no file written.")
    else:
        out_file = Path(out_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json_str, encoding="utf-8")
        log.info(f"✅  Saved → {out_file.resolve()}")
        log.info("Load in model: Data tab → Upload JSON  or  GitHub auto-load")


if __name__ == "__main__":
    main()
