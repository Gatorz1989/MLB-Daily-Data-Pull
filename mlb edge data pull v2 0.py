#!/usr/bin/env python3
"""
mlb_edge_data_pull_v2.0.py  —  MLB Edge Report Data Companion
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Companion to: MLB_Edge_Model_v1.26.html (The Edge Report — G-Money)

Data sources (all free; only OpenWeatherMap needs a key):
  ● The Odds API v4    — game lines (ML/RL/total) + player props
  ● MLB Stats API      — season stats, L/R splits, confirmed lineups,
                          last-5-start pitcher form, last-14-day batter form,
                          pitcher-vs-batter career matchups, team stats,
                          probable pitchers, injury report
  ● Baseball Savant    — Statcast: barrel rate, exit velo, launch angle,
                          hard-hit %, sprint speed, whiff rate, pitch mix,
                          xBA/xSLG/xERA, park factors (all auto-pulled via CSV)
  ● OpenWeatherMap     — wind speed/dir, temp, humidity per ballpark
                          (optional, free key → set WEATHER_API_KEY env var)

Usage:
  python mlb_edge_data_pull_v2.0.py
  python mlb_edge_data_pull_v2.0.py --date 2026-10-28 --season 2026
  python mlb_edge_data_pull_v2.0.py --no-savant --no-weather --verbose
  python mlb_edge_data_pull_v2.0.py --dry-run    # print counts, no file write

Environment variables (.env file supported):
  ODDS_API_KEY      — The Odds API key (required for game lines + props)
  WEATHER_API_KEY   — OpenWeatherMap free key (optional)
  ODDS_BOOKS        — comma-separated bookmakers (default: draftkings,fanduel,betmgm,bet365_us)
  ODDS_REGION       — us | uk | eu | au (default: us)
  EDGE_MIN_PCT      — minimum edge % to include in output (default: 0 = all)

Output: mlb_edge_data.json  (MODEL_DATA-compatible schema v2.0)
"""

import argparse, csv, io, json, logging, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv optional

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests not installed.  Run: pip install requests")

try:
    import pandas as pd
except ImportError:
    sys.exit("ERROR: pandas not installed.  Run: pip install pandas")

# ── CONFIG ───────────────────────────────────────────────────────────────────

ODDS_API_KEY    = os.environ.get("ODDS_API_KEY", "")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
ODDS_API_BASE   = "https://api.the-odds-api.com/v4"
MLB_API_BASE    = "https://statsapi.mlb.com/api/v1"
SAVANT_BASE     = "https://baseballsavant.mlb.com"
WEATHER_BASE    = "https://api.openweathermap.org/data/2.5"
DEFAULT_BOOKS   = os.environ.get("ODDS_BOOKS", "draftkings,fanduel,betmgm,bet365_us")
DEFAULT_REGION  = os.environ.get("ODDS_REGION", "us")
EDGE_MIN_PCT    = float(os.environ.get("EDGE_MIN_PCT", "0"))

# Bookmaker priority for best-line selection (lower = more trusted)
BK_ORDER = {
    "draftkings": 0, "fanduel": 1, "betmgm": 2,
    "bet365_us": 3, "bet365": 3, "betrivers": 3,
    "pointsbetus": 4, "williamhill_us": 4,
}

# Ballpark GPS coordinates for weather API
PARK_COORDS = {
    "yankee stadium":           (40.8296, -73.9262),
    "fenway park":              (42.3467, -71.0972),
    "wrigley field":            (41.9484, -87.6553),
    "guaranteed rate field":    (41.8300, -87.6339),
    "great american ball park": (39.0979, -84.5082),
    "progressive field":        (41.4962, -81.6852),
    "comerica park":            (42.3390, -83.0485),
    "kauffman stadium":         (39.0517, -94.4803),
    "target field":             (44.9817, -93.2781),
    "minute maid park":         (29.7573, -95.3555),
    "globe life field":         (32.7473, -97.0825),
    "angel stadium":            (33.8003, -117.8827),
    "dodger stadium":           (34.0739, -118.2400),
    "petco park":               (32.7076, -117.1570),
    "oracle park":              (37.7786, -122.3893),
    "t-mobile park":            (47.5914, -122.3326),
    "coors field":              (39.7559, -104.9942),
    "chase field":              (33.4453, -112.0667),
    "american family field":    (43.0280, -87.9712),
    "busch stadium":            (38.6226, -90.1928),
    "pnc park":                 (40.4469, -80.0057),
    "oriole park at camden yards": (39.2838, -76.6218),
    "camden yards":             (39.2838, -76.6218),
    "nationals park":           (38.8730, -77.0074),
    "truist park":              (33.8908, -84.4678),
    "citi field":               (40.7571, -73.8458),
    "citizens bank park":       (39.9061, -75.1665),
    "loandepot park":           (25.7781, -80.2197),
    "tropicana field":          (27.7683, -82.6534),
    "rogers centre":            (43.6414, -79.3894),
}

# Team abbr → typical home park (used when schedule API doesn't return venue)
TEAM_PARK = {
    "NYY": "yankee stadium",     "BOS": "fenway park",
    "CHC": "wrigley field",      "CWS": "guaranteed rate field",
    "CIN": "great american ball park", "CLE": "progressive field",
    "DET": "comerica park",      "KC":  "kauffman stadium",
    "MIN": "target field",       "HOU": "minute maid park",
    "TEX": "globe life field",   "LAA": "angel stadium",
    "LAD": "dodger stadium",     "SD":  "petco park",
    "SF":  "oracle park",        "SEA": "t-mobile park",
    "COL": "coors field",        "ARI": "chase field",
    "MIL": "american family field", "STL": "busch stadium",
    "PIT": "pnc park",           "BAL": "camden yards",
    "WSH": "nationals park",     "ATL": "truist park",
    "NYM": "citi field",         "PHI": "citizens bank park",
    "MIA": "loandepot park",     "TB":  "tropicana field",
    "TOR": "rogers centre",
}

log = logging.getLogger("mlb_pull")

# ── HELPERS ──────────────────────────────────────────────────────────────────

def norm(name: str) -> str:
    """Normalize player/park name for fuzzy matching."""
    return " ".join(name.lower().strip().split())

def mlb_get(path: str, params: dict = None, timeout: int = 20) -> dict:
    """GET from MLB Stats API with error handling."""
    r = requests.get(
        MLB_API_BASE + path,
        params=params or {},
        timeout=timeout,
        verify=True,
    )
    r.raise_for_status()
    return r.json()

def savant_csv(url: str, timeout: int = 40) -> pd.DataFrame:
    """Fetch a Baseball Savant CSV export and return as DataFrame."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; mlb-edge-model/2.0; +https://theedgereport.com)"}
    r = requests.get(url, headers=headers, timeout=timeout, verify=True)
    r.raise_for_status()
    text = r.text.strip()
    if not text or text.startswith("<!"):
        raise ValueError(f"Savant returned HTML (not CSV) for: {url}")
    return pd.read_csv(io.StringIO(text), low_memory=False)

def safe_float(v, default: float = 0.0) -> float:
    try:
        s = str(v).strip()
        if s in ("", "nan", "None", ".", "null", "N/A", "-"):
            return default
        return float(s)
    except (ValueError, TypeError):
        return default

def safe_int(v, default: int = 0) -> int:
    try:
        s = str(v).strip()
        if s in ("", "nan", "None", ".", "null", "N/A", "-"):
            return default
        return int(float(s))
    except (ValueError, TypeError):
        return default

def american_to_implied(odds: float) -> float:
    """Convert American odds to implied probability."""
    if odds >= 100:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)

def calc_edge(over_odds: float, under_odds: float) -> float:
    """Calculate edge % for Over side after vig removal."""
    oi = american_to_implied(over_odds)
    ui = american_to_implied(under_odds)
    total = oi + ui
    if total <= 0:
        return 0.0
    vf_over = oi / total  # vig-free over probability
    payout = over_odds / 100 if over_odds > 0 else 100 / abs(over_odds)
    return round((vf_over * payout - (1 - vf_over)) * 100, 2)

def tier_from_edge(edge: float) -> str:
    if edge >= 5:   return "ELITE"
    if edge >= 2:   return "STRONG"
    if edge >= 0:   return "LEAN"
    return "SKIP"

def normalize_savant_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize Savant column names (they vary slightly by endpoint)."""
    df.columns = [c.strip().lower().replace(" ", "_").replace(",_", "_") for c in df.columns]
    return df

