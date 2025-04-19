#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NBA‑App‑Backend  |  stabile Player‑Stats‑Erkennung + Replay‑Link‑Scraper
-----------------------------------------------------------------------
• Holt Spiel‑Daten von ESPN
• Scrapt watchreplay.net nach erstem Link für das jeweilige Home‑Team
  (Slug = letzter Wortteil des Team‑DisplayNames, z.B. “mavericks”)
• Alle ursprünglichen Stat‑, Badge‑ und Serien‑Routinen bleiben erhalten
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import tz
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# ────────────────────────────────────────────────────────────────────────────
#  Replay‑Link‑Scraper via Requests + BeautifulSoup
# ────────────────────────────────────────────────────────────────────────────

BASE_REPLAY_URL = "https://watchreplay.net"

def find_replay_link_for_team(team_full_name: str) -> str | None:
    """
    Nimmt z.B. "Dallas Mavericks" und sucht auf der Homepage den
    ersten <a href>, dessen URL den Team‑Slug (z.B. "mavericks") enthält.
    """
    # Slug: letztes Wort, lowercased
    slug = team_full_name.strip().lower().split()[-1]
    try:
        resp = requests.get(BASE_REPLAY_URL + "/", timeout=10)
        resp.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if slug in href.lower():
            # Absoluter Link
            return urljoin(BASE_REPLAY_URL, href)
    return None

# ────────────────────────────────────────────────────────────────────────────
#  ESPN‑Scoreboard & Stat‑Auswertung (unverändert)
# ────────────────────────────────────────────────────────────────────────────

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
ESPN_SUMMARY_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
)
VIENNA = tz.gettz("Europe/Vienna")

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)


