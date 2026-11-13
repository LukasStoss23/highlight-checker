"""
"Sehenswert"-Score (0–100): Wie viel Spaß macht dieses Spiel beim Nachschauen?

Grundlage (Recherche)
---------------------
* Game Excitement Index (Brian Burke / Inpredictable / FiveThirtyEight / Yale):
  Summe der absoluten Änderungen der Siegwahrscheinlichkeit über alle Spielzüge,
  normiert auf 48 Minuten, damit Verlängerungen nicht automatisch gewinnen.
* Inpredictable (2026): KL-Divergenz statt |Δp| – gewichtet Schwankungen in der
  Schlussphase / aus extremen Lagen viel stärker (Buzzer-Beater ≫ Q2-Korb).
* Ely/Frankel/Kamenica "Suspense and Surprise" + Kaplan (NBA-Quoten):
  Spannung (Ungewissheit vor dem Ausgang) erhöht Zuschauerzahlen messbar.
* Neuron-Studie (Princeton): Überraschungen und Führungswechsel machen Spiele
  einprägsam; Blowouts nicht.
* Fan-Umfrage Playoffs 2025: Upsets ≈ knappe Schlussphasen ≈ Star-Leistungen.

Komponenten (je als Perzentil gegenüber echten NBA-Spielen, 0–1)
-----------------------------------------------------------------
  gei        Excitement Index (Summe |Δp|, auf 48 min normiert)        26 %
  clutch     KL-Divergenz in den letzten 5 Min. + OT, plus Ungewissheit
             in den letzten 2 Minuten (Suspense)                       28 %
  lead       Führungswechsel + Gleichstände                            10 %
  comeback   größter Rückstand, den der Sieger aufgeholt hat           12 %
  upset      Außenseitersieg (Siegchance vor Spielbeginn)               8 %
  star       Top-Punkte / Triple-Double                                16 %
  + Bonus für Verlängerung(en)

Die Perzentil-Stützstellen unten wurden an echten Spielen kalibriert
(calibrate_watchability.py).
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from typing import Any, Dict, List, Optional

# Perzentil-Stützstellen: Rohwert -> Anteil der Spiele mit kleinerem Wert.
# (p0, p10, p25, p50, p75, p90, p97, p100) – per calibrate_watchability.py ermittelt
# an 389 Spielen der Saison 2025-26 (jeder 3. Tag, 25.10.2025–12.04.2026).
# Ergebnis: Median-Score 46, ~20 % der Spiele erreichen >= 70.
_Q = [0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 0.97, 1.0]
CALIBRATION: Dict[str, List[float]] = {
    "gei":      [0.0, 2.843, 5.281, 8.585, 11.466, 13.657, 15.553, 17.861],
    "clutch":   [0.0, 0.001, 0.003, 0.259, 1.34, 2.606, 6.023, 20.796],
    "lead":     [0.0, 1.0, 5.0, 11.0, 18.0, 26.0, 33.72, 55.0],
    "comeback": [0.0, 0.0, 3.0, 6.0, 10.0, 14.0, 18.0, 25.0],
    "upset":    [0.0, 0.0, 0.0, 0.0, 0.043, 0.17, 0.298, 0.439],
    "star":     [0.0, 23.0, 27.0, 30.0, 35.0, 40.2, 45.0, 55.0],
}
WEIGHTS = {"gei": 0.26, "clutch": 0.28, "lead": 0.10, "comeback": 0.12, "upset": 0.08, "star": 0.16}
OT_BONUS = 6.0          # pro Verlängerung, max. 2x
TRIPLE_DOUBLE_BONUS = 3.0
THRESHOLD = 70          # ab hier: Badge "Sehenswert"
MUST_WATCH = 85         # ab hier: "Muss man sehen"

REG_SECONDS = 48 * 60


def _percentile(name: str, value: float) -> float:
    xs = CALIBRATION[name]
    if value <= xs[0]:
        return 0.0
    if value >= xs[-1]:
        return 1.0
    i = bisect_right(xs, value) - 1
    # gleiche Stützstellen (z.B. upset 0,0,0,0) -> obersten Rang dieses Werts nehmen
    while i + 1 < len(xs) and xs[i + 1] == xs[i]:
        i += 1
    x0, x1 = xs[i], xs[i + 1]
    q0, q1 = _Q[i], _Q[i + 1]
    return q0 + (q1 - q0) * ((value - x0) / (x1 - x0) if x1 > x0 else 0.0)


_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d+(?:\.\d+)?)$")


def _clock_seconds(txt: str) -> float:
    m = _CLOCK_RE.match((txt or "").strip())
    if not m:
        return 0.0
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def _elapsed(period: int, clock: float) -> float:
    if period <= 4:
        return (period - 1) * 720 + (720 - clock)
    return REG_SECONDS + (period - 5) * 300 + (300 - clock)


def _kl(p: float, q: float) -> float:
    """KL-Divergenz zweier Bernoulli-Verteilungen in Bit (Inpredictable)."""
    eps = 1e-3
    p = min(max(p, eps), 1 - eps)
    q = min(max(q, eps), 1 - eps)
    return p * math.log2(p / q) + (1 - p) * math.log2((1 - p) / (1 - q))


def components(summary: Dict[str, Any], players: List[Dict[str, int]]) -> Optional[Dict[str, float]]:
    """Rohwerte aus einer ESPN-Summary (nur für beendete Spiele)."""
    plays = summary.get("plays") or []
    if not plays:
        return None

    timeline = []  # (elapsed, period, home, away, play_id)
    for pl in plays:
        per = int((pl.get("period") or {}).get("number") or 0)
        if per <= 0:
            continue
        t = _elapsed(per, _clock_seconds((pl.get("clock") or {}).get("displayValue", "")))
        timeline.append((t, per, int(pl.get("homeScore") or 0), int(pl.get("awayScore") or 0), str(pl.get("id"))))
    if not timeline:
        return None

    total_t = max(timeline[-1][0], REG_SECONDS)
    last_period = max(p for _, p, *_ in timeline)
    final_h, final_a = timeline[-1][2], timeline[-1][3]
    home_won = final_h > final_a

    # ── Führungswechsel, Gleichstände, Comeback ──
    lead_changes = ties = 0
    prev_sign = last_leader = 0
    max_home_lead = max_away_lead = 0
    for _, _, h, a, _ in timeline:
        d = h - a
        sign = (d > 0) - (d < 0)
        if sign == 0 and prev_sign != 0:
            ties += 1
        if sign != 0:
            # auch ein Wechsel "über" ein Unentschieden zählt als Führungswechsel
            if last_leader not in (0, sign):
                lead_changes += 1
            last_leader = sign
        prev_sign = sign
        max_home_lead = max(max_home_lead, d)
        max_away_lead = max(max_away_lead, -d)
    comeback = max_away_lead if home_won else max_home_lead

    # ── Siegwahrscheinlichkeit ──
    wp = summary.get("winprobability") or []
    gei = clutch = upset = None
    if len(wp) >= 20:
        t_by_id = {pid: t for t, _, _, _, pid in timeline}
        series = []
        last_t = 0.0
        for w in wp:
            t = t_by_id.get(str(w.get("playId")), last_t)
            last_t = t
            series.append((t, float(w.get("homeWinPercentage") or 0.0)))
        abs_sum = sum(abs(b[1] - a[1]) for a, b in zip(series, series[1:]))
        gei = abs_sum * REG_SECONDS / total_t

        crunch_start = REG_SECONDS - 300
        kl_late = sum(_kl(b[1], a[1]) for a, b in zip(series, series[1:]) if b[0] >= crunch_start)
        # Suspense: wie offen war das Spiel in den letzten 2 Regulär-Minuten?
        late = [p for t, p in series if REG_SECONDS - 120 <= t < REG_SECONDS]
        suspense = max((1 - abs(2 * p - 1) for p in late), default=0.0)
        clutch = kl_late + 0.6 * suspense + (0.4 if last_period > 4 else 0.0)

        pre = series[0][1]  # Siegchance Heimteam zu Spielbeginn
        winner_pre = pre if home_won else 1 - pre
        upset = max(0.0, 0.5 - winner_pre)

    max_pts = max((p["pts"] for p in players), default=0)
    triple = any(p["pts"] >= 10 and p["reb"] >= 10 and p["ast"] >= 10 for p in players)

    return {
        "gei": gei, "clutch": clutch, "upset": upset,
        "lead": float(lead_changes + ties), "comeback": float(comeback),
        "star": float(max_pts), "tripleDouble": 1.0 if triple else 0.0,
        "overtimes": float(max(0, last_period - 4)),
    }


def score(c: Optional[Dict[str, float]]) -> Optional[int]:
    if not c:
        return None
    total = wsum = 0.0
    for name, w in WEIGHTS.items():
        v = c.get(name)
        if v is None:  # z.B. keine Siegwahrscheinlichkeit vorhanden
            continue
        total += w * _percentile(name, v)
        wsum += w
    if wsum == 0:
        return None
    s = 100.0 * total / wsum
    s += OT_BONUS * min(c.get("overtimes", 0), 2)
    s += TRIPLE_DOUBLE_BONUS * c.get("tripleDouble", 0)
    return int(round(max(0.0, min(100.0, s))))
