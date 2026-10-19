const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
let appState = null;
let toastTimer;
let stateRevision = 0;
let latestStateRead = 0;

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map(part => part[0] || "").join("").toUpperCase();
}

function dateAtNoon(iso) {
  return new Date(`${iso}T12:00:00`);
}

function formatDate(iso, options = { month: "short", day: "numeric", year: "numeric" }) {
  return new Intl.DateTimeFormat("en-US", options).format(dateAtNoon(iso));
}

function isoWeek(date) {
  const utc = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  utc.setUTCDate(utc.getUTCDate() + 4 - (utc.getUTCDay() || 7));
  const firstDay = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1));
  return Math.ceil((((utc - firstDay) / 86400000) + 1) / 7);
}

function formatTime(value) {
  const [hours, minutes] = value.split(":").map(Number);
  const suffix = hours >= 12 ? "PM" : "AM";
  return `${hours % 12 || 12}:${String(minutes).padStart(2, "0")} ${suffix}`;
}

function countdown(iso) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = dateAtNoon(iso);
  target.setHours(0, 0, 0, 0);
  const days = Math.round((target - today) / 86400000);
  if (days < 0) return { amount: `${Math.abs(days)}d`, label: "overdue", overdue: true };
  if (days === 0) return { amount: "Today", label: "due", overdue: false };
  return { amount: `${days}d`, label: "to go", overdue: false };
}

function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("toast-error", isError);
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 3000);
}

function centerOf(element) {
  if (!element || !element.getBoundingClientRect) return null;
  const box = element.getBoundingClientRect();
  return box.width || box.height ? { x: box.left + box.width / 2, y: box.top + box.height / 2 } : null;
}

// The confetti starts at the button that was pressed. `origin` is read before the page redraws and removes that button.
function celebrate(title, { origin = null, big = true } = {}) {
  const s = appState && appState.settings;
  const subtitle = s ? `${formatDate(s.game_date, { weekday: "short", month: "short", day: "numeric" })} · ${formatTime(s.kickoff)} · ${s.location.split(",")[0]}` : "";
  const onScreen = origin && origin.x >= 0 && origin.x <= window.innerWidth && origin.y >= 0 && origin.y <= window.innerHeight;
  window.PickupCelebrate.celebrate({ title, subtitle, origin: onScreen ? origin : null, big });
}

// A random token kept in this browser proves which names were added from here. getRandomValues also works on plain http.
function playerToken() {
  try {
    let token = localStorage.getItem("pickup-poll-token");
    if (!token) {
      token = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
      localStorage.setItem("pickup-poll-token", token);
    }
    return token;
  } catch (error) {
    window.pickupPollTempToken = window.pickupPollTempToken || Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
    return window.pickupPollTempToken;
  }
}

// The club code from the group chat: typed once, then kept in this browser.
function storedCode() {
  try { return localStorage.getItem("pickup-poll-join-code") || ""; } catch (error) { return window.pickupPollCode || ""; }
}

function storeCode(code) {
  try { code ? localStorage.setItem("pickup-poll-join-code", code) : localStorage.removeItem("pickup-poll-join-code"); }
  catch (error) { window.pickupPollCode = code; }
}

// Email and club code are only asked for when the server wants them and this browser has not given them yet.
function renderJoinExtras() {
  const needEmail = Boolean(appState && appState.email_required && !appState.player_has_email);
  const needCode = Boolean(appState && appState.join_code_required && !storedCode());
  $("#email-field").classList.toggle("hidden", !needEmail);
  $("#code-field").classList.toggle("hidden", !needCode);
}

function storedPin() {
  try { return sessionStorage.getItem("pickup-poll-pin") || ""; } catch (error) { return ""; }
}

function storePin(pin) {
  try { pin ? sessionStorage.setItem("pickup-poll-pin", pin) : sessionStorage.removeItem("pickup-poll-pin"); } catch (error) { /* private mode */ }
}

async function api(url, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}), "X-Player-Token": playerToken() };
  if (method !== "GET") {
    headers["X-Pickup-Poll"] = "1";
    if (options.body) headers["Content-Type"] = "application/json";
    if (storedPin()) headers["X-Organizer-Pin"] = storedPin();
  }
  const response = await fetch(url, { ...options, headers });
  let body = {};
  try { body = await response.json(); } catch (error) { /* keep empty */ }
  if (!response.ok) {
    const err = new Error(body.error || "Something went wrong. Try again.");
    err.status = response.status;
    throw err;
  }
  return body;
}