def _to_int(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _debug_print_scoreboard(sb: Dict[str, Any]) -> None:
    print("\n=========  SCOREBOARD  (kurz) =========")
    for ev in sb.get("events", []):
        comp = ev["competitions"][0]
        home, away = comp["competitors"]
        if home["homeAway"] == "away":
            home, away = away, home
        state = comp["status"]["type"]["state"]
        h_pts = home.get("score") or "-"
        a_pts = away.get("score") or "-"
        series = comp.get("series", {}).get("summary", "")
        print(
            f"{ev['id']}: {away['team']['abbreviation']} @ {home['team']['abbreviation']} "
            f"{a_pts}-{h_pts} [{state}]  {series}"
        )


def _fetch_scoreboard(api_date: str) -> Dict[str, Any]:
    resp = requests.get(
        ESPN_SCOREBOARD_URL, params={"dates": api_date, "seasontype": 3}, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    _debug_print_scoreboard(data)
    return data


def _fetch_summary(game_id: str) -> Dict[str, Any]:
    resp = requests.get(ESPN_SUMMARY_URL, params={"event": game_id}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    box = data.get("boxscore", {})
    print(f"\n——  BOX‑SCORE {game_id}  —————————")
    print(json.dumps(box, indent=2, ensure_ascii=False))
    return data


def _parse_round_and_game(event: Dict[str, Any]) -> Tuple[str, str]:
    for note in event.get("notes", []):
        if note.get("type") == "event":
            parts = note.get("headline", "").split(" - ")
            if len(parts) >= 2:
                return parts[0], parts[1]
    return "", ""


def _lookup(names: List[str], wanted: str) -> int:
    wanted = wanted.lower()
    for idx, n in enumerate(names):
        if n.lower() in (
            wanted,
            {"pts": "points", "reb": "rebounds", "ast": "assists"}[wanted],
        ):
            return idx
    return -1


def _collect_players(boxscore: Dict[str, Any]) -> List[Dict[str, int]]:
    players: List[Dict[str, int]] = []
    for roster in boxscore.get("players", []):
        # Format A (neu)
        if (
            isinstance(roster, dict)
            and roster.get("statistics")
            and isinstance(roster["statistics"][0], dict)
            and roster["statistics"][0].get("athletes")
        ):
            block = roster["statistics"][0]
            cols = [c.upper() for c in block.get("names", [])]
            idx_pts, idx_reb, idx_ast = map(
                lambda k: _lookup(cols, k), ("pts", "reb", "ast")
            )
            for ath in block["athletes"]:
                raw = ath.get("stats", [])
                def _safe(i): return _to_int(raw[i]) if 0 <= i < len(raw) else 0
                players.append(
                    {
                        "name": ath.get("athlete", {}).get("displayName", "Unknown"),
                        "pts": _safe(idx_pts),
                        "reb": _safe(idx_reb),
                        "ast": _safe(idx_ast),
                    }
                )
        # Format B (klassisch)
        elif isinstance(roster, list):
            for ply in roster:
                vals = {"pts": 0, "reb": 0, "ast": 0}
                for cat in ply.get("statistics", []):
                    key = (cat.get("abbreviation") or cat.get("name", "")).upper()
                    if key in ("PTS", "POINTS"):
                        vals["pts"] = _to_int(cat.get("value"))
                    elif key in ("REB", "REBOUNDS"):
                        vals["reb"] = _to_int(cat.get("value"))
                    elif key in ("AST", "ASSISTS"):
                        vals["ast"] = _to_int(cat.get("value"))
                players.append(
                    {"name": ply.get("athlete", {}).get("displayName", "Unknown"), **vals}
                )
    return players


def _fallback_leaders(comp: Dict[str, Any]) -> List[Dict[str, int]]:
    mapping = {"PTS": "pts", "REB": "reb", "AST": "ast"}
    out: List[Dict[str, int]] = []
    for leader in comp.get("leaders", []):
        cat = mapping.get(leader.get("abbreviation"))
        if not cat:
            continue
        for l in leader.get("leaders", []):
            ath = l.get("athlete", {})
            entry = next((p for p in out if p["name"] == ath.get("displayName")), None)
            if not entry:
                entry = {"name": ath.get("displayName"), "pts": 0, "reb": 0, "ast": 0}
                out.append(entry)
            entry[cat] = _to_int(l.get("value"))
    return out


_SERIES_RE = re.compile(
    r"^(?P<team>.+?)\s+(?P<verb>leads|wins)\s+series\s+(?P<wins>\d+)-(?P<losses>\d+)",
    re.I,
)
_TIED_RE = re.compile(r"Series\s+(?:even|tied)\s+(?P<wins>\d+)-(?P<wins2>\d+)", re.I)


def _match_team_to_abbr(team_str: str, competitors: List[Dict[str, Any]]) -> str:
    for c in competitors:
        team = c["team"]
        if team_str.lower() in (
            team["displayName"].lower(),
            team["shortDisplayName"].lower(),
            team["name"].lower(),
            team["abbreviation"].lower(),
        ):
            return team["abbreviation"]
    return team_str


def _series_before_game(summary_txt: str, comp: Dict[str, Any]) -> str:
    competitors = comp["competitors"]
    winner_team = next((c for c in competitors if c.get("winner")), competitors[0])
    winner_abbr = winner_team["team"]["abbreviation"]
    loser_abbr = (
        competitors[0]["team"]["abbreviation"]
        if competitors[1] is winner_team
        else competitors[1]["team"]["abbreviation"]
    )

    m = _SERIES_RE.match(summary_txt)
    if m:
        leader_abbr = _match_team_to_abbr(m["team"], competitors)
        wins, losses = int(m["wins"]), int(m["losses"])
        if m["verb"].lower() == "wins":
            return f"{leader_abbr} leads series {wins-1}-{losses}"
        if leader_abbr == winner_abbr:
            return f"{leader_abbr} leads series {wins-1}-{losses}"
        return f"{leader_abbr} leads series {wins}-{losses-1}"

    m = _TIED_RE.match(summary_txt)
    if m:
        wins = int(m["wins"])
        return f"{loser_abbr} leads series {wins}-{wins-1}"

    return summary_txt


def _evaluate_games(sb: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for ev in sb.get("events", []):
        comp = ev["competitions"][0]
        if comp["status"]["type"].get("state", "").lower() != "post":
            continue

        home, away = comp["competitors"]
        if home["homeAway"] == "away":
            home, away = away, home

        # 1) Runde & Nummer
        round_txt, game_txt = _parse_round_and_game(ev)
        series_pre = _series_before_game(comp.get("series", {}).get("summary", ""), comp)

        # 2) GameType
        st = ev.get("season", {}).get("type", 1)
        game_type = {1: "Regular Season", 3: "Playoffs"}.get(st, "Play‑In")

        # 3) Boxscore & Players
        try:
            summ = _fetch_summary(str(ev["id"]))
        except Exception:
            summ = {}
        players = _collect_players(summ.get("boxscore", {})) or _fallback_leaders(comp)

        # 4) Badges
        badges: List[str] = []
        max_pts = max((p["pts"] for p in players), default=0)
        if max_pts >= 50:
            badges.append("pts50")
        elif max_pts >= 40:
            badges.append("pts40")
        elif max_pts >= 30:
            badges.append("pts30")
        if any(p["pts"] >= 10 and p["reb"] >= 10 and p["ast"] >= 10 for p in players):
            badges.append("tripleDouble")
        def q3tot(team):
            return sum(_to_int(ls.get("value")) for ls in team.get("linescores", [])[:3])
        if abs(q3tot(home) - q3tot(away)) <= 10:
            badges.append("close4")
        margin = abs(_to_int(home["score"]) - _to_int(away["score"]))
        if margin <= 7 or _to_int(comp["status"].get("period", 4)) > 4:
            badges.append("closeGame")
        if _to_int(comp["status"].get("period", 4)) > 4:
            badges.append("overtime")

        # 5) Replay‑Link für Home‑Team
        full_home = home["team"]["displayName"] or home["team"]["name"]
        replay_link = find_replay_link_for_team(full_home)

        out.append({
            "gameId":     ev["id"],
            "tipoffUTC":  ev["date"],
            "home":       home["team"]["abbreviation"],
            "away":       away["team"]["abbreviation"],
            "homeLogo":   home["team"]["logo"],
            "awayLogo":   away["team"]["logo"],
            "round":      round_txt,
            "gameNum":    game_txt,
            "gameType":   game_type,
            "seriesPre":  series_pre,
            "badges":     badges,
            "replayLink": replay_link,
        })

    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/games")
def api_games():
    date_param = request.args.get("date")
    if date_param:
        try:
            dt = _dt.datetime.strptime(date_param, "%Y-%m-%d").date()
            ret_date = date_param
        except ValueError:
            dt = None
            ret_date = date_param
        api_date = dt.strftime("%Y%m%d") if dt else ret_date.replace("-", "")
    else:
        today = _dt.datetime.now(VIENNA).date()
        ret_date = today.isoformat()
        api_date = today.strftime("%Y%m%d")

    sb    = _fetch_scoreboard(api_date)
    games = _evaluate_games(sb)
    return jsonify({"date": ret_date, "games": games})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀  NBA‑App‑Backend läuft auf Port {port}  (Debug aktiv)")
    app.run(debug=True, host="0.0.0.0", port=port)
