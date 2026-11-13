"""
Spieldaten für den Highlight Checker.

Quellen
-------
* nba.com/games?date=YYYY-MM-DD  -> Spielliste, Viertel-Ergebnisse und die
  Stream-Liste jedes Spiels (darin der "All Possessions"-Recap mit seiner
  MediaKind-Video-ID). Daraus bauen wir den Direktlink
  https://www.nba.com/game/<away>-vs-<home>-<gameId>?watchCondensedGame=true&streamId=<id>
* ESPN Scoreboard/Summary        -> Spieler-Statistiken (30/40/50 Punkte,
  Triple-Double) und Playoff-Runde/Serienstand.

Nichts, was an den Browser geht, enthält Ergebnisse oder Videolängen –
nur die Badges, die der Nutzer bewusst per Filter einblendet.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

import watchability

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

NBA_GAMES_URL = "https://www.nba.com/games"
NBA_GAME_URL = "https://www.nba.com/game/{away}-vs-{home}-{gid}"
NBA_LOGO_URL = "https://cdn.nba.com/logos/nba/{tid}/global/L/logo.svg"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# ESPN nutzt teils andere Kürzel als die NBA
_ESPN_TO_NBA = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA",
                "WSH": "WAS", "PHO": "PHX", "BKN": "BKN"}


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ────────────────────────────────────────────────────────────────────────────
#  nba.com
# ────────────────────────────────────────────────────────────────────────────

def fetch_nba_games(date: _dt.date) -> List[Dict[str, Any]]:
    resp = requests.get(NBA_GAMES_URL, params={"date": date.isoformat()},
                        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        raise RuntimeError("nba.com: keine Spieldaten auf der Seite gefunden")
    page = json.loads(m.group(1))["props"]["pageProps"]
    feed = page.get("gameCardFeed") or {}
    games = []
    for module in feed.get("modules", []):
        for card in module.get("cards", []):
            cd = card.get("cardData") or {}
            if cd.get("gameId"):
                games.append(cd)
    return games


def _find_stream(streams: List[Dict[str, Any]], franchise: str, title: str) -> Optional[Dict[str, Any]]:
    for s in streams:
        if s.get("type") == "vod" and s.get("mediaKindVideoId") and (
            s.get("videoFranchisesName") == franchise
            or (s.get("title") or "").strip().lower() == title
        ):
            return s
    return None


def _links_for(cd: Dict[str, Any]) -> Dict[str, Any]:
    away = cd["awayTeam"]["teamTricode"].lower()
    home = cd["homeTeam"]["teamTricode"].lower()
    base = NBA_GAME_URL.format(away=away, home=home, gid=cd["gameId"])
    streams = cd.get("streamOrder") or []
    ap = _find_stream(streams, "all-possessions-cg", "all possessions")
    cg = _find_stream(streams, "full-game-highlights", "condensed game")
    return {
        "gameUrl": base,
        "allPossessionsUrl": (f"{base}?watchCondensedGame=true&streamId={ap['mediaKindVideoId']}"
                              if ap else None),
        "condensedUrl": (f"{base}?watchCondensedGame=true&streamId={cg['mediaKindVideoId']}"
                         if cg else None),
        "watchUrl": f"{base}?watch",
    }


# ────────────────────────────────────────────────────────────────────────────
#  ESPN (Spieler-Stats, Playoff-Infos)
# ────────────────────────────────────────────────────────────────────────────

def _espn_abbr(team: Dict[str, Any]) -> str:
    a = (team.get("abbreviation") or "").upper()
    return _ESPN_TO_NBA.get(a, a)


def _lookup(names: List[str], wanted: str) -> int:
    alias = {"PTS": "POINTS", "REB": "REBOUNDS", "AST": "ASSISTS"}[wanted]
    for i, n in enumerate(names):
        if n.upper() in (wanted, alias):
            return i
    return -1


def _collect_players(boxscore: Dict[str, Any]) -> List[Dict[str, int]]:
    players: List[Dict[str, int]] = []
    for roster in boxscore.get("players", []):
        for block in roster.get("statistics", []) if isinstance(roster, dict) else []:
            if not isinstance(block, dict) or not block.get("athletes"):
                continue
            cols = [c.upper() for c in block.get("names", []) or block.get("labels", [])]
            ip, ir, ia = (_lookup(cols, k) for k in ("PTS", "REB", "AST"))
            for ath in block["athletes"]:
                raw = ath.get("stats", [])
                def g(i):
                    return _int(raw[i]) if 0 <= i < len(raw) else 0
                players.append({"pts": g(ip), "reb": g(ir), "ast": g(ia)})
    return players


def _fallback_leaders(comp: Dict[str, Any]) -> List[Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    mapping = {"PTS": "pts", "REB": "reb", "AST": "ast",
               "points": "pts", "rebounds": "reb", "assists": "ast"}
    for team in comp.get("competitors", []):
        for leader in team.get("leaders", []):
            cat = mapping.get(leader.get("abbreviation")) or mapping.get(leader.get("name"))
            if not cat:
                continue
            for l in leader.get("leaders", []):
                name = (l.get("athlete") or {}).get("displayName", "?")
                out.setdefault(name, {"pts": 0, "reb": 0, "ast": 0})[cat] = _int(l.get("value"))
    return list(out.values())


_SERIES_RE = re.compile(r"^(?P<team>.+?)\s+(?P<verb>leads|wins|won)\s+series\s+(?P<w>\d+)-(?P<l>\d+)", re.I)
_TIED_RE = re.compile(r"Series\s+(?:even|tied)\s+(?P<w>\d+)-(?P<w2>\d+)", re.I)


def _series_before_game(summary: str, comp: Dict[str, Any]) -> str:
    """ESPN liefert den Serienstand NACH dem Spiel – wir rechnen das Spiel heraus,
    damit nicht verraten wird, wer gewonnen hat."""
    if not summary:
        return ""
    cs = comp.get("competitors", [])
    if len(cs) != 2:
        return ""
    winner = next((c for c in cs if c.get("winner")), None)
    if not winner:
        return ""
    loser = cs[1] if cs[0] is winner else cs[0]
    wa, la = _espn_abbr(winner["team"]), _espn_abbr(loser["team"])
    m = _TIED_RE.search(summary)
    if m:  # nach dem Spiel ausgeglichen -> vorher führte der Verlierer
        w = int(m["w"])
        return f"{la} führt {w}-{w-1}"
    m = _SERIES_RE.match(summary)
    if m:
        hi, lo = int(m["w"]), int(m["l"])
        leader_is_winner = m["team"].strip().lower() in (
            (winner["team"].get(k) or "").lower()
            for k in ("displayName", "shortDisplayName", "name", "abbreviation", "location")
        )
        if leader_is_winner:
            hi -= 1
            leader, trailer = wa, la
        else:
            lo -= 1
            leader, trailer = la, wa
        if hi == lo:
            return f"Serie ausgeglichen {hi}-{lo}" if hi else "Spiel 1"
        if hi > lo:
            return f"{leader} führt {hi}-{lo}"
        return f"{trailer} führt {lo}-{hi}"
    return ""


def _espn_round(event: Dict[str, Any]) -> str:
    for note in event.get("competitions", [{}])[0].get("notes", []) + event.get("notes", []):
        txt = note.get("headline") or ""
        if txt:
            return txt
    return ""


def fetch_espn(date: _dt.date) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """{(away, home): {"players": [...], "round": str, "series": str}}"""
    resp = requests.get(ESPN_SCOREBOARD_URL, params={"dates": date.strftime("%Y%m%d")},
                        headers=HEADERS, timeout=15)
    resp.raise_for_status()
    events = resp.json().get("events", [])

    def one(ev):
        comp = ev["competitions"][0]
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        key = (_espn_abbr(teams["away"]["team"]), _espn_abbr(teams["home"]["team"]))
        players: List[Dict[str, int]] = []
        watch = None
        if comp["status"]["type"].get("state") == "post":
            summary: Dict[str, Any] = {}
            try:
                r = requests.get(ESPN_SUMMARY_URL, params={"event": ev["id"]},
                                 headers=HEADERS, timeout=20)
                r.raise_for_status()
                summary = r.json()
                players = _collect_players(summary.get("boxscore", {}))
            except Exception:
                players = []
            if not players:
                players = _fallback_leaders(comp)
            try:
                watch = watchability.score(watchability.components(summary, players))
            except Exception:
                watch = None
        series = _series_before_game((comp.get("series") or {}).get("summary", ""), comp)
        return key, {"players": players, "round": _espn_round(ev), "series": series, "watch": watch}

    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, events))


# ────────────────────────────────────────────────────────────────────────────
#  Zusammenführen
# ────────────────────────────────────────────────────────────────────────────

def _badges(cd: Dict[str, Any], players: List[Dict[str, int]]) -> List[str]:
    b: List[str] = []
    max_pts = max((p["pts"] for p in players), default=0)
    # Fallback: nba.com liefert pro Team den Top-Scorer
    for side in ("homeTeam", "awayTeam"):
        max_pts = max(max_pts, _int((cd[side].get("teamLeader") or {}).get("points")))
    if max_pts >= 50:
        b.append("pts50")
    elif max_pts >= 40:
        b.append("pts40")
    elif max_pts >= 30:
        b.append("pts30")
    if any(p["pts"] >= 10 and p["reb"] >= 10 and p["ast"] >= 10 for p in players):
        b.append("tripleDouble")

    home, away = cd["homeTeam"], cd["awayTeam"]
    hp = [_int(p.get("score")) for p in home.get("periods", [])]
    ap = [_int(p.get("score")) for p in away.get("periods", [])]
    period = _int(cd.get("period"))
    if len(hp) >= 3 and len(ap) >= 3 and abs(sum(hp[:3]) - sum(ap[:3])) <= 10:
        b.append("close4")
    margin = abs(_int(home.get("score")) - _int(away.get("score")))
    if margin <= 7 or period > 4:
        b.append("closeGame")
    if period > 4:
        b.append("overtime")
    return b


def build_games(date: _dt.date) -> List[Dict[str, Any]]:
    nba_games = fetch_nba_games(date)
    try:
        espn = fetch_espn(date)
    except Exception:
        espn = {}

    out = []
    for cd in nba_games:
        home, away = cd["homeTeam"], cd["awayTeam"]
        final = _int(cd.get("gameStatus")) == 3
        extra = espn.get((away["teamTricode"], home["teamTricode"]), {})
        links = _links_for(cd)
        badges = _badges(cd, extra.get("players", [])) if final else []
        watch = extra.get("watch") if final else None
        if watch is not None and watch >= watchability.THRESHOLD:
            badges.insert(0, "watch")
        out.append({
            "gameId": cd["gameId"],
            "tipoffUTC": cd.get("gameTimeUtc"),
            "final": final,
            "status": "final" if final else ("live" if _int(cd.get("gameStatus")) == 2 else "upcoming"),
            "seasonType": cd.get("seasonType") or "",
            "subtype": cd.get("gameSubtype") or "",
            "round": extra.get("round", ""),
            "series": extra.get("series", ""),
            "home": {"tricode": home["teamTricode"], "name": home.get("teamName", ""),
                     "logo": NBA_LOGO_URL.format(tid=home["teamId"])},
            "away": {"tricode": away["teamTricode"], "name": away.get("teamName", ""),
                     "logo": NBA_LOGO_URL.format(tid=away["teamId"])},
            "badges": badges,
            "watchScore": watch,
            **links,
        })
    return out


# ────────────────────────────────────────────────────────────────────────────
#  Cache
# ────────────────────────────────────────────────────────────────────────────

_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_lock = threading.Lock()


def get_games(date: _dt.date, today: _dt.date) -> List[Dict[str, Any]]:
    key = date.isoformat()
    age_days = (today - date).days
    # Ältere Tage ändern sich praktisch nicht mehr, frische Tage öfter neu laden
    # (All-Possessions-Videos erscheinen erst einige Stunden nach Spielende).
    ttl = 12 * 3600 if age_days >= 3 else 10 * 60
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    games = build_games(date)
    with _lock:
        _cache[key] = (now, games)
        if len(_cache) > 120:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:40]:
                _cache.pop(k, None)
    return games
