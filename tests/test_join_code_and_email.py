import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_pickup_poll import HEADERS, TODAY, TOKEN_A, TOKEN_B, Base, as_player, pp  # noqa: E402

PIN = "24681357"
CODE = "McManus22"
ORGANIZER = {**HEADERS, "X-Organizer-Pin": PIN}


class Guarded(Base):
    """A server like the live one: a PIN, a club code and Cornell emails required."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._player_tokens = {}
        self.server, _ = pp.make_server(os.path.join(self.tmp, "data"), "127.0.0.1", 0, PIN, today=lambda: TODAY,
                                       join_code=CODE, require_email=True)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, thread)

    def join(self, name, email="", code=CODE, token=TOKEN_A, status="going"):
        body = {"name": name, "status": status, "email": email, "join_code": code}
        return self.call("POST", "/api/rsvps", body, headers=as_player(token))


class JoinCodeTests(Guarded):
    def test_without_the_code_nobody_can_sign_up(self):
        status, body = self.join("Maya T.", "mt123@cornell.edu", code="")
        self.assertEqual(status, 403)
        self.assertIn("club code", body["error"])
        self.assertEqual(self.state()["rsvps"], [])

    def test_a_wrong_code_is_refused_and_the_right_one_ignores_case_and_spaces(self):
        self.assertEqual(self.join("Maya T.", "mt123@cornell.edu", code="wrong")[0], 403)
        self.assertEqual(self.join("Maya T.", "mt123@cornell.edu", code="  mcmanus22 ")[0], 200)

    def test_guessing_the_code_is_slowed_down(self):
        codes = [self.join("Guess", "g1@cornell.edu", code=f"guess{i}")[0] for i in range(11)]
        self.assertEqual(codes[:10], [403] * 10)
        self.assertEqual(codes[10], 429)

    def test_the_page_is_told_a_code_and_email_are_needed(self):
        st = self.state()
        self.assertTrue(st["join_code_required"])
        self.assertTrue(st["email_required"])
        self.assertFalse(st["player_has_email"])


class EmailTests(Guarded):
    def test_an_email_is_needed_the_first_time_and_must_be_cornell(self):
        self.assertIn("Cornell email", self.join("Maya T.", "")[1]["error"])
        self.assertIn("Cornell email", self.join("Maya T.", "maya@gmail.com")[1]["error"])
        self.assertIn("Cornell email", self.join("Maya T.", "maya@cornell.edu.evil.com")[1]["error"])
        self.assertEqual(self.join("Maya T.", " MT123@Cornell.EDU ")[0], 200)

    def test_after_the_first_reply_the_same_browser_does_not_retype_it(self):
        self.join("Maya T.", "mt123@cornell.edu")
        self.assertEqual(self.join("Maya T.", "", status="maybe")[0], 200)
        st = self.call("GET", "/api/state", headers={"X-Player-Token": TOKEN_A})[1]
        self.assertTrue(st["player_has_email"])

    def test_one_email_cannot_sign_up_a_second_browser(self):
        self.join("Maya T.", "mt123@cornell.edu", token=TOKEN_A)
        status, body = self.join("Fake Name", "mt123@cornell.edu", token=TOKEN_B)
        self.assertEqual(status, 409)
        self.assertIn("already signed up", body["error"])
        self.assertEqual([p["name"] for p in self.state()["rsvps"]], ["Maya T."])

    def test_emails_never_reach_the_public_page(self):
        self.join("Maya T.", "mt123@cornell.edu")
        raw = self.call("GET", "/api/state", headers={"X-Player-Token": TOKEN_B})[1]
        self.assertNotIn("mt123", str(raw))
        mine = self.call("GET", "/api/state", headers={"X-Player-Token": TOKEN_A})[1]
        self.assertNotIn("mt123", str(mine))                     # not even to the player who typed it


class OrganizerTests(Guarded):
    def test_signups_with_emails_need_the_pin(self):
        self.join("Maya T.", "mt123@cornell.edu")
        self.assertEqual(self.call("POST", "/api/signups", {}, headers=HEADERS)[0], 403)
        status, body = self.call("POST", "/api/signups", {}, headers=ORGANIZER)
        self.assertEqual(status, 200)
        self.assertEqual([(p["name"], p["email"], p["status"]) for p in body["players"]], [("Maya T.", "mt123@cornell.edu", "going")])
        self.assertTrue(body["players"][0]["updated_at"])

    def test_the_organizer_can_remove_any_name_even_a_locked_one_and_the_waitlist_moves_up(self):
        self.call("POST", "/api/game", {"club_name": "Football Not Soccer Club", "location": "McManus Field", "game_date": "2026-10-15",
                                        "kickoff": "16:00", "end_time": "18:00", "max_players": 2}, headers=ORGANIZER)
        self.join("Maya T.", "a1@cornell.edu", token="p" * 24)
        self.join("Fake Name", "a2@cornell.edu", token="q" * 24)
        self.join("Sam P.", "a3@cornell.edu", token="r" * 24)
        self.call("POST", "/api/lock", {"name": "Fake Name"}, headers=as_player("q" * 24))
        self.assertEqual(self.call("POST", "/api/remove-player", {"name": "Fake Name"}, headers=HEADERS)[0], 403)
        status, st = self.call("POST", "/api/remove-player", {"name": "Fake Name"}, headers=ORGANIZER)
        self.assertEqual(status, 200)
        rows = {p["name"]: p for p in st["rsvps"]}
        self.assertNotIn("Fake Name", rows)
        self.assertFalse(rows["Sam P."]["waitlisted"])          # moved up into the free spot

    def test_a_removed_browser_cannot_come_back_under_another_name(self):
        self.join("Fake Name", "a2@cornell.edu", token="q" * 24)
        self.call("POST", "/api/remove-player", {"name": "Fake Name"}, headers=ORGANIZER)
        status, body = self.join("Other Name", "a9@cornell.edu", token="q" * 24)
        self.assertEqual(status, 409)
        self.assertIn("already registered as Fake Name", body["error"])

    def test_removing_someone_not_on_the_list_says_so(self):
        self.assertEqual(self.call("POST", "/api/remove-player", {"name": "Nobody"}, headers=ORGANIZER)[0], 404)


class SetupTests(unittest.TestCase):
    def test_emails_need_a_pin_and_a_short_code_is_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(ValueError):
            pp.make_server(os.path.join(tmp, "a"), "127.0.0.1", 0, None, require_email=True)
        with self.assertRaises(ValueError):
            pp.make_server(os.path.join(tmp, "b"), "127.0.0.1", 0, PIN, join_code="ab")

    def test_without_the_options_nothing_changes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        store = pp.Store(os.path.join(tmp, "c"), lambda: TODAY)
        store.initialize()
        store.save_rsvp("Maya T.", "going", TOKEN_A)
        st = store.state(TOKEN_A)
        self.assertEqual((st["email_required"], st["player_has_email"]), (False, False))


if __name__ == "__main__":
    unittest.main()
