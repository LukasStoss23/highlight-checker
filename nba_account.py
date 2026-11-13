"""
NBA-ID-Zugangsdaten speichern und prüfen.

* Das Passwort wird mit Fernet (AES) verschlüsselt in data/settings.json
  abgelegt, der Schlüssel liegt in data/secret.key (Dateirechte 600).
* Das Passwort verlässt den Server nie wieder Richtung Browser.
* "Login testen" meldet sich bei identity.nba.com an (derselbe Endpunkt wie
  die nba.com-Anmeldeseite) und liest aus, ob League Pass aktiv ist.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(os.environ.get("HC_DATA_DIR", Path(__file__).with_name("data")))
SETTINGS_FILE = DATA_DIR / "settings.json"
KEY_FILE = DATA_DIR / "secret.key"

IDENTITY_URL = "https://identity.nba.com/api/v1/auth"
# öffentliche Client-ID der nba.com-Weboberfläche
IDENTITY_CLIENT_ID = "Ohp2l3Awgo1QfOWqlvxqcW-tw7i4EvdEQatAQTTjMTs"

_lock = threading.Lock()


def _secure_write(path: Path, data: bytes) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _fernet() -> Fernet:
    if not KEY_FILE.exists():
        _secure_write(KEY_FILE, Fernet.generate_key())
    return Fernet(KEY_FILE.read_bytes().strip())


def _load() -> Dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save(d: Dict[str, Any]) -> None:
    _secure_write(SETTINGS_FILE, json.dumps(d, indent=2, ensure_ascii=False).encode("utf-8"))


def public_settings() -> Dict[str, Any]:
    """Was der Browser sehen darf – niemals das Passwort."""
    d = _load()
    return {
        "nbaEmail": d.get("nba_email", ""),
        "hasPassword": bool(d.get("nba_password")),
        "lastCheck": d.get("last_check"),
    }


def save_credentials(email: str, password: str | None) -> Dict[str, Any]:
    with _lock:
        d = _load()
        email = (email or "").strip()
        if email != d.get("nba_email"):
            d.pop("last_check", None)
        d["nba_email"] = email
        if password:  # leeres Feld = bestehendes Passwort behalten
            d["nba_password"] = _fernet().encrypt(password.encode()).decode()
            d.pop("last_check", None)
        _save(d)
    return public_settings()


def delete_credentials() -> Dict[str, Any]:
    with _lock:
        d = _load()
        for k in ("nba_email", "nba_password", "last_check"):
            d.pop(k, None)
        _save(d)
    return public_settings()


def _password() -> str | None:
    enc = _load().get("nba_password")
    if not enc:
        return None
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken:
        return None


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


def check_login() -> Dict[str, Any]:
    d = _load()
    email, pw = d.get("nba_email"), _password()
    if not email or not pw:
        return {"ok": False, "message": "Bitte zuerst E-Mail und Passwort speichern."}
    try:
        r = requests.post(
            IDENTITY_URL,
            json={"email": email, "password": pw},
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
            result = {
                "ok": True,
                "name": " ".join(x for x in (user.get("givenName"), user.get("familyName")) if x),
                "subscriptions": subs,
                "leaguePass": lp,
                "message": "Login erfolgreich" + (" – League Pass aktiv" if lp else
                                                  " – kein League Pass gefunden"),
            }
        else:
            msg = ((body.get("data") or {}).get("message") if isinstance(body, dict) else None) \
                or f"NBA-Login fehlgeschlagen (HTTP {r.status_code})"
            result = {"ok": False, "message": msg}
    except requests.RequestException as e:
        result = {"ok": False, "message": f"NBA nicht erreichbar: {e.__class__.__name__}"}

    result["checkedAt"] = int(time.time())
    with _lock:
        d = _load()
        d["last_check"] = result
        _save(d)
    return result