// Organizer actions: ask for the PIN when the server needs one, and ask again if it was wrong.
function askPin() {
  return new Promise(resolve => {
    const dialog = $("#pin-dialog");
    const input = $("#pin-input");
    input.value = "";
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok" ? input.value.trim() : null), { once: true });
    dialog.showModal();
    input.focus();
  });
}

async function asOrganizer(action) {
  if (appState && appState.organizer_required && !storedPin()) {
    const pin = await askPin();
    if (!pin) return null;
    storePin(pin);
  }
  try {
    return await action();
  } catch (error) {
    if (error.status === 403 && /PIN/.test(error.message)) {
      storePin("");
      toast(error.message, true);
    } else if (error.status === 429 && /PIN/.test(error.message)) {
      storePin("");
      throw error;
    } else {
      throw error;
    }
    return null;
  }
}

async function loadState(quiet = false) {
  const requestId = ++latestStateRead;
  const revision = stateRevision;
  try {
    const wasWaiting = Boolean(myRow() && myRow().waitlisted);
    const next = await api("/api/state");
    if (requestId !== latestStateRead || revision !== stateRevision) return;
    acceptState(next);
    const now = myRow();
    if (wasWaiting && now && now.status === "going" && !now.waitlisted) toast("A spot opened up: you're in!");
    $("body").classList.remove("is-offline");
  } catch (error) {
    if (requestId !== latestStateRead || revision !== stateRevision) return;
    $("body").classList.add("is-offline");
    if (!quiet) toast("Could not reach the local app. Is the server still running?", true);
  }
}

function acceptState(next) {
  stateRevision += 1;
  appState = next;
  const nameInput = $("#player-name");
  nameInput.readOnly = Boolean(next.player_name);
  if (next.player_name) {
    nameInput.value = next.player_name;
    try { localStorage.setItem("pickup-poll-name", next.player_name); } catch (error) { /* private mode */ }
    $("#player-name-hint").textContent = "Saved to this browser. You can still change your reply.";
  } else {
    $("#player-name-hint").textContent = "One name per browser. You can still change your reply.";
  }
  renderJoinExtras();
  render();
}

const STATUS_WORDS = { going: "Going", maybe: "Maybe", out: "Out" };

function normalName(value) {
  return value.normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
}

// The row for the name typed in the box, if this browser added it.
function myRow() {
  if (!appState) return null;
  const key = normalName($("#player-name").value);
  if (!key) return null;
  return appState.rsvps.find(row => row.mine && normalName(row.name) === key) || null;
}

