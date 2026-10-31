/* Progressive decoration: a softly bending grid with no ripple trails. */
(() => {
  const layer = document.querySelector('.ambient-mesh');
  if (!layer) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const canvas = document.createElement('canvas');
  canvas.setAttribute('aria-hidden', 'true');
  const ctx = canvas.getContext('2d');
  if (!ctx) return; // The static SVG mesh remains available.

  let width = 0, height = 0, frame = 0, lastDraw = 0;
  let strength = 0, target = 0, x = 0, y = 0, aimX = 0, aimY = 0;
  let color = '';
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

  function point(px, py, seconds) {
    // Slow ambient waves remain continuous as the pointer enters or leaves.
    let vx = px + Math.sin(py / 220 + seconds * .22) * 10;
    let vy = py + Math.sin(px / 280 + seconds * .18) * 12;
    const dx = vx-x, dy = vy-y;
    const distance = Math.hypot(dx, dy);
    const well = Math.exp(-(distance * distance) / (210 * 210)) * strength;
    // Pull intersections towards the cursor and depress the sheet below it.
    vx -= dx * well * .26;
    vy += (36 - dy * .19) * well;
    return [vx, vy];
  }

  function draw(now) {
    frame = requestAnimationFrame(draw);
    if (now-lastDraw < 1000/30) return;
    lastDraw = now;
    strength += (target-strength)*(target ? .11 : .07);
    x += (aimX-x)*.13; y += (aimY-y)*.13;
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.globalAlpha = document.documentElement.dataset.theme === 'dark' ? .075 : .13;
    ctx.beginPath();
    const seconds = now/1000;
    for (let row = -spacing; row <= height+spacing; row += spacing) {
      for (let px = -spacing; px <= width+spacing; px += 20) {
        const [vx, vy] = point(px, row, seconds);
        if (px === -spacing) ctx.moveTo(vx, vy); else ctx.lineTo(vx, vy);
      }
    }
    for (let col = -spacing; col <= width+spacing; col += spacing) {
      for (let py = -spacing; py <= height+spacing; py += 20) {
        const perspectiveX = width/2 + (col-width/2)*(.87+py/Math.max(height,1)*.25);
        const [vx, vy] = point(perspectiveX, py, seconds);
        if (py === -spacing) ctx.moveTo(vx, vy); else ctx.lineTo(vx, vy);
      }
    }
    ctx.stroke();
  }

  function syncMotion() {
    cancelAnimationFrame(frame);
    frame = 0;
    if (reduced.matches) {
      canvas.remove(); layer.classList.remove('mesh-interactive');
      strength = 0; target = 0;
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
  }, { passive: true });
  document.documentElement.addEventListener('pointerleave', () => { target = 0; });
  window.addEventListener('blur', () => { target = 0; });
  window.addEventListener('resize', resize, { passive: true });
  document.addEventListener('mandali:themechange', resize);
  document.addEventListener('visibilitychange', syncMotion);
  reduced.addEventListener('change', syncMotion);
  syncMotion();
})();
