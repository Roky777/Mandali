/* Progressive decoration: an elastic grid and dissipating cursor ripples. */
(() => {
  const layer = document.querySelector('.ambient-mesh');
  if (!layer) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const canvas = document.createElement('canvas');
  canvas.setAttribute('aria-hidden', 'true');
  const ctx = canvas.getContext('2d');
  if (!ctx) return; // The static SVG mesh remains available.

  let width = 0, height = 0, frame = 0, lastDraw = 0, lastRipple = 0;
  let strength = 0, target = 0, x = 0, y = 0, aimX = 0, aimY = 0;
  let lastX = 0, lastY = 0, color = '', ripples = [];
  const spacing = 56;

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    const ratio = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    color = getComputedStyle(layer).getPropertyValue('--color-accent').trim();
  }

  function ripple(px, py, now, force = false) {
    if (force || (now - lastRipple > 180 && Math.hypot(px-lastX, py-lastY) > 65)) {
      ripples.push({ x: px, y: py, born: now });
      ripples = ripples.slice(-5);
      lastRipple = now; lastX = px; lastY = py;
    }
  }

  function point(px, py, seconds, now) {
    // Slow ambient waves remain continuous as the pointer enters or leaves.
    let vx = px + Math.sin(py / 220 + seconds * .22) * 10;
    let vy = py + Math.sin(px / 280 + seconds * .18) * 12;
    const dx = vx-x, dy = vy-y;
    const distance = Math.hypot(dx, dy);
    const well = Math.exp(-(distance * distance) / (210 * 210)) * strength;
    // Pull intersections towards the cursor and depress the sheet below it.
    vx -= dx * well * .26;
    vy += (36 - dy * .19) * well;
    for (const wave of ripples) {
      const rx = px-wave.x, ry = py-wave.y;
      const radius = Math.hypot(rx, ry) || 1;
      const age = (now-wave.born)/1000;
      const front = radius-age*190;
      const displacement = Math.sin(front/19) * Math.exp(-(front*front)/12000) * Math.exp(-age*1.35) * 10;
      vx += rx/radius * displacement;
      vy += ry/radius * displacement;
    }
    return [vx, vy];
  }

  function draw(now) {
    frame = requestAnimationFrame(draw);
    if (now-lastDraw < 1000/30) return;
    lastDraw = now;
    strength += (target-strength)*.11;
    x += (aimX-x)*.16; y += (aimY-y)*.16;
    ripples = ripples.filter(wave => now-wave.born < 2600);
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.globalAlpha = .13;
    ctx.beginPath();
    const seconds = now/1000;
    for (let row = -spacing; row <= height+spacing; row += spacing) {
      for (let px = -spacing; px <= width+spacing; px += 20) {
        const [vx, vy] = point(px, row, seconds, now);
        if (px === -spacing) ctx.moveTo(vx, vy); else ctx.lineTo(vx, vy);
      }
    }
    for (let col = -spacing; col <= width+spacing; col += spacing) {
      for (let py = -spacing; py <= height+spacing; py += 20) {
        const perspectiveX = width/2 + (col-width/2)*(.87+py/Math.max(height,1)*.25);
        const [vx, vy] = point(perspectiveX, py, seconds, now);
        if (py === -spacing) ctx.moveTo(vx, vy); else ctx.lineTo(vx, vy);
      }
    }
    ctx.stroke();
    // A faint expanding ring helps the soap-water gesture read immediately.
    for (const wave of ripples) {
      const age = (now-wave.born)/1000;
      ctx.globalAlpha = Math.max(0, .12*(1-age/2.6));
      ctx.beginPath();
      ctx.ellipse(wave.x, wave.y, Math.max(1, age*190), Math.max(1, age*165), 0, 0, Math.PI*2);
      ctx.stroke();
    }
  }

  function syncMotion() {
    cancelAnimationFrame(frame);
    frame = 0;
    if (reduced.matches) {
      canvas.remove(); layer.classList.remove('mesh-interactive');
      ripples = []; strength = 0; target = 0;
      return;
    }
    if (!canvas.isConnected) layer.append(canvas);
    layer.classList.add('mesh-interactive');
    resize();
    if (!document.hidden) { lastDraw = 0; frame = requestAnimationFrame(draw); }
  }
  document.addEventListener('pointermove', event => {
    if (reduced.matches || event.pointerType === 'touch') return;
    if (!target) { x = event.clientX; y = event.clientY; }
    target = 1; aimX = event.clientX; aimY = event.clientY;
    ripple(aimX, aimY, performance.now());
  }, { passive: true });
  document.addEventListener('pointerdown', event => {
    if (!reduced.matches) ripple(event.clientX, event.clientY, performance.now(), true);
  }, { passive: true });
  document.documentElement.addEventListener('pointerleave', () => { target = 0; });
  window.addEventListener('blur', () => { target = 0; });
  window.addEventListener('resize', resize, { passive: true });
  document.addEventListener('visibilitychange', syncMotion);
  reduced.addEventListener('change', syncMotion);
  syncMotion();
})();
