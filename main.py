#!/usr/bin/env python3
"""
Highlight Checker – spoilerfreie NBA-Re-Watch-Übersicht mit Benutzerkonten.

Jede Spielkarte verlinkt direkt auf den "All Possessions"-Recap in nba.com /
League Pass. Ergebnisse und Videolängen werden nie an den Browser geschickt.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from dateutil import tz
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

import nba_account
import nba_data
import users

VIENNA = tz.gettz("Europe/Vienna")
REMEMBER_DAYS = 30

app = Flask(__name__, static_folder="static", template_folder="templates")
users.init_db()
app.config.update(
    SECRET_KEY=users.flask_secret(),
    SESSION_COOKIE_NAME="hc_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HC_SECURE_COOKIES") == "1",  # bei HTTPS-Proxy setzen
    PERMANENT_SESSION_LIFETIME=_dt.timedelta(days=REMEMBER_DAYS),
)


# ── Schutz gegen Passwort-Raten ────────────────────────────
_fails: dict = defaultdict(deque)
_fails_lock = threading.Lock()
MAX_FAILS, MAX_IP_FAILS, FAIL_WINDOW = 5, 20, 10 * 60
MAX_FAIL_KEYS = 5000  # Obergrenze, damit der Zähler nicht unbegrenzt Speicher frisst


def _prune(key: str, now: float) -> None:
    q = _fails.get(key)
    if q is None:
        return
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if not q:
        del _fails[key]


def _too_many(key: str, limit: int = MAX_FAILS) -> bool:
    now = time.time()
    with _fails_lock:
        _prune(key, now)  # legt keinen Eintrag an, wenn es keinen gibt
        q = _fails.get(key)
        return bool(q) and len(q) >= limit


def _note_fail(key: str) -> None:
    now = time.time()
    with _fails_lock:
        if key not in _fails and len(_fails) >= MAX_FAIL_KEYS:
            for k in list(_fails.keys()):  # abgelaufene Einträge aufräumen
                _prune(k, now)
            if len(_fails) >= MAX_FAIL_KEYS:  # immer noch voll: ältesten verwerfen
                oldest = min(_fails, key=lambda k: _fails[k][-1])
                del _fails[oldest]
        _fails[key].append(now)


# ── Session-Helfer ─────────────────────────────────────────

def _start_session(row, remember: bool) -> None:
    session.clear()
    session["uid"] = row["id"]
    session["ver"] = row["session_ver"]
    session.permanent = bool(remember)  # sonst: Cookie gilt nur bis Browser geschlossen wird


@app.before_request
def _load_user():
    g.user = None
    uid = session.get("uid")
    if uid is not None:
        row = users.get(uid)
        if row and row["session_ver"] == session.get("ver"):
            g.user = row
        else:  # Konto gelöscht oder Passwort geändert -> abmelden
            session.clear()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Nicht angemeldet"}), 401
            return redirect(url_for("login_page"))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not g.user["is_admin"]:
            return jsonify({"error": "Nur für Admins"}), 403
        return fn(*a, **kw)
    return wrapper


@app.before_request
def _json_only_for_writes():
    # CSRF-Schutz: schreibende API-Aufrufe nur als JSON (Formulare fremder Seiten können das nicht)
    if request.path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE"):
        if request.method != "DELETE" and not request.is_json:
            return jsonify({"error": "JSON erwartet"}), 415


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _err(e: Exception, code: int = 400):
    return jsonify({"error": str(e)}), code


# ── Seiten ─────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html", user=g.user)


@app.route("/login")
def login_page():
    if g.user is not None:
        return redirect(url_for("index"))
    return render_template("login.html", signup_open=users.signup_open(),
                           first_user=users.user_count() == 0)


# ── Auth-API ───────────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    b = _body()
    username = (b.get("username") or "").strip()[:64]
    key = f"{request.remote_addr}|{username.lower()}"
    if _too_many(request.remote_addr or "-", MAX_IP_FAILS) or _too_many(key):
        return jsonify({"error": "Zu viele Fehlversuche – bitte in ein paar Minuten nochmal"}), 429
    row = users.authenticate(username, b.get("password") or "")
    if not row:
        _note_fail(key)
        _note_fail(request.remote_addr or "-")
        return jsonify({"error": "Benutzername oder Passwort falsch"}), 401
    _start_session(row, b.get("remember"))
    return jsonify({"ok": True})


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    b = _body()
    if _too_many(request.remote_addr or "-", MAX_IP_FAILS):
        return jsonify({"error": "Zu viele Versuche – bitte später nochmal"}), 429
    try:
        users.create(b.get("username", ""), b.get("password", ""))
    except users.UserError as e:
        _note_fail(request.remote_addr or "-")
        return _err(e)
    row = users.authenticate(b.get("username", ""), b.get("password", ""))
    _start_session(row, b.get("remember"))
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
@login_required
def api_me():
    return jsonify({"username": g.user["username"], "isAdmin": bool(g.user["is_admin"]),
                    "remembered": bool(session.permanent), "signupOpen": users.signup_open()})


@app.route("/api/me/password", methods=["POST"])
@login_required
def api_change_password():
    b = _body()
    try:
        ver = users.change_password(g.user["id"], b.get("oldPassword", ""), b.get("newPassword", ""))
    except users.UserError as e:
        return _err(e)
    session["ver"] = ver  # dieses Gerät bleibt angemeldet, alle anderen fliegen raus
    return jsonify({"ok": True})


# ── Weiterleitung zu nba.com (ohne dass iOS die NBA-App öffnet) ──
# iOS öffnet nba.com-Links in der NBA-App, wenn man sie direkt antippt
# ("Universal Links"). Eine Weiterleitung per JavaScript nach dem Laden
# einer eigenen Seite fängt iOS nicht ab – die Seite bleibt in Safari.
_GO_ALLOWED = re.compile(r"^https://www\.nba\.com/game/[a-z0-9-]+(\?[A-Za-z0-9=&_%-]*)?$")


@app.route("/go")
@login_required
def go():
    target = request.args.get("to", "")
    if not _GO_ALLOWED.match(target):
        return jsonify({"error": "Ziel nicht erlaubt"}), 400
    html = render_template("go.html", target=target)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Spiele ─────────────────────────────────────────────────

@app.route("/api/games")
@login_required
def api_games():
    today = _dt.datetime.now(VIENNA).date()
    raw = request.args.get("date")
    try:
        date = _dt.date.fromisoformat(raw) if raw else today - _dt.timedelta(days=1)
    except ValueError:
        return jsonify({"error": "Ungültiges Datum"}), 400
    try:
        games = nba_data.get_games(date, today)
    except Exception as e:  # nba.com nicht erreichbar o.ä.
        app.logger.exception("Spieldaten konnten nicht geladen werden")
        return jsonify({"error": f"Spieldaten nicht verfügbar ({e.__class__.__name__})"}), 502
    return jsonify({"date": date.isoformat(), "games": games})


# ── NBA-Zugangsdaten (pro Benutzer) ────────────────────────

@app.route("/api/settings", methods=["GET"])
@login_required
def get_settings():
    return jsonify(users.nba_public(g.user["id"]))


@app.route("/api/settings", methods=["POST"])
@login_required
def post_settings():
    b = _body()
    users.nba_save(g.user["id"], b.get("nbaEmail", ""), b.get("nbaPassword"))
    return jsonify(users.nba_public(g.user["id"]))


@app.route("/api/settings", methods=["DELETE"])
@login_required
def delete_settings():
    users.nba_delete(g.user["id"])
    return jsonify(users.nba_public(g.user["id"]))


@app.route("/api/settings/check", methods=["POST"])
@login_required
def check_settings():
    return jsonify(nba_account.check_login(g.user["id"]))


# ── Browser-Skript (Tampermonkey) ──────────────────────────
USERSCRIPT_TMPL = Path(__file__).with_name("userscript") / "highlight-checker.user.js.tmpl"
USERSCRIPT_VERSION = "1.5.0"


def _render_userscript(token: str, username: str) -> str:
    server = request.host_url.rstrip("/")
    return (USERSCRIPT_TMPL.read_text("utf-8")
            .replace("__HC_VERSION__", USERSCRIPT_VERSION)
            .replace("__HC_SERVER__", server)
            .replace("__HC_HOST__", request.host.split(":")[0])
            .replace("__HC_USER__", username)
            .replace("__HC_TOKEN__", token))


@app.route("/userscript/highlight-checker.user.js")
def userscript():
    """Alter fester Link: erzeugt bewusst KEINEN Schlüssel mehr (sonst könnte eine fremde
    Seite per Link-Aufruf Schlüssel erzeugen und alte verdrängen) – Installation läuft
    über den Knopf in den Einstellungen (POST /api/script/install-link)."""
    return redirect(url_for("index"))


@app.route("/api/script/install-link", methods=["POST"])
@login_required
def script_install_link():
    """Neuer persönlicher Schlüssel + Installationslink (nur per JSON-POST → CSRF-sicher)."""
    try:
        token = users.new_script_token(g.user["id"], g.user["session_ver"])
    except users.UserError as e:
        return _err(e, 401)
    return jsonify({"url": url_for("userscript_by_token", token=token)})


@app.route("/userscript/t/<token>/highlight-checker.user.js")
def userscript_by_token(token: str):
    """Persönlicher Installations- und Update-Link. Braucht keinen Web-Login (die
    Userscripts-App auf iOS und Tampermonkey-Updates haben keinen), liefert das
    Skript immer mit genau diesem Schlüssel – also auch für genau diesen Benutzer."""
    ip = request.remote_addr or "-"
    if _too_many("script|" + ip, MAX_IP_FAILS):
        return Response("// Zu viele Fehlversuche\n", 429, mimetype="text/plain")
    row = users.by_script_token(token)
    if not row:
        _note_fail("script|" + ip)
        return Response("// Installationslink ungültig oder gesperrt – im Highlight Checker "
                        "unter Einstellungen neu auf „Skript installieren“ tippen.\n",
                        404, mimetype="text/plain")
    # text/plain: iOS Safari zeigt das sicher als Text an (die Userscripts-App liest
    # das Skript aus der angezeigten Seite); Tampermonkey erkennt es an der Endung.
    return Response(_render_userscript(token, row["username"]), mimetype="text/plain",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": 'inline; filename="highlight-checker.user.js"'})


@app.route("/userscript/highlight-checker.meta.js")
def userscript_meta():
    """Nur der Skript-Kopf (Version) für Update-Prüfungen – ohne Schlüssel, ohne Login."""
    js = _render_userscript("", "")
    header = js.split("// ==/UserScript==")[0]
    header = "\n".join(l for l in header.splitlines() if "@downloadURL" not in l)
    return Response(header + "\n// ==/UserScript==\n", mimetype="text/plain",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/script/token", methods=["POST"])
@login_required
def regenerate_script_token():
    users.revoke_script_tokens(g.user["id"])
    return jsonify({"ok": True})


def _script_user():
    """Benutzer zum Skript-Schlüssel – oder (None, Fehlerantwort)."""
    ip = request.remote_addr or "-"
    if _too_many("script|" + ip, MAX_IP_FAILS):
        return None, (jsonify({"error": "Zu viele Fehlversuche"}), 429)
    row = users.by_script_token(request.headers.get("X-HC-Token", ""))
    if not row:
        _note_fail("script|" + ip)
        return None, (jsonify({"error": "Skript-Schlüssel ungültig oder widerrufen – "
                                        "Skript im Highlight Checker neu installieren"}), 401)
    return row, None


@app.route("/api/script/nba-account")
def script_nba_account():
    """Nur die gespeicherte NBA-E-Mail – damit das Skript ein falsch angemeldetes Konto erkennt."""
    row, err = _script_user()
    if err:
        return err
    return jsonify({"email": users.nba_credentials(row["id"])[0]})


@app.route("/api/script/nba-credentials")
def script_credentials():
    """Nur für das Browser-Skript: liefert die NBA-Daten, damit es nba.com anmelden kann."""
    row, err = _script_user()
    if err:
        return err
    email, pw = users.nba_credentials(row["id"])
    if not email or not pw:
        return jsonify({"error": "Keine NBA-Zugangsdaten im Highlight Checker gespeichert"}), 404
    return jsonify({"email": email, "password": pw})


# ── Admin ──────────────────────────────────────────────────

@app.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_users():
    return jsonify({"users": users.list_users(), "signupOpen": users.signup_open(),
                    "me": g.user["id"]})


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def admin_create_user():
    b = _body()
    try:
        users.create(b.get("username", ""), b.get("password", ""), acting_admin_id=g.user["id"])
    except users.UserError as e:
        return _err(e)
    return admin_users()


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@admin_required
def admin_delete_user(uid: int):
    try:
        users.delete(uid, acting_admin_id=g.user["id"])
    except users.UserError as e:
        return _err(e)
    return admin_users()


@app.route("/api/admin/users/<int:uid>/admin", methods=["POST"])
@admin_required
def admin_set_admin(uid: int):
    try:
        users.set_admin(uid, bool(_body().get("isAdmin")), acting_admin_id=g.user["id"])
    except users.UserError as e:
        return _err(e)
    return admin_users()


@app.route("/api/admin/signup", methods=["POST"])
@admin_required
def admin_signup():
    try:
        users.set_signup_open(bool(_body().get("open")), acting_admin_id=g.user["id"])
    except users.UserError as e:
        return _err(e)
    return admin_users()


@app.after_request
def _headers(resp):
    if request.path.startswith("/api/") or request.path in ("/", "/login"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), host="0.0.0.0", port=port)
