// Builds an iCalendar file for a permit reminder. Kept apart from app.js so it can be tested on its own.
(function (root) {
  function escapeIcs(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/\r\n|\r|\n/g, "\\n").replace(/,/g, "\\,").replace(/;/g, "\\;");
  }

  // Lines over 75 bytes are folded with CRLF + space, as the format requires. Counted in UTF-8 bytes, never splitting a character.
  function fold(line) {
    const out = [];
    let current = "";
    let size = 0;
    for (const ch of line) {
      const bytes = ch.codePointAt(0) < 0x80 ? 1 : ch.codePointAt(0) < 0x800 ? 2 : ch.codePointAt(0) < 0x10000 ? 3 : 4;
      if (size + bytes > 75) { out.push(current); current = " "; size = 1; }
      current += ch;
      size += bytes;
    }
    out.push(current);
    return out.join("\r\n");
  }

  function compact(iso) { return iso.replace(/-/g, ""); }

  function nextDay(iso) {
    const d = new Date(iso + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }

  function buildIcs(permit, now) {
    const stamp = (now || new Date()).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
    return [
      "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Pickup Poll//Club dates//EN", "CALSCALE:GREGORIAN",
      "BEGIN:VEVENT", "UID:pickup-poll-" + permit.id + "@local", "DTSTAMP:" + stamp,
      "DTSTART;VALUE=DATE:" + compact(permit.date), "DTEND;VALUE=DATE:" + compact(nextDay(permit.date)),
      fold("SUMMARY:" + escapeIcs(permit.title)),
      fold("DESCRIPTION:" + escapeIcs(permit.note || "Club permit reminder")),
      "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", "DESCRIPTION:Club permit reminder tomorrow", "END:VALARM",
      "END:VEVENT", "END:VCALENDAR",
    ].join("\r\n") + "\r\n";
  }

  const api = { buildIcs, escapeIcs, fold };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PickupIcs = api;
})(typeof window !== "undefined" ? window : globalThis);
