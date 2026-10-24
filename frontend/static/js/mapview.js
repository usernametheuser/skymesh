// Pan/zoom floor-plan viewer. The world is the image at natural pixel size; an SVG overlay with the
// same viewBox carries markers. Positions are stored normalized (0..1) and converted only for drawing,
// so markers stay aligned through zoom, pan and resizes.
import { h, s, clear } from './ui.js';

export class MapView {
  constructor() {
    this.k = 1; this.tx = 0; this.ty = 0; this.w = 1; this.h = 1;
    this.img = h('img', { alt: 'Floor plan', draggable: 'false' });
    this.svg = s('svg', { 'aria-label': 'Map overlay' });
    this.layers = {};
    for (const name of ['base', 'features', 'rooms', 'anchors', 'accuracy', 'devices', 'obs', 'incidents', 'draw']) {
      this.layers[name] = s('g', { class: `layer-${name}` });
      this.svg.append(this.layers[name]);
    }
    this.world = h('div', { class: 'map-world' }, this.img, this.svg);
    this.stage = h('div', { class: 'map-stage', tabindex: '0', 'aria-label': 'Campus map. Arrow keys pan, plus and minus zoom.' }, this.world,
      h('div', { class: 'map-zoom' },
        h('button', { type: 'button', 'aria-label': 'Zoom in', onclick: () => this.zoomBy(1.3) }, '+'),
        h('button', { type: 'button', 'aria-label': 'Zoom out', onclick: () => this.zoomBy(1 / 1.3) }, '−'),
        h('button', { type: 'button', 'aria-label': 'Fit to screen', onclick: () => this.fit() }, '⤢')));
    this.onClick = null; this.onZoom = null; this.onMove = null;
    this._bind();
    this._ro = new ResizeObserver(() => { if (!this._userMoved) this.fit(); });
    this._ro.observe(this.stage);
  }

  destroy() { this._ro.disconnect(); cancelAnimationFrame(this._raf); clearTimeout(this._zoomT); }

  setImage(url, w, hgt) {
    // Same image again (a refresh): keep it and the user's zoom instead of downloading and refitting.
    if (this._url === url && this.w === w && this.h === hgt && this.img.complete) return Promise.resolve();
    this._url = url;
    return new Promise((resolve, reject) => {
      this.w = w; this.h = hgt;
      this.img.width = w; this.img.height = hgt;
      this.world.style.width = `${w}px`; this.world.style.height = `${hgt}px`;
      this.svg.setAttribute('viewBox', `0 0 ${w} ${hgt}`);
      this.img.onload = () => { this._userMoved = false; this.fit(); resolve(); };
      this.img.onerror = () => reject(new Error('Floor-plan image failed to load'));
      this.img.src = url;
    });
  }

  // normalized <-> world units
  X(x) { return x * this.w; }
  Y(y) { return y * this.h; }
  px(n) { return n / this.k; } // a size in screen pixels expressed in world units

  toNorm(clientX, clientY) {
    const r = this.stage.getBoundingClientRect();
    const wx = (clientX - r.left - this.tx) / this.k;
    const wy = (clientY - r.top - this.ty) / this.k;
    return [wx / this.w, wy / this.h];
  }

  fit() {
    const r = this.stage.getBoundingClientRect();
    if (!r.width || !r.height) return;
    this.k = Math.min(r.width / this.w, r.height / this.h) * 0.98;
    this.tx = (r.width - this.w * this.k) / 2;
    this.ty = (r.height - this.h * this.k) / 2;
    this._apply();
  }

  zoomBy(f, cx, cy) {
    const r = this.stage.getBoundingClientRect();
    cx ??= r.width / 2; cy ??= r.height / 2;
    const nk = Math.min(Math.max(this.k * f, 0.05), 12);
    this.tx = cx - (cx - this.tx) * (nk / this.k);
    this.ty = cy - (cy - this.ty) * (nk / this.k);
    this.k = nk;
    this._userMoved = true;
    this._apply();
  }

  centerOn(x, y, k) {
    const r = this.stage.getBoundingClientRect();
    if (k) this.k = k;
    this.tx = r.width / 2 - this.X(x) * this.k;
    this.ty = r.height / 2 - this.Y(y) * this.k;
    this._userMoved = true;
    this._apply();
  }

