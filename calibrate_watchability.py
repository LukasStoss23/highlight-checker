#!/usr/bin/env python3
"""
Kalibriert die Perzentil-Stützstellen in watchability.CALIBRATION an echten Spielen.

    python calibrate_watchability.py 2025-11-01 2026-04-12 --every 4

Lädt jeden N-ten Tag die ESPN-Summaries, berechnet die Rohwerte und gibt
neue Stützstellen + die Verteilung des Gesamtscores aus.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor

import requests

import nba_data
import watchability as W


def games_for(day: dt.date):
    r = requests.get(nba_data.ESPN_SCOREBOARD_URL, params={"dates": day.strftime("%Y%m%d")},
                     headers=nba_data.HEADERS, timeout=20)
    r.raise_for_status()
    out = []
    for ev in r.json().get("events", []):
        if ev["competitions"][0]["status"]["type"].get("state") == "post":
            out.append((ev["id"], ev.get("shortName", "")))
    return out


def comps(event):
    eid, name = event
    try:
        r = requests.get(nba_data.ESPN_SUMMARY_URL, params={"event": eid},
                         headers=nba_data.HEADERS, timeout=30)
        r.raise_for_status()
        s = r.json()
        players = nba_data._collect_players(s.get("boxscore", {}))
        return name, W.components(s, players)
    except Exception:
        return name, None


def quantiles(vals, qs):
    vals = sorted(vals)
    out = []
    for q in qs:
        k = q * (len(vals) - 1)
        lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
        out.append(round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 3))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--every", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    d, end = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)
    days = []
    while d <= end:
        days.append(d)
        d += dt.timedelta(days=a.every)
    with ThreadPoolExecutor(8) as pool:
        events = [e for lst in pool.map(games_for, days) for e in lst]
        results = [(n, c) for n, c in pool.map(comps, events) if c]
    print(f"{len(results)} Spiele ausgewertet")

    cal = {}
    for k in W.CALIBRATION:
        vals = [c[k] for _, c in results if c.get(k) is not None]
        q = quantiles(vals, W._Q)
        q[0] = 0.0
        cal[k] = q
        print(f'    "{k}": {q},')
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"calibration": cal, "games": results}, f)

    W.CALIBRATION.update(cal)
    scores = sorted(((W.score(c), n) for n, c in results), reverse=True)
    s_only = [s for s, _ in scores]
    print("Score-Perzentile p10/25/50/75/90:", quantiles(s_only, [.1, .25, .5, .75, .9]))
    print(f"Anteil >= {W.THRESHOLD}: {sum(s >= W.THRESHOLD for s in s_only) / len(s_only):.1%}")
    print("Top 10:", scores[:10])
    print("Flop 5:", scores[-5:])


if __name__ == "__main__":
    main()