function mapsUrl(place) {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place)}`;
}

function render() {
  if (!appState) return;
  const { settings, rsvps, permits } = appState;
  const real = rsvps.filter(player => !player.is_sample);
  const sampleCount = rsvps.length - real.length;
  const going = real.filter(player => player.status === "going" && !player.waitlisted);
  const waiting = real.filter(player => player.waitlisted);
  const maybe = real.filter(player => player.status === "maybe");
  const canCopySquad = appState.organizer_required && going.length > 0 && going.every(player => player.locked);
  $("#roster-copy-tools").classList.toggle("hidden", !(appState.organizer_required && real.length > 0));
  $("#copy-locked-squad").classList.toggle("hidden", !canCopySquad);
  const max = settings.max_players;
  const gameDate = dateAtNoon(settings.game_date);
  const month = new Intl.DateTimeFormat("en-US", { month: "short" }).format(gameDate).toUpperCase();
  const week = isoWeek(gameDate);
  const cancelled = appState.notice && appState.notice.kind === "cancelled";

  $("#week-label").textContent = `WEEK ${week}`;
  $("#match-week").textContent = `WEEK ${week} · OPEN RUN`;
  $("#date-month").textContent = month;
  $("#date-day").textContent = String(gameDate.getDate()).padStart(2, "0");
  $("#match-date-full").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric" }).format(gameDate);
  $("#club-name").textContent = settings.club_name;
  $("#kickoff").textContent = `${formatTime(settings.kickoff)} to ${formatTime(settings.end_time)}`;
  const place = $("#location");
  place.textContent = settings.location;
  place.href = mapsUrl(settings.location);
  $("#match-notes").textContent = settings.notes || "";
  $("#match-notes").classList.toggle("hidden", !settings.notes);
  $("#sun-text").textContent = `Sunset ${window.PickupSun.sunsetLabel(settings.game_date)}`;
  $("#add-to-calendar").classList.toggle("hidden", cancelled);
  $("#going-count").textContent = going.length;
  $("#max-players").textContent = max;
  $("#maybe-count").textContent = `${maybe.length} maybe${maybe.length === 1 ? "" : "s"}`;
  $("#roster-total").textContent = sampleCount
    ? `${real.length} ${real.length === 1 ? "reply" : "replies"} · ${sampleCount} sample${sampleCount === 1 ? "" : "s"}`
    : `${real.length} player${real.length === 1 ? "" : "s"}`;
  $("#demo-note").classList.toggle("hidden", !appState.has_sample);
  renderNotice(appState.notice);
  renderRecent(appState.recent || []);
  const teams = window.PickupTeams.assignTeams(going, settings.team_seed);
  renderPitch(teams);
  renderTeams(teams);
  $("#capacity-track").value = Math.min(going.length, max);
  $("#capacity-track").setAttribute("aria-valuemax", max);
  $("#capacity-track").setAttribute("aria-valuenow", going.length);
  $("#squad-status-copy").textContent = (going.length >= max ? "Squad is full" : `${max - going.length} spot${max - going.length === 1 ? "" : "s"} still open`)
    + (waiting.length ? ` · ${waiting.length} on the waitlist` : "");
  $("#footer-date").textContent = `THURSDAY · ${formatDate(settings.game_date, { month: "short", day: "numeric" }).toUpperCase()}`;
  const faces = $("#squad-faces");
  faces.replaceChildren();
  going.slice(0, 5).forEach(player => {
    const face = document.createElement("span");
    face.className = "squad-face";
    face.textContent = initials(player.name);
    face.title = player.name;
    faces.append(face);
  });
  if (going.length > 5) {
    const extra = document.createElement("span");
    extra.className = "squad-face more";
    extra.textContent = `+${going.length - 5}`;
    faces.append(extra);
  }

  renderJoin(going.length >= max, cancelled);
  renderRoster(rsvps);
  renderPermits(permits);
}

function renderNotice(notice) {
  const banner = $("#notice-banner");
  banner.classList.toggle("hidden", !notice);
  banner.classList.toggle("is-cancelled", Boolean(notice && notice.kind === "cancelled"));
  banner.textContent = notice ? notice.text : "";
}

// The join box: one tap for a returning player, name plus one tap for a new one, and a plain sentence about where you stand.
function renderJoin(full, cancelled) {
  const row = myRow();
  const name = $("#player-name").value.trim();
  const holdsSpot = Boolean(row && row.status === "going" && !row.waitlisted);
  $("#join-in-label").textContent = full && !holdsSpot ? "Join the waitlist" : "I'm in";
  $$(".join-btn").forEach(button => {
    const status = button.dataset.status;
    button.classList.toggle("selected", Boolean(row) && row.status === status);
    button.disabled = Boolean(cancelled) && status !== "out";
  });
  const heroLabel = $("#join-hero-label");
  const hero = $("#join-hero");
  hero.disabled = Boolean(cancelled);
  if (cancelled) heroLabel.textContent = "Game cancelled";
  else if (row && row.status === "going") heroLabel.textContent = row.waitlisted ? `Waitlist #${row.waitlist_pos}` : "You're in";
  else if (name) heroLabel.textContent = full ? "Join the waitlist" : `I'm in, ${name.split(" ")[0]}`;
  else heroLabel.textContent = "Add your name";

  const status = $("#join-status");
  status.replaceChildren();
  status.classList.toggle("hidden", false);
  const line = document.createElement("p");
  line.className = "join-line";
  const actions = document.createElement("div");
  actions.className = "join-extra";
  const addAction = (label, action, extra = {}) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "join-chip";
    button.dataset.action = action;
    Object.assign(button.dataset, extra);
    button.textContent = label;
    actions.append(button);
  };
  if (cancelled) {
    line.textContent = "This game is cancelled. See the update at the top of the page.";
  } else if (!row) {
    line.textContent = full ? "The squad is full. Type your name to join the waitlist: you move up automatically when someone drops out."
                            : "No account needed. One name per browser. Type it and tap I'm in.";
  } else if (row.status === "going" && row.waitlisted) {
    line.textContent = `You're number ${row.waitlist_pos} on the waitlist. You move up by yourself if someone drops out, and this page will tell you.`;
  } else if (row.status === "going" && row.locked) {
    line.textContent = "You're locked in. See you there.";
    addAction("Replay celebration ⚽", "replay-celebration");
    addAction("Add to calendar", "game-calendar");
  } else if (row.status === "going") {
    line.textContent = "You're in. See you there.";
    addAction("Lock in my spot", "lock-rsvp", { name: row.name });
    addAction("Add to calendar", "game-calendar");
  } else if (row.status === "maybe") {
    line.textContent = "You're a maybe. Tap I'm in when you're sure.";
  } else {
    line.textContent = "You're out this week. Changed your mind? Tap I'm in.";
  }
  status.append(line);
  if (actions.children.length) status.append(actions);

  const lockDate = formatDate(appState.settings.lock_from, { weekday: "long", month: "short", day: "numeric" });
  $("#rsvp-hint").textContent = cancelled ? "" : appState.settings.auto_locked
    ? "Spots are locked now: if you are in, you are in. You can still sign up if there is room."
    : `Spots lock from ${lockDate}, or sooner if you press Lock in. After that nobody can drop out.`;
}

