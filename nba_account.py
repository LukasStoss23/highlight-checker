"""
NBA-ID-Login prüfen (pro Benutzer, Zugangsdaten liegen in users.py).

"Login testen" meldet sich bei identity.nba.com an (derselbe Endpunkt wie die
nba.com-Anmeldeseite) und liest aus, ob League Pass aktiv ist.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import requests

import users

IDENTITY_URL = "https://identity.nba.com/api/v1/auth"
# öffentliche Client-ID der nba.com-Weboberfläche
IDENTITY_CLIENT_ID = "Ohp2l3Awgo1QfOWqlvxqcW-tw7i4EvdEQatAQTTjMTs"


def _subscriptions(data: Dict[str, Any]) -> List[str]:
    subs = data.get("subscriptions") or {}
    items = subs.get("AccountServiceMessage") if isinstance(subs, dict) else subs
    names = []
    for s in items or []:
        if isinstance(s, dict):
            n = s.get("displayName") or s.get("name") or s.get("serviceID")
            if n:
                names.append(str(n))
    return names


def _login(email: str, password: str) -> Dict[str, Any]:
    try:
        r = requests.post(
            IDENTITY_URL,
            json={"email": email, "password": password},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-client-platform": "web",
                "x-client-id": IDENTITY_CLIENT_ID,
                "Origin": "https://www.nba.com",
                "Referer": "https://www.nba.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            },
            timeout=20,
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.ok and body.get("status", "success") == "success":
            data = body.get("data") or {}
            user = data.get("user") or {}
            subs = _subscriptions(data)
            lp = any("league" in s.lower() or s.upper().startswith("LP") for s in subs)
            return {
                "ok": True,
                "name": " ".join(x for x in (user.get("givenName"), user.get("familyName")) if x),
                "subscriptions": subs,
                "leaguePass": lp,
                "message": "Login erfolgreich" + (" – League Pass aktiv" if lp else
                                                  " – kein League Pass gefunden"),
            }
        msg = ((body.get("data") or {}).get("message") if isinstance(body, dict) else None) \
            or f"NBA-Login fehlgeschlagen (HTTP {r.status_code})"
        return {"ok": False, "message": msg}
    except requests.RequestException as e:
        return {"ok": False, "message": f"NBA nicht erreichbar: {e.__class__.__name__}"}


def check_login(user_id: int) -> Dict[str, Any]:
    email, pw = users.nba_credentials(user_id)
    if not email or not pw:
        return {"ok": False, "message": "Bitte zuerst E-Mail und Passwort speichern."}
    result = _login(email, pw)
    result["checkedAt"] = int(time.time())
    users.nba_set_check(user_id, result)
    return result
