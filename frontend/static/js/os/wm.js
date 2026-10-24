// Window manager: open / focus / move / resize / minimise / maximise / close app windows.
import { h, mount } from '../ui.js';
import { icon } from './icons.js';

const DEFAULT_SIZE = [780, 540];

export class WindowManager {
  constructor(desktop, apps, onChange) {
    this.desktop = desktop;
    this.apps = apps;
    this.onChange = onChange;
    this.wins = new Map();
    this.z = 10;
    this.focused = null;
    this.mobile = false;
  }

  app(id) { return this.apps.find((a) => a.id === id); }
  isOpen(id) { return this.wins.has(id); }

  async open(id, params = {}) {
    const app = this.app(id);
    if (!app) return;
    const existing = this.wins.get(id);
    if (existing && !params.reload) {
      existing.el.classList.remove('min');
      this.focus(id);
      if (Object.keys(params).length) await this._render(existing, params);
      return;
    }
    if (existing) this.close(id);

    const body = h('div', { class: 'win-body' });
    const title = h('span', { class: 'win-title' }, app.label);
    const bar = h('div', { class: 'win-bar' },
      h('button', { class: 'win-btn win-back', 'aria-label': 'Back to home', onclick: () => this.close(id) }, icon('back')),
      icon(app.icon), title,
      h('button', { class: 'win-btn min-b', 'aria-label': 'Minimise', onclick: () => this.minimize(id) }, icon('min')),
      h('button', { class: 'win-btn max-b', 'aria-label': 'Maximise', onclick: () => this.toggleMax(id) }, icon('max')),
      h('button', { class: 'win-btn close', 'aria-label': 'Close', onclick: () => this.close(id) }, icon('close')));
    const grip = h('div', { class: 'win-resize', 'aria-hidden': 'true' });
    const el = h('section', { class: 'win', role: 'dialog', 'aria-label': app.label }, bar, body, grip);
    const w = { id, app, el, body, cleanup: null };
    this.wins.set(id, w);
    this._place(w);
    this.desktop.append(el);
    el.addEventListener('pointerdown', () => this.focus(id), true);
    bar.addEventListener('dblclick', (e) => { if (!e.target.closest('button')) this.toggleMax(id); });
    this._drag(w, bar);
    this._resize(w, grip);
    this.focus(id);
    await this._render(w, params);
  }

  async _render(w, params) {
    if (w.cleanup) { try { w.cleanup(); } catch { /* ignore */ } w.cleanup = null; }
    const page = h('div', { class: ['page', w.app.full && 'full'] });
    mount(w.body, page);
    try {
      const mod = await w.app.load();
      w.cleanup = (await mod.render(page, { params, wm: this })) || null;
    } catch (e) {
      mount(page, h('div', { class: 'notice bad' }, `${w.app.label} could not start: ${e.message}`));
    }
  }

  focus(id) {
    const w = this.wins.get(id);
    if (!w) return;
    for (const o of this.wins.values()) o.el.classList.toggle('focused', o === w);
    w.el.style.zIndex = String(++this.z);
    this.focused = id;
    this.onChange?.();
  }

  close(id) {
    const w = this.wins.get(id);
    if (!w) return;
    if (w.cleanup) { try { w.cleanup(); } catch { /* ignore */ } }
    w.el.remove();
    this.wins.delete(id);
    if (this.focused === id) {
      const top = [...this.wins.values()].filter((x) => !x.el.classList.contains('min'))
        .sort((a, b) => Number(b.el.style.zIndex) - Number(a.el.style.zIndex))[0];
      this.focused = null;
      if (top) this.focus(top.id);
    }
    this.onChange?.();
  }

  closeAll() { for (const id of [...this.wins.keys()]) this.close(id); }

  minimize(id) {
    const w = this.wins.get(id);
    if (!w) return;
    w.el.classList.add('min');
    if (this.focused === id) this.focused = null;
    this.onChange?.();
  }

  toggle(id) {
    const w = this.wins.get(id);
    if (!w) return this.open(id);
    if (w.el.classList.contains('min') || this.focused !== id) { w.el.classList.remove('min'); this.focus(id); }
    else this.minimize(id);
  }

  toggleMax(id) {
    const w = this.wins.get(id);
    if (!w) return;
    w.el.classList.toggle('max');
    this._save(w);
  }

  // ---------------------------------------------------------------- geometry

  _bounds() {
    const r = this.desktop.getBoundingClientRect();
    return { w: r.width, h: Math.max(200, r.height - 70) }; // keep clear of the dock
  }

  _place(w) {
    let g = null;
    try { g = JSON.parse(localStorage.getItem(`skymesh.win.${w.id}`)); } catch { /* ignore */ }
    const b = this._bounds();
    const [dw, dh] = w.app.size || DEFAULT_SIZE;
    const n = this.wins.size - 1;
    g ||= { w: Math.min(dw, b.w - 40), h: Math.min(dh, b.h - 30), x: 0, y: 0, max: false, fresh: true };
    if (g.fresh) { g.x = Math.max(10, (b.w - g.w) / 2 + n * 26 - 60); g.y = Math.max(10, (b.h - g.h) / 2 + n * 22 - 30); }
    g.w = Math.min(Math.max(g.w, 320), b.w); g.h = Math.min(Math.max(g.h, 220), b.h);
    g.x = Math.min(Math.max(g.x, 0), Math.max(0, b.w - 120)); g.y = Math.min(Math.max(g.y, 0), Math.max(0, b.h - 40));
    Object.assign(w.el.style, { left: `${g.x}px`, top: `${g.y}px`, width: `${g.w}px`, height: `${g.h}px` });
    w.el.classList.toggle('max', !!g.max);
  }

  _save(w) {
    const s = w.el.style;
    try {
      localStorage.setItem(`skymesh.win.${w.id}`, JSON.stringify({ x: parseFloat(s.left), y: parseFloat(s.top),
        w: parseFloat(s.width), h: parseFloat(s.height), max: w.el.classList.contains('max') }));
    } catch { /* storage unavailable */ }
  }

  _drag(w, bar) {
    let start = null;
    bar.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || e.target.closest('button') || this.mobile || w.el.classList.contains('max')) return;
      start = { x: e.clientX, y: e.clientY, l: parseFloat(w.el.style.left), t: parseFloat(w.el.style.top) };
      bar.setPointerCapture(e.pointerId);
    });
    bar.addEventListener('pointermove', (e) => {
      if (!start) return;
      const b = this._bounds();
      const l = Math.min(Math.max(start.l + e.clientX - start.x, -w.el.offsetWidth + 120), b.w - 120);
      const t = Math.min(Math.max(start.t + e.clientY - start.y, 0), b.h + 30);
      w.el.style.left = `${l}px`; w.el.style.top = `${t}px`;
    });
    const end = () => { if (start) { start = null; this._save(w); } };
    bar.addEventListener('pointerup', end);
    bar.addEventListener('pointercancel', end);
  }

  _resize(w, grip) {
    let start = null;
    grip.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      start = { x: e.clientX, y: e.clientY, w: w.el.offsetWidth, h: w.el.offsetHeight };
      grip.setPointerCapture(e.pointerId);
    });
    grip.addEventListener('pointermove', (e) => {
      if (!start) return;
      w.el.style.width = `${Math.max(320, start.w + e.clientX - start.x)}px`;
      w.el.style.height = `${Math.max(220, start.h + e.clientY - start.y)}px`;
    });
    const end = () => { if (start) { start = null; this._save(w); } };
    grip.addEventListener('pointerup', end);
    grip.addEventListener('pointercancel', end);
  }
}
