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

    def rsvp(self, name, status="going", **kw):
        return self.call("POST", "/api/rsvps", {"name": name, "status": status}, **kw)

    def state(self):
        return self.call("GET", "/api/state")[1]

    def game(self, **over):
        data = {"club_name": "Thursday United", "location": "McManus Field", "game_date": "2026-10-15", "kickoff": "18:30", "max_players": 14}
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

    def test_the_squad_limit_holds_and_frees_up(self):
        self.call("POST", "/api/game", self.game(max_players=3))
        for n in "ABC":
            self.assertEqual(self.rsvp(n)[0], 200)
        code, body = self.rsvp("D")
        self.assertEqual((code, "full" in body["error"]), (409, True))
        self.assertEqual(self.rsvp("A", "going")[0], 200)       # someone already in can confirm again
        self.rsvp("A", "out")
        self.assertEqual(self.rsvp("D")[0], 200)
        self.assertEqual(self.rsvp("E", "maybe")[0], 200)       # maybe never takes a spot

    def test_a_game_cannot_collect_unlimited_replies(self):
        store = self.server.poll.store
        for i in range(pp.MAX_RSVPS_PER_GAME):
            store.save_rsvp(f"player {i}", "maybe", TOKEN_A)
        with self.assertRaises(pp.PollError):
            store.save_rsvp("one too many", "maybe", TOKEN_A)
        store.save_rsvp("player 1", "out", TOKEN_A)         # existing players can still change their answer


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
        self.assertEqual(self.rsvp("Real Three")[0], 409)

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
        for path in ("/", "/app.js", "/ics.js", "/styles.css"):
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
        self.assertEqual(self.state()["settings"]["club_name"], "Thursday United")

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
        self.rsvp("Sam", "maybe")
        self.assertEqual(self.lock("Sam")[0], 409)
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
        self.assertEqual(self.call("DELETE", "/api/rsvps?name=Sam", headers=as_player(TOKEN_A))[0], 200)      # a maybe can still leave

    def test_it_stays_locked_on_game_day(self):
        self.rsvp("Maya")
        self.server.poll.store.today = lambda: date(2026, 10, 15)
        self.assertTrue(self.row("Maya")["locked"])

    def test_late_sign_ups_still_work_while_locked_and_can_also_lock_in(self):
        self.rsvp("Maya")
        self.server.poll.store.today = lambda: date(2026, 10, 14)
        self.assertEqual(self.rsvp("Late Larry", "going", headers=as_player(TOKEN_B))[0], 200)
        self.assertEqual(self.rsvp("Sam", "maybe")[0], 200)
        self.assertEqual(self.rsvp("Sam", "going")[0], 200)               # maybe to going is allowed
        self.assertEqual(self.rsvp("Sam", "out")[0], 409)                 # but not back out of it

    def test_the_squad_limit_still_applies_after_the_lock(self):
        self.call("POST", "/api/game", self.game(max_players=2))
        self.rsvp("A"); self.rsvp("B")
        self.server.poll.store.today = lambda: date(2026, 10, 14)
        self.assertEqual(self.rsvp("C")[0], 409)

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
