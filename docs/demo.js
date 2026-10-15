// Demo mode for the static website. GitHub Pages cannot run the Python server, so this file answers the page's /api/ calls
// inside the browser, with the same rules as the real server (squad limit and waitlist, locking, one name per browser,
// cancel notices). The other players are made up. Nothing is sent anywhere, and the state lives in this tab only.
(function (root) {
  const STATUSES = ["going", "maybe", "out"];
  const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  const LOCK_DAYS_BEFORE = 1;
  const MAX_RSVPS = 200;
  const MAX_PERMITS = 50;
  const DEMO_PLAYERS = [["Maya T.", "going"], ["Chris L.", "going"], ["Jordan K.", "going"], ["Alex R.", "going"], ["Sam P.", "going"],
    ["Taylor W.", "going"], ["Priya N.", "going"], ["Dev S.", "going"], ["Ana M.", "going"], ["Leo B.", "going"], ["Rin K.", "going"],
    ["Omar H.", "going"], ["Kai D.", "maybe"], ["Ines F.", "maybe"], ["Tom V.", "out"]];

  class PollError extends Error {
    constructor(message, status = 400) { super(message); this.status = status; }
  }

  const pad = n => String(n).padStart(2, "0");
  const isoDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseDate = text => /^\d{4}-\d{2}-\d{2}$/.test(text) ? new Date(`${text}T12:00:00`) : null;
  const dayStart = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const daysBetween = (a, b) => Math.round((dayStart(a) - dayStart(b)) / 86400000);
  const addDays = (d, n) => { const r = dayStart(d); r.setDate(r.getDate() + n); return r; };
  const nextThursdayOnOrAfter = d => addDays(d, (4 - d.getDay() + 7) % 7);
  const nameKey = name => name.normalize("NFKC").toLowerCase();

  // Text from a form: no control or invisible characters, extra spaces collapsed, length checked (the server does the same).
  function cleanText(value, label, low, high, message) {
    if (typeof value !== "string") throw new PollError(message || `${label} must be text.`);
    const text = value.normalize("NFC");
    for (const ch of text) {
      if (!/\s/.test(ch) && /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Cn}]/u.test(ch)) throw new PollError(`${label} contains characters that are not allowed.`);
    }
    const clean = text.split(/\s+/).filter(Boolean).join(" ");
    if (clean.length < low || clean.length > high) throw new PollError(message || `${label} must be ${low} to ${high} characters.`);
    return clean;
  }

  class DemoServer {
    constructor(options = {}) {
      this.today = options.today || (() => new Date());
      this.rng = options.rng || Math.random;
      this.storage = options.storage || null;
      const saved = this.storage && this.load();
      if (saved) Object.assign(this, saved);
      else this.reset();
    }

    reset() {
      const game = nextThursdayOnOrAfter(addDays(this.today(), 3));       // always a few days away, so spots are not auto-locked yet
      this.settings = { club_name: "Football Not Soccer Club", game_date: isoDate(game), kickoff: "16:00", end_time: "18:00",
        location: "Jack McManus Field, 729 Main St, New York, NY 10044", notes: "Pinnies provided. The field has lights. Bring water.",
        max_players: 14, team_seed: 0, team_seed_game: "" };
      this.notice = null;                                               // {text, kind, game}
      this.identity = null;                                             // {key, name}: one name per browser
      this.permits = [];
      this.nextSeq = 1;
      this.nextPermit = 1;
      this.rsvps = DEMO_PLAYERS.map(([name, status]) => ({ seq: this.nextSeq++, game: this.settings.game_date, key: nameKey(name), name, status,
        updated_at: new Date().toISOString().slice(0, 16), owner: "demo", locked: false, waitlisted: false, waitAt: 0 }));
      this.save();
    }

    load() {
      try { return JSON.parse(this.storage.getItem("pickup-poll-demo")); } catch (error) { return null; }
    }

    save() {
      if (!this.storage) return;
      const { settings, notice, identity, permits, nextSeq, nextPermit, rsvps } = this;
      try { this.storage.setItem("pickup-poll-demo", JSON.stringify({ settings, notice, identity, permits, nextSeq, nextPermit, rsvps })); } catch (error) { /* private mode */ }
    }

    // ---- helpers -------------------------------------------------------------------------------------------
    gameDate() {
      const chosen = parseDate(this.settings.game_date);
      return chosen && daysBetween(chosen, this.today()) >= 0 ? chosen : nextThursdayOnOrAfter(this.today());
    }

    game() { return isoDate(this.gameDate()); }
    autoLocked() { return daysBetween(this.gameDate(), this.today()) <= LOCK_DAYS_BEFORE; }
    cancelled() { return Boolean(this.notice && this.notice.game === this.game() && this.notice.kind === "cancelled" && this.notice.text); }
    rows() { return this.rsvps.filter(r => r.game === this.game()); }
    find(key) { return this.rows().find(r => r.key === key); }
    holdsSpot(r) { return r.status === "going" && !r.waitlisted; }

    isLocked(r) {
      return Boolean(r) && !this.cancelled() && r.owner === "me" && this.holdsSpot(r) && (r.locked || this.autoLocked());
    }

    promote() {
      while (this.rows().filter(r => this.holdsSpot(r)).length < this.settings.max_players) {
        const next = this.rows().filter(r => r.status === "going" && r.waitlisted).sort((a, b) => a.waitAt - b.waitAt)[0];
        if (!next) return;
        next.waitlisted = false;
        next.waitAt = 0;
      }
    }

    // ---- what the page reads ------------------------------------------------------------------------------
    state() {
      const game = this.game();
      const order = r => (this.holdsSpot(r) ? 0 : r.status === "going" ? 1 : r.status === "maybe" ? 2 : 3);
      const list = this.rows().slice().sort((a, b) => order(a) - order(b) || a.waitAt - b.waitAt || a.name.localeCompare(b.name));
      let position = 0;
      const rsvps = list.map(r => {
        const waitlistPos = r.waitlisted ? (position += 1) : null;
        const locked = !this.cancelled() && this.holdsSpot(r) && r.owner === "me" && (r.locked || this.autoLocked());
        return { seq: r.seq, name: r.name, status: r.status, updated_at: r.updated_at, is_sample: false, mine: r.owner === "me",
          waitlisted: r.waitlisted, waitlist_pos: waitlistPos, locked, lock_kind: locked ? (r.locked ? "manual" : "auto") : null };
      });
      const s = this.settings;
      const here = this.notice && this.notice.game === game && this.notice.text;
      return { today: isoDate(this.today()), player_name: this.identity ? this.identity.name : null,
        settings: { club_name: s.club_name, game_date: game, kickoff: s.kickoff, end_time: s.end_time, location: s.location, notes: s.notes,
          max_players: s.max_players, lock_from: isoDate(addDays(this.gameDate(), -LOCK_DAYS_BEFORE)), auto_locked: this.autoLocked(),
          team_seed: s.team_seed_game === game ? s.team_seed : 0 },
        notice: here ? { text: this.notice.text, kind: this.notice.kind } : null,
        rsvps, permits: this.permits.map(p => ({ ...p, is_sample: false })), recent: [], has_sample: false, organizer_required: false };
    }

    // ---- what the page changes ----------------------------------------------------------------------------
    saveRsvp(rawName, status) {
      const name = cleanText(rawName, "Your name", 1, 36, "Enter your name (up to 36 characters).");
      if (!STATUSES.includes(status)) throw new PollError("Choose going, maybe or out.");
      const key = nameKey(name);
      if (status !== "out" && this.cancelled()) throw new PollError("This game is cancelled. Check the update at the top of the page.", 409);
      if (this.identity && this.identity.key !== key) throw new PollError(`This browser is already registered as ${this.identity.name}. Use that name for your reply.`, 409);
      const row = this.find(key);
      if (row && row.owner !== "me") throw new PollError("That name was added from another device. Use a different name (for example add an initial).", 403);
      const holds = Boolean(row) && this.holdsSpot(row);
      if (holds && status !== "going" && this.isLocked(row)) throw new PollError("Your spot is locked in, so you can no longer drop out. Message the organizer if something has come up.", 409);
      if (!row && this.rows().length >= MAX_RSVPS) throw new PollError("This game already has the maximum number of replies.", 409);
      let waitlisted = false;
      if (status === "going" && !holds) {
        const taken = this.rows().filter(r => this.holdsSpot(r) && r.key !== key).length;
        waitlisted = taken >= this.settings.max_players;
      }
      this.identity = { key, name };
      const stamp = new Date().toISOString().slice(0, 16);
      if (row) {
        const wasWaiting = row.waitlisted;
        Object.assign(row, { name, status, updated_at: stamp, locked: status === "going" ? row.locked : false, waitlisted,
          waitAt: waitlisted ? (wasWaiting ? row.waitAt : Date.now()) : 0 });
      } else {
        this.rsvps.push({ seq: this.nextSeq++, game: this.game(), key, name, status, updated_at: stamp, owner: "me", locked: false, waitlisted, waitAt: waitlisted ? Date.now() : 0 });
      }
      this.promote();
    }

    deleteRsvp(rawName) {
      const name = cleanText(rawName, "Player", 1, 36, "Choose a player to remove.");
      const row = this.find(nameKey(name));
      if (!row) return;
      if (row.owner !== "me") throw new PollError("You can only remove your own name.", 403);
      if (this.isLocked(row)) throw new PollError("Your spot is locked in, so it can no longer be removed. Message the organizer if something has come up.", 409);
      this.rsvps = this.rsvps.filter(r => r !== row);
      this.promote();
    }

    lockRsvp(rawName) {
      const name = cleanText(rawName, "Player", 1, 36, "Choose a player to lock in.");
      if (this.cancelled()) throw new PollError("This game is cancelled. Check the update at the top of the page.", 409);
      const row = this.find(nameKey(name));
      if (!row) throw new PollError("That name is not on the list.", 404);
      if (row.owner !== "me") throw new PollError("You can only lock in your own name.", 403);
      if (!this.holdsSpot(row)) throw new PollError("Only a confirmed spot can be locked in.", 409);
      row.locked = true;
    }

    setNotice(rawText, kind) {
      let text = cleanText(rawText == null ? "" : rawText, "The update", 0, 140, "An update can be up to 140 characters.");
      if (kind !== "info" && kind !== "cancelled") throw new PollError("Choose an update type.");
      if (kind === "cancelled" && !text) text = "This game is cancelled.";
      this.notice = text ? { text, kind, game: this.game() } : null;
    }

    shuffleTeams() {
      this.settings.team_seed = Math.floor(this.rng() * (2 ** 31 - 1)) + 1;
      this.settings.team_seed_game = this.game();
    }

    addPermit(rawTitle, date, rawNote) {
      const title = cleanText(rawTitle, "The title", 2, 60, "Give the permit reminder a short title.");
      const note = cleanText(rawNote == null ? "" : rawNote, "The note", 0, 120, "Notes can be up to 120 characters.");
      const due = parseDate(date);
      if (!due) throw new PollError("Choose a valid date.");
      if (daysBetween(due, this.today()) < 0) throw new PollError("That date has already passed. Choose today or a later date.");
      if (daysBetween(due, this.today()) > 3 * 366) throw new PollError("Choose a date within the next three years.");
      if (this.permits.length >= MAX_PERMITS) throw new PollError("There are already 50 reminders. Remove one first.", 409);
      this.permits.push({ id: this.nextPermit++, title, date, note });
      this.permits.sort((a, b) => a.date.localeCompare(b.date) || a.id - b.id);
    }

    deletePermit(id) { this.permits = this.permits.filter(p => p.id !== id); }

    updateGame(data) {
      const club = cleanText(data.club_name, "The club name", 2, 48, "Club name must be 2 to 48 characters.");
      const location = cleanText(data.location, "The location", 2, 100, "Add a field or meeting point.");
      const chosen = typeof data.game_date === "string" ? parseDate(data.game_date) : null;
      if (!chosen) throw new PollError("Choose a valid Thursday.");
      if (chosen.getDay() !== 4) throw new PollError("Game day must be a Thursday.");
      if (daysBetween(chosen, this.today()) < 0) throw new PollError("That Thursday has already passed.");
      if (typeof data.kickoff !== "string" || !TIME_RE.test(data.kickoff)) throw new PollError("Choose a valid kickoff time.");
      if (data.end_time != null && (typeof data.end_time !== "string" || !TIME_RE.test(data.end_time) || data.end_time <= data.kickoff)) throw new PollError("The end time must be a valid time after kickoff.");
      const notes = data.notes == null ? null : cleanText(data.notes, "The notes", 0, 200, "Notes can be up to 200 characters.");
      const capacity = data.max_players;
      if (!Number.isInteger(capacity) || capacity < 2 || capacity > 40) throw new PollError("Squad size must be between 2 and 40.");
      const date = isoDate(chosen);
      const going = this.rsvps.filter(r => r.game === date && this.holdsSpot(r)).length;
      if (capacity < going) throw new PollError(`${going} players are already going on that date. Choose a squad size of at least ${going}.`, 409);
      Object.assign(this.settings, { club_name: club, location, game_date: date, kickoff: data.kickoff, max_players: capacity });
      if (data.end_time != null) this.settings.end_time = data.end_time;
      else if (this.settings.end_time <= data.kickoff) throw new PollError("The end time must be after kickoff.");
      if (notes != null) this.settings.notes = notes;
      this.promote();
    }

    resetWeek() { this.rsvps = this.rsvps.filter(r => r.game !== this.game()); }

    // ---- the request router ---------------------------------------------------------------------------------
    handle(method, path, params, body) {
      try {
        body = body || {};
        if (method === "GET" && path === "/api/state") return { status: 200, body: this.state() };
        if (method === "POST" && path === "/api/rsvps") this.saveRsvp(body.name, body.status);
        else if (method === "POST" && path === "/api/lock") this.lockRsvp(body.name);
        else if (method === "POST" && path === "/api/notice") this.setNotice(body.text, body.kind || "info");
        else if (method === "POST" && path === "/api/shuffle") this.shuffleTeams();
        else if (method === "POST" && path === "/api/permits") this.addPermit(body.title, body.date, body.note);
        else if (method === "POST" && path === "/api/game") this.updateGame(body);
        else if (method === "POST" && (path === "/api/clear-sample" || path === "/api/unlock")) { /* nothing to clear or unlock in the demo */ }
        else if (method === "POST" && path === "/api/reset-week") this.resetWeek();
        else if (method === "DELETE" && path === "/api/rsvps") this.deleteRsvp(params.get("name") || "");
        else if (method === "DELETE" && path === "/api/permits") {
          const id = Number(params.get("id"));
          if (!Number.isInteger(id)) throw new PollError("That item could not be removed.");
          this.deletePermit(id);
        } else throw new PollError("Not found.", 404);
        this.save();
        return { status: 200, body: this.state() };
      } catch (error) {
        if (error instanceof PollError) return { status: error.status, body: { error: error.message } };
        throw error;
      }
    }
  }

  // ---- the browser part: answer /api/ calls locally and say clearly that this is a demo --------------------
  function install() {
    let storage = null;
    try { storage = window.sessionStorage; } catch (error) { storage = null; }
    const server = new DemoServer({ storage });
    const realFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
      const url = new URL(typeof input === "string" ? input : input.url, location.href);
      const at = url.pathname.indexOf("/api/");
      if (at === -1) return realFetch(input, init);
      let body = null;
      if (init.body) { try { body = JSON.parse(init.body); } catch (error) { body = null; } }
      await new Promise(resolve => setTimeout(resolve, 100 + Math.random() * 120));      // feels like a real request
      const result = server.handle((init.method || "GET").toUpperCase(), url.pathname.slice(at), url.searchParams, body);
      return new Response(JSON.stringify(result.body), { status: result.status, headers: { "Content-Type": "application/json" } });
    };
    document.addEventListener("DOMContentLoaded", () => {
      const main = document.querySelector("main");
      if (!main) return;
      const banner = document.createElement("div");
      banner.className = "demo-banner";
      banner.setAttribute("role", "note");
      const text = document.createElement("span");
      text.textContent = "This is a demo. The other players are made up, nothing is saved or sent, and your changes stay in this tab.";
      const link = document.createElement("a");
      link.href = "https://github.com/elmir132/pickup-poll";
      link.textContent = "Get the code";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      banner.append(text, link);
      main.prepend(banner);
    });
  }

  const api = { DemoServer, PollError, nextThursdayOnOrAfter, isoDate };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else { root.PickupDemo = api; install(); }
})(typeof window !== "undefined" ? window : globalThis);
