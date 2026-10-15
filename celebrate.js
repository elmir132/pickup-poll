// The "you're locked in" moment: confetti that shoots from the button you pressed (gravity, drag, fluttering
// pieces, small footballs) and a card with an animated check mark and the game details.
// The physics is plain functions so it can be tested without a browser.
(function (root) {
  const GRAVITY = 1500;                                   // pixels per second squared
  const PALETTE = ["#d8f36a", "#b4d06b", "#f2a273", "#fffefa", "#1f5a43", "#e7f2df"];   // dark theme: lime, sage, orange, cream, green, mint
  const PALETTE_LIGHT = ["#143e2e", "#2f7a57", "#8fb339", "#a9d43a", "#f2a273", "#e58a4d"]; // light theme: pale colours would vanish on the cream page

  function paletteFor(theme) { return theme === "dark" ? PALETTE : PALETTE_LIGHT; }
  const DEG = Math.PI / 180;

  function between(rng, low, high) { return low + (high - low) * rng(); }

  // One burst: a fan of pieces from a point. angle range is in degrees, -90 is straight up.
  function makeParticles(options) {
    const { count, origin, rng = Math.random, angle = [-155, -25], speed = [420, 980], balls = 0, delay = 0, palette = PALETTE } = options;
    const pieces = [];
    for (let i = 0; i < count; i += 1) {
      const aim = between(rng, angle[0], angle[1]) * DEG;
      const power = between(rng, speed[0], speed[1]);
      const roll = rng();
      const isBall = i < balls;
      const kind = isBall ? "ball" : roll < 0.5 ? "rect" : roll < 0.78 ? "strip" : "dot";
      pieces.push({
        kind,
        x: origin.x, y: origin.y,
        vx: Math.cos(aim) * power, vy: Math.sin(aim) * power,
        drag: between(rng, 1.4, 2.4),                      // slows pieces down the way air does
        rot: between(rng, 0, Math.PI * 2), vrot: between(rng, -9, 9),
        flip: between(rng, 0, Math.PI * 2), vflip: between(rng, 6, 15),     // makes a piece flutter as if it turned over
        w: kind === "strip" ? 3.5 : kind === "dot" ? 7 : between(rng, 9, 14),
        h: kind === "strip" ? between(rng, 14, 20) : kind === "dot" ? 7 : between(rng, 5, 8),
        size: isBall ? between(rng, 18, 24) : 0,
        color: palette[Math.floor(rng() * palette.length)],
        age: -delay, life: between(rng, 1.7, 2.7),
      });
    }
    return pieces;
  }

  // Advance every piece by dt seconds. Returns how many are still visible.
  function step(pieces, dt) {
    let alive = 0;
    for (const p of pieces) {
      let t = dt;                                                    // how long this piece actually moves in this step
      if (p.age < 0) {                                               // waiting for its turn
        p.age += dt;
        if (p.age < 0) { alive += 1; continue; }
        t = p.age;                                                   // its delay ended part way through this step
      } else {
        p.age += dt;
      }
      if (p.age >= p.life) continue;
      const damp = Math.exp(-p.drag * t);
      p.vx *= damp;
      p.vy = p.vy * damp + GRAVITY * t;
      p.x += p.vx * t;
      p.y += p.vy * t;
      p.rot += p.vrot * t;
      p.flip += p.vflip * t;
      alive += 1;
    }
    return alive;
  }

  function opacity(p) {
    const fadeStart = 0.62 * p.life;
    return p.age <= fadeStart ? 1 : Math.max(0, 1 - (p.age - fadeStart) / (p.life - fadeStart));
  }

  function drawBall(ctx, r) {
    ctx.fillStyle = "#fffefa";
    ctx.strokeStyle = "#1d2721";
    ctx.lineWidth = Math.max(1, r * 0.1);
    ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#1d2721";
    ctx.beginPath();
    for (let i = 0; i < 5; i += 1) {                                      // the central pentagon
      const a = (i / 5) * Math.PI * 2 - Math.PI / 2;
      const x = Math.cos(a) * r * 0.42, y = Math.sin(a) * r * 0.42;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath(); ctx.fill();
    ctx.beginPath();
    for (let i = 0; i < 5; i += 1) {                                      // seams out to the edge
      const a = (i / 5) * Math.PI * 2 - Math.PI / 2;
      ctx.moveTo(Math.cos(a) * r * 0.42, Math.sin(a) * r * 0.42);
      ctx.lineTo(Math.cos(a) * r * 0.95, Math.sin(a) * r * 0.95);
    }
    ctx.stroke();
  }

  function draw(ctx, pieces) {
    for (const p of pieces) {
      if (p.age < 0 || p.age >= p.life) continue;
      ctx.save();
      ctx.globalAlpha = opacity(p);
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      if (p.kind === "ball") {
        drawBall(ctx, p.size / 2);
      } else {
        ctx.fillStyle = p.color;
        if (p.kind === "dot") { ctx.beginPath(); ctx.arc(0, 0, p.w / 2, 0, Math.PI * 2); ctx.fill(); }
        else { ctx.scale(1, Math.cos(p.flip)); ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); }
      }
      ctx.restore();
    }
  }

  // ---- the page part (needs a browser) ----------------------------------------------------------------------

  let active = null;

  function stop() {
    if (!active) return;
    cancelAnimationFrame(active.frame);
    clearTimeout(active.timer);
    active.canvas && active.canvas.remove();
    active.badge && active.badge.remove();
    document.removeEventListener("keydown", active.onKey);
    active = null;
  }

  function buildBadge(title, subtitle) {
    const svgNs = "http://www.w3.org/2000/svg";
    const badge = document.createElement("div");
    badge.className = "celebrate-badge";
    badge.setAttribute("role", "status");
    const check = document.createElement("span");
    check.className = "celebrate-check";
    const svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("viewBox", "0 0 52 52");
    svg.setAttribute("aria-hidden", "true");
    const ring = document.createElementNS(svgNs, "circle");
    ring.setAttribute("class", "celebrate-ring");
    ring.setAttribute("cx", "26"); ring.setAttribute("cy", "26"); ring.setAttribute("r", "23");
    const tick = document.createElementNS(svgNs, "path");
    tick.setAttribute("class", "celebrate-tick");
    tick.setAttribute("d", "M15 27l8 8 14-16");
    svg.append(ring, tick);
    check.append(svg);
    const text = document.createElement("span");
    text.className = "celebrate-text";
    const strong = document.createElement("strong");
    strong.textContent = title;
    text.append(strong);
    if (subtitle) {
      const small = document.createElement("span");
      small.textContent = subtitle;
      text.append(small);
    }
    badge.append(check, text);
    return badge;
  }

  // options: title, subtitle, origin {x, y} in viewport pixels (the button), big (more confetti and side cannons)
  function celebrate(options = {}) {
    stop();
    const { title = "You're locked in", subtitle = "", big = true } = options;
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const state = { frame: 0, timer: 0, canvas: null, badge: buildBadge(title, subtitle), onKey: null };
    active = state;
    if (reduced) state.badge.classList.add("is-still");
    document.body.append(state.badge);
    const dismiss = () => {
      if (active !== state) return;
      state.badge.classList.add("is-leaving");
      setTimeout(() => { if (active === state) stop(); }, reduced ? 0 : 260);
    };
    state.badge.addEventListener("click", dismiss);
    state.onKey = event => { if (event.key === "Escape") dismiss(); };
    document.addEventListener("keydown", state.onKey);
    state.timer = setTimeout(dismiss, reduced ? 2400 : 3400);
    if (reduced) return;

    const tapped = !navigator.userActivation || navigator.userActivation.isActive;      // browsers refuse to vibrate without a recent tap
    if (navigator.vibrate && tapped) { try { navigator.vibrate(big ? [18, 40, 18] : 14); } catch (error) { /* not supported */ } }

    const width = window.innerWidth, height = window.innerHeight;
    const origin = options.origin || { x: width / 2, y: height * 0.6 };
    const palette = paletteFor(document.documentElement.dataset.theme);
    let pieces = makeParticles({ count: big ? 120 : 60, origin, balls: big ? 4 : 2, speed: big ? [460, 1050] : [380, 820], palette });
    if (big) {                                                              // two cannons from the bottom corners
      pieces = pieces.concat(
        makeParticles({ count: 40, origin: { x: -10, y: height + 10 }, angle: [-82, -48], speed: [800, 1250], delay: 0.14, palette }),
        makeParticles({ count: 40, origin: { x: width + 10, y: height + 10 }, angle: [-132, -98], speed: [800, 1250], delay: 0.14, palette }));
    }
    const canvas = document.createElement("canvas");
    canvas.className = "celebrate-canvas";
    canvas.setAttribute("aria-hidden", "true");
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    document.body.append(canvas);
    state.canvas = canvas;
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    let last = performance.now();
    const tick = now => {
      if (active !== state) return;
      const dt = Math.min((now - last) / 1000, 1 / 30);                     // never jump after a slow frame
      last = now;
      ctx.clearRect(0, 0, width, height);
      const alive = step(pieces, dt);
      draw(ctx, pieces);
      if (alive > 0) state.frame = requestAnimationFrame(tick);
      else { canvas.remove(); state.canvas = null; }
    };
    state.frame = requestAnimationFrame(tick);
  }

  const api = { celebrate, makeParticles, step, opacity, paletteFor, PALETTE, PALETTE_LIGHT, GRAVITY };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PickupCelebrate = api;
})(typeof window !== "undefined" ? window : globalThis);
