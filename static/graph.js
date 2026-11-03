// The Horizons map (horizons.html's overview): db.horizon_graph(), embedded as #horizon-graph, drawn as a graph:
// purpose & vision in the middle, the areas on a ring around it, their goals, projects and H0 actions outward, and
// anything serving nothing floating loose on the outside. Nothing else reads that data, so another view of it (a
// matrix, a tree) can replace this file without touching the server.
// Layout is deterministic (a radial tree, then a fixed number of force steps), so a reload draws the same map.
// Hovering a node lights it and its neighbours; a click opens it (a record in its overlay, purpose & vision its
// card); a node drags, the background pans, the wheel zooms. The swatches under it (ul.levels) show or hide a level.
(() => {
  const box = document.querySelector('.horizon-map'), src = document.getElementById('horizon-graph');
  if (!box || !src) return;
  const data = JSON.parse(src.textContent);
  const SVG = 'http://www.w3.org/2000/svg';
  const RADIUS = {purpose: 13, area: 8, goal: 6, project: 4.5, task: 3};
  const RING = {purpose: 0, area: 170, goal: 270, project: 360, task: 400};  // each level's distance out
  const LINK = {area: 120, goal: 80, project: 60, task: 45};  // a child's spring length to its parent
  const LABELLED = new Set(['purpose', 'area', 'goal']);  // the rest are labelled on hover or zoomed in

  const byId = new Map(data.nodes.map(n => [n.id, {...n, x: 0, y: 0, vx: 0, vy: 0, kids: [], parent: null, near: new Set()}]));
  const nodes = [...byId.values()];
  const edges = data.edges.map(([a, b]) => [byId.get(a), byId.get(b)]).filter(([a, b]) => a && b);
  for (const [a, b] of edges) { a.kids.push(b); b.parent = a; a.near.add(b); b.near.add(a); }
  const root = nodes.find(n => n.kind === 'purpose');
  const loose = nodes.filter(n => !n.parent && n !== root);  // goals, projects, actions serving nothing
  for (const n of nodes) n.loose = !n.parent && n !== root;

  // Seed: a radial tree. Each area gets a slice of the circle sized by its leaves; children sit in their parent's
  // slice, at their level's ring. The loose ones go round the outside, evenly.
  const leaves = n => n.kids.length ? n.kids.reduce((s, k) => s + leaves(k), 0) : 1;
  const place = (n, from, to) => {
    const a = (from + to) / 2, r = (RING[n.kind] ?? 300) + (n.loose ? 90 : 0);
    n.x = Math.cos(a) * r; n.y = Math.sin(a) * r;
    let at = from;
    for (const k of n.kids) { const w = (to - from) * leaves(k) / leaves(n); place(k, at, at + w); at += w; }
  };
  const share = n => Math.sqrt(leaves(n));  // a busy area gets more room, but not so much the rest crowd together
  const areas = root?.kids || [], total = areas.reduce((s, b) => s + share(b), 0) || 1;
  let at = -Math.PI / 2;
  for (const b of areas) { const w = 2 * Math.PI * share(b) / total; place(b, at, at + w); at += w; }
  const ring = loose.reduce((s, b) => s + leaves(b), 0) || 1;
  at = -Math.PI / 2 + Math.PI / ring;
  for (const b of loose) { const w = 2 * Math.PI * leaves(b) / ring; place(b, at, at + w); at += w; }
  if (root) root.x = root.y = 0;  // the centre, fixed

  // Relax it: springs along edges, every pair repelling, a pull toward each level's ring, no overlaps.
  const tick = (alpha, fixed) => {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy || .01;
        const d = Math.sqrt(d2), f = 900 * alpha / d2, min = RADIUS[a.kind] + RADIUS[b.kind] + 14;
        let push = f + (d < min ? (min - d) * .5 : 0);
        dx /= d; dy /= d;
        a.vx -= dx * push; a.vy -= dy * push; b.vx += dx * push; b.vy += dy * push;
      }
    }
    for (const [p, c] of edges) {
      const dx = c.x - p.x, dy = c.y - p.y, d = Math.hypot(dx, dy) || .01, f = (d - LINK[c.kind]) / d * .08 * alpha;
      p.vx += dx * f; p.vy += dy * f; c.vx -= dx * f; c.vy -= dy * f;
    }
    for (const n of nodes) {
      const r = Math.hypot(n.x, n.y) || .01, want = RING[n.kind] + (n.loose ? 90 : 0);
      if (want) { const f = (want - r) / r * .1 * alpha; n.vx += n.x * f; n.vy += n.y * f; }
      if (n === root || n === fixed) { n.vx = n.vy = 0; continue; }
      n.x += n.vx *= .55; n.y += n.vy *= .55;
    }
  };
  for (let i = 0; i < 300; i++) tick(1 - i / 300);

  // Draw.
  const el = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    parent?.append(e);
    return e;
  };
  const svg = el('svg', {class: 'graph', role: 'img'}, box);
  const view = el('g', {}, svg), lines = el('g', {class: 'edges'}, view), dots = el('g', {class: 'nodes'}, view);
  for (const e of edges) e.line = el('line', {class: `k-${e[1].kind}`}, lines);
  for (const n of nodes) {
    const cls = ['node', `k-${n.kind}`, n.dim && 'dim', n.warn && 'warn', n.loose && 'loose', LABELLED.has(n.kind) && 'labelled'];
    n.g = el('a', {class: cls.filter(Boolean).join(' '), href: n.url}, dots);
    el('title', {}, n.g).textContent = [n.title, n.warn && `⚠ ${n.warn}`, n.sub, n.loose && 'Serves nothing above it'].filter(Boolean).join('\n');
    el('circle', {r: RADIUS[n.kind]}, n.g);
    const t = el('text', {x: RADIUS[n.kind] + 4, y: 4}, n.g);
    t.textContent = (n.warn ? '⚠ ' : '') + n.title;
  }
  const draw = () => {
    for (const e of edges) { const [a, b] = e; e.line.setAttribute('x1', a.x); e.line.setAttribute('y1', a.y); e.line.setAttribute('x2', b.x); e.line.setAttribute('y2', b.y); }
    for (const n of nodes) n.g.setAttribute('transform', `translate(${n.x},${n.y})`);
  };
  draw();

  // Pan and zoom: one transform on the view, fitted to the map on load and on resize.
  let k = 1, tx = 0, ty = 0;
  const apply = () => { view.setAttribute('transform', `translate(${tx},${ty}) scale(${k})`); svg.classList.toggle('zoomed', k > 1.4); };
  const fit = () => {
    const shown = nodes.filter(n => !n.hidden);
    if (!shown.length) return;
    const xs = shown.map(n => n.x), ys = shown.map(n => n.y), w = box.clientWidth, h = box.clientHeight;
    const x0 = Math.min(...xs) - 40, x1 = Math.max(...xs) + 140, y0 = Math.min(...ys) - 30, y1 = Math.max(...ys) + 30;
    k = Math.min(w / (x1 - x0), h / (y1 - y0), 1.6);
    tx = w / 2 - k * (x0 + x1) / 2; ty = h / 2 - k * (y0 + y1) / 2;
    apply();
  };
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    const r = svg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const f = Math.exp(-e.deltaY * .0015), nk = Math.min(Math.max(k * f, .2), 5);
    tx = mx - (mx - tx) * nk / k; ty = my - (my - ty) * nk / k; k = nk;
    apply();
  }, {passive: false});

  // Hover: the node and its neighbours stay lit, the rest fade.
  const light = n => {
    svg.classList.toggle('focus', !!n);
    for (const m of nodes) m.g.classList.toggle('lit', !!n && (m === n || n.near.has(m)));
    for (const e of edges) e.line.classList.toggle('lit', !!n && (e[0] === n || e[1] === n));
  };
  for (const n of nodes) {
    n.g.addEventListener('pointerenter', () => drag || light(n));
    n.g.addEventListener('pointerleave', () => drag || light(null));
    n.g.addEventListener('focus', () => light(n));
    n.g.addEventListener('blur', () => light(null));
  }

  // Drag a node (the rest settle around it), or the background (pan). A press that doesn't move is a click.
  let drag = null;
  const settle = n => { for (let i = 0; i < 3; i++) tick(.15, n); draw(); };
  svg.addEventListener('pointerdown', e => {
    if (e.button) return;
    const n = nodes.find(m => m.g.contains(e.target));
    drag = {n, x: e.clientX, y: e.clientY, tx, ty, moved: false};
  });
  svg.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) svg.setPointerCapture(e.pointerId);  // only once it moves, so a click still lands on its node
    drag.moved = true;
    svg.classList.add('dragging');
    if (drag.n) {
      const r = svg.getBoundingClientRect();
      drag.n.x = (e.clientX - r.left - tx) / k; drag.n.y = (e.clientY - r.top - ty) / k;
      settle(drag.n);
    } else { tx = drag.tx + dx; ty = drag.ty + dy; apply(); }
  });
  const end = () => { if (drag?.moved) setTimeout(() => drag = null); else drag = null; svg.classList.remove('dragging'); };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);
  // A click opens: a record in its overlay (⌘/Ctrl: a new tab), purpose & vision its card.
  svg.addEventListener('click', e => {
    const n = nodes.find(m => m.g.contains(e.target));
    e.preventDefault();
    if (!n || drag?.moved) return;
    if (e.metaKey || e.ctrlKey) open(n.url);
    else if (top.overlays && !n.url.includes('?')) top.overlays.open(n.url);
    else location = n.url;
  });

  // The swatches under the map show or hide a level; what's hidden is remembered (H0 actions start hidden).
  const KEY = 'horizon-map-hidden';
  let hidden;
  try { hidden = new Set(JSON.parse(localStorage[KEY] ?? '["task"]')); } catch { hidden = new Set(['task']); }
  const show = () => {
    for (const n of nodes) n.g.classList.toggle('hidden', n.hidden = hidden.has(n.kind));
    for (const e of edges) e.line.classList.toggle('hidden', e[0].hidden || e[1].hidden);
    document.querySelectorAll('.levels [data-kind]').forEach(b => b.setAttribute('aria-pressed', !hidden.has(b.dataset.kind)));
  };
  document.querySelectorAll('.levels [data-kind]').forEach(b => b.addEventListener('click', () => {
    hidden.has(b.dataset.kind) ? hidden.delete(b.dataset.kind) : hidden.add(b.dataset.kind);
    try { localStorage[KEY] = JSON.stringify([...hidden]); } catch {}
    show();
  }));
  show();
  fit();
  new ResizeObserver(fit).observe(box);
})();
