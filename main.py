#!/usr/bin/env python3
"""
Highlight Checker – spoilerfreie NBA-Re-Watch-Übersicht.

Jede Spielkarte verlinkt direkt auf den "All Possessions"-Recap in nba.com /
League Pass. Ergebnisse und Videolängen werden nie an den Browser geschickt.
"""

from __future__ import annotations

import datetime as _dt
import os

from dateutil import tz
from flask import Flask, jsonify, render_template, request

import nba_account
import nba_data

VIENNA = tz.gettz("Europe/Vienna")

app = Flask(__name__, static_folder="static", template_folder="templates")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/games")
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


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(nba_account.public_settings())


@app.route("/api/settings", methods=["POST"])
def post_settings():
    body = request.get_json(silent=True) or {}
    return jsonify(nba_account.save_credentials(body.get("nbaEmail", ""), body.get("nbaPassword")))


@app.route("/api/settings", methods=["DELETE"])
def delete_settings():
    return jsonify(nba_account.delete_credentials())


@app.route("/api/settings/check", methods=["POST"])
def check_settings():
    return jsonify(nba_account.check_login())


@app.after_request
def _no_cache_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), host="0.0.0.0", port=port)