// One dot per confirmed player. White and orange pinnies each get one half of the pitch, in a simple formation.
const TEAM_COLUMNS = [[50], [25, 50, 75], [20, 40, 60, 80], [30, 50, 70]];
const TEAM_X = [8, 20, 33, 44];
let renderedDots = null;

function pitchSlot(team, k) {
  let x, y, used = 0;
  for (let column = 0; column < TEAM_COLUMNS.length; column += 1) {
    const ys = TEAM_COLUMNS[column];
    if (k < used + ys.length) { x = TEAM_X[column]; y = ys[k - used]; break; }
    used += ys.length;
  }
  if (x === undefined) {            // more than 11 a side: spare players along the touchlines
    const extra = k - used;
    x = 12 + (extra % 5) * 8;
    y = extra % 2 === 0 ? 8 : 92;
  }
  return { left: team === 0 ? x : 100 - x, top: y };
}

function renderPitch(teams) {
  const holder = $("#pitch-dots");
  const previous = renderedDots;
  const all = [...teams.white, ...teams.orange];
  renderedDots = new Set(all.map(player => player.seq));
  holder.replaceChildren();
  [teams.white, teams.orange].forEach((list, team) => list.forEach((player, k) => {
    const slot = pitchSlot(team, k);
    const dot = document.createElement("span");
    dot.className = `pitch-dot team-${team}` + (previous && !previous.has(player.seq) ? " pop" : "");
    dot.style.left = `${slot.left}%`;
    dot.style.top = `${slot.top}%`;
    dot.title = `${player.name} (${team === 0 ? "white" : "orange"} pinnie)`;
    holder.append(dot);
  }));
  $("#pitch-count").textContent = all.length ? `${all.length} on the pitch` : "";
}

function renderTeams(teams) {
  const total = teams.white.length + teams.orange.length;
  $("#teams-panel").classList.toggle("hidden", total < 2);
  [["white", teams.white], ["orange", teams.orange]].forEach(([id, list]) => {
    $(`#${id}-count`).textContent = `(${list.length})`;
    const ol = $(`#${id}-list`);
    ol.replaceChildren();
    list.forEach(player => {
      const li = document.createElement("li");
      li.textContent = player.name + (player.mine ? " (you)" : "");
      if (player.mine) li.className = "is-me";
      ol.append(li);
    });
  });
}

function renderRecent(recent) {
  const box = $("#recent-games");
  box.classList.toggle("hidden", recent.length === 0);
  box.textContent = recent.length
    ? "Recent games: " + recent.map(game => `${formatDate(game.game_date, { month: "short", day: "numeric" })} · ${game.going} in`).join("  ·  ")
    : "";
}