def savant_name_key(row: pd.Series, col: str = "last_name__first_name") -> str:
    """
    Savant leaderboard CSVs use 'Last, First' format in a combined column.
    Returns normalized 'first last' key for matching.
    """
    raw = str(row.get(col) or row.get("last_name__first_name") or
              row.get("last_name,_first_name") or row.get("name") or "")
    if not raw or raw in ("nan", "None"):
        return ""
    if "," in raw:
        parts = raw.split(",", 1)
        return norm(parts[1].strip() + " " + parts[0].strip())
    return norm(raw)

# ── THE ODDS API ──────────────────────────────────────────────────────────────

def fetch_game_lines(books: str, region: str) -> list[dict]:
    """Fetch today's game lines (ML, RL, total) from The Odds API."""
    if not ODDS_API_KEY:
        log.warning("No ODDS_API_KEY — skipping game lines fetch.")
        return []
    url = f"{ODDS_API_BASE}/sports/baseball_mlb/odds"
    params = {
        "apiKey": ODDS_API_KEY, "regions": region,
        "markets": "h2h,spreads,totals", "bookmakers": books,
        "oddsFormat": "american",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log.warning(f"Odds API returned non-list response: {type(data)}")
            return []
        games = []
        for ev in data:
            gm = {
                "awayTeam":  ev.get("away_team", ""),
                "homeTeam":  ev.get("home_team", ""),
                "gameDate":  (ev.get("commence_time", "") or "")[:10],
                "_oddsId":   ev.get("id", ""),
                "homePitcher": "TBD", "awayPitcher": "TBD",
                "homeERA": 0, "awayERA": 0, "homeK9": 0, "awayK9": 0,
                "homeWHIP": 0, "awayWHIP": 0,
                "park_run_factor": 1.0, "park_hr_factor": 1.0, "park_hit_factor": 1.0,
            }
            for bk in (ev.get("bookmakers") or []):
                bk_key = bk.get("key", "")
                priority = BK_ORDER.get(bk_key, 99)
                for mkt in (bk.get("markets") or []):
                    key = mkt.get("key", "")
                    ht_last = gm["homeTeam"].split(" ")[-1].lower()
                    for o in (mkt.get("outcomes") or []):
                        oname = (o.get("name") or "").lower()
                        if key == "h2h":
                            side = "home" if ht_last in oname else "away"
                            k = f"{side}ML_pri"
                            if priority < gm.get(k, 99):
                                gm[f"{side}ML"] = o.get("price")
                                gm[k] = priority
                        elif key == "spreads":
                            side = "home" if ht_last in oname else "away"
                            k = f"{side}RL_pri"
                            if priority < gm.get(k, 99):
                                pt = o.get("point", 0)
                                gm[f"{side}RLLine"] = ("+" if pt > 0 else "") + str(pt)
                                gm[f"{side}RL"] = o.get("price")
                                gm[k] = priority
                        elif key == "totals" and oname == "over":
                            if priority < gm.get("total_pri", 99):
                                gm["total"] = o.get("point")
                                gm["totalOdds"] = o.get("price")
                                gm["total_pri"] = priority
            # Scrub internal priority keys
            for k in list(gm.keys()):
                if k.endswith("_pri"):
                    del gm[k]
            games.append(gm)
        log.info(f"  {len(games)} games from Odds API")
        return games
    except Exception as e:
        log.warning(f"fetch_game_lines failed: {e}")
        return []


def fetch_props(event_ids: list[str], books: str, region: str) -> list[dict]:
    """Fetch player props for today's games. Returns raw prop list (edge recalculated later)."""
    if not ODDS_API_KEY or not event_ids:
        return []
    markets = (
        "pitcher_strikeouts,"
        "batter_hits,batter_home_runs,batter_rbis,"
        "batter_runs_scored,batter_total_bases"
    )
    PROP_LABELS = {
        "pitcher_strikeouts": "K", "batter_hits": "H",
        "batter_home_runs": "HR", "batter_rbis": "RBI",
        "batter_runs_scored": "R", "batter_total_bases": "TB",
    }
    all_props = []
    for ev_id in event_ids[:15]:  # cap to conserve API credits
        url = f"{ODDS_API_BASE}/sports/baseball_mlb/events/{ev_id}/odds"
        params = {
            "apiKey": ODDS_API_KEY, "regions": region,
            "markets": markets, "bookmakers": books, "oddsFormat": "american",
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            if not r.ok:
                continue
            data = r.json()
            market_map: dict = {}
            for bk in (data.get("bookmakers") or []):
                bk_key  = bk.get("key", "")
                priority = BK_ORDER.get(bk_key, 99)
                for mkt in (bk.get("markets") or []):
                    ptype = mkt.get("key", "")
                    for o in (mkt.get("outcomes") or []):
                        name_is_ou = (o.get("name", "") or "").lower() in ("over", "under")
                        pname   = norm(o.get("description", "") if name_is_ou else o.get("name", ""))
                        side    = (o.get("name", "") or "").lower() if name_is_ou else (o.get("description", "") or "").lower()
                        mk      = (ptype, pname)
                        cur     = market_map.get(mk, {})
                        if priority < cur.get("_pri", 99):
                            entry = {"_pri": priority, "ptype": ptype, "player": pname, "_evId": ev_id}
                            if side == "over":  entry["line"] = o.get("point"); entry["overOdds"] = o.get("price")
                            if side == "under": entry["underOdds"] = o.get("price")
                            market_map[mk] = {**cur, **entry}
                        else:
                            if side == "over" and "line" not in cur:
                                cur["line"] = o.get("point"); cur["overOdds"] = o.get("price")
                            if side == "under" and "underOdds" not in cur:
                                cur["underOdds"] = o.get("price")
            for entry in market_map.values():
                line       = safe_float(entry.get("line"))
                over_odds  = safe_float(entry.get("overOdds"), -110)
                under_odds = safe_float(entry.get("underOdds"), -110)
                edge = calc_edge(over_odds, under_odds)
                all_props.append({
                    "player":        entry.get("player", ""),
                    "propType":      entry.get("ptype", ""),
                    "propTypeLabel": PROP_LABELS.get(entry.get("ptype", ""), entry.get("ptype", "")),
                    "line":          line,
                    "projection":    line,   # overwritten by enrich_props if stats available
                    "edge":          edge,
                    "tier":          tier_from_edge(edge),
                    "odds":          over_odds,
                    "underOdds":     under_odds,
                    "_evId":         entry.get("_evId", ""),
                })
            time.sleep(0.25)
        except Exception as e:
            log.warning(f"Props fetch error for {ev_id}: {e}")
    log.info(f"  {len(all_props)} props from Odds API")
    return all_props

# ── MLB STATS API — SEASON STATS ─────────────────────────────────────────────

def fetch_mlb_season_batting(season: str) -> list[dict]:
    log.info("MLB Stats API: season batting …")
    try:
        data = mlb_get("/stats", {
            "stats": "season", "group": "hitting", "gameType": "R",
            "season": season, "sportId": 1, "limit": 1200,
        })
        out = []
        for sp in (data.get("stats") or []):
            for split in (sp.get("splits") or []):
                pl = split.get("player") or {}
                tm = split.get("team") or {}
                st = split.get("stat") or {}
                pa = safe_int(st.get("plateAppearances"))
                ab = safe_int(st.get("atBats"))
                so = safe_int(st.get("strikeOuts"))
                bb = safe_int(st.get("baseOnBalls"))
                out.append({
                    "name":    pl.get("fullName", ""),
                    "mlb_id":  pl.get("id", 0),
                    "team":    tm.get("abbreviation", ""),
                    "team_id": tm.get("id", 0),
                    "bats":    "",          # populated from roster if needed
                    "lineup_pos": 0,        # populated from today's lineup
                    "avg":   safe_float(st.get("avg")),
                    "obp":   safe_float(st.get("obp")),
                    "slg":   safe_float(st.get("slg")),
                    "ops":   safe_float(st.get("ops")),
                    "hr":    safe_int(st.get("homeRuns")),
                    "rbi":   safe_int(st.get("rbi")),
                    "sb":    safe_int(st.get("stolenBases")),
                    "hits":  safe_int(st.get("hits")),
                    "ab":    ab,
                    "pa":    pa,
                    "k_pct": round(so / max(pa, 1), 3),
                    "bb_pct":round(bb / max(pa, 1), 3),
                })
        log.info(f"  {len(out)} batters")
        return out
    except Exception as e:
        log.warning(f"fetch_mlb_season_batting failed: {e}")
        return []


def fetch_mlb_season_pitching(season: str) -> list[dict]:
    log.info("MLB Stats API: season pitching …")
    try:
        data = mlb_get("/stats", {
            "stats": "season", "group": "pitching", "gameType": "R",
            "season": season, "sportId": 1, "limit": 600,
        })
        out = []
        for sp in (data.get("stats") or []):
            for split in (sp.get("splits") or []):
                pl = split.get("player") or {}
                tm = split.get("team") or {}
                st = split.get("stat") or {}
                ip = safe_float(st.get("inningsPitched"))
                k  = safe_int(st.get("strikeOuts"))
                bb = safe_int(st.get("baseOnBalls"))
                out.append({
                    "name":    pl.get("fullName", ""),
                    "mlb_id":  pl.get("id", 0),
                    "team":    tm.get("abbreviation", ""),
                    "team_id": tm.get("id", 0),
                    "throws":  "",          # populated from roster if needed
                    "era":   safe_float(st.get("era")),
                    "whip":  safe_float(st.get("whip")),
                    "k9":    round(k / max(ip, 0.1) * 9, 2),
                    "bb9":   round(bb / max(ip, 0.1) * 9, 2),
                    "wins":  safe_int(st.get("wins")),
                    "losses":safe_int(st.get("losses")),
                    "ip":    ip,
                    "gs":    safe_int(st.get("gamesStarted")),
                    "saves": safe_int(st.get("saves")),
                })
        log.info(f"  {len(out)} pitchers")
        return out
    except Exception as e:
        log.warning(f"fetch_mlb_season_pitching failed: {e}")
        return []

# ── MLB STATS API — L/R SPLITS ────────────────────────────────────────────────

def fetch_batting_splits(season: str) -> tuple[dict, dict]:
    """
    Returns (vs_lhp, vs_rhp) — dicts keyed by mlb_id with split stats.
    sitCode 'vl' = batter vs left-handed pitcher
    sitCode 'vr' = batter vs right-handed pitcher
    """
    log.info("MLB Stats API: batting L/R splits …")
    results = {}
    for sit, label in [("vl", "vs_lhp"), ("vr", "vs_rhp")]:
        results[label] = {}
        try:
            data = mlb_get("/stats", {
                "stats": "statSplits", "group": "hitting",
                "gameType": "R", "season": season,
                "sportId": 1, "sitCodes": sit, "limit": 1200,
            })
            for sp in (data.get("stats") or []):
                for split in (sp.get("splits") or []):
                    pl = split.get("player") or {}
                    st = split.get("stat") or {}
                    pid = pl.get("id", 0)
                    ab  = safe_int(st.get("atBats"))
                    if pid and ab >= 10:
                        results[label][pid] = {
                            "avg":  safe_float(st.get("avg")),
                            "obp":  safe_float(st.get("obp")),
                            "slg":  safe_float(st.get("slg")),
                            "ops":  safe_float(st.get("ops")),
                            "hr":   safe_int(st.get("homeRuns")),
                            "hits": safe_int(st.get("hits")),
                            "ab":   ab,
                        }
        except Exception as e:
            log.warning(f"Batting splits ({sit}) failed: {e}")
    log.info(f"  {len(results['vs_lhp'])} vs-LHP / {len(results['vs_rhp'])} vs-RHP")
    return results["vs_lhp"], results["vs_rhp"]


def fetch_pitching_splits(season: str) -> tuple[dict, dict]:
    """
    Returns (vs_lhb, vs_rhb) — dicts keyed by mlb_id with split stats.
    sitCode 'vl' = pitcher vs left-handed batter
    sitCode 'vr' = pitcher vs right-handed batter
    """
    log.info("MLB Stats API: pitching L/R splits …")
    results = {}
    for sit, label in [("vl", "vs_lhb"), ("vr", "vs_rhb")]:
        results[label] = {}
        try:
            data = mlb_get("/stats", {
                "stats": "statSplits", "group": "pitching",
                "gameType": "R", "season": season,
                "sportId": 1, "sitCodes": sit, "limit": 600,
            })
            for sp in (data.get("stats") or []):
                for split in (sp.get("splits") or []):
                    pl = split.get("player") or {}
                    st = split.get("stat") or {}
                    pid = pl.get("id", 0)
                    ip  = safe_float(st.get("inningsPitched"))
                    if pid and ip >= 5:
                        k = safe_int(st.get("strikeOuts"))
                        results[label][pid] = {
                            "era":  safe_float(st.get("era")),
                            "whip": safe_float(st.get("whip")),
                            "k9":   round(k / max(ip, 0.1) * 9, 2),
                            "ab":   safe_int(st.get("atBats")),
                            "ip":   ip,
                        }
        except Exception as e:
            log.warning(f"Pitching splits ({sit}) failed: {e}")
    log.info(f"  {len(results['vs_lhb'])} vs-LHB / {len(results['vs_rhb'])} vs-RHB")
    return results["vs_lhb"], results["vs_rhb"]

# ── MLB STATS API — RECENT FORM ────────────────────────────────────────────────

def fetch_pitcher_last5(pitcher_id: int, season: str) -> dict:
    """Last 5 game logs for one starter. Returns rolling stats dict."""
    try:
        data = mlb_get(f"/people/{pitcher_id}/stats", {
            "stats": "gameLog", "group": "pitching",
            "gameType": "R,F,D,L,W", "season": season, "limit": 5,
        })
        logs = []
        for sp in (data.get("stats") or []):
            for split in (sp.get("splits") or []):
                st  = split.get("stat") or {}
                ip  = safe_float(st.get("inningsPitched"))
                k   = safe_int(st.get("strikeOuts"))
                er  = safe_int(st.get("earnedRuns"))
                bf  = safe_int(st.get("battersFaced"))
                h   = safe_int(st.get("hits"))
                bb  = safe_int(st.get("baseOnBalls"))
                logs.append({"ip": ip, "k": k, "er": er, "h": h, "bb": bb, "bf": bf})
        if not logs:
            return {}
        total_ip = sum(l["ip"] for l in logs)
        total_k  = sum(l["k"]  for l in logs)
        total_er = sum(l["er"] for l in logs)
        total_h  = sum(l["h"]  for l in logs)
        total_bb = sum(l["bb"] for l in logs)
        return {
            "era_last5":          round(total_er / max(total_ip, 0.1) * 9, 2),
            "k_per_start_last5":  round(total_k  / max(len(logs), 1), 1),
            "ip_per_start_last5": round(total_ip / max(len(logs), 1), 1),
            "whip_last5":         round((total_h + total_bb) / max(total_ip, 0.1), 2),
            "k9_last5":           round(total_k / max(total_ip, 0.1) * 9, 2),
        }
    except Exception as e:
        log.debug(f"Pitcher last-5 failed for {pitcher_id}: {e}")
        return {}


def fetch_batter_last14(batter_id: int, season: str) -> dict:
    """Last 14 game logs for one batter. Returns rolling stats dict."""
    try:
        data = mlb_get(f"/people/{batter_id}/stats", {
            "stats": "gameLog", "group": "hitting",
            "gameType": "R,F,D,L,W", "season": season, "limit": 14,
        })
        logs = []
        for sp in (data.get("stats") or []):
            for split in (sp.get("splits") or []):
                st = split.get("stat") or {}
                logs.append({
                    "h":  safe_int(st.get("hits")),
                    "ab": safe_int(st.get("atBats")),
                    "hr": safe_int(st.get("homeRuns")),
                    "rbi":safe_int(st.get("rbi")),
                    "bb": safe_int(st.get("baseOnBalls")),
                    "so": safe_int(st.get("strikeOuts")),
                    "tb": safe_int(st.get("totalBases")),
                })
        if not logs:
            return {}
        total_h  = sum(l["h"]  for l in logs)
        total_ab = sum(l["ab"] for l in logs)
        total_hr = sum(l["hr"] for l in logs)
        total_tb = sum(l["tb"] for l in logs)
        last7_h  = sum(l["h"] for l in logs[:7])
        return {
            "avg_last14":   round(total_h  / max(total_ab, 1), 3),
            "slg_last14":   round(total_tb / max(total_ab, 1), 3),
            "hr_last14":    total_hr,
            "hits_last7":   last7_h,
        }
    except Exception as e:
        log.debug(f"Batter last-14 failed for {batter_id}: {e}")
        return {}

# ── MLB STATS API — LINEUPS ───────────────────────────────────────────────────

def fetch_todays_lineups(date_str: str) -> dict[int, int]:
    """Returns {mlb_id: batting_order_position} for all confirmed lineups today."""
    log.info(f"MLB Stats API: confirmed lineups for {date_str} …")
    lineup_map: dict[int, int] = {}
    try:
        data = mlb_get("/schedule", {
            "sportId": 1, "date": date_str,
            "hydrate": "lineups,team", "gameType": "R,F,D,L,W",
        })
        for day in (data.get("dates") or []):
            for g in (day.get("games") or []):
                lineups = g.get("lineups") or {}
                for side in ("homePlayers", "awayPlayers"):
                    for pos, player in enumerate((lineups.get(side) or []), 1):
                        pid = player.get("id", 0)
                        if pid:
                            lineup_map[pid] = pos
    except Exception as e:
        log.warning(f"fetch_todays_lineups failed: {e}")
    log.info(f"  {len(lineup_map)} players in confirmed lineups")
    return lineup_map

# ── MLB STATS API — PROBABLE PITCHERS ────────────────────────────────────────

def fetch_probable_pitchers(date_str: str) -> dict[str, dict]:
    """Returns {team_name_lower: {name, id}} for today's probable starters."""
    log.info(f"MLB Stats API: probable pitchers for {date_str} …")
    result: dict[str, dict] = {}
    try:
        data = mlb_get("/schedule", {
            "sportId": 1, "date": date_str,
            "hydrate": "probablePitcher,team",
            "gameType": "R,F,D,L,W",
        })
        for day in (data.get("dates") or []):
            for g in (day.get("games") or []):
                for side in ("away", "home"):
                    t       = (g.get("teams") or {}).get(side, {})
                    pitcher = t.get("probablePitcher") or {}
                    team    = (t.get("team") or {}).get("name", "")
                    abbr    = (t.get("team") or {}).get("abbreviation", "")
                    name    = pitcher.get("fullName", "")
                    pid     = pitcher.get("id", 0)
                    if team and name:
                        result[team.lower()] = {"name": name, "id": pid, "abbr": abbr}
                        if abbr:
                            result[abbr.lower()] = {"name": name, "id": pid, "abbr": abbr}
    except Exception as e:
        log.warning(f"fetch_probable_pitchers failed: {e}")
    log.info(f"  {len(result)//2} probable pitchers (stored by team name and abbr)")
    return result

# ── MLB STATS API — TEAM STATS ────────────────────────────────────────────────

def fetch_team_stats(season: str) -> dict[str, dict]:
    """Returns {team_abbr: stats_dict} with offensive + K-rate info."""
    log.info("MLB Stats API: team stats …")
    teams: dict[str, dict] = {}
    try:
        data = mlb_get("/teams/stats", {
            "season": season, "sportId": 1,
            "group": "hitting", "stats": "season",
        })
        for sp in (data.get("stats") or []):
            for split in (sp.get("splits") or []):
                tm  = split.get("team") or {}
                st  = split.get("stat") or {}
                abbr = tm.get("abbreviation", "")
                gp   = safe_int(st.get("gamesPlayed"), 1) or 1
                pa   = safe_int(st.get("plateAppearances"), 1) or 1
                so   = safe_int(st.get("strikeOuts"))
                if abbr:
                    teams[abbr] = {
                        "name":           tm.get("name", ""),
                        "team_id":        tm.get("id", 0),
                        "runs_per_game":  round(safe_int(st.get("runs")) / gp, 2),
                        "ops":            safe_float(st.get("ops")),
                        "avg":            safe_float(st.get("avg")),
                        "k_per_game":     round(so / gp, 2),
                        "k_pct":          round(so / pa, 3),
                        "bb_pct":         round(safe_int(st.get("baseOnBalls")) / pa, 3),
                        "hr_per_game":    round(safe_int(st.get("homeRuns")) / gp, 2),
                    }
    except Exception as e:
        log.warning(f"fetch_team_stats (hitting) failed: {e}")
    log.info(f"  {len(teams)} teams")
    return teams

# ── MLB STATS API — CAREER MATCHUP ───────────────────────────────────────────

def fetch_matchup_stats(pitcher_id: int, batter_id: int) -> dict:
    """Career pitcher-vs-batter stats. Returns empty dict if < 3 PA."""
    try:
        data = mlb_get("/stats", {
            "stats": "vsPlayer", "group": "hitting",
            "personIds": batter_id, "opposingPlayerId": pitcher_id,
        })
        for sp in (data.get("stats") or []):
            for split in (sp.get("splits") or []):
                st = split.get("stat") or {}
                ab = safe_int(st.get("atBats"))
                if ab >= 3:
                    return {
                        "career_ab":  ab,
                        "career_avg": safe_float(st.get("avg")),
                        "career_hr":  safe_int(st.get("homeRuns")),
                        "career_k":   safe_int(st.get("strikeOuts")),
                        "career_bb":  safe_int(st.get("baseOnBalls")),
                        "career_hits":safe_int(st.get("hits")),
                    }
    except Exception:
        pass
    return {}

# ── MLB STATS API — INJURIES ──────────────────────────────────────────────────

def fetch_injuries(season: str) -> list[dict]:
    injuries = []
    try:
        data = mlb_get("/injuries", {"sportId": 1, "season": season})
        for item in (data.get("roster") or []):
            pl = item.get("person") or {}
            tm = item.get("team") or {}
            injuries.append({
                "name":   pl.get("fullName", ""),
                "mlb_id": pl.get("id", 0),
                "team":   tm.get("abbreviation", ""),
                "status": item.get("injuryStatus", ""),
            })
    except Exception as e:
        log.debug(f"Injury fetch failed: {e}")
    return injuries

# ── BASEBALL SAVANT — BATTER STATCAST ────────────────────────────────────────

def fetch_savant_batter_statcast(season: str) -> dict[str, dict]:
    """
    Pulls Savant expected-stats leaderboard (xBA, xSLG, EV, barrel rate, etc.)
    for batters. Returns {norm_name: stats_dict}.
    """
    log.info("Baseball Savant: batter Statcast …")
    out: dict[str, dict] = {}
    try:
        url = (
            f"{SAVANT_BASE}/leaderboard/expected_statistics"
            f"?type=batter&year={season}&position=&team=&min=25&csv=true"
        )
        df = normalize_savant_columns(savant_csv(url))
        for _, row in df.iterrows():
            nk = savant_name_key(row)
            if not nk:
                continue
            barrel_raw = safe_float(row.get("barrel_batted_rate") or row.get("barrel") or 0)
            hh_raw     = safe_float(row.get("hard_hit_percent") or 0)
            out[nk] = {
                "xba":          safe_float(row.get("xba")),
                "xslg":         safe_float(row.get("xslg")),
                "exit_velo":    safe_float(row.get("exit_velocity_avg") or row.get("avg_hit_speed")),
                "launch_angle": safe_float(row.get("launch_angle_avg") or row.get("avg_launch_angle")),
                # Normalize pct fields: Savant may send 12.5 (pct) or 0.125 (rate)
                "barrel_rate":  round(barrel_raw / 100 if barrel_raw > 1 else barrel_raw, 4),
                "hard_hit_pct": round(hh_raw     / 100 if hh_raw > 1     else hh_raw,     4),
                "savant_pa":    safe_int(row.get("pa")),
            }
        log.info(f"  {len(out)} batters from Savant expected stats")
    except Exception as e:
        log.warning(f"Savant batter expected stats failed: {e}")
    return out


def fetch_savant_sprint_speed(season: str) -> dict[str, float]:
    """Returns {norm_name: sprint_speed_ft_per_sec}."""
    log.info("Baseball Savant: sprint speed …")
    out: dict[str, float] = {}
    try:
        url = f"{SAVANT_BASE}/leaderboard/sprint_speed?year={season}&team=&position=&csv=true"
        df  = normalize_savant_columns(savant_csv(url))
        for _, row in df.iterrows():
            nk    = savant_name_key(row, col="last_name__first_name")
            speed = safe_float(row.get("sprint_speed") or row.get("r_sprint_speed_top50percent") or 0)
            if nk and speed:
                out[nk] = speed
        log.info(f"  {len(out)} sprint speeds from Savant")
    except Exception as e:
        log.warning(f"Savant sprint speed failed: {e}")
    return out


# ── BASEBALL SAVANT — PITCHER STATCAST ───────────────────────────────────────

def fetch_savant_pitcher_statcast(season: str) -> dict[str, dict]:
    """
    Pulls Savant expected-stats leaderboard + pitch-arsenal CSV for pitchers.
    Returns {norm_name: stats_dict}.
    """
    log.info("Baseball Savant: pitcher Statcast …")
    out: dict[str, dict] = {}

    # 1) Expected stats (xERA, EV allowed, barrel rate allowed)
    try:
        url = (
            f"{SAVANT_BASE}/leaderboard/expected_statistics"
            f"?type=pitcher&year={season}&position=&team=&min=25&csv=true"
        )
        df = normalize_savant_columns(savant_csv(url))
        for _, row in df.iterrows():
            nk = savant_name_key(row)
            if not nk:
                continue
            barrel_raw = safe_float(row.get("barrel_batted_rate") or row.get("barrel") or 0)
            out[nk] = {
                "xera":                safe_float(row.get("xera")),
                "exit_velo_allowed":   safe_float(row.get("exit_velocity_avg") or row.get("avg_hit_speed")),
                "barrel_rate_allowed": round(barrel_raw / 100 if barrel_raw > 1 else barrel_raw, 4),
                "savant_bf":           safe_int(row.get("pa")),
            }
        log.info(f"  {len(out)} pitchers from Savant expected stats")
    except Exception as e:
        log.warning(f"Savant pitcher expected stats failed: {e}")

    # 2) Pitch-arsenal stats (pitch mix % + per-pitch whiff rate)
    try:
        url2 = (
            f"{SAVANT_BASE}/leaderboard/pitch-arsenal-stats"
            f"?type=pitcher&pitchType=&year={season}&team=&min=50&csv=true"
        )
        df2 = normalize_savant_columns(savant_csv(url2))
        # Aggregate per pitcher across pitch types
        mix_map: dict[str, dict] = {}
        for _, row in df2.iterrows():
            nk = savant_name_key(row, col="last_name__first_name")
            if not nk:
                continue
            ptype   = str(row.get("pitch_type", "") or "").upper()
            pct_raw = safe_float(row.get("pitch_percent") or row.get("pitches_percent") or 0)
            pct     = pct_raw / 100 if pct_raw > 1 else pct_raw
            whiff_raw = safe_float(row.get("whiff_percent") or row.get("whiff_pct") or 0)
            whiff   = whiff_raw / 100 if whiff_raw > 1 else whiff_raw

            if nk not in mix_map:
                mix_map[nk] = {
                    "fastball_pct": 0.0, "breaking_pct": 0.0, "offspeed_pct": 0.0,
                    "whiff_rate": 0.0, "_total_pct": 0.0,
                }
            if ptype in ("FF", "SI", "FC"):
                mix_map[nk]["fastball_pct"] += pct
            elif ptype in ("SL", "CU", "KC", "CS", "SV", "ST", "SC"):
                mix_map[nk]["breaking_pct"] += pct
            elif ptype in ("CH", "FS", "FO"):
                mix_map[nk]["offspeed_pct"] += pct
            # Weighted whiff rate
            mix_map[nk]["whiff_rate"]   += whiff * pct
            mix_map[nk]["_total_pct"]   += pct

        for nk, mix in mix_map.items():
            total = mix["_total_pct"] or 1
            if nk not in out:
                out[nk] = {}
            out[nk].update({
                "fastball_pct": round(mix["fastball_pct"], 3),
                "breaking_pct": round(mix["breaking_pct"], 3),
                "offspeed_pct": round(mix["offspeed_pct"], 3),
                "whiff_rate":   round(mix["whiff_rate"] / total, 4),
            })
        log.info(f"  pitch mix data for {len(mix_map)} pitchers from Savant")
    except Exception as e:
        log.warning(f"Savant pitch arsenal failed: {e}")

    return out

# ── BASEBALL SAVANT — PARK FACTORS ────────────────────────────────────────────

def fetch_savant_park_factors(season: str) -> dict[str, dict]:
    """
    Returns {park_name_norm: {run_factor, hr_factor, hit_factor}}.
    Factors normalized to 1.00 scale (1.05 = 5% above league avg).
    """
    log.info("Baseball Savant: park factors …")
    out: dict[str, dict] = {}
    try:
        url = f"{SAVANT_BASE}/leaderboard/statcast-venue?csv=true&year={season}"
        df  = normalize_savant_columns(savant_csv(url))
        for _, row in df.iterrows():
            park = norm(str(row.get("venue_name") or row.get("park") or ""))
            if not park:
                continue
            def pf_norm(col: str) -> float:
                v = safe_float(row.get(col), 1.0)
                # Savant park factors are often on a 100-point scale
                return round(v / 100 if v > 10 else v, 3)
            out[park] = {
                "run_factor": pf_norm("runs"),
                "hr_factor":  pf_norm("home_run"),
                "hit_factor": pf_norm("hits"),
            }
        log.info(f"  {len(out)} parks from Savant")
    except Exception as e:
        log.warning(f"Savant park factors failed: {e}")
    return out

# ── WEATHER ───────────────────────────────────────────────────────────────────

def fetch_weather(lat: float, lon: float) -> dict:
    """Fetch current weather at lat/lon via OpenWeatherMap free tier."""
    if not WEATHER_API_KEY:
        return {}
    try:
        r = requests.get(
            f"{WEATHER_BASE}/weather",
            params={"lat": lat, "lon": lon, "appid": WEATHER_API_KEY, "units": "imperial"},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        wind_deg  = (d.get("wind") or {}).get("deg", 0)
        wind_dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        return {
            "temp":       round(safe_float((d.get("main") or {}).get("temp", 70))),
            "wind_speed": round(safe_float((d.get("wind") or {}).get("speed", 0))),
            "wind_dir":   wind_dirs[round(wind_deg / 45) % 8],
            "humidity":   safe_int((d.get("main") or {}).get("humidity", 50)),
            "conditions": ((d.get("weather") or [{}])[0]).get("description", ""),
        }
    except Exception as e:
        log.debug(f"Weather fetch failed ({lat},{lon}): {e}")
        return {}

# ── ENRICHMENT ────────────────────────────────────────────────────────────────

def enrich_batters(
    batters: list[dict],
    split_lhp: dict, split_rhp: dict,
    sprint: dict[str, float],
    savant_bat: dict[str, dict],
    lineup_map: dict[int, int],
    season: str,
) -> list[dict]:
    """Attach L/R splits, Statcast, sprint speed, lineup pos, and last-14 form."""
    log.info("Enriching batters …")
    # Fetch recent form only for players in today's lineup (targeted API calls)
    form_cache: dict[int, dict] = {}
    lineup_ids = [b["mlb_id"] for b in batters if b.get("mlb_id") and b["mlb_id"] in lineup_map]
    for bid in lineup_ids[:80]:      # cap at 80 to stay under 2-3 min runtime
        form_cache[bid] = fetch_batter_last14(bid, season)
        time.sleep(0.05)

    for b in batters:
        pid = b.get("mlb_id", 0)
        nk  = norm(b.get("name", ""))
        # L/R splits
        if pid in split_lhp:
            sp = split_lhp[pid]
            b.update({
                "avg_vs_lhp": sp["avg"], "obp_vs_lhp": sp["obp"],
                "slg_vs_lhp": sp["slg"], "ops_vs_lhp": sp["ops"],
                "hr_vs_lhp":  sp["hr"],
            })
        if pid in split_rhp:
            sp = split_rhp[pid]
            b.update({
                "avg_vs_rhp": sp["avg"], "obp_vs_rhp": sp["obp"],
                "slg_vs_rhp": sp["slg"], "ops_vs_rhp": sp["ops"],
                "hr_vs_rhp":  sp["hr"],
            })
        # Lineup position (0 = not in lineup today)
        b["lineup_pos"] = lineup_map.get(pid, 0)
        # Sprint speed
        b["sprint_speed"] = sprint.get(nk, 0.0)
        # Statcast
        sv = savant_bat.get(nk, {})
        b.update({k: v for k, v in sv.items() if v})
        # Last-14 day form
        if pid in form_cache:
            b.update(form_cache[pid])
    return batters


def enrich_pitchers(
    pitchers: list[dict],
    split_lhb: dict, split_rhb: dict,
    savant_pit: dict[str, dict],
    season: str,
    probable_ids: list[int],
) -> list[dict]:
    """Attach L/R splits, Statcast, and last-5-start form."""
    log.info("Enriching pitchers …")
    form_cache: dict[int, dict] = {}
    for pid in probable_ids:
        form_cache[pid] = fetch_pitcher_last5(pid, season)
        time.sleep(0.05)

    for p in pitchers:
        pid = p.get("mlb_id", 0)
        nk  = norm(p.get("name", ""))
        if pid in split_lhb:
            sp = split_lhb[pid]
            p.update({"era_vs_lhb": sp["era"], "k9_vs_lhb": sp["k9"], "whip_vs_lhb": sp["whip"]})
        if pid in split_rhb:
            sp = split_rhb[pid]
            p.update({"era_vs_rhb": sp["era"], "k9_vs_rhb": sp["k9"], "whip_vs_rhb": sp["whip"]})
        if pid in form_cache:
            p.update(form_cache[pid])
        sv = savant_pit.get(nk, {})
        p.update({k: v for k, v in sv.items() if v})
    return pitchers


def attach_pitcher_data_to_games(
    games: list[dict],
    pitchers: list[dict],
    prob_map: dict[str, dict],
) -> list[dict]:
    """Inject probable pitcher names and SP stats into each game record."""
    pitcher_map = {norm(p["name"]): p for p in pitchers}
    for g in games:
        for side, opp in [("home", "away"), ("away", "home")]:
            team_name = g.get(f"{side}Team", "").lower()
            team_words = team_name.split()
            pitcher_entry = None
            for key in prob_map:
                # Match by last word of team name or abbreviation
                if key in (team_name, team_words[-1] if team_words else ""):
                    pitcher_entry = prob_map[key]
                    break
            if pitcher_entry:
                name = pitcher_entry.get("name", "TBD")
                pid  = pitcher_entry.get("id", 0)
                g[f"{side}Pitcher"]   = name
                g[f"{side}PitcherId"] = pid
                sp = pitcher_map.get(norm(name), {})
                if sp:
                    g[f"{side}ERA"]  = sp.get("era", 0)
                    g[f"{side}K9"]   = sp.get("k9", 0)
                    g[f"{side}WHIP"] = sp.get("whip", 0)
    return games


def attach_park_data_to_games(games: list[dict], parks: dict[str, dict]) -> list[dict]:
    """Fuzzy-match park factors from Savant into each game using home team."""
    for g in games:
        team_abbr = ""
        ht = g.get("homeTeam", "").upper().split()
        # Try to get last word as city/mascot clue; check TEAM_PARK map
        for word in ht:
            for abbr, park_name in TEAM_PARK.items():
                if abbr in word:
                    team_abbr = abbr
                    break
        park_name = TEAM_PARK.get(team_abbr, "")
        pf = parks.get(park_name, {}) if park_name else {}
        g["park_run_factor"] = pf.get("run_factor", 1.0)
        g["park_hr_factor"]  = pf.get("hr_factor",  1.0)
        g["park_hit_factor"] = pf.get("hit_factor",  1.0)
        g["park_name"]       = park_name
    return games


def attach_weather_to_games(games: list[dict]) -> list[dict]:
    """Fetch game-time weather for outdoor ballparks."""
    if not WEATHER_API_KEY:
        return games
    indoor = {"tropicana field", "rogers centre", "chase field", "minute maid park", "american family field", "globe life field"}
    for g in games:
        park = (g.get("park_name") or "").lower()
        if park in indoor:
            g["weather"] = {"conditions": "Dome"}
            continue
        coords = PARK_COORDS.get(park)
        if coords:
            g["weather"] = fetch_weather(*coords)
            time.sleep(0.2)
    return games


# ── STATS-BASED PROJECTION ────────────────────────────────────────────────────

def enrich_props_with_projections(
    props: list[dict],
    batters: list[dict],
    pitchers: list[dict],
    games: list[dict],
) -> list[dict]:
    """
    Replace prop.projection (previously = market line) with a stats-based
    projection for each prop type, then recalculate edge using blend of
    stats edge + odds edge.

    Algorithm summary
    ─────────────────
    Strikeouts (pitcher):
      proj_K = k9_eff / 9 × exp_IP × opp_k_adj
      where k9_eff = season_k9 × 0.4 + k9_last5 × 0.6 (if available)
      opp_k_adj = opponent_team_k_pct / league_avg_k_pct (default 1.0)

    Hits (batter):
      proj_H = avg_vs_handedness × exp_AB × park_hit_factor
      exp_AB from lineup position (leadoff ≈ 4.2, cleanup ≈ 3.8, 8-hole ≈ 3.2)
      recency_weight blends season avg vs. last-14 avg (40/60 if last14 available)

    Home Runs (batter):
      proj_HR = (hr/ab) × barrel_factor × exp_AB × park_hr_factor
      barrel_factor = player_barrel_rate / 0.083 (league avg)

    Total Bases (batter):
      proj_TB = slg_vs_handedness × exp_AB × park_hit_factor

    RBI / Runs:
      context-adjusted OPS proxy × lineup-position factor

    Edge blend:
      final_edge = odds_edge × 0.5 + stats_edge × 0.5
      stats_edge = (proj - line) / line × 25  (scaled to edge % space)
    """
    log.info("Enriching props with stats projections …")
    LEAGUE_AVG_K_PCT = 0.227   # ~22.7% league K rate
    LEAGUE_AVG_BARREL = 0.083  # ~8.3% barrel rate

    batter_map  = {norm(b["name"]): b for b in batters}
    pitcher_map = {norm(p["name"]): p for p in pitchers}
    game_map    = {g.get("_oddsId", ""): g for g in games}

    for prop in props:
        pname  = norm(prop.get("player", ""))
        ptype  = prop.get("propType", "")
        line   = safe_float(prop.get("line"))
        game   = game_map.get(prop.get("_evId", ""), {})
        park_hit = game.get("park_hit_factor", 1.0) or 1.0
        park_hr  = game.get("park_hr_factor",  1.0) or 1.0
        weather  = game.get("weather", {})
        wind_sp  = weather.get("wind_speed", 0)
        wind_dir = weather.get("wind_dir", "")
        temp     = weather.get("temp", 72)
        proj     = None

        # ── PITCHER STRIKEOUTS ──────────────────────────────────────────
        if ptype == "pitcher_strikeouts":
            p = pitcher_map.get(pname)
            if p and p.get("k9", 0) > 0:
                k9_season = p["k9"]
                k9_last5  = p.get("k9_last5", 0)
                k9_eff    = k9_season * 0.4 + k9_last5 * 0.6 if k9_last5 else k9_season
                # Whiff rate adjustment (if available; high whiff → bonus)
                whiff_adj = 1.0
                if p.get("whiff_rate"):
                    league_avg_whiff = 0.248
                    whiff_adj = p["whiff_rate"] / league_avg_whiff
                    whiff_adj = max(0.85, min(1.20, whiff_adj))  # cap at ±20%
                # Expected innings (use last-5 avg or default 5.5)
                exp_ip = p.get("ip_per_start_last5", 5.5) or 5.5
                # Opponent team K rate adjustment
                opp_abbr = (game.get("awayTeam", "") if game.get("homePitcher", "") == prop.get("player", "")
                            else game.get("homeTeam", ""))
                # (team stats not in scope here yet; would require teams param)
                opp_k_adj = 1.0
                proj = round(k9_eff / 9 * exp_ip * whiff_adj * opp_k_adj, 1)

        # ── BATTER HITS ─────────────────────────────────────────────────
        elif ptype == "batter_hits":
            b = batter_map.get(pname)
            if b and b.get("avg", 0) > 0:
                # Identify opposing pitcher and their handedness
                home_sp_name = norm(game.get("homePitcher", ""))
                away_sp_name = norm(game.get("awayPitcher", ""))
                # Batter team — find which side they're on
                home_team = game.get("homeTeam", "").lower()
                batter_team = (b.get("team") or "").lower()
                if batter_team in home_team or home_team.endswith(batter_team):
                    opp_pitcher = pitcher_map.get(away_sp_name, {})
                else:
                    opp_pitcher = pitcher_map.get(home_sp_name, {})
                throws = opp_pitcher.get("throws", "R") if opp_pitcher else "R"
                # Select appropriate split avg
                if throws == "L":
                    avg_base = b.get("avg_vs_lhp") or b.get("avg", 0)
                else:
                    avg_base = b.get("avg_vs_rhp") or b.get("avg", 0)
                # Recency blend: season 40% / last14 60% if form data available
                avg_last14 = b.get("avg_last14", 0)
                avg_eff    = avg_base * 0.4 + avg_last14 * 0.6 if avg_last14 else avg_base
                # Career matchup adjustment (if available and meaningful)
                career_avg = b.get("career_avg", 0)
                career_ab  = b.get("career_ab", 0)
                if career_ab >= 10 and career_avg > 0:
                    avg_eff = avg_eff * 0.7 + career_avg * 0.3
                # Expected ABs from lineup position
                pos    = b.get("lineup_pos") or 5
                exp_ab = max(4.3 - (pos - 1) * 0.12, 3.1)
                # xBA adjustment (Statcast signal)
                xba = b.get("xba", 0)
                xba_adj = 1.0
                if xba and avg_eff:
                    xba_adj = max(0.90, min(1.10, (xba / avg_eff) ** 0.3))
                proj = round(avg_eff * exp_ab * park_hit * xba_adj, 2)

        # ── BATTER HOME RUNS ────────────────────────────────────────────
        elif ptype == "batter_home_runs":
            b = batter_map.get(pname)
            if b and b.get("hr", 0) > 0:
                hr_rate  = b["hr"] / max(b.get("ab", 1), 1)
                barrel   = b.get("barrel_rate", 0)
                # Barrel rate: strongest HR predictor; normalize to league avg
                barrel_factor = (barrel / LEAGUE_AVG_BARREL) if barrel else 1.0
                barrel_factor = max(0.5, min(2.5, barrel_factor))  # cap outliers
                # L/R split HR rate adjustment
                home_sp_name = norm(game.get("homePitcher", ""))
                away_sp_name = norm(game.get("awayPitcher", ""))
                batter_team  = (b.get("team") or "").lower()
                home_team    = game.get("homeTeam", "").lower()
                if batter_team in home_team or home_team.endswith(batter_team):
                    opp_pitcher = pitcher_map.get(away_sp_name, {})
                else:
                    opp_pitcher = pitcher_map.get(home_sp_name, {})
                throws = opp_pitcher.get("throws", "R") if opp_pitcher else "R"
                hr_split_rate = hr_rate
                if throws == "L" and b.get("hr_vs_lhp") and b.get("ab", 0):
                    lhp_ab = b.get("avg_vs_lhp", 0) and b.get("ab") or 0  # estimate
                    # Use season ratio if split available
                    if b.get("hr_vs_lhp", 0):
                        hr_split_rate = b["hr_vs_lhp"] / max(b.get("ab", 1) * 0.35, 1)
                elif throws == "R" and b.get("hr_vs_rhp"):
                    if b.get("hr_vs_rhp", 0):
                        hr_split_rate = b["hr_vs_rhp"] / max(b.get("ab", 1) * 0.65, 1)
                # Wind bonus for outdoor parks
                wind_factor = 1.0
                if wind_sp >= 10 and wind_dir in ("OUT", "SW", "S", "SE"):
                    wind_factor = 1.0 + min(wind_sp * 0.008, 0.12)  # up to +12%
                elif wind_sp >= 10 and wind_dir in ("IN", "NE", "N", "NW"):
                    wind_factor = 1.0 - min(wind_sp * 0.006, 0.09)  # down to -9%
                # Cold suppression
                temp_factor = 1.0 if temp >= 60 else max(0.88, 1.0 - (60 - temp) * 0.006)
                exp_ab = 3.7
                proj   = round(hr_split_rate * barrel_factor * exp_ab * park_hr * wind_factor * temp_factor, 3)

        # ── BATTER TOTAL BASES ──────────────────────────────────────────
        elif ptype == "batter_total_bases":
            b = batter_map.get(pname)
            if b and b.get("slg", 0) > 0:
                home_sp_name = norm(game.get("homePitcher", ""))
                away_sp_name = norm(game.get("awayPitcher", ""))
                batter_team  = (b.get("team") or "").lower()
                home_team    = game.get("homeTeam", "").lower()
                if batter_team in home_team or home_team.endswith(batter_team):
                    opp_pitcher = pitcher_map.get(away_sp_name, {})
                else:
                    opp_pitcher = pitcher_map.get(home_sp_name, {})
                throws = opp_pitcher.get("throws", "R") if opp_pitcher else "R"
                slg_base = (b.get("slg_vs_lhp") if throws == "L" else b.get("slg_vs_rhp")) or b.get("slg", 0)
                slg_last14 = b.get("slg_last14", 0)
                slg_eff    = slg_base * 0.4 + slg_last14 * 0.6 if slg_last14 else slg_base
                pos        = b.get("lineup_pos") or 5
                exp_ab     = max(4.3 - (pos - 1) * 0.12, 3.1)
                proj       = round(slg_eff * exp_ab * park_hit, 2)

        # ── RBI / RUNS ──────────────────────────────────────────────────
        elif ptype in ("batter_rbis", "batter_runs_scored"):
            b = batter_map.get(pname)
            if b and b.get("ops", 0) > 0:
                ops = b["ops"]
                pos = b.get("lineup_pos") or 5
                # Lineup position factor: leadoff scores more runs; cleanup drives in more
                if ptype == "batter_runs_scored":
                    pos_factor = 1.20 if pos <= 2 else 1.05 if pos <= 4 else 0.85
                else:
                    pos_factor = 0.85 if pos <= 2 else 1.15 if pos in (3, 4, 5) else 0.90
                proj = round(ops * 0.48 * pos_factor, 2)

        # ── Apply stats-based projection and recalculate edge ───────────
        if proj is not None and proj > 0 and line > 0:
            prop["projection"] = proj
            # Stats edge: how far projection is from market line, scaled
            stats_edge = round((proj - line) / max(line, 0.5) * 25, 1)
            odds_edge  = prop.get("edge", 0)
            # Blend 50/50; clamp to reasonable range
            blended = round((odds_edge + stats_edge) / 2, 1)
            prop["edge"]    = blended
            prop["tier"]    = tier_from_edge(blended)
            prop["stats_edge"] = stats_edge   # preserve for debugging
            prop["odds_edge"]  = odds_edge
        elif proj is not None:
            prop["projection"] = proj

    return props

# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="MLB Edge Data Pull v2.0 — generates mlb_edge_data.json"
    )
    ap.add_argument("--date",       default=None, help="Pull date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--season",     default=None, help="MLB season year (default: current year)")
    ap.add_argument("--out",        default="mlb_edge_data.json", help="Output file path")
    ap.add_argument("--dry-run",    action="store_true", help="Print counts only, no file write")
    ap.add_argument("--verbose",    action="store_true", help="Debug-level logging")
    ap.add_argument("--no-props",   action="store_true", help="Skip props fetch (game lines only)")
    ap.add_argument("--no-stats",   action="store_true", help="Skip all MLB Stats API calls")
    ap.add_argument("--no-savant",  action="store_true", help="Skip Baseball Savant pulls")
    ap.add_argument("--no-weather", action="store_true", help="Skip OpenWeatherMap call")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("━━━ MLB Edge Data Pull v2.0 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    now      = datetime.now(timezone.utc)
    date_str = args.date   or now.strftime("%Y-%m-%d")
    season   = args.season or str(now.year)
    log.info(f"Date: {date_str}   Season: {season}")

    # ── Step 1: Game lines ─────────────────────────────────────────────
    log.info("── Step 1: Game lines (Odds API) ─────────────────────────")
    games     = fetch_game_lines(DEFAULT_BOOKS, DEFAULT_REGION)
    event_ids = [g["_oddsId"] for g in games if g.get("_oddsId")]

    # ── Step 2: Player props ───────────────────────────────────────────
    log.info("── Step 2: Player props (Odds API) ───────────────────────")
    props = [] if args.no_props else fetch_props(event_ids, DEFAULT_BOOKS, DEFAULT_REGION)

    batters = pitchers = []
    teams   = parks    = {}
    injuries = []

    if not args.no_stats:
        # ── Step 3: MLB Stats API ──────────────────────────────────────
        log.info("── Step 3: MLB Stats API ─────────────────────────────────")
        batters  = fetch_mlb_season_batting(season)
        pitchers = fetch_mlb_season_pitching(season)

        log.info("── Step 4: L/R splits (MLB Stats API) ───────────────────")
        split_lhp, split_rhp = fetch_batting_splits(season)
        split_lhb, split_rhb = fetch_pitching_splits(season)

        log.info("── Step 5: Lineups + probable pitchers (MLB Stats API) ───")
        lineup_map = fetch_todays_lineups(date_str)
        prob_map   = fetch_probable_pitchers(date_str)
        probable_ids = [v["id"] for v in prob_map.values() if v.get("id")]

        log.info("── Step 6: Team stats (MLB Stats API) ───────────────────")
        teams = fetch_team_stats(season)

        log.info("── Step 7: Injuries (MLB Stats API) ─────────────────────")
        injuries = fetch_injuries(season)

        # ── Step 8: Baseball Savant ────────────────────────────────────
        savant_bat = savant_pit = sprint = parks = {}
        if not args.no_savant:
            log.info("── Step 8: Baseball Savant (Statcast) ───────────────────")
            savant_bat = fetch_savant_batter_statcast(season)
            savant_pit = fetch_savant_pitcher_statcast(season)
            sprint     = fetch_savant_sprint_speed(season)
            parks      = fetch_savant_park_factors(season)
        else:
            log.info("── Step 8: Baseball Savant skipped (--no-savant) ────────")

        # ── Step 9: Enrich all records ─────────────────────────────────
        log.info("── Step 9: Enriching records ─────────────────────────────")
        batters  = enrich_batters(batters, split_lhp, split_rhp, sprint, savant_bat, lineup_map, season)
        pitchers = enrich_pitchers(pitchers, split_lhb, split_rhb, savant_pit, season, probable_ids)
        games    = attach_pitcher_data_to_games(games, pitchers, prob_map)
        games    = attach_park_data_to_games(games, parks)

        # ── Step 10: Weather (optional) ────────────────────────────────
        if not args.no_weather and WEATHER_API_KEY:
            log.info("── Step 10: Weather (OpenWeatherMap) ────────────────────")
            games = attach_weather_to_games(games)
        else:
            log.info("── Step 10: Weather skipped ──────────────────────────────")

        # ── Step 11: Stats-based projections for props ─────────────────
        if props:
            log.info("── Step 11: Enriching props with stats projections ───────")
            props = enrich_props_with_projections(props, batters, pitchers, games)

    # ── Assemble output ────────────────────────────────────────────────
    elite  = [p for p in props if p.get("tier") == "ELITE"]
    strong = [p for p in props if p.get("tier") == "STRONG"]
    lean   = [p for p in props if p.get("tier") == "LEAN"]

    output = {
        "generated_at": now.isoformat(),
        "pull_date":    date_str,
        "version":      "mlb_edge_data_pull/v2.0",
        "games":        games,
        "props":        props,
        "batters":      batters,
        "pitchers":     pitchers,
        "teams":        teams,
        "parks":        parks,
        "injuries":     injuries,
        "_counts": {
            "games":    len(games),
            "props":    len(props),
            "batters":  len(batters),
            "pitchers": len(pitchers),
            "teams":    len(teams),
            "parks":    len(parks),
            "injuries": len(injuries),
            "elite_props":  len(elite),
            "strong_props": len(strong),
            "lean_props":   len(lean),
        },
    }

    if args.dry_run:
        log.info("Dry run — no file written.")
        print(json.dumps(output["_counts"], indent=2))
        return

    out_path = Path(args.out)
    out_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    log.info(f"✅  Wrote {out_path}  ({size_kb:.1f} KB)")
    log.info(f"    games={len(games)}  props={len(props)}")
    log.info(f"    batters={len(batters)}  pitchers={len(pitchers)}  teams={len(teams)}")
    log.info(f"    ELITE={len(elite)}  STRONG={len(strong)}  LEAN={len(lean)}")
    log.info("━━━ Done ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    main()
