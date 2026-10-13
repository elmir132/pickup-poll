#!/usr/bin/env python3
"""Pickup Poll: a small RSVP board and permit reminder for a weekly football club.

Standard library only. Answers belong to a game date, so every week starts empty. Changes to
the match, the permit dates and removing players need an organizer PIN whenever the server can
be reached from other devices. Every write must come from this page itself, so another website
open in your browser cannot change anything.
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
          "/ics.js": ("ics.js", "text/javascript; charset=utf-8"),
          "/sun.js": ("sun.js", "text/javascript; charset=utf-8"),
          "/teams.js": ("teams.js", "text/javascript; charset=utf-8")}
STATUSES = ("going", "maybe", "out")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_BODY = 16_384
MAX_RSVPS_PER_GAME = 200
MAX_PERMITS = 50
DEFAULTS = {"club_name": "Football Not Soccer Club", "kickoff": "16:00", "end_time": "18:00",
            "location": "Jack McManus Field, 729 Main St, New York, NY 10044", "max_players": "14",
            "notes": "Pinnies provided. The field has lights. Bring water.",
            "notice_text": "", "notice_kind": "info", "notice_game": "", "team_seed": "0", "team_seed_game": ""}
OLD_DEFAULTS = {"club_name": ("Thursday United",), "kickoff": ("18:30",),
                "location": ("Add your field or meeting point", "Your usual pitch")}
LOCK_DAYS_BEFORE = 1       # from this many days before the game, spots that are going are locked
SCHEMA_VERSION = 2
SAMPLE_NAMES = ("Maya T.", "Chris L.", "Jordan K.", "Alex R.", "Sam P.", "Taylor W.")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class PollError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Dates and text
# ---------------------------------------------------------------------------

def next_thursday_on_or_after(day):
    return day + timedelta(days=(3 - day.weekday()) % 7)


def effective_game_date(stored, today):
    """The organizer's chosen date, unless it has passed: then the next Thursday, so the page
    never shows a game that is already over."""
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


def token_hash(token):
    """Players are told apart by a random token their browser keeps. Only its hash is stored."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise PollError("Reload the page and try again.", 400)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def name_key(name):
    return unicodedata.normalize("NFKC", name).casefold()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

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
        previous = os.umask(0o077)               # the database holds player names: private to this user
        try:
            with self.tx() as db:
                self._create_or_migrate(db)
                today = self.today()
                game_default = next_thursday_on_or_after(today).isoformat()
                for key, value in {**DEFAULTS, "game_date": game_default}.items():
                    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
                if not db.execute("SELECT 1 FROM meta WHERE key = 'defaults_v3'").fetchone():
                    for key, old_values in OLD_DEFAULTS.items():       # replace the first version's placeholders once
                        for old in old_values:
                            db.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?", (DEFAULTS[key], key, old))
                    db.execute("INSERT INTO meta(key, value) VALUES ('defaults_v3', '1')")
                if demo:
                    self._seed_demo(db)
        finally:
            os.umask(previous)
        os.chmod(self.path, 0o600)

    def _create_or_migrate(self, db):
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if "rsvps" in tables and "game_date" not in {r["name"] for r in db.execute("PRAGMA table_info(rsvps)")}:
            self._migrate_v1(db)
        db.execute("""CREATE TABLE IF NOT EXISTS rsvps (
                game_date TEXT NOT NULL,
                name_key TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('going', 'maybe', 'out')),
                updated_at TEXT NOT NULL,
                is_sample INTEGER NOT NULL DEFAULT 0,
                owner_hash TEXT,
                locked INTEGER NOT NULL DEFAULT 0,
                waitlisted INTEGER NOT NULL DEFAULT 0,
                waitlist_at INTEGER,
                PRIMARY KEY (game_date, name_key)
            )""")
        if "owner_hash" not in {r["name"] for r in db.execute("PRAGMA table_info(rsvps)")}:
            db.execute("ALTER TABLE rsvps ADD COLUMN owner_hash TEXT")
        if "locked" not in {r["name"] for r in db.execute("PRAGMA table_info(rsvps)")}:
            db.execute("ALTER TABLE rsvps ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        if "waitlisted" not in {r["name"] for r in db.execute("PRAGMA table_info(rsvps)")}:
            db.execute("ALTER TABLE rsvps ADD COLUMN waitlisted INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE rsvps ADD COLUMN waitlist_at INTEGER")
        db.execute("""CREATE TABLE IF NOT EXISTS permits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                is_sample INTEGER NOT NULL DEFAULT 0
            )""")
        if "is_sample" not in {r["name"] for r in db.execute("PRAGMA table_info(permits)")}:
            db.execute("ALTER TABLE permits ADD COLUMN is_sample INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE permits SET is_sample = 1 WHERE note LIKE 'Demo date%'")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _migrate_v1(self, db):
        """Version 1 kept one list of answers for ever. Keep them, as answers for the saved game
        date, and recognise the built-in sample names so they can be cleared."""
        game_row = db.execute("SELECT value FROM settings WHERE key = 'game_date'").fetchone()
        game = game_row["value"] if game_row else next_thursday_on_or_after(self.today()).isoformat()
        old = [dict(r) for r in db.execute("SELECT name, status, updated_at FROM rsvps")]
        db.execute("DROP TABLE rsvps")
        db.execute("""CREATE TABLE rsvps (game_date TEXT NOT NULL, name_key TEXT NOT NULL, name TEXT NOT NULL,
                      status TEXT NOT NULL CHECK(status IN ('going', 'maybe', 'out')), updated_at TEXT NOT NULL,
                      is_sample INTEGER NOT NULL DEFAULT 0, owner_hash TEXT, locked INTEGER NOT NULL DEFAULT 0, waitlisted INTEGER NOT NULL DEFAULT 0, waitlist_at INTEGER, PRIMARY KEY (game_date, name_key))""")
        stamps = {r["updated_at"] for r in old if r["name"] in SAMPLE_NAMES}
        for row in old:
            sample = int(row["name"] in SAMPLE_NAMES and len(stamps) == 1 and row["updated_at"] in stamps)
            db.execute("INSERT OR IGNORE INTO rsvps(game_date, name_key, name, status, updated_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
                       (game, name_key(row["name"]), row["name"], row["status"], row["updated_at"], sample))

    def _seed_demo(self, db):
        """Sample data is added once. After it has been cleared it never comes back on restart."""
        if db.execute("SELECT 1 FROM meta WHERE key = 'demo_seeded'").fetchone():
            return
        db.execute("INSERT INTO meta(key, value) VALUES ('demo_seeded', '1')")
        game = self._game_date(db).isoformat()
        if db.execute("SELECT COUNT(*) FROM rsvps").fetchone()[0] == 0:
            stamp = datetime.now().isoformat(timespec="minutes")
            for name, status in zip(SAMPLE_NAMES, ("going",) * 5 + ("maybe",)):
                db.execute("INSERT INTO rsvps(game_date, name_key, name, status, updated_at, is_sample) VALUES (?, ?, ?, ?, ?, 1)",
                           (game, name_key(name), name, status, stamp))
        if db.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 0:
            today = self.today()
            for title, days, note in (("Field-use permit", 18, "Sample date · replace with your permit deadline"),
                                      ("Season insurance check", 41, "Sample date · add your renewal details")):
                db.execute("INSERT INTO permits(title, date, note, is_sample) VALUES (?, ?, ?, 1)",
                           (title, (today + timedelta(days=days)).isoformat(), note))

    def auto_locked(self, game):
        """True from one day before the game: spots that are going can no longer be dropped."""
        return (game - self.today()).days <= LOCK_DAYS_BEFORE

    # -- reading ------------------------------------------------------------

    def _settings(self, db):
        return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}

    def _game_date(self, db):
        return effective_game_date(self._settings(db).get("game_date"), self.today())

    def state(self, token=None):
        mine = None
        if token and TOKEN_RE.fullmatch(token):
            mine = hashlib.sha256(token.encode("ascii")).hexdigest()
        with closing(self.connect()) as db:
            settings = self._settings(db)
            game = effective_game_date(settings.get("game_date"), self.today())
            cancelled = (settings.get("notice_game") == game.isoformat()
                         and settings.get("notice_kind") == "cancelled" and bool(settings.get("notice_text")))
            rsvps = [dict(r) for r in db.execute(
                "SELECT rowid AS seq, name, status, updated_at, is_sample, owner_hash, locked, waitlisted, waitlist_at FROM rsvps "
                "WHERE game_date = ? ORDER BY "
                "CASE WHEN status = 'going' AND waitlisted = 0 THEN 0 WHEN status = 'going' THEN 1 WHEN status = 'maybe' THEN 2 ELSE 3 END, "
                "waitlist_at, name COLLATE NOCASE", (game.isoformat(),))]
            permits = [dict(r) for r in db.execute("SELECT id, title, date, note, is_sample FROM permits ORDER BY date, id")]
            recent = [dict(r) for r in db.execute(
                "SELECT game_date, SUM(status = 'going' AND waitlisted = 0) AS going, SUM(status = 'maybe') AS maybe, SUM(status = 'out') AS out "
                "FROM rsvps WHERE game_date < ? AND is_sample = 0 GROUP BY game_date ORDER BY game_date DESC LIMIT 4", (self.today().isoformat(),))]
        auto = self.auto_locked(game)
        for row in rsvps + permits:
            row["is_sample"] = bool(row["is_sample"])
        position = 0
        for row in rsvps:
            owner = row.pop("owner_hash")                                   # the hash itself is never sent
            row["mine"] = bool(mine) and owner == mine
            manual = bool(row.pop("locked"))
            row["waitlisted"] = bool(row["waitlisted"])
            row.pop("waitlist_at")
            row["waitlist_pos"] = None
            if row["waitlisted"]:
                position += 1
                row["waitlist_pos"] = position
            row["locked"] = (not cancelled and row["status"] == "going" and not row["waitlisted"]
                              and not row["is_sample"] and (manual or auto))
            row["lock_kind"] = ("manual" if manual else "auto") if row["locked"] else None
        here = settings.get("notice_game") == game.isoformat() and settings.get("notice_text")
        return {"today": self.today().isoformat(),
                "settings": {"club_name": settings["club_name"], "game_date": game.isoformat(), "kickoff": settings["kickoff"],
                             "end_time": settings["end_time"], "location": settings["location"], "notes": settings["notes"],
                             "max_players": int(settings["max_players"]),
                             "lock_from": (game - timedelta(days=LOCK_DAYS_BEFORE)).isoformat(), "auto_locked": auto,
                             "team_seed": int(settings["team_seed"]) if settings.get("team_seed_game") == game.isoformat() else 0},
                "notice": {"text": settings["notice_text"], "kind": settings["notice_kind"]} if here else None,
                "rsvps": rsvps, "permits": permits, "recent": recent,
                "has_sample": any(r["is_sample"] for r in rsvps + permits)}

    # -- writing --------------------------------------------------------------

    def _cancelled(self, db, game):
        settings = self._settings(db)
        return settings.get("notice_game") == game and settings.get("notice_kind") == "cancelled" and bool(settings.get("notice_text"))

    def _promote(self, db, game):
        """When a spot is free, the person who has waited longest moves up."""
        if self._cancelled(db, game):
            return
        limit = int(self._settings(db)["max_players"])
        while True:
            taken = db.execute("SELECT COUNT(*) FROM rsvps WHERE game_date = ? AND status = 'going' AND waitlisted = 0 AND is_sample = 0",
                               (game,)).fetchone()[0]
            if taken >= limit:
                return
            nxt = db.execute("SELECT name_key FROM rsvps WHERE game_date = ? AND status = 'going' AND waitlisted = 1 "
                             "ORDER BY waitlist_at LIMIT 1", (game,)).fetchone()
            if nxt is None:
                return
            db.execute("UPDATE rsvps SET waitlisted = 0, waitlist_at = NULL WHERE game_date = ? AND name_key = ?", (game, nxt["name_key"]))

    def save_rsvp(self, name, status, token):
        name = clean_text(name, "Your name", 1, 36, "Enter your name (up to 36 characters).")
        if not isinstance(status, str) or status not in STATUSES:
            raise PollError("Choose going, maybe or out.")
        owner = token_hash(token)
        key = name_key(name)
        with self.tx() as db:
            game = self._game_date(db).isoformat()
            if status != "out" and self._cancelled(db, game):
                raise PollError("This game is cancelled. Check the update at the top of the page.", 409)
            row = db.execute("SELECT owner_hash, is_sample, status, locked, waitlisted FROM rsvps WHERE game_date = ? AND name_key = ?", (game, key)).fetchone()
            if row and row["owner_hash"] and row["owner_hash"] != owner and not row["is_sample"]:
                raise PollError("That name was added from another device. Use a different name (for example add an initial).", 403)
            holds_spot = bool(row) and not row["is_sample"] and row["status"] == "going" and not row["waitlisted"]
            if holds_spot and status != "going" and self._is_locked(db, game, key):
                raise PollError("Your spot is locked in, so you can no longer drop out. Message the organizer if something has come up.", 409)
            if not row and db.execute("SELECT COUNT(*) FROM rsvps WHERE game_date = ?", (game,)).fetchone()[0] >= MAX_RSVPS_PER_GAME:
                raise PollError("This game already has the maximum number of replies.", 409)
            waitlisted = 0
            if status == "going" and not holds_spot:
                limit = int(self._settings(db)["max_players"])
                taken = db.execute("SELECT COUNT(*) FROM rsvps WHERE game_date = ? AND status = 'going' AND waitlisted = 0 AND is_sample = 0 "
                                   "AND name_key != ?", (game, key)).fetchone()[0]
                waitlisted = int(taken >= limit)
            stamp = time.time_ns() if waitlisted else None
            db.execute("INSERT INTO rsvps(game_date, name_key, name, status, updated_at, is_sample, owner_hash, waitlisted, waitlist_at) "
                       "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?) "
                       "ON CONFLICT(game_date, name_key) DO UPDATE SET name = excluded.name, status = excluded.status, "
                       "updated_at = excluded.updated_at, is_sample = 0, owner_hash = COALESCE(rsvps.owner_hash, excluded.owner_hash), "
                       "locked = CASE WHEN excluded.status = 'going' THEN rsvps.locked ELSE 0 END, "
                       "waitlisted = excluded.waitlisted, "
                       "waitlist_at = CASE WHEN excluded.waitlisted = 1 THEN COALESCE(rsvps.waitlist_at, excluded.waitlist_at) ELSE NULL END",
                       (game, key, name, status, datetime.now().isoformat(timespec="minutes"), owner, waitlisted, stamp))
            self._promote(db, game)

    def delete_rsvp(self, name, token):
        """Only the browser that added a name can remove it. Nobody can remove anyone else's."""
        name = clean_text(name, "Player", 1, 36, "Choose a player to remove.")
        owner = token_hash(token)
        with self.tx() as db:
            game = self._game_date(db).isoformat()
            row = db.execute("SELECT owner_hash FROM rsvps WHERE game_date = ? AND name_key = ?", (game, name_key(name))).fetchone()
            if row is None:
                return
            if row["owner_hash"] != owner:
                raise PollError("You can only remove your own name.", 403)
            if self._is_locked(db, game, name_key(name)):
                raise PollError("Your spot is locked in, so it can no longer be removed. Message the organizer if something has come up.", 409)
            db.execute("DELETE FROM rsvps WHERE game_date = ? AND name_key = ?", (game, name_key(name)))
            self._promote(db, game)

    def _is_locked(self, db, game, key):
        if self._cancelled(db, game):
            return False
        row = db.execute("SELECT status, locked, is_sample, waitlisted FROM rsvps WHERE game_date = ? AND name_key = ?", (game, key)).fetchone()
        return (bool(row) and not row["is_sample"] and row["status"] == "going" and not row["waitlisted"]
                and bool(row["locked"] or self.auto_locked(date.fromisoformat(game))))

    def lock_rsvp(self, name, token):
        """Lock in your own spot. It cannot be undone by the player."""
        name = clean_text(name, "Player", 1, 36, "Choose a player to lock in.")
        owner = token_hash(token)
        with self.tx() as db:
            game = self._game_date(db).isoformat()
            if self._cancelled(db, game):
                raise PollError("This game is cancelled. Check the update at the top of the page.", 409)
            row = db.execute("SELECT owner_hash, status, is_sample, waitlisted FROM rsvps WHERE game_date = ? AND name_key = ?", (game, name_key(name))).fetchone()
            if row is None:
                raise PollError("That name is not on the list.", 404)
            if row["owner_hash"] != owner:
                raise PollError("You can only lock in your own name.", 403)
            if row["status"] != "going" or row["waitlisted"]:
                raise PollError("Only a confirmed spot can be locked in.", 409)
            db.execute("UPDATE rsvps SET locked = 1 WHERE game_date = ? AND name_key = ?", (game, name_key(name)))

    def set_notice(self, text, kind):
        """An update at the top of the page for this game, such as a cancellation. Empty text clears it."""
        text = clean_text(text if text is not None else "", "The update", 0, 140, "An update can be up to 140 characters.")
        if kind not in ("info", "cancelled"):
            raise PollError("Choose an update type.")
        if kind == "cancelled" and not text:
            text = "This game is cancelled."
        with self.tx() as db:
            game = self._game_date(db).isoformat()
            for key, value in (("notice_text", text), ("notice_kind", kind if text else "info"), ("notice_game", game if text else "")):
                db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))

    def shuffle_teams(self):
        with self.tx() as db:
            game = self._game_date(db).isoformat()
            for key, value in (("team_seed", str(secrets.randbelow(2**31 - 1) + 1)), ("team_seed_game", game)):
                db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))

    def add_permit(self, title, permit_date, note):
        title = clean_text(title, "The title", 2, 60, "Give the permit reminder a short title.")
        note = clean_text(note if note is not None else "", "The note", 0, 120, "Notes can be up to 120 characters.")
        try:
            due = date.fromisoformat(permit_date)
        except (TypeError, ValueError):
            raise PollError("Choose a valid date.")
        today = self.today()
        if due < today:
            raise PollError("That date has already passed. Choose today or a later date.")
        if due > today + timedelta(days=3 * 366):
            raise PollError("Choose a date within the next three years.")
        with self.tx() as db:
            if db.execute("SELECT COUNT(*) FROM permits").fetchone()[0] >= MAX_PERMITS:
                raise PollError("There are already 50 reminders. Remove one first.", 409)
            db.execute("INSERT INTO permits(title, date, note) VALUES (?, ?, ?)", (title, due.isoformat(), note))

    def delete_permit(self, permit_id):
        with self.tx() as db:
            db.execute("DELETE FROM permits WHERE id = ?", (permit_id,))

    def update_game(self, data):
        club = clean_text(data.get("club_name"), "The club name", 2, 48, "Club name must be 2 to 48 characters.")
        location = clean_text(data.get("location"), "The location", 2, 100, "Add a field or meeting point.")
        try:
            chosen = date.fromisoformat(data.get("game_date"))
        except (TypeError, ValueError):
            raise PollError("Choose a valid Thursday.")
        if chosen.weekday() != 3:
            raise PollError("Game day must be a Thursday.")
        if chosen < self.today():
            raise PollError("That Thursday has already passed.")
        kickoff = data.get("kickoff")
        if not isinstance(kickoff, str) or not TIME_RE.fullmatch(kickoff):
            raise PollError("Choose a valid kickoff time.")
        end_time = data.get("end_time")
        if end_time is not None and (not isinstance(end_time, str) or not TIME_RE.fullmatch(end_time) or end_time <= kickoff):
            raise PollError("The end time must be a valid time after kickoff.")
        notes = data.get("notes")
        if notes is not None:
            notes = clean_text(notes, "The notes", 0, 200, "Notes can be up to 200 characters.")
        capacity = data.get("max_players")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 2 <= capacity <= 40:
            raise PollError("Squad size must be between 2 and 40.")
        with self.tx() as db:
            going = db.execute("SELECT COUNT(*) FROM rsvps WHERE game_date = ? AND status = 'going' AND waitlisted = 0 AND is_sample = 0",
                               (chosen.isoformat(),)).fetchone()[0]
            if capacity < going:
                raise PollError(f"{going} players are already going on that date. Choose a squad size of at least {going}.", 409)
            updates = [("club_name", club), ("location", location), ("game_date", chosen.isoformat()),
                       ("kickoff", kickoff), ("max_players", str(capacity))]
            if end_time is not None:
                updates.append(("end_time", end_time))
            elif self._settings(db)["end_time"] <= kickoff:
                raise PollError("The end time must be after kickoff.")
            if notes is not None:
                updates.append(("notes", notes))
            for key, value in updates:
                db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            self._promote(db, chosen.isoformat())

    def clear_sample(self):
        with self.tx() as db:
            db.execute("DELETE FROM rsvps WHERE is_sample = 1")
            db.execute("DELETE FROM permits WHERE is_sample = 1")

    def reset_week(self):
        with self.tx() as db:
            db.execute("DELETE FROM rsvps WHERE game_date = ?", (self._game_date(db).isoformat(),))


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------