function renderRoster(rsvps) {
  const list = $("#roster-list");
  list.replaceChildren();
  $("#roster-empty").classList.toggle("hidden", rsvps.length > 0);
  rsvps.forEach(player => {
    const row = document.createElement("div");
    row.className = "roster-row" + (player.is_sample ? " is-sample" : "");
    const avatar = document.createElement("span");
    avatar.className = "roster-avatar";
    avatar.textContent = initials(player.name);
    const name = document.createElement("span");
    name.className = "roster-name";
    name.textContent = player.name;
    const status = document.createElement("span");
    status.className = "roster-status";
    const dot = document.createElement("span");
    dot.className = `option-dot ${player.waitlisted ? "maybe-dot" : player.status === "going" ? "going-dot" : player.status === "maybe" ? "maybe-dot" : "out-dot"}`;
    const label = document.createElement("span");
    label.textContent = (player.waitlisted ? `Waitlist #${player.waitlist_pos}` : STATUS_WORDS[player.status]) + (player.is_sample ? " · sample" : "");
    status.append(dot, label);
    const remove = document.createElement("button");
    remove.className = "remove-rsvp";
    remove.type = "button";
    remove.dataset.action = "remove-rsvp";
    remove.dataset.name = player.name;
    remove.setAttribute("aria-label", `Remove ${player.name}`);
    remove.title = "Remove RSVP";
    remove.textContent = "×";
    remove.title = "Remove my name";
    const actions = document.createElement("span");
    actions.className = "roster-actions";
    if (player.locked) {
      const lock = document.createElement("span");
      lock.className = "locked-pill";
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 16 16");
      icon.setAttribute("aria-hidden", "true");
      const shackle = document.createElementNS("http://www.w3.org/2000/svg", "path");
      shackle.setAttribute("d", "M5 7V5a3 3 0 0 1 6 0v2");
      const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      body.setAttribute("x", "3.5");
      body.setAttribute("y", "7");
      body.setAttribute("width", "9");
      body.setAttribute("height", "7");
      body.setAttribute("rx", "1.5");
      icon.append(shackle, body);
      const label = document.createElement("span");
      label.textContent = "Locked in";
      lock.append(icon, label);
      lock.title = player.lock_kind === "auto" ? "Spots lock automatically one day before the game" : "This player locked in their spot";
      actions.append(lock);
    } else if (player.mine) {
      if (player.status === "going" && !player.waitlisted && !player.is_sample
          && !(appState.notice && appState.notice.kind === "cancelled")) {
        const lockButton = document.createElement("button");
        lockButton.className = "lock-button";
        lockButton.type = "button";
        lockButton.dataset.action = "lock-rsvp";
        lockButton.dataset.name = player.name;
        lockButton.textContent = "Lock in";
        lockButton.title = "Lock in your spot. You will not be able to remove it.";
        actions.append(lockButton);
      }
      actions.append(remove);
    }
    row.append(avatar, name, status, actions);
    list.append(row);
  });
}

function renderPermits(permits) {
  const list = $("#permit-list");
  const isEmpty = permits.length === 0;
  list.replaceChildren();
  $("#permit-empty").classList.toggle("hidden", !isEmpty);
  $("#permit-reminder-note").classList.toggle("hidden", isEmpty);
  $(".permits-panel").classList.toggle("is-empty", isEmpty);
  permits.forEach(permit => {
    const card = document.createElement("article");
    card.className = "permit-card";
    const icon = document.createElement("span");
    icon.className = "permit-icon";
    icon.innerHTML = "<svg viewBox='0 0 24 24' aria-hidden='true'><rect x='4' y='5' width='16' height='15' rx='2'/><path d='M8 3v4M16 3v4M4 10h16M8 14h3'/></svg>";
    const copy = document.createElement("div");
    copy.className = "permit-copy";
    const title = document.createElement("p");
    title.className = "permit-title";
    title.textContent = permit.title;
    const subtitle = document.createElement("p");
    subtitle.className = "permit-subtitle";
    const formatted = formatDate(permit.date, { weekday: "short", month: "short", day: "numeric" });
    subtitle.textContent = permit.note ? `${formatted} · ${permit.note}` : formatted;
    copy.append(title, subtitle);
    const remaining = countdown(permit.date);
    const count = document.createElement("div");
    count.className = `permit-countdown${remaining.overdue ? " overdue" : ""}`;
    const amount = document.createElement("strong");
    amount.textContent = remaining.amount;
    const countLabel = document.createElement("span");
    countLabel.textContent = remaining.label;
    count.append(amount, countLabel);
    const actions = document.createElement("div");
    actions.className = "permit-actions";
    const reminder = document.createElement("button");
    reminder.className = "permit-action";
    reminder.type = "button";
    reminder.dataset.action = "calendar";
    reminder.dataset.id = permit.id;
    reminder.title = "Add to calendar";
    reminder.setAttribute("aria-label", `Add ${permit.title} to calendar`);
    reminder.innerHTML = "<svg viewBox='0 0 24 24' aria-hidden='true'><rect x='4' y='5' width='16' height='15' rx='2'/><path d='M8 3v4M16 3v4M4 10h16M8 14h3'/></svg>";
    const remove = document.createElement("button");
    remove.className = "permit-action delete";
    remove.type = "button";
    remove.dataset.action = "remove-permit";
    remove.dataset.id = permit.id;
    remove.title = "Remove reminder";
    remove.setAttribute("aria-label", `Remove ${permit.title}`);
    remove.textContent = "×";
    actions.append(reminder, remove);
    card.append(icon, copy, count, actions);
    list.append(card);
  });
}

