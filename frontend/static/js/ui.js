// DOM helpers. All text goes through textContent / attributes — never innerHTML with data.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

const SVGNS = 'http://www.w3.org/2000/svg';
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

function applyAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'class') el.setAttribute('class', Array.isArray(v) ? v.filter(Boolean).join(' ') : v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'value' && 'value' in el) el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...children) { clear(el); append(el, children); return el; }

// ---------------------------------------------------------------- formatting

export function ago(iso) {
  if (!iso) return 'never';
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return `${Math.floor(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
export function ageSec(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}
export function time(iso) { return iso ? new Date(iso).toLocaleString() : '—'; }
export function clock(iso) { return iso ? new Date(iso).toLocaleTimeString() : '—'; }

const STATE_CLASS = {
  LIVE: 'b-ok', RECENT: 'b-info', STALE: 'b-warn', LAST_KNOWN: 'b-muted', OFFLINE: 'b-muted', PERMISSION_DENIED: 'b-bad',
  SHARING_DISABLED: 'b-muted', POSITION_UNAVAILABLE: 'b-muted', LOW_CONFIDENCE: 'b-warn',
  NEW: 'b-sos', ACKNOWLEDGED: 'b-warn', RESPONDING: 'b-info', RESOLVED: 'b-ok', FALSE_ALARM: 'b-muted',
};
export function badge(text, cls) { return h('span', { class: ['badge', cls || STATE_CLASS[text] || 'b-muted'] }, String(text).replace(/_/g, ' ')); }
export function onlineBadge(online) {
  if (online === true) return badge('ONLINE', 'b-ok');
  if (online === false) return badge('OFFLINE', 'b-bad');
  return badge('NO HEARTBEAT', 'b-muted');
}

export function uid() {
  const a = new Uint8Array(12);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

// ---------------------------------------------------------------- toasts & dialogs

let toastBox;
export function toast(msg, kind = '', ms = 5000) {
  const pop = 'showPopover' in HTMLElement.prototype;
  if (!toastBox) {
    toastBox = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite', popover: pop ? 'manual' : null });
    document.body.append(toastBox);
  }
  const t = h('div', { class: ['toast', kind] }, h('div', {}, msg));
  toastBox.append(t);
  // Re-open the popover so it is the newest top-layer element, above any dialog opened since.
  if (pop) { try { if (toastBox.matches(':popover-open')) toastBox.hidePopover(); toastBox.showPopover(); } catch { /* not connected */ } }
  setTimeout(() => { t.remove(); if (pop && !toastBox.children.length) { try { toastBox.hidePopover(); } catch { /* ignore */ } } }, ms);
  // Every toast is also kept in the notification centre.
  window.dispatchEvent(new CustomEvent('skymesh:notify', { detail: { text: String(msg), kind } }));
}

export function openDialog(title, body, actions) {
  return new Promise((resolve) => {
    const dlg = h('dialog', {});
    const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const btns = actions.map((a) => h('button', { class: ['btn', a.kind], type: 'button', onclick: async () => {
      if (a.validate && !(await a.validate())) return;
      close(a.value);
    } }, a.label));
    dlg.append(h('h3', {}, title), body, h('div', { class: 'dialog-actions' }, btns));
    dlg.addEventListener('cancel', () => resolve(undefined));
    document.body.append(dlg);
    dlg.showModal();
  });
}

export async function confirmDialog(title, text, okLabel = 'Confirm', kind = 'primary') {
  return (await openDialog(title, h('p', {}, text), [{ label: 'Cancel', value: false }, { label: okLabel, value: true, kind }])) === true;
}

// fields: [{name, label, type, value, options:[{value,label}], required, placeholder}]
export async function formDialog(title, fields, okLabel = 'Save', note) {
  const inputs = {};
  const body = h('div', { class: 'stack' }, note ? h('p', { class: 'muted' }, note) : null, fields.map((f) => {
    let input;
    if (f.type === 'select') {
      input = h('select', { name: f.name }, f.options.map((o) => h('option', { value: o.value, selected: o.value === f.value }, o.label)));
    } else if (f.type === 'textarea') {
      input = h('textarea', { name: f.name, rows: 3, placeholder: f.placeholder || '' });
      input.value = f.value ?? '';
    } else if (f.type === 'checkbox') {
      input = h('input', { type: 'checkbox', name: f.name, checked: !!f.value });
    } else if (f.type === 'file') {
      input = h('input', { type: 'file', name: f.name, accept: f.accept || '' });
    } else {
      input = h('input', { type: f.type || 'text', name: f.name, value: f.value ?? '', placeholder: f.placeholder || '',
        step: f.step || null, autocomplete: 'off' });
    }
    inputs[f.name] = input;
    return h('label', { class: 'field' }, f.label, input, f.help ? h('span', { class: 'dim' }, f.help) : null);
  }));
  const ok = await openDialog(title, body, [{ label: 'Cancel', value: false }, { label: okLabel, value: true, kind: 'primary',
    validate: () => fields.every((f) => {
      if (!f.required) return true;
      const v = f.type === 'file' ? inputs[f.name].files.length : inputs[f.name].value.trim();
      if (!v) { inputs[f.name].focus(); toast(`${f.label} is required`, 'bad'); }
      return !!v;
    }) }]);
  if (!ok) return null;
  const out = {};
  for (const f of fields) {
    const el = inputs[f.name];
    if (f.type === 'checkbox') out[f.name] = el.checked;
    else if (f.type === 'file') out[f.name] = el.files[0] || null;
    else if (f.type === 'number') out[f.name] = el.value === '' ? null : Number(el.value);
    else out[f.name] = el.value.trim();
  }
  return out;
}

export function emptyState(text) { return h('div', { class: 'empty' }, text); }

export function kv(pairs) {
  return h('dl', { class: 'kv' }, pairs.filter(Boolean).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v ?? '—')]));
}

export function table(cols, rows, onRow) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 't' },
    h('thead', {}, h('tr', {}, cols.map((c) => h('th', {}, c.label)))),
    h('tbody', {}, rows.map((r) => h('tr', { onclick: onRow ? () => onRow(r) : null, style: null },
      cols.map((c) => h('td', {}, c.render ? c.render(r) : (r[c.key] ?? '—'))))))));
}