def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def local_names(bind_host):
    """Host names this server should answer to. Anything else is refused, which stops a web page
    on another domain from reaching a server on your machine or network by pointing its name at it."""
    names = {"localhost", "127.0.0.1", "::1"}
    if bind_host not in ("0.0.0.0", "::", ""):
        names.add(bind_host.lower())
        return names
    try:
        names.add(socket.gethostname().lower())
        names.add(socket.gethostname().split(".")[0].lower() + ".local")
    except OSError:
        pass
    return names


def access_urls(bind_host, port):
    """Return usable URLs, rather than advertising a wildcard bind address to players."""
    if bind_host not in ("0.0.0.0", "::", ""):
        host = f"[{bind_host}]" if ":" in bind_host and not bind_host.startswith("[") else bind_host
        return [f"http://{host}:{port}"]

    urls = [f"http://127.0.0.1:{port}"]
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        addresses = ()
    ipv4 = set()
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address[4][0])
        except ValueError:
            continue
        if not parsed.is_loopback and not parsed.is_unspecified and (parsed.is_private or parsed.is_link_local):
            ipv4.add(str(parsed))
    urls.extend(f"http://{address}:{port}" for address in sorted(ipv4))
    return urls


class Limiter:
    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock, self.hits = limit, window, clock, {}

    def allow(self, key):
        now = self.clock()
        recent = [t for t in self.hits.get(key, []) if now - t < self.window]
        if len(recent) >= self.limit:
            self.hits[key] = recent
            return False
        recent.append(now)
        self.hits[key] = recent
        return True