function openSettings() {
  if (!appState) return;
  const { settings } = appState;
  $("#settings-club").value = settings.club_name;
  $("#settings-date").value = settings.game_date;
  $("#settings-date").min = appState.today;
  $("#settings-time").value = settings.kickoff;
  $("#settings-end").value = settings.end_time;
  $("#settings-notes").value = settings.notes || "";
  $("#settings-capacity").value = settings.max_players;
  $("#settings-location").value = settings.location;
  $("#settings-dialog").showModal();
}

function downloadCalendar(permit) {
  const content = window.PickupIcs.buildIcs(permit);
  const url = URL.createObjectURL(new Blob([content], { type: "text/calendar;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `pickup-poll-${permit.date}-${permit.id}.ics`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("Calendar reminder downloaded.");
}

function downloadGameCalendar() {
  if (!appState) return;
  const content = window.PickupIcs.buildGameIcs(appState.settings);
  const url = URL.createObjectURL(new Blob([content], { type: "text/calendar;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `football-${appState.settings.game_date}.ics`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("Added: open the downloaded file to put it in your calendar.");
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const box = document.createElement("textarea");        // plain http pages have no clipboard API
    box.value = text;
    box.style.position = "fixed";
    box.style.opacity = "0";
    document.body.append(box);
    box.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (copyError) { ok = false; }
    box.remove();
    return ok;
  }
}

async function invite() {
  if (!appState) return;
  const s = appState.settings;
  const text = `${s.club_name}: ${formatDate(s.game_date, { weekday: "long", month: "long", day: "numeric" })}, ${formatTime(s.kickoff)} to ${formatTime(s.end_time)} at ${s.location}. Are you in?`;
  const url = `${location.origin}${location.pathname}`;
  if (navigator.share) {
    try { await navigator.share({ title: s.club_name, text, url }); return; } catch (error) { if (error.name === "AbortError") return; }
  }
  const copied = await copyText(`${text} ${url}`);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  if (!copied) toast("Could not copy. Select the page address and share it.", true);
  else toast(local ? "Copied. This link only opens on this computer until the app is hosted." : "Invite copied. Paste it in the group chat.");
}

function openNoticeDialog() {
  if (!appState) return;
  const notice = appState.notice;
  $("#notice-text").value = notice ? notice.text : "";
  $("#notice-cancelled").checked = Boolean(notice && notice.kind === "cancelled");
  $("#notice-dialog").showModal();
}

async function postNotice(text, kind) {
  try {
    const next = await asOrganizer(() => api("/api/notice", { method: "POST", body: JSON.stringify({ text, kind }) }));
    if (!next) return;
    acceptState(next);
    $("#notice-dialog").close();
    toast(text || kind === "cancelled" ? "Update posted." : "Update removed.");
  } catch (error) { toast(error.message, true); }
}

// One tap answers: used by the three buttons, Enter in the name box and the big match-card button.
async function submitAnswer(status) {
  const name = $("#player-name").value.trim().replace(/\s+/g, " ");
  if (!name) {
    toast("Type your name first.", true);
    $("#player-name").focus();
    return;
  }
  const email = $("#player-email").value.trim();
  const code = storedCode() || $("#join-code").value.trim();
  if (appState && appState.email_required && !appState.player_has_email && !email) {
    toast("Add your Cornell email first.", true);
    $("#player-email").focus();
    return;
  }
  if (appState && appState.join_code_required && !code) {
    toast("Add the club code from the group chat.", true);
    $("#code-field").classList.remove("hidden");
    $("#join-code").focus();
    return;
  }
  const currentRow = myRow();
  const wasGoing = Boolean(currentRow && currentRow.status === "going" && !currentRow.waitlisted);
  const joinOrigin = centerOf($("#join-in"));
  $$(".join-btn, #join-hero").forEach(button => { button.disabled = true; });
  try {
    const next = await api("/api/rsvps", { method: "POST", body: JSON.stringify({ name, status, email, join_code: code }) });
    if (code) storeCode(code);
    $("#join-code").value = "";
    try { localStorage.setItem("pickup-poll-name", name); } catch (error) { /* private mode */ }
    acceptState(next);
    const row = myRow();
    if (row && row.waitlisted) toast(`You're number ${row.waitlist_pos} on the waitlist.`);
    else if (status === "going") {
      if (!wasGoing) celebrate("You're in", { origin: joinOrigin, big: false });
      toast("You're in. See you there.");
    }
    else if (status === "maybe") toast("Marked as a maybe.");
    else toast("Got it, you're out this week.");
  } catch (error) {
    if (error.status === 403 && /club code/.test(error.message)) {   // a wrong or changed code: ask again
      storeCode("");
      renderJoinExtras();
      $("#join-code").focus();
    }
    toast(error.message, true);
    render();
  }
}

// Organizer: every sign-up with its Cornell email, and a way to remove fake names.
function renderSignups(players) {
  const list = $("#signup-list");
  list.replaceChildren();
  if (!players.length) {
    const empty = document.createElement("p");
    empty.className = "dialog-copy";
    empty.textContent = "No sign-ups yet.";
    list.append(empty);
    return;
  }
  players.forEach(player => {
    const row = document.createElement("div");
    row.className = "signup-row";
    const who = document.createElement("div");
    who.className = "signup-who";
    const name = document.createElement("strong");
    name.textContent = player.name;
    const email = document.createElement("span");
    email.className = "signup-email";
    email.textContent = player.email || (player.is_sample ? "sample" : "no email given");
    const when = document.createElement("span");
    when.className = "signup-when";
    const answer = player.waitlisted ? "waitlist" : STATUS_WORDS[player.status].toLowerCase();
    when.textContent = `${answer} · ${player.updated_at.replace("T", " ")}`;
    who.append(name, email, when);
    const remove = document.createElement("button");
    remove.className = "button button-quiet";
    remove.type = "button";
    remove.dataset.action = "organizer-remove";
    remove.dataset.name = player.name;
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove ${player.name}`);
    row.append(who, remove);
    list.append(row);
  });
}

async function openSignups() {
  try {
    const result = await asOrganizer(() => api("/api/signups", { method: "POST", body: "{}" }));
    if (!result) return;
    renderSignups(result.players);
    if (!$("#signups-dialog").open) $("#signups-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function copyLockedSquad() {
  try {
    const result = await asOrganizer(() => api("/api/locked-squad", { method: "POST", body: "{}" }));
    if (!result) return;
    const names = result.names.join("\n");
    if (!await copyText(names)) throw new Error("Could not copy the names. Please try again.");
    toast(`Copied ${result.names.length} locked ${result.names.length === 1 ? "name" : "names"}. Paste into WhatsApp.`);
  } catch (error) {
    toast(error.message, true);
  }
}

function openPermitDialog() {
  $("#permit-form").reset();
  $("#permit-date").value = "";
  $("#permit-date").min = appState ? appState.today : "";
  $("#permit-dialog").showModal();
}

function showView(name) {
  const permits = name === "permits";
  document.body.classList.toggle("view-permits", permits);
  $$(".nav-link").forEach(link => {
    const active = link.dataset.view === (permits ? "permits" : "match");
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0 });
}

document.addEventListener("DOMContentLoaded", () => {
  const themeToggle = $("#theme-toggle");
  const updateThemeToggle = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    const label = `Switch to ${dark ? "light" : "dark"} mode`;
    themeToggle.setAttribute("aria-label", label);
    themeToggle.setAttribute("title", label);
    themeToggle.setAttribute("aria-pressed", String(dark));
  };
  updateThemeToggle();
  themeToggle.addEventListener("click", () => {
    window.pickupPollTheme.toggle();
    updateThemeToggle();
  });

  $$(".nav-link").forEach(link => link.addEventListener("click", event => {
    event.preventDefault();
    history.replaceState(null, "", "#" + link.dataset.view);
    showView(link.dataset.view);
  }));
  showView(location.hash === "#permits" ? "permits" : "match");
  let savedName = "";
  try { savedName = localStorage.getItem("pickup-poll-name") || ""; } catch (error) { /* private mode */ }
  if (savedName) $("#player-name").value = savedName;
  loadState();
  setInterval(() => loadState(true), 30000);

  $("#rsvp-form").addEventListener("submit", event => {
    event.preventDefault();
    submitAnswer("going");
  });
  $$('.join-btn[data-status="maybe"], .join-btn[data-status="out"]').forEach(button => button.addEventListener("click", () => submitAnswer(button.dataset.status)));
  $("#player-name").addEventListener("input", () => { if (appState) render(); });
  $("#join-hero").addEventListener("click", () => {
    const row = myRow();
    if ((!row || row.status !== "going") && $("#player-name").value.trim()) { submitAnswer("going"); return; }
    $("#rsvp-form").scrollIntoView({ behavior: "smooth", block: "center" });
    if (!$("#player-name").value.trim()) $("#player-name").focus();
  });
  $("#add-to-calendar").addEventListener("click", downloadGameCalendar);
  $("#invite").addEventListener("click", invite);
  $("#copy-locked-squad").addEventListener("click", copyLockedSquad);
  $("#open-signups").addEventListener("click", openSignups);
  $$(".post-notice-trigger").forEach(button => button.addEventListener("click", openNoticeDialog));
  $("#notice-form").addEventListener("submit", event => {
    event.preventDefault();
    const text = $("#notice-text").value.trim();
    postNotice(text, $("#notice-cancelled").checked ? "cancelled" : "info");
  });
  $("#notice-clear").addEventListener("click", () => postNotice("", "info"));
  $("#shuffle-teams").addEventListener("click", async () => {
    try {
      const next = await asOrganizer(() => api("/api/shuffle", { method: "POST", body: "{}" }));
      if (!next) return;
      acceptState(next);
      toast("Teams shuffled.");
    } catch (error) { toast(error.message, true); }
  });

  $("#permit-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const next = await asOrganizer(() => api("/api/permits", { method: "POST", body: JSON.stringify(Object.fromEntries(form.entries())) }));
      if (!next) return;
      acceptState(next);
      $("#permit-dialog").close();
      toast("Permit reminder saved.");
    } catch (error) {
      toast(error.message, true);
    }
  });

  $("#settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const data = Object.fromEntries(form.entries());
    data.max_players = Number(data.max_players);
    try {
      const next = await asOrganizer(() => api("/api/game", { method: "POST", body: JSON.stringify(data) }));
      if (!next) return;
      acceptState(next);
      $("#settings-dialog").close();
      toast("Match details updated.");
    } catch (error) {
      toast(error.message, true);
    }
  });

  document.addEventListener("click", async event => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    if (target.dataset.action === "replay-celebration") { celebrate("You're locked in", { origin: centerOf(target) }); return; }
    if (target.dataset.action === "game-calendar") { downloadGameCalendar(); return; }
    if (target.dataset.action === "calendar") {
      const permit = appState?.permits.find(item => String(item.id) === target.dataset.id);
      if (permit) downloadCalendar(permit);
      return;
    }
    if (target.dataset.action === "lock-rsvp") {
      if (!confirm("Lock in your spot? After this you cannot remove it or change to Maybe or Out.")) return;
      const origin = centerOf(target);                    // the button disappears when the page redraws
      try {
        const next = await api("/api/lock", { method: "POST", body: JSON.stringify({ name: target.dataset.name }) });
        acceptState(next);
        celebrate("You're locked in", { origin });
        toast("You are locked in.");
      } catch (error) { toast(error.message, true); }
      return;
    }
    if (target.dataset.action === "remove-rsvp") {
      try {
        const next = await api(`/api/rsvps?name=${encodeURIComponent(target.dataset.name)}`, { method: "DELETE" });
        acceptState(next);
        toast("RSVP removed.");
      } catch (error) { toast(error.message, true); }
    }
    if (target.dataset.action === "organizer-remove") {
      if (!confirm(`Remove ${target.dataset.name} from this game? If someone is on the waitlist, they move up.`)) return;
      try {
        const next = await asOrganizer(() => api("/api/remove-player", { method: "POST", body: JSON.stringify({ name: target.dataset.name }) }));
        if (!next) return;
        acceptState(next);
        toast(`${target.dataset.name} was removed.`);
        await openSignups();
      } catch (error) { toast(error.message, true); }
      return;
    }
    if (target.dataset.action === "remove-permit") {
      try {
        const next = await asOrganizer(() => api(`/api/permits?id=${encodeURIComponent(target.dataset.id)}`, { method: "DELETE" }));
        if (!next) return;
        acceptState(next);
        toast("Reminder removed.");
      } catch (error) { toast(error.message, true); }
    }
  });

  $("#add-permit-link").addEventListener("click", openPermitDialog);
  $$(".settings-trigger, .edit-match-trigger").forEach(button => button.addEventListener("click", openSettings));
  $(".demo-note-close").addEventListener("click", async () => {
    try {
      const next = await asOrganizer(() => api("/api/clear-sample", { method: "POST", body: "{}" }));
      if (!next) return;
      acceptState(next);
      toast("Sample data removed.");
    } catch (error) { toast(error.message, true); }
  });
  $("#reset-week").addEventListener("click", async () => {
    if (!confirm("Remove every reply for this game? This cannot be undone.")) return;
    try {
      const next = await asOrganizer(() => api("/api/reset-week", { method: "POST", body: "{}" }));
      if (!next) return;
      acceptState(next);
      $("#settings-dialog").close();
      toast("This week's replies were cleared.");
    } catch (error) { toast(error.message, true); }
  });
});
