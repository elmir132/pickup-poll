#!/usr/bin/env python3
"""Pickup Poll: a small RSVP board and permit reminder for a weekly football club.

Standard library only. Answers belong to a game date, so every week starts empty.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import time
import unicodedata
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/ics.js": ("ics.js", "text/javascript; charset=utf-8")}
STATUSES = ("going", "maybe", "out")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_BODY = 16_384
MAX_RSVPS_PER_GAME = 200
MAX_PERMITS = 50
SCHEMA_VERSION = 2
SAMPLE_NAMES = ("Maya T.", "Chris L.", "Jordan K.", "Alex R.", "Sam P.", "Taylor W.")


class PollError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def next_thursday_on_or_after(day):
    return day + timedelta(days=(3 - day.weekday()) % 7)


def effective_game_date(stored, today):
    try:
        chosen = date.fromisoformat(stored)
    except (TypeError, ValueError):
        chosen = None
    return chosen if chosen and chosen >= today else next_thursday_on_or_after(today)


def clean_text(value, label, low, high, message=None):
    """Text from a form: no control or invisible characters, extra spaces collapsed, length checked."""
    if not isinstance(value, str):
        raise PollError(message or f"{label} must be text.")
    text = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Cn") and not ch.isspace() for ch in text):
        raise PollError(f"{label} contains characters that are not allowed.")
    text = " ".join(text.split())
    if not low <= len(text) <= high:
        raise PollError(message or f"{label} must be {low} to {high} characters.")
    return text


def name_key(name):
    return unicodedata.normalize("NFKC", name).casefold()


class Store:
    def __init__(self, data_dir, today=date.today):
        self.dir = Path(data_dir)
        self.path = self.dir / "pickup-poll.sqlite3"
        self.today = today

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def initialize(self, demo=False):
        self.dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        previous = os.umask(0o077)
        try:
            with self.tx() as db:
                self._create_or_migrate(db)
                today = self.today()
                for key, value in {"club_name": "Thursday United", "game_date": next_thursday_on_or_after(today).isoformat(),
                                   "kickoff": "18:30", "location": "Add your field or meeting point", "max_players": "14"}.items():
                    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        finally:
            os.umask(previous)
        os.chmod(self.path, 0o600)

    def _create_or_migrate(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS rsvps (
                game_date TEXT NOT NULL,
                name_key TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('going', 'maybe', 'out')),
                updated_at TEXT NOT NULL,
                is_sample INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (game_date, name_key)
            )""")
        db.execute("""CREATE TABLE IF NOT EXISTS permits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                is_sample INTEGER NOT NULL DEFAULT 0
            )""")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _settings(self, db):
        return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}

    def _game_date(self, db):
        return effective_game_date(self._settings(db).get("game_date"), self.today())

    def state(self):
        with closing(self.connect()) as db:
            settings = self._settings(db)
            game = effective_game_date(settings.get("game_date"), self.today())
            rsvps = [dict(r) for r in db.execute(
                "SELECT name, status, updated_at, is_sample FROM rsvps WHERE game_date = ? ORDER BY "
                "CASE status WHEN 'going' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END, name COLLATE NOCASE", (game.isoformat(),))]
            permits = [dict(r) for r in db.execute("SELECT id, title, date, note, is_sample FROM permits ORDER BY date, id")]
            recent = [dict(r) for r in db.execute(
                "SELECT game_date, SUM(status = 'going') AS going, SUM(status = 'maybe') AS maybe, SUM(status = 'out') AS out "
                "FROM rsvps WHERE game_date < ? AND is_sample = 0 GROUP BY game_date ORDER BY game_date DESC LIMIT 4", (self.today().isoformat(),))]
        for row in rsvps + permits:
            row["is_sample"] = bool(row["is_sample"])
        return {"today": self.today().isoformat(),
                "settings": {"club_name": settings["club_name"], "game_date": game.isoformat(), "kickoff": settings["kickoff"],
                             "location": settings["location"], "max_players": int(settings["max_players"])},
                "rsvps": rsvps, "permits": permits, "recent": recent,
                "has_sample": any(r["is_sample"] for r in rsvps + permits)}