class Security:
    def __init__(self, bind_host, pin=None, extra_hosts=(), clock=time.monotonic):
        self.bind_host = bind_host
        self.pin = pin
        self.names = local_names(bind_host) | {h.lower() for h in extra_hosts}
        self.private_ips_ok = not is_loopback(bind_host)
        self.clock = clock
        self.failures, self.locked = {}, {}
        self.writes = Limiter(120, 60, clock)

    def host_allowed(self, header):
        host = (header or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]")[0]
        elif host.count(":") == 1:
            host = host.split(":")[0]
        if host in self.names:
            return True
        if self.private_ips_ok:        # someone typed this machine's network address into their browser
            try:
                ip = ipaddress.ip_address(host)
                return ip.is_private or ip.is_loopback or ip.is_link_local
            except ValueError:
                return False
        return False

    def check_pin(self, client, provided):
        if self.pin is None:
            return
        if not provided:
            raise PollError("The organizer PIN is required for that.", 403)
        if self.locked.get(client, 0) > self.clock():
            raise PollError("Too many wrong PINs. Try again in a minute.", 429)
        if hmac.compare_digest(provided.encode("utf-8"), self.pin.encode("utf-8")):
            self.failures.pop(client, None)
            return
        self.failures[client] = self.failures.get(client, 0) + 1
        if self.failures[client] >= 5:
            self.locked[client] = self.clock() + 60
            self.failures.pop(client, None)
            raise PollError("Too many wrong PINs. Try again in a minute.", 429)
        raise PollError("That organizer PIN is not right.", 403)


