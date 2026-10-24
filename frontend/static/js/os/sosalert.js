// Full-width SOS alarm on the desktop: appears for every new SOS (and every repeat press), stays for
// `sos.alert_display_s` seconds (Administration → Settings), then fades. The incident itself stays open in
// Emergency until a responder handles it; this is only the attention-getter.
import { api } from '../api.js';
import { h, toast, ago } from '../ui.js';
import { can } from '../state.js';
import { icon } from './icons.js';

let stack;
const shown = new Map();  // incident id -> element (a repeat press refreshes the same alarm)

function host() {
  if (!stack) {
    stack = h('div', { class: 'sos-alerts', role: 'alert', 'aria-live': 'assertive', popover: 'showPopover' in HTMLElement.prototype ? 'manual' : null });
    document.body.append(stack);
  }
  // Re-open as the newest top-layer element so it sits above open dialogs too.
  if (stack.popover) { try { if (stack.matches(':popover-open')) stack.hidePopover(); stack.showPopover(); } catch { /* ignore */ } }
  return stack;
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < 3; i++) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'square'; o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + i * 0.35);
      g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + i * 0.35 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i * 0.35 + 0.25);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.35); o.stop(ctx.currentTime + i * 0.35 + 0.27);
    }
    setTimeout(() => ctx.close(), 1500);
  } catch { /* audio blocked until the page was clicked once */ }
}

export function showSosAlert({ incident: inc, repeat = 0, display_s: secs = 10 }, openApp) {
  const ms = Math.max(2, Number(secs) || 10) * 1000;
  const who = inc.reporter_name || inc.device_name || 'unknown';
  const auto = inc.source === 'sensor';
  shown.get(inc.id)?._close(true);
  const bar = h('i', { class: 'sos-alert-bar' });
  bar.style.animationDuration = `${ms}ms`;
  const ackBtn = can('incident.manage') && inc.state === 'NEW' ? h('button', { class: 'btn small', onclick: async (e) => {
    e.stopPropagation();
    try { await api.post(`incidents/${inc.id}/transition`, { to_state: 'ACKNOWLEDGED' }); toast('Acknowledged — the badge is told help is coming', 'ok'); close(); }
    catch (err) { toast(err.message, 'bad'); }
  } }, 'Acknowledge') : null;
  const el = h('div', { class: ['sos-alert', auto && 'auto'], onclick: () => { openApp?.('emergency'); close(); } },
    h('div', { class: 'sos-alert-ico' }, icon('emergency')),
    h('div', { class: 'sos-alert-body' },
      h('b', {}, auto ? `Possible fall — ${inc.device_name || 'camera'}` : `SOS — ${who}`,
        repeat ? h('span', { class: 'sos-alert-rep' }, `pressed ${repeat + 1}×`) : null),
      h('span', {}, inc.room_name ? `${inc.room_name} · ` : '', inc.location_note || ''),
      h('small', {}, `${inc.source} · ${ago(inc.last_alert_at || inc.created_at)}${auto ? ' · check the snapshot' : ''}`)),
    h('div', { class: 'sos-alert-act' }, ackBtn,
      h('button', { class: 'btn small', onclick: (e) => { e.stopPropagation(); openApp?.('emergency'); close(); } }, 'Open'),
      h('button', { class: 'win-btn', 'aria-label': 'Dismiss', onclick: (e) => { e.stopPropagation(); close(); } }, icon('close'))),
    bar);
  let timer;
  function close(instant = false) {
    clearTimeout(timer);
    shown.delete(inc.id);
    if (instant) { el.remove(); return done(); }
    el.classList.add('out');
    setTimeout(() => { el.remove(); done(); }, 600);
  }
  function done() { if (stack && !stack.children.length && stack.popover) { try { stack.hidePopover(); } catch { /* ignore */ } } }
  el._close = close;
  // Hovering keeps it up (someone is reading it); leaving restarts a short countdown.
  el.addEventListener('mouseenter', () => { clearTimeout(timer); el.classList.add('hold'); });
  el.addEventListener('mouseleave', () => { el.classList.remove('hold'); timer = setTimeout(close, 3000); });
  const box = host();
  box.prepend(el);
  while (box.children.length > 3) {
    const last = box.lastElementChild;
    if (last._close) last._close(true); else last.remove();
  }
  shown.set(inc.id, el);
  timer = setTimeout(close, ms);
  beep();
  // Also kept in the notification centre.
  window.dispatchEvent(new CustomEvent('skymesh:notify', { detail: { text: auto ? `Possible fall seen by ${inc.device_name || 'a camera'}` : `SOS from ${who}${repeat ? ` (pressed ${repeat + 1}×)` : ''} — ${inc.location_note || ''}`, kind: 'sos' } }));
}
