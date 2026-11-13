"""
Benutzerkonten (SQLite in data/users.db).

* Passwörter der App-Konten: gesalzener Hash (werkzeug, scrypt/pbkdf2) – nie im Klartext.
* NBA-Passwörter: Fernet-verschlüsselt (Schlüssel data/secret.key), weil der Server
  sie für den NBA-Login wieder braucht.
* Der erste registrierte Benutzer wird Admin. Danach sind Registrierungen nur offen,
  wenn ein Admin sie in den Einstellungen erlaubt.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = Path(os.environ.get("HC_DATA_DIR", Path(__file__).with_name("data")))
DB_FILE = DATA_DIR / "users.db"
KEY_FILE = DATA_DIR / "secret.key"
FLASK_KEY_FILE = DATA_DIR / "flask_secret.key"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
MIN_PASSWORD = 8

_lock = threading.Lock()
# Hash für nicht existierende Benutzer – damit Login-Zeit nichts über Benutzernamen verrät
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


class UserError(ValueError):
    pass


# ── Dateien / Schlüssel ─────────────────────────────────────

def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def _read_or_create_secret(path: Path, factory) -> bytes:
    _ensure_dir()
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(factory())
    return path.read_bytes().strip()


def flask_secret() -> bytes:
    return _read_or_create_secret(FLASK_KEY_FILE, lambda: os.urandom(32).hex().encode())


def _fernet() -> Fernet:
    return Fernet(_read_or_create_secret(KEY_FILE, Fernet.generate_key))


# ── Datenbank ───────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    _ensure_dir()
    c = sqlite3.connect(DB_FILE, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def _tx():
    """Schreibtransaktion, die die Datenbank schon zu Beginn sperrt (BEGIN IMMEDIATE).
    Prüfungen (z.B. "ist er noch Admin?") und das Schreiben sind dadurch atomar –
    auch wenn mehrere Server-Prozesse gleichzeitig arbeiten."""
    _ensure_dir()
    c = sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        with _lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
    finally:
        c.close()


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pw_hash       TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                session_ver   INTEGER NOT NULL DEFAULT 1,
                nba_email     TEXT NOT NULL DEFAULT '',
                nba_password  TEXT NOT NULL DEFAULT '',
                nba_check     TEXT NOT NULL DEFAULT '',
                created_at    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS script_tokens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                token_hash  TEXT NOT NULL UNIQUE,
                created_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_script_tokens_user ON script_tokens(user_id);
            """
        )
        # Migration: früher lag ein Klartext-Token in users.script_token -> nur noch Hash speichern
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "script_token" in cols:
            for r in c.execute("SELECT id, script_token FROM users WHERE script_token != ''").fetchall():
                c.execute("INSERT OR IGNORE INTO script_tokens(user_id, token_hash, created_at) VALUES(?,?,?)",
                          (r["id"], _token_hash(r["script_token"]), int(time.time())))
            c.execute("UPDATE users SET script_token=''")
    try:
        os.chmod(DB_FILE, 0o600)
    except OSError:
        pass


