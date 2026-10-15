import http.client
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as pp  # noqa: E402

pp.Handler.log_message = lambda *args: None     # keep test output readable

TODAY = date(2026, 10, 13)   # a Tuesday: the next game is Thursday 15 October
TOKEN_A = "a" * 24
TOKEN_B = "b" * 24
HEADERS = {"X-Pickup-Poll": "1", "Content-Type": "application/json", "X-Player-Token": TOKEN_A}


def as_player(token, **extra):
    return {**HEADERS, "X-Player-Token": token, **extra}


class Base(unittest.TestCase):
    pin = None
    demo = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._player_tokens = {}
        self.server, _ = pp.make_server(os.path.join(self.tmp, "data"), "127.0.0.1", 0, self.pin, demo=self.demo, today=lambda: TODAY)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, thread)

    def _stop(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(5)

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = raw if raw is not None else (json.dumps(body) if body is not None else None)
            conn.request(method, path, body=data, headers=headers if headers is not None else (HEADERS if method != "GET" else {}))
            resp = conn.getresponse()
            payload = resp.read()
            try:
                return resp.status, json.loads(payload)
            except ValueError:
                return resp.status, payload
        finally:
            conn.close()

    def player_token(self, name):
        key = pp.name_key(str(name))
        if key not in self._player_tokens:
            index = len(self._player_tokens)
            self._player_tokens[key] = (TOKEN_A, TOKEN_B)[index] if index < 2 else f"test_{index:020d}"
        return self._player_tokens[key]

    def player_headers(self, name):
        return as_player(self.player_token(name))

    def rsvp(self, name, status="going", **kw):
        if "headers" not in kw:
            kw["headers"] = self.player_headers(name)
        return self.call("POST", "/api/rsvps", {"name": name, "status": status}, **kw)

    def state(self):
        return self.call("GET", "/api/state")[1]

    def game(self, **over):
        data = {"club_name": "Football Not Soccer Club", "location": "McManus Field", "game_date": "2026-10-15", "kickoff": "16:00",
                "end_time": "18:00", "notes": "Pinnies provided.", "max_players": 14}
        data.update(over)
        return data


class RsvpTests(Base):
    def test_a_fresh_install_has_no_players_and_the_next_thursday(self):
        st = self.state()
        self.assertEqual((st["rsvps"], st["permits"], st["has_sample"]), ([], [], False))
        self.assertEqual(st["settings"]["game_date"], "2026-10-15")

    def test_rsvp_and_change_of_mind(self):
        self.assertEqual(self.rsvp("Maya T.")[0], 200)
        self.rsvp("maya t.", "out")
        players = self.state()["rsvps"]
        self.assertEqual([(p["name"], p["status"]) for p in players], [("maya t.", "out")])

    def test_names_that_differ_only_by_case_or_form_are_one_person(self):
        self.rsvp("Éric")
        self.rsvp("éric")           # the same name typed with a combining accent
        self.rsvp("ÉRIC")
        self.assertEqual(len(self.state()["rsvps"]), 1)

    def test_names_are_cleaned_and_checked(self):
        self.rsvp("  Sam   P.  ")
        self.assertEqual(self.state()["rsvps"][0]["name"], "Sam P.")
        self.rsvp("Line\nBreak\r\nName")                    # pasted line breaks become single spaces, never stored as line breaks
        self.assertIn("Line Break Name", [r["name"] for r in self.state()["rsvps"]])
        for bad in ("", "   ", "x" * 37, "bad\x00name", "bell\x07", "zero​width", 5, None):
            code, body = self.rsvp(bad)
            self.assertEqual(code, 400, repr(bad))
            self.assertIn("error", body)
        self.assertEqual(self.rsvp("Sam", "dancing")[0], 400)

    def test_markup_in_a_name_is_stored_as_plain_text(self):
        self.rsvp("<script>alert(1)</script>")
        self.assertEqual(self.state()["rsvps"][0]["name"], "<script>alert(1)</script>")

    def test_a_full_squad_puts_the_next_player_on_a_waitlist_who_moves_up_when_a_spot_frees(self):
        self.call("POST", "/api/game", self.game(max_players=3))
        for n in "ABC":
            self.assertEqual(self.rsvp(n)[0], 200)
        self.assertEqual(self.rsvp("D")[0], 200)
        self.assertEqual(self.rsvp("E")[0], 200)
        rows = {r["name"]: r for r in self.state()["rsvps"]}
        self.assertEqual((rows["D"]["waitlisted"], rows["D"]["waitlist_pos"], rows["E"]["waitlist_pos"]), (True, 1, 2))
        self.assertFalse(rows["A"]["waitlisted"])
        self.assertEqual(self.rsvp("A", "going")[0], 200)       # someone already in confirming again changes nothing
        self.rsvp("A", "out")
        rows = {r["name"]: r for r in self.state()["rsvps"]}
        self.assertEqual((rows["D"]["waitlisted"], rows["E"]["waitlist_pos"]), (False, 1))       # D moved up, E is next
        self.assertEqual(self.rsvp("F", "maybe")[0], 200)       # maybe never takes a spot or a waitlist place

    def test_the_waitlist_is_first_come_first_served_and_a_raised_squad_size_promotes(self):
        self.call("POST", "/api/game", self.game(max_players=2))
        for n in "ABCDE":
            self.rsvp(n)
        self.call("POST", "/api/game", self.game(max_players=4))
        rows = {r["name"]: r for r in self.state()["rsvps"]}
        self.assertEqual([rows[n]["waitlisted"] for n in "ABCDE"], [False, False, False, False, True])

    def test_leaving_the_waitlist_and_asking_again_goes_to_the_back(self):
        self.call("POST", "/api/game", self.game(max_players=2))
        for n in "ABCD":
            self.rsvp(n)
        self.rsvp("C", "maybe")
        self.rsvp("C", "going")
        rows = {r["name"]: r["waitlist_pos"] for r in self.state()["rsvps"]}
        self.assertEqual((rows["D"], rows["C"]), (1, 2))

    def test_removing_a_confirmed_player_promotes_the_first_in_line(self):
        self.call("POST", "/api/game", self.game(max_players=2))
        for n in ("A", "B", "C"):
            self.rsvp(n, headers=self.player_headers(n))
        self.call("DELETE", "/api/rsvps?name=A", headers=self.player_headers("A"))
        self.assertEqual(sorted((r["name"], r["waitlisted"]) for r in self.state()["rsvps"]), [("B", False), ("C", False)])

    def test_a_waitlisted_player_cannot_lock_and_can_leave_even_in_the_last_day(self):
        self.call("POST", "/api/game", self.game(max_players=2))
        for n in ("A", "B"):
            self.rsvp(n, headers=self.player_headers(n))
        self.rsvp("W", headers=self.player_headers("W"))
        self.server.poll.store.today = lambda: date(2026, 10, 14)
        self.assertEqual(self.call("POST", "/api/lock", {"name": "W"}, headers=self.player_headers("W"))[0], 409)
        row = [r for r in self.state()["rsvps"] if r["name"] == "W"][0]
        self.assertEqual((row["waitlisted"], row["locked"]), (True, False))
        self.assertEqual(self.rsvp("W", "out", headers=self.player_headers("W"))[0], 200)

    def test_a_game_cannot_collect_unlimited_replies(self):
        store = self.server.poll.store
        for i in range(pp.MAX_RSVPS_PER_GAME):
            name = f"player {i}"
            store.save_rsvp(name, "maybe", self.player_token(name))
        with self.assertRaises(pp.PollError):
            store.save_rsvp("one too many", "maybe", self.player_token("one too many"))
        store.save_rsvp("player 1", "out", self.player_token("player 1"))         # existing players can still change their answer


class WeekTests(Base):
    def test_each_game_date_starts_empty_and_old_answers_are_kept_as_history(self):
        self.rsvp("Maya")
        self.rsvp("Chris", "maybe")
        self.call("POST", "/api/game", self.game(game_date="2026-10-22"))
        st = self.state()
        self.assertEqual(st["rsvps"], [])
        self.assertEqual(st["recent"], [])           # that game has not been played yet, so it is not history
        self.call("POST", "/api/game", self.game(game_date="2026-10-15"))
        self.assertEqual(len(self.state()["rsvps"]), 2)     # going back brings the answers back

    def test_a_game_date_in_the_past_rolls_to_the_next_thursday(self):
        self.rsvp("Maya")
        self.server.poll.store.today = lambda: date(2026, 10, 16)       # the day after the game
        st = self.state()
        self.assertEqual(st["settings"]["game_date"], "2026-10-22")
        self.assertEqual(st["rsvps"], [])
        self.assertEqual([(g["game_date"], g["going"]) for g in st["recent"]], [("2026-10-15", 1)])

    def test_game_day_itself_still_shows_that_game(self):
        self.server.poll.store.today = lambda: date(2026, 10, 15)
        self.assertEqual(self.state()["settings"]["game_date"], "2026-10-15")

    def test_game_settings_are_validated(self):
        for over in ({"game_date": "2026-10-16"}, {"game_date": "2026-10-08"}, {"game_date": "soon"}, {"kickoff": "25:00"}, {"kickoff": "6pm"},
                     {"max_players": 1}, {"max_players": 41}, {"max_players": "14"}, {"max_players": True}, {"club_name": "x"},
                     {"location": ""}, {"location": "a\x07b"}):
            self.assertEqual(self.call("POST", "/api/game", self.game(**over))[0], 400, over)
        self.assertEqual(self.call("POST", "/api/game", self.game(club_name="Club", kickoff="07:05"))[0], 200)

    def test_the_squad_cannot_be_set_below_the_players_already_going(self):
        for n in "ABCD":
            self.rsvp(n)
        code, body = self.call("POST", "/api/game", self.game(max_players=3))
        self.assertEqual(code, 409)
        self.assertIn("4 players", body["error"])

    def test_clearing_a_game(self):
        self.rsvp("Maya")
        self.assertEqual(self.call("POST", "/api/reset-week", {})[0], 200)
        self.assertEqual(self.state()["rsvps"], [])


class SampleDataTests(Base):
    demo = True

    def test_sample_data_is_marked_and_never_takes_a_real_spot(self):
        st = self.state()
        self.assertTrue(st["has_sample"])
        self.assertTrue(all(r["is_sample"] for r in st["rsvps"]) and len(st["rsvps"]) == 6)
        self.assertTrue(all(p["is_sample"] for p in st["permits"]))
        self.call("POST", "/api/game", self.game(max_players=2))
        self.assertEqual(self.rsvp("Real One")[0], 200)
        self.assertEqual(self.rsvp("Real Two")[0], 200)
        self.assertEqual(self.rsvp("Real Three")[0], 200)
        self.assertTrue([r for r in self.state()["rsvps"] if r["name"] == "Real Three"][0]["waitlisted"])

    def test_a_sample_player_who_replies_becomes_real(self):
        self.rsvp("Maya T.", "out")
        row = [r for r in self.state()["rsvps"] if r["name"] == "Maya T."][0]
        self.assertFalse(row["is_sample"])

    def test_clearing_sample_data_leaves_real_data(self):
        self.rsvp("Real")
        self.call("POST", "/api/permits", {"title": "My permit", "date": "2026-11-20", "note": ""})
        self.call("POST", "/api/clear-sample", {})
        st = self.state()
        self.assertEqual([r["name"] for r in st["rsvps"]], ["Real"])
        self.assertEqual([p["title"] for p in st["permits"]], ["My permit"])
        self.assertFalse(st["has_sample"])

    def test_demo_data_is_not_added_again_on_restart(self):
        self.call("POST", "/api/clear-sample", {})
        self.server.poll.store.initialize(demo=True)
        self.assertEqual(self.state()["rsvps"], [])


class PermitTests(Base):
    def permit(self, **over):
        data = {"title": "Field permit", "date": "2026-11-20", "note": "Call the office"}
        data.update(over)
        return self.call("POST", "/api/permits", data)

    def test_add_and_remove(self):
        self.assertEqual(self.permit()[0], 200)
        pid = self.state()["permits"][0]["id"]
        self.assertEqual(self.call("DELETE", f"/api/permits?id={pid}")[0], 200)
        self.assertEqual(self.state()["permits"], [])
        self.assertEqual(self.call("DELETE", "/api/permits?id=abc")[0], 400)

    def test_validation(self):
        for over in ({"date": "2026-10-12"}, {"date": "2040-01-01"}, {"date": "tomorrow"}, {"date": None}, {"title": "x"},
                     {"title": "bad\x00title"}, {"note": "x\x1bATTACH"}, {"note": "n" * 121}):
            self.assertEqual(self.permit(**over)[0], 400, over)
        self.assertEqual(self.permit(date="2026-10-13", note="")[0], 200)       # today is allowed

    def test_line_breaks_in_a_note_cannot_reach_the_calendar_file(self):
        self.permit(note="x\r\nATTACH:http://evil.example")
        note = self.state()["permits"][0]["note"]
        self.assertEqual(note, "x ATTACH:http://evil.example")
        self.assertFalse(any(c in note for c in "\r\n"))

    def test_the_number_of_reminders_is_capped(self):
        for i in range(pp.MAX_PERMITS):
            self.server.poll.store.add_permit(f"Permit {i}", "2026-12-01", "")
        self.assertEqual(self.permit()[0], 409)


class RequestSafetyTests(Base):
    def test_page_files_are_served_with_security_headers(self):
        for path in ("/", "/app.js", "/ics.js", "/celebrate.js", "/styles.css"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200, path)
            self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
            self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
            conn.close()

    def test_a_write_needs_the_custom_header(self):
        code, _ = self.call("POST", "/api/rsvps", {"name": "X", "status": "going"}, headers={"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        self.assertEqual(self.state()["rsvps"], [])

    def test_a_plain_text_body_is_refused(self):
        """The 'simple request' a hostile web page can send without any permission."""
        code, _ = self.call("POST", "/api/rsvps", raw='{"name":"X","status":"going"}', headers={"Content-Type": "text/plain", "X-Pickup-Poll": "1"})
        self.assertEqual(code, 415)
        code, _ = self.call("POST", "/api/game", raw=json.dumps(self.game(club_name="Hijacked FC")), headers={"Content-Type": "text/plain"})
        self.assertIn(code, (403, 415))
        self.assertEqual(self.state()["settings"]["club_name"], "Football Not Soccer Club")

    def test_requests_from_another_origin_are_refused(self):
        code, _ = self.rsvp("X", headers={**HEADERS, "Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.rsvp("X", headers={**HEADERS, "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)
        code, _ = self.call("DELETE", "/api/rsvps?name=X", headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.rsvp("Fine", headers={**HEADERS, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 200)

    def test_another_host_name_is_refused(self):
        for host in ("attacker.example", f"attacker.example:{self.port}", "127.0.0.1.evil.example"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            conn.request("GET", "/api/state", headers={"Host": host})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 421, host)
            conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/state", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(conn.getresponse().status, 200)

    def test_bad_bodies_and_methods(self):
        self.assertEqual(self.call("POST", "/api/rsvps", raw="[1,2]")[0], 400)
        self.assertEqual(self.call("POST", "/api/rsvps", raw="{nope")[0], 400)
        self.assertEqual(self.call("POST", "/api/rsvps", raw="")[0], 400)
        self.assertEqual(self.call("POST", "/api/rsvps", raw='{"name":"' + "a" * 20000 + '"}')[0], 400)
        self.assertEqual(self.call("POST", "/api/nothing", {})[0], 404)
        self.assertEqual(self.call("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.call("PUT", "/api/rsvps", {})[0], 405)

    def test_writes_are_rate_limited(self):
        codes = [self.rsvp(f"p{i}", "out")[0] for i in range(125)]
        self.assertEqual(codes.count(429), 5)

    def test_the_limiter_forgets_old_hits(self):
        now = [0.0]
        limiter = pp.Limiter(2, 10, lambda: now[0])
        self.assertEqual([limiter.allow("a"), limiter.allow("a"), limiter.allow("a")], [True, True, False])
        now[0] = 11
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))


class PinTests(Base):
    pin = "4321"

    def organizer(self, pin="4321"):
        return {**HEADERS, "X-Organizer-Pin": pin}

    def test_players_do_not_need_the_pin_but_organizer_actions_do(self):
        self.assertEqual(self.rsvp("Maya")[0], 200)
        self.assertTrue(self.state()["organizer_required"])
        self.assertEqual(self.call("POST", "/api/game", self.game())[0], 403)
        self.assertEqual(self.call("POST", "/api/permits", {"title": "Permit", "date": "2026-11-20"})[0], 403)
        self.assertEqual(self.call("DELETE", "/api/permits?id=1")[0], 403)
        self.assertEqual(self.call("POST", "/api/clear-sample", {})[0], 403)
        self.assertEqual(self.call("POST", "/api/reset-week", {})[0], 403)
        self.assertEqual(len(self.state()["rsvps"]), 1)

    def test_the_right_pin_works(self):
        self.assertEqual(self.call("POST", "/api/game", self.game(club_name="Renamed"), headers=self.organizer())[0], 200)
        self.assertEqual(self.call("DELETE", "/api/permits?id=1", headers=self.organizer())[0], 200)
        self.assertEqual(self.call("POST", "/api/unlock", {}, headers=self.organizer())[0], 200)

    def test_five_wrong_pins_lock_the_door_even_for_the_right_one(self):
        codes = [self.call("POST", "/api/game", self.game(), headers=self.organizer("0000"))[0] for _ in range(5)]
        self.assertEqual(codes, [403, 403, 403, 403, 429])
        self.assertEqual(self.call("POST", "/api/game", self.game(), headers=self.organizer())[0], 429)

    def test_a_pin_is_generated_when_the_server_is_open_to_the_network(self):
        server, generated = pp.make_server(os.path.join(self.tmp, "net"), "0.0.0.0", 0, None, today=lambda: TODAY)
        self.addCleanup(server.server_close)
        self.assertTrue(generated)
        self.assertRegex(server.poll.security.pin, r"^\d{6}$")
        self.assertTrue(server.poll.security.host_allowed("192.168.1.20:8787"))
        self.assertFalse(server.poll.security.host_allowed("8.8.8.8"))
        self.assertFalse(server.poll.security.host_allowed("evil.example"))

    def test_a_short_pin_is_refused(self):
        with self.assertRaises(ValueError):
            pp.make_server(os.path.join(self.tmp, "x"), "127.0.0.1", 0, "12", today=lambda: TODAY)


class DetailsTests(Base):
    def test_a_fresh_install_has_the_real_club_details(self):
        s = self.state()["settings"]
        self.assertEqual((s["club_name"], s["kickoff"], s["end_time"], s["max_players"]), ("Football Not Soccer Club", "16:00", "18:00", 14))
        self.assertIn("Jack McManus Field", s["location"])
        self.assertIn("lights", s["notes"])

    def test_end_time_and_notes_are_validated_and_saved(self):
        self.assertEqual(self.call("POST", "/api/game", self.game(end_time="15:00"))[0], 400)          # before kickoff
        self.assertEqual(self.call("POST", "/api/game", self.game(end_time="16:00"))[0], 400)          # equal to kickoff
        self.assertEqual(self.call("POST", "/api/game", self.game(end_time="6pm"))[0], 400)
        self.assertEqual(self.call("POST", "/api/game", self.game(notes="n" * 201))[0], 400)
        self.assertEqual(self.call("POST", "/api/game", self.game(notes="bad\x00note"))[0], 400)
        code, body = self.call("POST", "/api/game", self.game(end_time="17:30", notes="Bring a dark shirt."))
        self.assertEqual((code, body["settings"]["end_time"], body["settings"]["notes"]), (200, "17:30", "Bring a dark shirt."))

    def test_leaving_out_end_time_and_notes_keeps_the_old_values(self):
        data = self.game()
        data.pop("end_time"); data.pop("notes")
        code, body = self.call("POST", "/api/game", data)
        self.assertEqual((code, body["settings"]["end_time"]), (200, "18:00"))

    def test_the_first_versions_placeholders_are_replaced_once_and_real_choices_are_kept(self):
        path = os.path.join(self.tmp, "old2")
        os.makedirs(path)
        db = sqlite3.connect(os.path.join(path, "pickup-poll.sqlite3"))
        db.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES ('club_name','Thursday United'),('game_date','2026-10-15'),('kickoff','18:30'),
                                        ('location','Your usual pitch'),('max_players','14');
        """)
        db.commit(); db.close()
        store = pp.Store(path, lambda: TODAY)
        store.initialize()
        s = store.state()["settings"]
        self.assertEqual((s["club_name"], s["kickoff"]), ("Football Not Soccer Club", "16:00"))
        store.update_game({"club_name": "My Own Club", "location": "Somewhere", "game_date": "2026-10-15", "kickoff": "18:30",
                          "end_time": "20:00", "max_players": 14})
        store.initialize()                                           # a restart must not undo the organizer's edits
        self.assertEqual(store.state()["settings"]["club_name"], "My Own Club")


class NoticeTests(Base):
    def notice(self, text, kind="info", headers=None):
        return self.call("POST", "/api/notice", {"text": text, "kind": kind}, headers=headers)

    def test_the_organizer_can_post_and_clear_an_update(self):
        self.assertEqual(self.state()["notice"], None)
        code, body = self.notice("Moved to Pony Field")
        self.assertEqual((code, body["notice"]), (200, {"text": "Moved to Pony Field", "kind": "info"}))
        self.assertEqual(self.notice("")[1]["notice"], None)

    def test_a_cancellation_stops_new_yes_and_maybe_but_not_out(self):
        self.rsvp("Maya")
        self.notice("Cancelled: rain", "cancelled")
        self.assertEqual(self.state()["notice"]["kind"], "cancelled")
        code, body = self.rsvp("Chris", "going")
        self.assertEqual(code, 409)
        self.assertIn("cancelled", body["error"])
        self.assertEqual(self.rsvp("Chris", "maybe")[0], 409)
        self.assertEqual(self.rsvp("Maya", "out")[0], 200)

    def test_cancelling_without_text_uses_a_default_and_a_new_date_forgets_the_update(self):
        self.assertEqual(self.notice("", "cancelled")[1]["notice"]["text"], "This game is cancelled.")
        self.call("POST", "/api/game", self.game(game_date="2026-10-22"))
        self.assertIsNone(self.state()["notice"])
        self.assertEqual(self.rsvp("Maya")[0], 200)

    def test_updates_are_validated_and_organizer_only(self):
        self.assertEqual(self.notice("x" * 141)[0], 400)
        self.assertEqual(self.notice("bad\x00text")[0], 400)
        self.assertEqual(self.notice("ok", "weird")[0], 400)
        self.server.poll.security.pin = "4321"
        self.assertEqual(self.notice("Cancelled", "cancelled")[0], 403)
        self.assertIsNone(self.state()["notice"])


class TeamSeedTests(Base):
    def test_the_organizer_can_shuffle_and_a_new_date_resets_it(self):
        self.assertEqual(self.state()["settings"]["team_seed"], 0)
        code, body = self.call("POST", "/api/shuffle", {})
        seed = body["settings"]["team_seed"]
        self.assertEqual(code, 200)
        self.assertGreater(seed, 0)
        self.call("POST", "/api/game", self.game(game_date="2026-10-22"))
        self.assertEqual(self.state()["settings"]["team_seed"], 0)

    def test_shuffling_needs_the_pin_when_there_is_one(self):
        self.server.poll.security.pin = "4321"
        self.assertEqual(self.call("POST", "/api/shuffle", {})[0], 403)


class OwnershipTests(Base):
    def test_only_the_browser_that_added_a_name_can_remove_it(self):
        self.rsvp("Maya", headers=as_player(TOKEN_A))
        code, body = self.call("DELETE", "/api/rsvps?name=Maya", headers=as_player(TOKEN_B))
        self.assertEqual(code, 403)
        self.assertIn("own name", body["error"])
        self.assertEqual(len(self.state()["rsvps"]), 1)
        code, body = self.call("DELETE", "/api/rsvps?name=maya", headers=as_player(TOKEN_A))      # any capitalisation of your own name
        self.assertEqual((code, body["rsvps"]), (200, []))

    def test_the_organizer_pin_does_not_let_anyone_remove_a_player(self):
        self.server.poll.security.pin = "4321"
        self.rsvp("Maya", headers=as_player(TOKEN_A))
        code, _ = self.call("DELETE", "/api/rsvps?name=Maya", headers=as_player(TOKEN_B, **{"X-Organizer-Pin": "4321"}))
        self.assertEqual(code, 403)
        self.assertEqual(len(self.state()["rsvps"]), 1)

    def test_nobody_can_change_another_persons_answer_either(self):
        self.rsvp("Maya", "going", headers=as_player(TOKEN_A))
        code, body = self.rsvp("MAYA", "out", headers=as_player(TOKEN_B))
        self.assertEqual(code, 403)
        self.assertEqual(self.state()["rsvps"][0]["status"], "going")
        self.assertEqual(self.rsvp("maya", "maybe", headers=as_player(TOKEN_A))[0], 200)       # the owner can

    def test_the_page_is_told_which_names_are_yours_and_never_sees_the_hashes(self):
        self.rsvp("Maya", headers=as_player(TOKEN_A))
        self.rsvp("Chris", headers=as_player(TOKEN_B))
        mine = lambda token: sorted(r["name"] for r in self.call("GET", "/api/state", headers={"X-Player-Token": token})[1]["rsvps"] if r["mine"])
        self.assertEqual((mine(TOKEN_A), mine(TOKEN_B), mine("c" * 24)), (["Maya"], ["Chris"], []))
        raw = json.dumps(self.state())
        self.assertNotIn("owner_hash", raw)
        self.assertNotIn(__import__("hashlib").sha256(TOKEN_A.encode()).hexdigest(), raw)

    def test_a_reply_needs_a_valid_token(self):
        for token in (None, "short", "x" * 65, "has space " * 3):
            headers = {"X-Pickup-Poll": "1", "Content-Type": "application/json"}
            if token is not None:
                headers["X-Player-Token"] = token
            self.assertEqual(self.rsvp("Maya", headers=headers)[0], 400, token)
        self.assertEqual(self.state()["rsvps"], [])

    def test_names_without_an_owner_cannot_be_removed_by_anyone(self):
        self.server.poll.store.initialize(demo=True)
        self.assertEqual(self.call("DELETE", "/api/rsvps?name=Maya%20T.", headers=as_player(TOKEN_A))[0], 403)

    def test_a_sample_name_can_be_claimed_by_a_real_player(self):
        self.server.poll.store.initialize(demo=True)
        self.assertEqual(self.rsvp("Maya T.", headers=as_player(TOKEN_A))[0], 200)
        self.assertEqual(self.call("DELETE", "/api/rsvps?name=Maya%20T.", headers=as_player(TOKEN_A))[0], 200)

    def test_join_order_is_stable_so_the_pitch_dots_do_not_jump(self):
        for name in ("Zed", "Amy", "Bob"):
            self.rsvp(name)
        self.rsvp("Zed", "out")
        self.rsvp("Zed", "going")
        order = [r["name"] for r in sorted(self.state()["rsvps"], key=lambda r: r["seq"])]
        self.assertEqual(order, ["Zed", "Amy", "Bob"])


class LockTests(Base):
    def lock(self, name, token=TOKEN_A):
        return self.call("POST", "/api/lock", {"name": name}, headers=as_player(token))

    def row(self, name):
        return [r for r in self.state()["rsvps"] if r["name"] == name][0]

    def test_a_player_can_lock_in_and_then_cannot_drop_out_or_be_removed(self):
        self.rsvp("Maya")
        self.assertEqual(self.lock("Maya")[0], 200)
        self.assertEqual((self.row("Maya")["locked"], self.row("Maya")["lock_kind"]), (True, "manual"))
        code, body = self.call("DELETE", "/api/rsvps?name=Maya", headers=as_player(TOKEN_A))
        self.assertEqual(code, 409)
        self.assertIn("locked", body["error"])
        self.assertEqual(self.rsvp("Maya", "out")[0], 409)
        self.assertEqual(self.rsvp("Maya", "maybe")[0], 409)
        self.assertEqual(self.rsvp("Maya", "going")[0], 200)            # confirming again is harmless
        self.assertEqual(self.row("Maya")["status"], "going")

    def test_only_the_owner_can_lock_and_only_a_spot_that_is_going(self):
        self.rsvp("Maya", headers=as_player(TOKEN_A))
        self.assertEqual(self.lock("Maya", TOKEN_B)[0], 403)
        self.assertEqual(self.lock("Nobody")[0], 404)
        self.rsvp("Sam", "maybe", headers=as_player(TOKEN_B))
        self.assertEqual(self.lock("Sam", TOKEN_B)[0], 409)
        self.assertFalse(self.row("Maya")["locked"])

    def test_nothing_unlocks_a_spot_for_the_player(self):
        self.rsvp("Maya")
        self.lock("Maya")
        self.assertEqual(self.call("POST", "/api/unlock", {}, headers=as_player(TOKEN_A))[0], 200)     # that endpoint only checks the PIN
        self.assertTrue(self.row("Maya")["locked"])

    def test_one_day_before_the_game_every_spot_that_is_going_locks_by_itself(self):
        self.rsvp("Maya")
        self.rsvp("Sam", "maybe")
        self.assertFalse(self.state()["settings"]["auto_locked"])
        self.server.poll.store.today = lambda: date(2026, 10, 14)        # Wednesday, one day before Thursday's game
        st = self.state()
        self.assertTrue(st["settings"]["auto_locked"])
        self.assertEqual(st["settings"]["lock_from"], "2026-10-14")
        self.assertEqual((self.row("Maya")["locked"], self.row("Maya")["lock_kind"]), (True, "auto"))
        self.assertFalse(self.row("Sam")["locked"])                      # a maybe holds no spot
        self.assertEqual(self.call("DELETE", "/api/rsvps?name=Maya", headers=as_player(TOKEN_A))[0], 409)
        self.assertEqual(self.rsvp("Maya", "out")[0], 409)
        self.assertEqual(self.call("DELETE", "/api/rsvps?name=Sam", headers=self.player_headers("Sam"))[0], 200)      # a maybe can still leave

    def test_it_stays_locked_on_game_day(self):
        self.rsvp("Maya")
        self.server.poll.store.today = lambda: date(2026, 10, 15)
        self.assertTrue(self.row("Maya")["locked"])

    def test_late_sign_ups_still_work_while_locked_and_can_also_lock_in(self):
        self.rsvp("Maya")
        self.server.poll.store.today = lambda: date(2026, 10, 14)
        self.assertEqual(self.rsvp("Late Larry", "going", headers=self.player_headers("Late Larry"))[0], 200)
        self.assertEqual(self.rsvp("Sam", "maybe")[0], 200)
        self.assertEqual(self.rsvp("Sam", "going")[0], 200)               # maybe to going is allowed
        self.assertEqual(self.rsvp("Sam", "out")[0], 409)                 # but not back out of it

    def test_the_squad_limit_still_applies_after_the_lock(self):
        self.call("POST", "/api/game", self.game(max_players=2))
        self.rsvp("A"); self.rsvp("B")
        self.server.poll.store.today = lambda: date(2026, 10, 14)
        self.assertEqual(self.rsvp("C")[0], 200)
        self.assertTrue([r for r in self.state()["rsvps"] if r["name"] == "C"][0]["waitlisted"])

    def test_a_new_game_date_starts_unlocked(self):
        self.rsvp("Maya")
        self.lock("Maya")
        self.call("POST", "/api/game", self.game(game_date="2026-10-22"))
        self.assertEqual(self.state()["rsvps"], [])
        self.rsvp("Maya")
        self.assertFalse(self.row("Maya")["locked"])

    def test_the_organizer_can_still_clear_a_game(self):
        self.rsvp("Maya")
        self.lock("Maya")
        self.call("POST", "/api/reset-week", {})
        self.assertEqual(self.state()["rsvps"], [])

    def test_sample_players_are_never_locked(self):
        self.server.poll.store.initialize(demo=True)
        self.server.poll.store.today = lambda: date(2026, 10, 14)
        self.assertFalse(any(r["locked"] for r in self.state()["rsvps"]))


class StorageTests(Base):
    def test_the_database_and_its_folder_are_private(self):
        self.rsvp("Maya")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.server.poll.store.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(self.server.poll.store.dir).st_mode), 0o700)

    def test_a_version_1_database_is_migrated_without_losing_players(self):
        path = os.path.join(self.tmp, "old")
        os.makedirs(path)
        db = sqlite3.connect(os.path.join(path, "pickup-poll.sqlite3"))
        db.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE rsvps (name TEXT PRIMARY KEY COLLATE NOCASE, status TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE permits (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, date TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
            INSERT INTO settings VALUES ('club_name','Old Club'),('game_date','2026-10-15'),('kickoff','18:30'),('location','Old field'),('max_players','14');
            INSERT INTO rsvps VALUES ('Maya T.','going','2026-10-10T10:00'),('Chris L.','going','2026-10-10T10:00'),('Real Person','going','2026-10-12T09:00');
            INSERT INTO permits(title,date,note) VALUES ('Field-use permit','2026-11-01','Demo date · replace'),('Real permit','2026-11-05','');
        """)
        db.commit()
        db.close()
        store = pp.Store(path, lambda: TODAY)
        store.initialize()
        st = store.state()
        self.assertEqual(st["settings"]["club_name"], "Old Club")
        by_name = {r["name"]: r["is_sample"] for r in st["rsvps"]}
        self.assertEqual(by_name, {"Maya T.": True, "Chris L.": True, "Real Person": False})     # the seeded names share one time stamp; a later reply does not
        self.assertEqual({p["title"]: p["is_sample"] for p in st["permits"]}, {"Field-use permit": True, "Real permit": False})
        check = sqlite3.connect(os.path.join(path, "pickup-poll.sqlite3"))
        cols = {r[1] for r in check.execute("PRAGMA table_info(rsvps)")}
        check.close()
        self.assertTrue({"owner_hash", "locked"} <= cols)
        store.initialize()                                  # running the migration twice is harmless
        self.assertEqual(len(store.state()["rsvps"]), 3)


def run_node(script, *args):
    out = subprocess.run(["node", "-e", script, *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BrowserModuleTests(unittest.TestCase):
    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def test_sunset_matches_the_expected_times_within_two_minutes(self):
        got = run_node("""const s=require(process.argv[1]); process.stdout.write(JSON.stringify(
          ['2026-10-22','2026-11-05','2026-12-21','2027-06-21'].map(d => s.sunsetLabel(d))));""", os.path.join(self.ROOT, "sun.js"))
        expected = ["6:06 PM", "4:49 PM", "4:30 PM", "8:31 PM"]       # New York local time, standard references
        def minutes(label):
            clock, half = label.split()
            h, m = map(int, clock.split(":"))
            return (h % 12 + (12 if half == "PM" else 0)) * 60 + m
        for label, want in zip(got, expected):
            self.assertLessEqual(abs(minutes(label) - minutes(want)), 2, (label, want))

    def test_teams_are_balanced_repeatable_and_cover_everyone_once(self):
        result = run_node("""const t=require(process.argv[1]);
          const ps=Array.from({length:9},(_,i)=>({seq:i+1}));
          const same=JSON.stringify(t.assignTeams(ps,777))===JSON.stringify(t.assignTeams(ps,777));
          const plain=t.assignTeams(ps,0), mixed=t.assignTeams(ps,777);
          process.stdout.write(JSON.stringify({same, plain:[plain.white.length,plain.orange.length], mixed:[mixed.white.length,mixed.orange.length],
            all:[...mixed.white,...mixed.orange].map(p=>p.seq).sort((a,b)=>a-b), order:plain.white.map(p=>p.seq),
            reshuffled: JSON.stringify(t.assignTeams(ps,778))!==JSON.stringify(mixed)}));""", os.path.join(self.ROOT, "teams.js"))
        self.assertTrue(result["same"])
        self.assertEqual((result["plain"], result["mixed"]), ([5, 4], [5, 4]))
        self.assertEqual(result["all"], list(range(1, 10)))
        self.assertEqual(result["order"], [1, 3, 5, 7, 9])               # no seed: alternate in join order
        self.assertTrue(result["reshuffled"])

    def test_the_game_calendar_file_has_the_times_place_and_a_reminder(self):
        text = run_node("""const i=require(process.argv[1]);
          process.stdout.write(JSON.stringify(i.buildGameIcs({game_date:'2026-10-22',kickoff:'16:00',end_time:'18:00',club_name:'Football Not Soccer Club',
          location:'Jack McManus Field, 729 Main St, New York, NY 10044',notes:'Pinnies; bring water\\r\\nATTACH:http://evil.example'}, new Date('2026-10-13T10:00:00Z'))));""",
                        os.path.join(self.ROOT, "ics.js"))
        lines = text.split("\r\n")
        self.assertIn("DTSTART:20261022T160000", lines)
        self.assertIn("DTEND:20261022T180000", lines)
        self.assertIn("SUMMARY:Football Not Soccer Club", lines)
        self.assertIn("TRIGGER:-PT2H", lines)
        self.assertTrue(any(line.startswith("LOCATION:Jack McManus Field\\, 729 Main St") for line in lines))
        self.assertTrue(all(len(line.encode()) <= 75 for line in lines))
        self.assertFalse(any(line.startswith("ATTACH") for line in lines))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CelebrationTests(unittest.TestCase):
    """The physics of the lock-in confetti. Drawing needs a browser; the rules of motion do not."""
    MODULE = os.path.join(os.path.dirname(__file__), "..", "celebrate.js")

    def run_js(self, body):
        script = "const c = require(process.argv[1]); const seeded = s => () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296;\n" + body
        out = subprocess.run(["node", "-e", script, self.MODULE], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_a_burst_starts_at_the_button_and_flies_upward_and_outward(self):
        r = self.run_js("""
          const ps = c.makeParticles({ count: 200, origin: { x: 300, y: 500 }, rng: seeded(7) });
          console.log(JSON.stringify({ n: ps.length, atOrigin: ps.every(p => p.x === 300 && p.y === 500),
            allUp: ps.every(p => p.vy < 0), left: ps.filter(p => p.vx < 0).length, right: ps.filter(p => p.vx > 0).length,
            speeds: [Math.min(...ps.map(p => Math.hypot(p.vx, p.vy))), Math.max(...ps.map(p => Math.hypot(p.vx, p.vy)))] }));""")
        self.assertEqual((r["n"], r["atOrigin"], r["allUp"]), (200, True, True))
        self.assertGreater(r["left"], 40)
        self.assertGreater(r["right"], 40)                               # a fan, not a single line
        self.assertGreaterEqual(r["speeds"][0], 419)
        self.assertLessEqual(r["speeds"][1], 981)

    def test_gravity_pulls_pieces_down_and_everything_disappears_in_time(self):
        r = self.run_js("""
          const ps = c.makeParticles({ count: 40, origin: { x: 0, y: 0 }, speed: [0, 0], rng: seeded(3) });
          c.step(ps, 0.1);
          const falling = ps.every(p => p.vy > 0 && p.y > 0);
          let seconds = 0, alive = ps.length;
          while (alive > 0 && seconds < 10) { alive = c.step(ps, 1 / 60); seconds += 1 / 60; }
          console.log(JSON.stringify({ falling, seconds, alive }));""")
        self.assertTrue(r["falling"])
        self.assertEqual(r["alive"], 0)
        self.assertLess(r["seconds"], 3.1)                               # the whole effect is over within about three seconds

    def test_delayed_pieces_wait_for_their_turn(self):
        r = self.run_js("""
          const ps = c.makeParticles({ count: 10, origin: { x: 50, y: 50 }, delay: 0.5, rng: seeded(5) });
          c.step(ps, 0.3);
          const still = ps.every(p => p.x === 50 && p.y === 50);
          c.step(ps, 0.3);
          console.log(JSON.stringify({ still, moved: ps.every(p => p.x !== 50 || p.y !== 50) }));""")
        self.assertTrue(r["still"] and r["moved"])

    def test_pieces_fade_out_smoothly_and_only_at_the_end_of_their_life(self):
        r = self.run_js("""
          const p = { age: 0, life: 2 };
          const at = a => { p.age = a; return c.opacity(p); };
          console.log(JSON.stringify({ start: at(0), mid: at(1), fading: at(1.6), end: at(2), steps: [at(1.3), at(1.5), at(1.7), at(1.9)] }));""")
        self.assertEqual((r["start"], r["mid"], r["end"]), (1, 1, 0))
        self.assertTrue(0 < r["fading"] < 1)
        self.assertEqual(r["steps"], sorted(r["steps"], reverse=True))

    def test_colours_come_from_the_palette_and_the_light_theme_has_no_pale_pieces(self):
        r = self.run_js("""
          const ps = c.makeParticles({ count: 300, origin: { x: 0, y: 0 }, palette: c.PALETTE_LIGHT, rng: seeded(11) });
          const lum = hex => { const n = parseInt(hex.slice(1), 16); return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255; };
          console.log(JSON.stringify({ inPalette: ps.every(p => c.PALETTE_LIGHT.includes(p.color)),
            lightest: Math.max(...c.PALETTE_LIGHT.map(lum)), usesDark: c.paletteFor("dark") === c.PALETTE, usesLight: c.paletteFor("light") === c.PALETTE_LIGHT,
            balls: ps.filter(p => p.kind === "ball").length }));""")
        self.assertTrue(r["inPalette"] and r["usesDark"] and r["usesLight"])
        self.assertLess(r["lightest"], 0.85)                              # nothing close to white on the cream page

    def test_footballs_are_limited_to_the_number_asked_for(self):
        r = self.run_js("""
          const ps = c.makeParticles({ count: 50, origin: { x: 0, y: 0 }, balls: 4, rng: seeded(2) });
          console.log(JSON.stringify({ balls: ps.filter(p => p.kind === "ball").length, sizes: ps.filter(p => p.kind === "ball").map(p => p.size) }));""")
        self.assertEqual(r["balls"], 4)
        self.assertTrue(all(18 <= s <= 24 for s in r["sizes"]))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DemoModeTests(unittest.TestCase):
    """demo.js stands in for the server on the static website. It must follow the same rules and return the same data shape."""
    MODULE = os.path.join(os.path.dirname(__file__), "..", "demo.js")

    def run_js(self, body):
        script = ("const { DemoServer } = require(process.argv[1]);\n"
                  "const day = (m, d) => () => new Date(2026, m - 1, d);\n"
                  "const call = (s, method, path, body, q) => s.handle(method, path, new URLSearchParams(q || {}), body);\n" + body)
        out = subprocess.run(["node", "-e", script, self.MODULE], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_the_demo_starts_with_made_up_players_and_a_game_a_few_days_away(self):
        r = self.run_js("""
          const s = new DemoServer({ today: day(10, 14) });
          const st = call(s, "GET", "/api/state").body;
          console.log(JSON.stringify({ date: st.settings.game_date, locked: st.settings.auto_locked, n: st.rsvps.length,
            going: st.rsvps.filter(r => r.status === "going").length, mine: st.rsvps.some(r => r.mine), sample: st.has_sample, name: st.player_name }));""")
        self.assertEqual(r["date"], "2026-10-22")                               # a Thursday, at least three days after "today"
        self.assertEqual((r["locked"], r["n"], r["going"], r["mine"], r["sample"], r["name"]), (False, 15, 12, False, False, None))

    def test_joining_locking_and_the_one_name_per_browser_rule(self):
        r = self.run_js("""
          const s = new DemoServer({ today: day(10, 14) });
          const join = (n, st) => call(s, "POST", "/api/rsvps", { name: n, status: st || "going" });
          const a = join("Visitor One");
          const other = join("Someone Else");
          const fresh = new DemoServer({ today: day(10, 14) });
          const taken = call(fresh, "POST", "/api/rsvps", { name: "Maya T.", status: "going" });      // a visitor who has not joined yet
          const lock = call(s, "POST", "/api/lock", { name: "Visitor One" });
          const drop = join("Visitor One", "out");
          const remove = call(s, "DELETE", "/api/rsvps", null, { name: "Visitor One" });
          const mine = lock.body.rsvps.find(r => r.mine);
          console.log(JSON.stringify({ a: a.status, other: [other.status, other.body.error], taken: taken.status,
            lock: [lock.status, mine.locked, mine.lock_kind], drop: [drop.status, drop.body.error], remove: remove.status, name: remove.body.error }));""")
        self.assertEqual(r["a"], 200)
        self.assertEqual(r["other"][0], 409)
        self.assertIn("already registered as Visitor One", r["other"][1])
        self.assertEqual(r["taken"], 403)                                        # a made-up player's name belongs to someone else
        self.assertEqual(r["lock"], [200, True, "manual"])
        self.assertEqual(r["drop"][0], 409)
        self.assertIn("locked in", r["drop"][1])
        self.assertEqual(r["remove"], 409)

    def test_a_full_squad_gives_a_waitlist_and_a_bigger_squad_promotes(self):
        r = self.run_js("""
          const s = new DemoServer({ today: day(10, 14) });
          const game = call(s, "GET", "/api/state").body.settings;
          const update = max => call(s, "POST", "/api/game", { club_name: "Football Not Soccer Club", location: game.location, game_date: game.game_date,
            kickoff: "16:00", end_time: "18:00", notes: "x", max_players: max });
          update(12);
          const joined = call(s, "POST", "/api/rsvps", { name: "Late One", status: "going" }).body.rsvps.find(r => r.mine);
          const lock = call(s, "POST", "/api/lock", { name: "Late One" });
          const promoted = update(13).body.rsvps.find(r => r.mine);
          const tooSmall = update(5);
          console.log(JSON.stringify({ joined: [joined.waitlisted, joined.waitlist_pos], lock: lock.status, promoted: [promoted.waitlisted, promoted.waitlist_pos], tooSmall: [tooSmall.status, tooSmall.body.error] }));""")
        self.assertEqual(r["joined"], [True, 1])
        self.assertEqual(r["lock"], 409)                                          # a waitlisted spot cannot be locked
        self.assertEqual(r["promoted"], [False, None])
        self.assertEqual(r["tooSmall"][0], 409)
        self.assertIn("already going", r["tooSmall"][1])

    def test_a_cancelled_game_stops_new_yes_answers_but_not_out(self):
        r = self.run_js("""
          const s = new DemoServer({ today: day(10, 14) });
          call(s, "POST", "/api/notice", { text: "", kind: "cancelled" });
          const st = call(s, "GET", "/api/state").body;
          const yes = call(s, "POST", "/api/rsvps", { name: "Visitor", status: "going" });
          const no = call(s, "POST", "/api/rsvps", { name: "Visitor", status: "out" });
          const cleared = call(s, "POST", "/api/notice", { text: "", kind: "info" }).body.notice;
          console.log(JSON.stringify({ notice: st.notice, yes: yes.status, no: no.status, cleared }));""")
        self.assertEqual(r["notice"], {"text": "This game is cancelled.", "kind": "cancelled"})
        self.assertEqual((r["yes"], r["no"], r["cleared"]), (409, 200, None))

    def test_spots_lock_by_themselves_the_day_before_the_game(self):
        r = self.run_js("""
          const s = new DemoServer({ today: day(10, 21) });
          const game = call(s, "GET", "/api/state").body.settings;
          call(s, "POST", "/api/game", { club_name: "Football Not Soccer Club", location: game.location, game_date: "2026-10-22", kickoff: "16:00", end_time: "18:00", notes: "x", max_players: 14 });
          const st = call(s, "POST", "/api/rsvps", { name: "Visitor", status: "going" }).body;
          const mine = st.rsvps.find(r => r.mine);
          const drop = call(s, "POST", "/api/rsvps", { name: "Visitor", status: "maybe" });
          console.log(JSON.stringify({ auto: st.settings.auto_locked, from: st.settings.lock_from, mine: [mine.locked, mine.lock_kind], drop: drop.status }));""")
        self.assertEqual((r["auto"], r["from"], r["mine"], r["drop"]), (True, "2026-10-21", [True, "auto"], 409))

    def test_bad_input_is_refused_like_the_real_server(self):
        r = self.run_js("""
          const s = new DemoServer({ today: day(10, 14) });
          const names = ["", "   ", "x".repeat(37), "bell\x07", "zero\u200bwidth"].map(n => call(s, "POST", "/api/rsvps", { name: n, status: "going" }).status);
          const status = call(s, "POST", "/api/rsvps", { name: "Ok", status: "dancing" }).status;
          const game = call(s, "GET", "/api/state").body.settings;
          const base = { club_name: "Club", location: "Field", game_date: game.game_date, kickoff: "16:00", end_time: "18:00", max_players: 14 };
          const bad = [{ game_date: "2026-10-23" }, { game_date: "2026-10-08" }, { kickoff: "6pm" }, { end_time: "15:00" }, { max_players: 1 }, { max_players: "14" }, { club_name: "x" }]
            .map(o => call(s, "POST", "/api/game", { ...base, ...o }).status);
          const permit = [["x", "2026-12-01"], ["Permit", "2026-10-01"], ["Permit", "soon"], ["Permit", "2040-01-01"]].map(([t, d]) => call(s, "POST", "/api/permits", { title: t, date: d }).status);
          console.log(JSON.stringify({ names, status, bad, permit, unknown: call(s, "POST", "/api/nothing", {}).status }));""")
        self.assertEqual(r["names"], [400] * 5)
        self.assertEqual(r["status"], 400)
        self.assertEqual(r["bad"], [400] * 7)
        self.assertEqual(r["permit"], [400] * 4)
        self.assertEqual(r["unknown"], 404)

    def test_the_state_survives_a_reload_through_storage(self):
        r = self.run_js("""
          const store = { data: {}, getItem(k) { return this.data[k] ?? null; }, setItem(k, v) { this.data[k] = v; } };
          const first = new DemoServer({ today: day(10, 14), storage: store });
          call(first, "POST", "/api/rsvps", { name: "Visitor", status: "going" });
          call(first, "POST", "/api/shuffle", {});
          const second = new DemoServer({ today: day(10, 14), storage: store });
          const st = call(second, "GET", "/api/state").body;
          console.log(JSON.stringify({ name: st.player_name, mine: st.rsvps.filter(r => r.mine).length, seed: st.settings.team_seed > 0 }));""")
        self.assertEqual(r, {"name": "Visitor", "mine": 1, "seed": True})

    def test_the_demo_returns_the_same_data_shape_as_the_real_server(self):
        """If the real API gains or loses a field, the page would break on the website. This test catches the drift."""
        real = pp.Store(os.path.join(tempfile.mkdtemp(), "data"), lambda: TODAY)
        self.addCleanup(shutil.rmtree, real.dir.parent, True)
        real.initialize()
        real.save_rsvp("Shape Check", "going", TOKEN_A)
        real.add_permit("Permit", "2026-11-20", "note")
        real.set_notice("Update", "info")
        real_state = real.state(TOKEN_A)
        demo = self.run_js("""
          const s = new DemoServer({ today: day(10, 13) });
          call(s, "POST", "/api/rsvps", { name: "Shape Check", status: "going" });
          call(s, "POST", "/api/permits", { title: "Permit", date: "2026-11-20", note: "note" });
          call(s, "POST", "/api/notice", { text: "Update", kind: "info" });
          const st = call(s, "GET", "/api/state").body;
          console.log(JSON.stringify({ top: Object.keys(st).sort(), settings: Object.keys(st.settings).sort(), row: Object.keys(st.rsvps.find(r => r.mine)).sort(),
            permit: Object.keys(st.permits[0]).sort(), notice: Object.keys(st.notice).sort() }));""")
        server_top = sorted(set(real_state) | {"organizer_required"})            # the HTTP layer adds this field
        self.assertEqual(demo["top"], server_top)
        self.assertEqual(demo["settings"], sorted(real_state["settings"]))
        self.assertEqual(demo["row"], sorted([k for k in real_state["rsvps"][0]]))
        self.assertEqual(demo["permit"], sorted(real_state["permits"][0]))
        self.assertEqual(demo["notice"], sorted(real_state["notice"]))


class DemoBuildTests(unittest.TestCase):
    def test_the_static_site_uses_relative_paths_and_loads_the_demo_before_the_app(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_demo", os.path.join(os.path.dirname(__file__), "..", "scripts", "build_demo.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        files = module.build(out)
        for name in ("index.html", "app.js", "demo.js", "celebrate.js", "styles.css", ".nojekyll"):
            self.assertIn(name, files)
        page = open(os.path.join(out, "index.html"), encoding="utf-8").read()
        self.assertNotRegex(page, r'(?:src|href)="/')                            # an absolute path would break under /pickup-poll/
        self.assertLess(page.index('src="demo.js"'), page.index('src="app.js"'))
        self.assertNotIn("app.py", " ".join(files))                              # the server is not part of the website

    def test_the_checked_in_site_is_up_to_date(self):
        import importlib.util
        root = os.path.join(os.path.dirname(__file__), "..")
        spec = importlib.util.spec_from_file_location("build_demo", os.path.join(root, "scripts", "build_demo.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        module.build(out)
        for name in os.listdir(out):
            with open(os.path.join(out, name), "rb") as fresh, open(os.path.join(root, "docs", name), "rb") as kept:
                self.assertEqual(fresh.read(), kept.read(), f"docs/{name} is out of date: run python3 scripts/build_demo.py")


class CalendarFileTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_calendar_file_is_valid_and_cannot_be_injected_into(self):
        script = """
        const ics = require(process.argv[1]);
        const text = ics.buildIcs({id: 7, date: '2026-12-31', title: 'Permit, due; soon', note: 'a\\r\\nATTACH:http://evil.example ' + 'y'.repeat(120)}, new Date('2026-10-13T10:00:00Z'));
        process.stdout.write(JSON.stringify(text));
        """
        out = subprocess.run(["node", "-e", script, os.path.join(os.path.dirname(__file__), "..", "ics.js")], capture_output=True, text=True, check=True)
        text = json.loads(out.stdout)
        self.assertNotIn("\n\n", text.replace("\r\n", ""))
        lines = text.split("\r\n")
        self.assertEqual(lines[0], "BEGIN:VCALENDAR")
        self.assertEqual(lines[-2:], ["END:VCALENDAR", ""])
        self.assertIn("DTSTART;VALUE=DATE:20261231", lines)
        self.assertIn("DTEND;VALUE=DATE:20270101", lines)       # the day after: correct across a year end
        self.assertIn("SUMMARY:Permit\\, due\\; soon", lines)
        self.assertTrue(all(len(line.encode()) <= 75 for line in lines))
        self.assertFalse(any(line.startswith("ATTACH") for line in lines))      # the injected line did not become a property


if __name__ == "__main__":
    unittest.main()