class PollApp:
    def __init__(self, store, security):
        self.store, self.security = store, security


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PickupPoll/2.0"
    timeout = 10          # a client that stalls cannot hold a thread for ever

    @property
    def poll(self):
        return self.server.poll

    def log_message(self, format_string, *args):
        # Do not log query strings: the RSVP delete endpoint includes a player's name there.
        method = getattr(self, "command", "-") or "-"
        path = re.sub(r"[\x00-\x1f\x7f]", "", urlparse(getattr(self, "path", "")).path)[:120] or "-"
        status = "-"
        status_arg = args[3] if len(args) >= 5 else args[1] if len(args) >= 2 else None
        if status_arg is not None and str(status_arg).isdigit():
            status = str(status_arg)
        print("[%s] %s %s %s" % (self.log_date_time_string(), method, path, status))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(payload)

    def _fail(self, err):
        self._send(err.status, {"error": str(err)})

    def _check_host(self):
        if not self.poll.security.host_allowed(self.headers.get("Host")):
            raise PollError("This address is not allowed. Open the page using the address printed when the server started.", 421)

    def _check_write(self):
        """Only this page may write: it sends a custom header (which other sites cannot send
        without permission), comes from the same origin, and is not flooding the server."""
        if self.headers.get("X-Pickup-Poll") != "1":
            raise PollError("Requests must come from the Pickup Poll page.", 403)
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise PollError("Requests from other sites are not allowed.", 403)
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            raise PollError("Requests from other sites are not allowed.", 403)
        if not self.poll.security.writes.allow(self.client_address[0]):
            raise PollError("Too many changes in a short time. Wait a minute.", 429)

    def _need_organizer(self):
        self.poll.security.check_pin(self.client_address[0], self.headers.get("X-Organizer-Pin"))

    def _json_body(self):
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise PollError("Send the request as application/json.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_BODY:
                raise ValueError
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise PollError("Send a valid JSON object of at most 16 KB.")

    def do_GET(self):
        try:
            self._check_host()
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                state = self.poll.store.state(self.headers.get("X-Player-Token"))
                state["organizer_required"] = self.poll.security.pin is not None
                self._send(200, state)
            elif parsed.path in STATIC:
                filename, content_type = STATIC[parsed.path]
                self._send(200, (ROOT / filename).read_bytes(), content_type)
            else:
                raise PollError("Not found.", 404)
        except OSError:
            self._send(404, {"error": "File not found."})
        except PollError as err:
            self._fail(err)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            self._check_host()
            self._check_write()
            data = self._json_body()
            store = self.poll.store
            if path == "/api/rsvps":
                store.save_rsvp(data.get("name"), data.get("status"), self.headers.get("X-Player-Token"))
            elif path == "/api/lock":
                store.lock_rsvp(data.get("name"), self.headers.get("X-Player-Token"))
            elif path in ("/api/unlock", "/api/game", "/api/permits", "/api/clear-sample", "/api/reset-week", "/api/notice", "/api/shuffle"):
                self._need_organizer()
                if path == "/api/notice":
                    store.set_notice(data.get("text"), data.get("kind", "info"))
                elif path == "/api/shuffle":
                    store.shuffle_teams()
                elif path == "/api/game":
                    store.update_game(data)
                elif path == "/api/permits":
                    store.add_permit(data.get("title"), data.get("date"), data.get("note"))
                elif path == "/api/clear-sample":
                    store.clear_sample()
                elif path == "/api/reset-week":
                    store.reset_week()
            else:
                raise PollError("Not found.", 404)
            state = store.state(self.headers.get("X-Player-Token"))
            state["organizer_required"] = self.poll.security.pin is not None
            self._send(200, state)
        except PollError as err:
            self._fail(err)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            self._check_host()
            self._check_write()
            if parsed.path == "/api/rsvps":
                self.poll.store.delete_rsvp(query.get("name", [""])[0], self.headers.get("X-Player-Token"))
            elif parsed.path == "/api/permits":
                self._need_organizer()
                try:
                    self.poll.store.delete_permit(int(query.get("id", [""])[0]))
                except ValueError:
                    raise PollError("That item could not be removed.")
            else:
                raise PollError("Not found.", 404)
            state = self.poll.store.state(self.headers.get("X-Player-Token"))
            state["organizer_required"] = self.poll.security.pin is not None
            self._send(200, state)
        except PollError as err:
            self._fail(err)

    def _method_not_allowed(self):
        self._send(405, {"error": "That method is not supported."})

    do_PUT = do_PATCH = do_OPTIONS = do_HEAD = _method_not_allowed


def make_server(data_dir, host="127.0.0.1", port=8787, pin=None, extra_hosts=(), demo=False, today=date.today):
    """Build the server. A PIN is required when the server can be reached from other devices;
    one is generated if none is given. Returns (server, pin_was_generated)."""
    generated = False
    if pin is None and not is_loopback(host):
        pin, generated = "".join(secrets.choice("0123456789") for _ in range(6)), True
    if pin is not None and len(pin) < 4:
        raise ValueError("The organizer PIN must be at least 4 characters.")
    store = Store(data_dir, today)
    store.initialize(demo)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.poll = PollApp(store, Security(host, pin, extra_hosts))
    return server, generated


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the Pickup Poll club RSVP app.")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default: this computer only)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", default=os.environ.get("PICKUP_POLL_DATA", str(ROOT / "data")), help="where the database lives")
    parser.add_argument("--organizer-pin", default=os.environ.get("PICKUP_POLL_PIN"), help="PIN for organizer actions (or set PICKUP_POLL_PIN)")
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME", help="an extra host name this server may answer to")
    parser.add_argument("--demo", action="store_true", help="fill an empty database with clearly marked sample players and dates")
    args = parser.parse_args(argv)
    try:
        server, generated = make_server(args.data_dir, args.host, args.port, args.organizer_pin, args.allow_host, args.demo)
    except (ValueError, OSError) as err:
        print(f"pickup-poll: {err}")
        return 1
    actual_port = server.server_address[1]
    urls = access_urls(args.host, actual_port)
    print("Pickup Poll is running at:")
    for url in urls:
        print("  " + url)
    if args.host in ("0.0.0.0", "::", "") and len(urls) == 1:
        print("No local network address was detected. Use this computer's Wi-Fi IP on other devices.")
    print(f"Data is stored in {server.poll.store.path} (private to your user). Press Ctrl+C to stop.")
    if server.poll.security.pin is not None:
        print("Organizer PIN: " + ("%s (generated for this run)" % server.poll.security.pin if generated else "set") +
              ". It is needed to edit the match, add or remove permit dates, and remove players.")
    if not is_loopback(args.host):
        print("This server can be reached from other devices on your network. Use the organizer PIN and keep it off the public internet.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Pickup Poll.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