def _setting(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(key: str, value: str) -> None:
    with _tx() as c:
        c.execute("INSERT INTO app_settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def user_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def signup_open() -> bool:
    return user_count() == 0 or _setting("signup_open", "0") == "1"


def set_signup_open(value: bool, *, acting_admin_id: int) -> None:
    with _tx() as c:
        _require_admin_tx(c, acting_admin_id)
        c.execute("INSERT INTO app_settings(key,value) VALUES('signup_open',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if value else "0",))


# ── Konten ──────────────────────────────────────────────────

def _public(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": row["id"], "username": row["username"], "isAdmin": bool(row["is_admin"]),
            "createdAt": row["created_at"]}


def get(user_id: int) -> Optional[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def _validate(username: str, password: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise UserError("Benutzername: 3–32 Zeichen, nur Buchstaben, Zahlen, . _ -")
    if len(password or "") < MIN_PASSWORD:
        raise UserError(f"Passwort: mindestens {MIN_PASSWORD} Zeichen")


def create(username: str, password: str, *, acting_admin_id: Optional[int] = None) -> Dict[str, Any]:
    username = (username or "").strip()
    _validate(username, password)
    with _tx() as c:
        first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if acting_admin_id is not None:
            _require_admin_tx(c, acting_admin_id)
        elif not first:
            s = c.execute("SELECT value FROM app_settings WHERE key='signup_open'").fetchone()
            if not s or s["value"] != "1":
                raise UserError("Registrierung ist geschlossen – bitte einen Admin fragen")
        try:
            cur = c.execute(
                "INSERT INTO users(username,pw_hash,is_admin,created_at) VALUES(?,?,?,?)",
                (username, generate_password_hash(password), 1 if first else 0, int(time.time())),
            )
        except sqlite3.IntegrityError:
            raise UserError("Benutzername ist schon vergeben")
        row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    if first:
        _adopt_legacy_settings(row["id"])
    return _public(row)


def authenticate(username: str, password: str) -> Optional[sqlite3.Row]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
    if not row:
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    return row if check_password_hash(row["pw_hash"], password or "") else None


def change_password(user_id: int, old: str, new: str) -> int:
    row = get(user_id)
    if not row or not check_password_hash(row["pw_hash"], old or ""):
        raise UserError("Aktuelles Passwort ist falsch")
    if len(new or "") < MIN_PASSWORD:
        raise UserError(f"Neues Passwort: mindestens {MIN_PASSWORD} Zeichen")
    with _tx() as c:
        # session_ver++ meldet alle anderen Geräte ab; installierte Browser-Skripte werden gesperrt
        c.execute("UPDATE users SET pw_hash=?, session_ver=session_ver+1 WHERE id=?",
                  (generate_password_hash(new), user_id))
        c.execute("DELETE FROM script_tokens WHERE user_id=?", (user_id,))
        return c.execute("SELECT session_ver FROM users WHERE id=?", (user_id,)).fetchone()[0]


def list_users() -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [{**_public(r), "hasNba": bool(r["nba_email"])} for r in rows]


def _require_admin_tx(c: sqlite3.Connection, acting_admin_id: int) -> None:
    """Admin-Recht des Handelnden innerhalb der Transaktion erneut prüfen
    (er könnte es inzwischen durch eine parallele Aktion verloren haben)."""
    row = c.execute("SELECT is_admin FROM users WHERE id=?", (acting_admin_id,)).fetchone()
    if not row or not row["is_admin"]:
        raise UserError("Keine Admin-Rechte mehr")


def _ensure_admin_left_tx(c: sqlite3.Connection) -> None:
    if c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0] < 1:
        raise UserError("Es muss mindestens ein Admin übrig bleiben")


def delete(user_id: int, *, acting_admin_id: int) -> None:
    if user_id == acting_admin_id:
        raise UserError("Du kannst dein eigenes Konto hier nicht löschen")
    with _tx() as c:
        _require_admin_tx(c, acting_admin_id)
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        c.execute("DELETE FROM script_tokens WHERE user_id=?", (user_id,))
        _ensure_admin_left_tx(c)  # Exception -> Rollback


def set_admin(user_id: int, value: bool, *, acting_admin_id: int) -> None:
    if user_id == acting_admin_id and not value:
        raise UserError("Du kannst dir die Admin-Rechte nicht selbst entziehen")
    with _tx() as c:
        _require_admin_tx(c, acting_admin_id)
        c.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if value else 0, user_id))
        _ensure_admin_left_tx(c)  # Exception -> Rollback


# ── NBA-Zugangsdaten pro Benutzer ──────────────────────────

def nba_public(user_id: int) -> Dict[str, Any]:
    row = get(user_id)
    if not row:
        return {"nbaEmail": "", "hasPassword": False, "lastCheck": None}
    try:
        check = json.loads(row["nba_check"]) if row["nba_check"] else None
    except ValueError:
        check = None
    return {"nbaEmail": row["nba_email"], "hasPassword": bool(row["nba_password"]), "lastCheck": check}


def nba_save(user_id: int, email: str, password: Optional[str]) -> None:
    email = (email or "").strip()
    with _tx() as c:
        row = c.execute("SELECT nba_email FROM users WHERE id=?", (user_id,)).fetchone()
        reset_check = row is None or email != row["nba_email"]
        if password:  # leeres Feld = bestehendes Passwort behalten
            c.execute("UPDATE users SET nba_email=?, nba_password=?, nba_check='' WHERE id=?",
                      (email, _fernet().encrypt(password.encode()).decode(), user_id))
        else:
            c.execute("UPDATE users SET nba_email=?" + (", nba_check=''" if reset_check else "") +
                      " WHERE id=?", (email, user_id))


def nba_delete(user_id: int) -> None:
    with _tx() as c:
        c.execute("UPDATE users SET nba_email='', nba_password='', nba_check='' WHERE id=?", (user_id,))
        c.execute("DELETE FROM script_tokens WHERE user_id=?", (user_id,))


def nba_credentials(user_id: int) -> tuple[str, Optional[str]]:
    row = get(user_id)
    if not row or not row["nba_password"]:
        return (row["nba_email"] if row else ""), None
    try:
        return row["nba_email"], _fernet().decrypt(row["nba_password"].encode()).decode()
    except InvalidToken:
        return row["nba_email"], None


def nba_set_check(user_id: int, result: Dict[str, Any]) -> None:
    with _tx() as c:
        c.execute("UPDATE users SET nba_check=? WHERE id=?", (json.dumps(result), user_id))


# ── Schlüssel für das Browser-Skript (Tampermonkey / Userscripts) ──
# Das Skript läuft auf nba.com und holt sich damit die NBA-Zugangsdaten, um den
# Browser dort anzumelden. Jeder Skript-Download bekommt einen eigenen Schlüssel;
# gespeichert wird nur sein SHA-256-Hash (eine DB-Kopie allein nützt also nichts).
# Widerrufen werden alle Schlüssel bei: "Schlüssel erneuern", Passwortwechsel,
# Löschen der NBA-Daten und Löschen des Kontos.
MAX_SCRIPT_TOKENS = 20


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_script_token(user_id: int, session_ver: int) -> str:
    token = secrets.token_urlsafe(32)
    with _tx() as c:
        # Session-Version in derselben Transaktion prüfen: Wurde das Passwort inzwischen
        # geändert, darf eine alte Session keinen neuen Schlüssel mehr bekommen.
        row = c.execute("SELECT session_ver FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or row["session_ver"] != session_ver:
            raise UserError("Sitzung abgelaufen – bitte neu anmelden")
        c.execute("INSERT INTO script_tokens(user_id, token_hash, created_at) VALUES(?,?,?)",
                  (user_id, _token_hash(token), int(time.time())))
        # nur die neuesten N behalten
        c.execute("""DELETE FROM script_tokens WHERE user_id=? AND id NOT IN (
                       SELECT id FROM script_tokens WHERE user_id=? ORDER BY id DESC LIMIT ?)""",
                  (user_id, user_id, MAX_SCRIPT_TOKENS))
    return token


def revoke_script_tokens(user_id: int) -> None:
    with _tx() as c:
        c.execute("DELETE FROM script_tokens WHERE user_id=?", (user_id,))


def by_script_token(token: str) -> Optional[sqlite3.Row]:
    if not token or len(token) < 20 or len(token) > 200:
        return None
    with _conn() as c:
        return c.execute("""SELECT u.* FROM script_tokens t JOIN users u ON u.id = t.user_id
                            WHERE t.token_hash=?""", (_token_hash(token),)).fetchone()


def _adopt_legacy_settings(user_id: int) -> None:
    """Übernimmt NBA-Daten aus der alten Einzelnutzer-Version (data/settings.json)."""
    legacy = DATA_DIR / "settings.json"
    try:
        d = json.loads(legacy.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return
    if d.get("nba_email") or d.get("nba_password"):
        with _tx() as c:
            c.execute("UPDATE users SET nba_email=?, nba_password=? WHERE id=?",
                      (d.get("nba_email", ""), d.get("nba_password", ""), user_id))
    legacy.rename(legacy.with_suffix(".json.migrated"))
