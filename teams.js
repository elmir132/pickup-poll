// Splits the confirmed players into two teams. The same split drives the pitch dots and the team lists.
(function (root) {
  // Small seeded random number generator, so everyone sees the same shuffle.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // players: [{seq, ...}] confirmed to be going. seed 0 = alternate in join order.
  function assignTeams(players, seed) {
    const ordered = players.slice().sort((a, b) => a.seq - b.seq);
    if (seed) {
      const random = mulberry32(seed);
      for (let i = ordered.length - 1; i > 0; i -= 1) {
        const j = Math.floor(random() * (i + 1));
        [ordered[i], ordered[j]] = [ordered[j], ordered[i]];
      }
    }
    const white = [];
    const orange = [];
    ordered.forEach((player, index) => (index % 2 === 0 ? white : orange).push(player));
    return { white, orange };
  }

  const api = { assignTeams, mulberry32 };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PickupTeams = api;
})(typeof window !== "undefined" ? window : globalThis);
