const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
let appState = null;
let selectedStatus = "going";
let toastTimer;

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

function storedPin() {
  try { return sessionStorage.getItem("pickup-poll-pin") || ""; } catch (error) { return ""; }
}

function storePin(pin) {
  try { pin ? sessionStorage.setItem("pickup-poll-pin", pin) : sessionStorage.removeItem("pickup-poll-pin"); } catch (error) { /* private mode */ }
}

async function api(url, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
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
  try {
    appState = await api("/api/state");
    render();
    $("body").classList.remove("is-offline");
  } catch (error) {
    $("body").classList.add("is-offline");
    if (!quiet) toast("Could not reach the local app. Is the server still running?", true);
  }
}

function render() {
  if (!appState) return;
  const { settings, rsvps, permits } = appState;
  const real = rsvps.filter(player => !player.is_sample);
  const going = real.filter(player => player.status === "going");
  const maybe = real.filter(player => player.status === "maybe");
  const max = settings.max_players;
  const gameDate = dateAtNoon(settings.game_date);
  const month = new Intl.DateTimeFormat("en-US", { month: "short" }).format(gameDate).toUpperCase();
  const week = isoWeek(gameDate);

  $("#week-label").textContent = `WEEK ${week}`;
  $("#match-week").textContent = `WEEK ${week} · OPEN RUN`;
  $("#date-month").textContent = month;
  $("#date-day").textContent = String(gameDate.getDate()).padStart(2, "0");
  $("#match-date-full").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric" }).format(gameDate);
  $("#club-name").textContent = settings.club_name;
  $("#kickoff").textContent = formatTime(settings.kickoff);
  $("#location").textContent = settings.location;
  $("#going-count").textContent = going.length;
  $("#max-players").textContent = max;
  $("#maybe-count").textContent = `${maybe.length} maybe${maybe.length === 1 ? "" : "s"}`;
  $("#roster-total").textContent = `${rsvps.length} player${rsvps.length === 1 ? "" : "s"}`;
  $("#demo-note").classList.toggle("hidden", !appState.has_sample);
  renderRecent(appState.recent || []);
  $("#capacity-track").value = Math.min(going.length, max);
  $("#capacity-track").setAttribute("aria-valuemax", max);
  $("#capacity-track").setAttribute("aria-valuenow", going.length);
  $("#squad-status-copy").textContent = going.length >= max ? "Squad is full" : `${max - going.length} spots still open`;
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

  renderRoster(rsvps);
  renderPermits(permits);
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
    dot.className = `option-dot ${player.status === "going" ? "going-dot" : player.status === "maybe" ? "maybe-dot" : "out-dot"}`;
    const label = document.createElement("span");
    label.textContent = (player.status === "going" ? "Going" : player.status === "maybe" ? "Maybe" : "Out") + (player.is_sample ? " · sample" : "");
    status.append(dot, label);
    const remove = document.createElement("button");
    remove.className = "remove-rsvp";
    remove.type = "button";
    remove.dataset.action = "remove-rsvp";
    remove.dataset.name = player.name;
    remove.setAttribute("aria-label", `Remove ${player.name}`);
    remove.title = "Remove RSVP";
    remove.textContent = "×";
    row.append(avatar, name, status, remove);
    list.append(row);
  });
}

function renderPermits(permits) {
  const list = $("#permit-list");
  list.replaceChildren();
  $("#permit-empty").classList.toggle("hidden", permits.length > 0);
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
    reminder.textContent = "♧";
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

function openPermitDialog() {
  $("#permit-form").reset();
  $("#permit-date").value = "";
  $("#permit-date").min = appState ? appState.today : "";
  $("#permit-dialog").showModal();
}

document.addEventListener("DOMContentLoaded", () => {
  const savedName = localStorage.getItem("pickup-poll-name");
  if (savedName) $("#player-name").value = savedName;
  loadState();
  setInterval(() => loadState(true), 30000);

  $$(".status-option").forEach(button => button.addEventListener("click", () => {
    selectedStatus = button.dataset.status;
    $$(".status-option").forEach(option => option.classList.toggle("selected", option === button));
  }));

  $("#rsvp-form").addEventListener("submit", async event => {
    event.preventDefault();
    const name = $("#player-name").value.trim();
    const submit = $(".submit-rsvp");
    submit.disabled = true;
    try {
      appState = await api("/api/rsvps", { method: "POST", body: JSON.stringify({ name, status: selectedStatus }) });
      localStorage.setItem("pickup-poll-name", name);
      render();
      toast("Your RSVP is on the list.");
    } catch (error) {
      toast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#permit-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const next = await asOrganizer(() => api("/api/permits", { method: "POST", body: JSON.stringify(Object.fromEntries(form.entries())) }));
      if (!next) return;
      appState = next;
      render();
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
      appState = next;
      render();
      $("#settings-dialog").close();
      toast("Match details updated.");
    } catch (error) {
      toast(error.message, true);
    }
  });

  document.addEventListener("click", async event => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    if (target.dataset.action === "calendar") {
      const permit = appState?.permits.find(item => String(item.id) === target.dataset.id);
      if (permit) downloadCalendar(permit);
      return;
    }
    if (target.dataset.action === "remove-rsvp") {
      try {
        const next = await asOrganizer(() => api(`/api/rsvps?name=${encodeURIComponent(target.dataset.name)}`, { method: "DELETE" }));
        if (!next) return;
        appState = next;
        render();
        toast("RSVP removed.");
      } catch (error) { toast(error.message, true); }
    }
    if (target.dataset.action === "remove-permit") {
      try {
        const next = await asOrganizer(() => api(`/api/permits?id=${encodeURIComponent(target.dataset.id)}`, { method: "DELETE" }));
        if (!next) return;
        appState = next;
        render();
        toast("Reminder removed.");
      } catch (error) { toast(error.message, true); }
    }
  });

  $("#add-permit-trigger").addEventListener("click", openPermitDialog);
  $("#add-permit-link").addEventListener("click", openPermitDialog);
  $$(".settings-trigger, .edit-match-trigger").forEach(button => button.addEventListener("click", openSettings));
  $(".demo-note-close").addEventListener("click", async () => {
    try {
      const next = await asOrganizer(() => api("/api/clear-sample", { method: "POST", body: "{}" }));
      if (!next) return;
      appState = next;
      render();
      toast("Sample data removed.");
    } catch (error) { toast(error.message, true); }
  });
  $("#reset-week").addEventListener("click", async () => {
    if (!confirm("Remove every reply for this game? This cannot be undone.")) return;
    try {
      const next = await asOrganizer(() => api("/api/reset-week", { method: "POST", body: "{}" }));
      if (!next) return;
      appState = next;
      render();
      $("#settings-dialog").close();
      toast("This week's replies were cleared.");
    } catch (error) { toast(error.message, true); }
  });
});
