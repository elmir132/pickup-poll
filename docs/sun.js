// Approximate sunset for Roosevelt Island, NYC (NOAA general solar equations). Good to a couple of minutes.
(function (root) {
  const LAT = 40.7625;
  const LON = -73.95;

  function dayOfYear(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(y, 0, 0)) / 86400000);
  }

  // Returns a Date (the instant of sunset) for a YYYY-MM-DD date.
  function sunsetDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    const gamma = (2 * Math.PI / 365) * (dayOfYear(iso) - 1);
    const eqTime = 229.18 * (0.000075 + 0.001868 * Math.cos(gamma) - 0.032077 * Math.sin(gamma)
      - 0.014615 * Math.cos(2 * gamma) - 0.040849 * Math.sin(2 * gamma));
    const decl = 0.006918 - 0.399912 * Math.cos(gamma) + 0.070257 * Math.sin(gamma) - 0.006758 * Math.cos(2 * gamma)
      + 0.000907 * Math.sin(2 * gamma) - 0.002697 * Math.cos(3 * gamma) + 0.00148 * Math.sin(3 * gamma);
    const rad = Math.PI / 180;
    const cosHa = Math.cos(90.833 * rad) / (Math.cos(LAT * rad) * Math.cos(decl)) - Math.tan(LAT * rad) * Math.tan(decl);
    const ha = Math.acos(Math.max(-1, Math.min(1, cosHa))) / rad;
    const minutesUtc = 720 - 4 * (LON - ha) - eqTime;
    return new Date(Date.UTC(y, m - 1, d) + minutesUtc * 60000);
  }

  // "4:49 PM" in New York time, whatever the viewer's own time zone is.
  function sunsetLabel(iso) {
    return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }).format(sunsetDate(iso));
  }

  const api = { sunsetDate, sunsetLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PickupSun = api;
})(typeof window !== "undefined" ? window : globalThis);