  // One transform per animation frame; markers are re-sized only when the zoom actually changed
  // (panning moves the world with CSS alone, which is what keeps dragging smooth).
  _apply() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this.world.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.k})`;
      if (this.k !== this._drawnK) {
        this._drawnK = this.k;
        clearTimeout(this._zoomT);
        // While the wheel is turning, scale markers with the world; redraw them crisp once it settles.
        this._zoomT = setTimeout(() => this.onZoom?.(this.k), 90);
      }
    });
  }

  _bind() {
    const pts = new Map();
    let last = null, moved = false, pinch = null, downHit = null;
    this.stage.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = this.stage.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    this.stage.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.map-zoom, .map-legend, .map-ctl')) return;
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      moved = false; last = [e.clientX, e.clientY];
      downHit = e.target.closest?.('[data-hit]') || null;
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: this.k };
      }
    });
    this.stage.addEventListener('pointermove', (e) => {
      if (!pts.has(e.pointerId)) { this.onMove?.(this.toNorm(e.clientX, e.clientY)); return; }
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      if (pts.size === 2 && pinch) {
        const [a, b] = [...pts.values()];
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        const r = this.stage.getBoundingClientRect();
        this.zoomBy((pinch.k * d / pinch.d) / this.k, (a[0] + b[0]) / 2 - r.left, (a[1] + b[1]) / 2 - r.top);
        moved = true;
        return;
      }
      const dx = e.clientX - last[0], dy = e.clientY - last[1];
      if (!moved && Math.abs(dx) + Math.abs(dy) > 3) {
        moved = true;
        this.stage.setPointerCapture(e.pointerId); // capture only once a drag starts, so taps reach markers
      }
      if (moved) {
        this.stage.classList.add('dragging');
        this.tx += dx; this.ty += dy; last = [e.clientX, e.clientY];
        this._userMoved = true;
        this._apply();
      }
    });
    const up = (e) => {
      if (!pts.has(e.pointerId)) return;
      pts.delete(e.pointerId);
      if (pts.size < 2) pinch = null;
      this.stage.classList.remove('dragging');
      if (!moved && pts.size === 0) {
        if (downHit?._onHit) downHit._onHit(this.toNorm(e.clientX, e.clientY));
        else this.onClick?.(this.toNorm(e.clientX, e.clientY), e);
      }
      downHit = null;
    };
    this.stage.addEventListener('pointerup', up);
    this.stage.addEventListener('pointercancel', up);
    this.stage.addEventListener('keydown', (e) => {
      const step = 60;
      const m = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[e.key];
      if (m) { this.tx += m[0]; this.ty += m[1]; this._userMoved = true; this._apply(); e.preventDefault(); }
      if (e.key === '+' || e.key === '=') this.zoomBy(1.25);
      if (e.key === '-') this.zoomBy(0.8);
      if (e.key === '0') this.fit();
    });
  }

  clearLayer(name) { return clear(this.layers[name]); }
}

// Make an SVG element tappable. `fn(pointNorm)` returns false to fall through to the map click.
export function hit(el, fn, view) {
  el.dataset.hit = '1';
  el._onHit = (pt) => { if (fn(pt) === false) view?.onClick?.(pt); };
  return el;
}

// ------------------------------------------------------------ digital floor plan (shared by Map and Map Trainer)

const DIGITAL = {
  room: 'd-room', corridor: 'd-corridor', stair: 'd-stair', lift: 'd-lift', void: 'd-void', toilet: 'd-toilet',
  restricted: 'd-restricted', other: 'd-other', entrance: 'd-other', exit: 'd-other', assembly_point: 'd-other',
};

// Draws the clean vector plan into the 'base' layer. look: 'digital' | 'blend' | 'photo'.
// Built off-DOM and attached once, so a 30-room plan costs a single layout.
export function drawPlan(view, features, look) {
  const g = view.clearLayer('base');
  view.img.style.opacity = look === 'photo' ? '1' : look === 'blend' ? '0.22' : '0';
  view.stage.classList.toggle('digital', look !== 'photo');
  if (look === 'photo') return;
  const frag = document.createDocumentFragment();
  const polys = features.filter((f) => f.geometry_type === 'polygon');
  frag.append(s('rect', { x: 0, y: 0, width: view.w, height: view.h, class: 'd-ground' }));
  if (!polys.length) {
    frag.append(s('text', { x: view.w / 2, y: view.h / 2, 'text-anchor': 'middle', class: 'd-empty', 'font-size': view.w / 40 },
      'No rooms mapped yet: use Edit map → Draw area, or switch to Photo.'));
    g.append(frag);
    return;
  }
  // Building outline: the extent of everything mapped, with a little margin, drawn as the outer wall.
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const f of polys) for (const [x, y] of f.geometry) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  const pad = 0.006;
  frag.append(s('rect', { x: view.X(x0 - pad), y: view.Y(y0 - pad), width: view.X(x1 - x0 + 2 * pad), height: view.Y(y1 - y0 + 2 * pad), class: 'd-building' }));
  // Corridors first (floor), then rooms on top, voids last.
  const order = { corridor: 0, toilet: 1, room: 2, other: 2, restricted: 3, stair: 4, lift: 4, void: 5 };
  for (const f of [...polys].sort((a, b) => (order[a.kind] ?? 2) - (order[b.kind] ?? 2))) {
    frag.append(s('polygon', { points: polygonPoints(view, f.geometry), class: ['d-area', DIGITAL[f.kind] || 'd-other', !f.verified && 'd-draft'].filter(Boolean).join(' ') }));
    const fx = f.geometry.map((p) => p[0]), fy = f.geometry.map((p) => p[1]);
    const [ax, ay, bx, by] = [Math.min(...fx), Math.min(...fy), Math.max(...fx), Math.max(...fy)];
    if (f.kind === 'void') {
      frag.append(s('line', { x1: view.X(ax), y1: view.Y(ay), x2: view.X(bx), y2: view.Y(by), class: 'd-void-x' }),
        s('line', { x1: view.X(bx), y1: view.Y(ay), x2: view.X(ax), y2: view.Y(by), class: 'd-void-x' }));
    } else if (f.kind === 'stair') {
      // Stair treads: a few lines across the bounding box.
      const vertical = (by - ay) * view.h > (bx - ax) * view.w;
      for (let i = 1; i < 8; i++) {
        const k = i / 8;
        frag.append(vertical
          ? s('line', { x1: view.X(ax), y1: view.Y(ay + (by - ay) * k), x2: view.X(bx), y2: view.Y(ay + (by - ay) * k), class: 'd-tread' })
          : s('line', { x1: view.X(ax + (bx - ax) * k), y1: view.Y(ay), x2: view.X(ax + (bx - ax) * k), y2: view.Y(by), class: 'd-tread' }));
      }
    }
    // Labels: name, and the "(52+1 seats)" detail on a second line.
    const [cx, cy] = centroid(f.geometry);
    const m = f.name.match(/^(.*?)\s*\((.*)\)\s*$/);
    const title = m ? m[1] : f.name;
    const wpx = (bx - ax) * view.w;
    const size = Math.max(9, Math.min(22, wpx / Math.max(6, title.length) * 1.5));
    if (f.kind !== 'void' && wpx > 40) {
      frag.append(s('text', { x: view.X(cx), y: view.Y(cy), 'text-anchor': 'middle', 'dominant-baseline': 'middle', class: `d-label d-label-${f.kind}`, 'font-size': size }, title));
      if (m && f.kind === 'room') {
        frag.append(s('text', { x: view.X(cx), y: view.Y(cy) + size * 1.15, 'text-anchor': 'middle', 'dominant-baseline': 'middle', class: 'd-sub', 'font-size': size * 0.62 }, m[2]));
      }
    }
  }
  // Exits as green "EXIT" tags.
  for (const f of features.filter((x) => x.geometry_type === 'point')) {
    const [[px, py]] = f.geometry;
    const x = view.X(px), y = view.Y(py), w = view.w * 0.034, hh = view.w * 0.016;
    if (f.kind === 'exit' && /EXIT label/i.test(f.name)) {
      frag.append(s('rect', { x: x - w / 2, y: y - hh / 2, width: w, height: hh, rx: hh * 0.2, class: ['d-exit', !f.verified && 'd-draft'].filter(Boolean).join(' ') }),
        s('text', { x, y, 'text-anchor': 'middle', 'dominant-baseline': 'central', class: 'd-exit-t', 'font-size': hh * 0.62 }, 'EXIT'));
    }
  }
  g.append(frag);
}

export function polygonPoints(view, geom) { return geom.map(([x, y]) => `${view.X(x)},${view.Y(y)}`).join(' '); }

export function centroid(geom) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < geom.length; i++) {
    const [x0, y0] = geom[i], [x1, y1] = geom[(i + 1) % geom.length];
    const c = x0 * y1 - x1 * y0;
    a += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c;
  }
  if (Math.abs(a) < 1e-12) return [geom.reduce((t, p) => t + p[0], 0) / geom.length, geom.reduce((t, p) => t + p[1], 0) / geom.length];
  a *= 0.5;
  return [cx / (6 * a), cy / (6 * a)];
}
