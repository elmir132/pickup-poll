# Pickup Poll

A small RSVP board for a weekly football club. Players answer In, Maybe or Out for the next Thursday, everyone sees the headcount, and the organizer keeps field-permit dates in one place with calendar reminders. One Python file for the server (standard library only), plain HTML, CSS and JavaScript for the page, SQLite for storage.

## Run it

Python 3.9 or newer, nothing to install.

```bash
python3 app.py                    # http://127.0.0.1:8787, this computer only
python3 app.py --demo             # same, with clearly marked sample players and dates on first run
python3 app.py --host 0.0.0.0     # prints local and detected Wi-Fi URLs; a 6-digit organizer PIN is generated and printed
```

Options: `--port`, `--data-dir` (or `PICKUP_POLL_DATA`), `--organizer-pin` (or `PICKUP_POLL_PIN`, at least 4 characters), `--allow-host NAME` for an extra host name you open it by.

## How it works

- **Every game date has its own replies.** A new Thursday starts empty, and last week's answers stay in the database as history ("Recent games" on the page). If the saved date has passed, the page moves to the next Thursday by itself.
- **Players** need no account: they type a name and choose In, Maybe or Out. The same name again (any capitalisation or accent form) updates that answer. Each browser is bound to one name across games, even if its reply is removed; it can still change that name's answer. The server stores only a hash of the browser token. This is a browser-level limit, not identity verification: someone can use another device or clear browser data to submit another name. Nobody can remove or overwrite another browser's name, the organizer PIN included. Each player who is going also appears as a dot on the pitch, one team per half, in the order people joined. "In" stops at the squad size; "Maybe" never takes a spot.
- **Locking in:** a player who is going can press **Lock in** on their own row, any time. From **one day before the game** (Wednesday for a Thursday game) every going spot locks by itself. A locked spot cannot be removed or changed to Maybe or Out, not even by its owner, so the squad count can be trusted on game day. If the organizer cancels the game, players can still change to Out or remove their reply. Late sign-ups are still allowed while there is room, and a Maybe can still leave or become In. Only the organizer clearing the whole game undoes a lock on an active game. The one-day rule is the `LOCK_DAYS_BEFORE` constant in `app.py`. Locking in (and joining) plays a short celebration: confetti and small footballs shoot from the button you pressed, with a card that shows the game details. It uses a canvas, respects the reduced-motion setting (then only the card shows, without movement), and takes its colours from the current theme. Tap the card or press Escape to dismiss it.
- **The organizer** edits the match and adds or removes permit dates (and can clear a whole game). On `127.0.0.1` there is no PIN, since only your own computer can reach the page. Once the server is open to the network, those actions need the organizer PIN (asked once per browser tab; five wrong tries lock it for a minute).
- **Joining is two taps:** type a name and tap **I'm in** (or **Maybe** / **Can't make it**). The browser remembers the name, so next time the big button on the match card says "I'm in, Elmir" and one tap is enough. A sentence under the buttons says where you stand: in, locked in, waitlisted (with your place), maybe or out.
- **Waitlist:** when the squad is full the button becomes **Join the waitlist**. If a confirmed player drops out (or the squad size is raised), the person who has waited longest moves up by themselves and sees a message. Waitlisted players can leave at any time and are not locked.
- **Match details:** day, start and end time, field with a Google Maps link, notes for players, and the approximate sunset (computed for Roosevelt Island, accurate to a few minutes). **Add to calendar** downloads the game as an `.ics` file (local time, reminder two hours before); **Invite a friend** shares or copies a message with the link (the link only works for others once the app is hosted).
- **Updates and cancellation:** the organizer can post a short update at the top of the page ("Moved to Pony Field") or mark the game cancelled, which stops new In and Maybe answers.
- **Teams:** the confirmed players are split into white and orange pinnies (alternating in join order; the organizer can shuffle). The same split colours the dots on the pitch.
- **Two tabs:** Matchday (the match, headcount and roll call) and Permits (the dates list). `/#permits` opens the second one directly.
- **Permit dates** show a countdown and download as an `.ics` file with a reminder one day before.
- **Sample data** (`--demo`, or what the first version created) is marked "sample", shown faded, never counts toward the squad, and goes away with one button. It is not added again on restart.

## Safety

- Only the page itself can change data: every write needs a custom header and a JSON body, comes from the same origin, and other web pages in your browser cannot send that. The server also checks the `Host` header, so a web page cannot reach it through a rebinding trick.
- Writes are rate limited, request bodies are capped at 16 KB, replies per game and permit dates are capped, and names and notes cannot contain control characters, so a stored note cannot inject lines into a calendar file.
- The page shows everything as text, with a strict Content-Security-Policy. The database and its folder are private to your user.
- **Not included:** per-player accounts or verified identities. The one-name rule applies to a browser token; a person can use another device or clear browser data to submit a different name. Anyone who can open the page can add a name for someone else who has not answered yet. Use it with a club you trust, on a trusted network. It speaks plain HTTP, so on shared Wi-Fi the PIN can be seen by others on that network. Do not expose it to the internet without HTTPS in front.

## Tests

```bash
python3 -m unittest discover -s tests
```

72 tests start the real server on a free port with a fixed date: replies, weekly rollover, sample data, the migration of the first version's database, permits, cross-site and wrong-host requests, rate limits, the PIN and its lockout, file permissions, and the calendar file (with Node, if installed). CI runs them on Python 3.9 and 3.12.

## Files

`app.py` server and storage · `index.html`, `styles.css`, `app.js` page · `ics.js` calendar files · `sun.js` sunset time · `teams.js` team split · `data/` created on first run (ignored by git).

## Notes

Prototype built with AI assistance (Claude Code) and reviewed and hardened by testing. The page layout is not yet checked on real phones.

## License

MIT
