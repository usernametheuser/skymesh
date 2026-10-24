import { api } from '../api.js';
import { h, mount, badge, table, formDialog, toast, ago } from '../ui.js';
import { state } from '../state.js';

// Plain-language labels for Administration → Settings (unknown keys still show, under "Other").
const SETTING_GROUPS = { 'SOS alerts': ['sos.', 'badge.sos_'], 'Badges and photos': ['badge.'], 'Cameras': ['camera.'],
  'Messages': ['chat.'], 'How long data is kept (days)': ['retention.'] };
const SETTING_INFO = {
  'sos.alert_display_s': { label: 'Alarm on screen (seconds)', help: 'How long a new SOS stays on every desktop before it fades. It stays in Emergency until handled.' },
  'sos.repeat_mode': { label: 'Pressing SOS again', choices: [['same_incident', 'Re-alert the open SOS (one incident, press count goes up)'], ['new_incident', 'Open a new SOS every time']] },
  'badge.sos_hold_ms': { label: 'Badge button hold time (ms)', help: '1500 = hold for 1.5 s. Shorter is faster but easier to press by accident.', step: 100 },
  'badge.sos_lockout_s': { label: 'Gap between badge alerts (s)', help: 'The badge ignores a new press for this long after an alert. 0 = no gap.', step: 0.5 },
  'badge.photo_on_sos': { label: 'Photo with every badge SOS' },
  'badge.remote_photo_enabled': { label: 'Allow “Request photo”', help: 'Responders: any badge. Others: their own badge. All logged.' },
  'camera.analysis_enabled': { label: 'Person detection on cameras' },
  'camera.analysis_interval_s': { label: 'Detection interval (s)' },
  'camera.fall_detection_enabled': { label: 'Automatic SOS on a fall' },
  'camera.fall_still_seconds': { label: 'Still after a fall before alerting (s)' },
  'camera.no_movement_seconds': { label: 'Alert when a person does not move for (s)', help: '0 turns this alert off.' },
  'chat.max_message_chars': { label: 'Longest message (characters)' },
};

export async function render(page) {
  const tabsEl = h('div', { class: 'tabs', role: 'tablist' });
  const body = h('div', {});
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'root@skymesh'), ':~# admin'), h('h1', {}, 'Administration'))), tabsEl, body);
  const tabs = { users: usersTab, settings: settingsTab, audit: auditTab, moderation: moderationTab };
  let cur = 'users';
  function renderTabs() {
    mount(tabsEl, Object.keys(tabs).map((k) => h('button', { class: cur === k ? 'active' : '', role: 'tab', 'aria-selected': String(cur === k), onclick: () => { cur = k; renderTabs(); tabs[k](); } }, k[0].toUpperCase() + k.slice(1))));
  }

  async function usersTab() {
    const users = await api.get('users');
    mount(body, h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: createUser }, 'Create user')), h('br'),
      table([{ label: 'Name', key: 'display_name' }, { label: 'Username', render: (u) => h('span', { class: 'mono' }, u.username) },
        { label: 'Role', render: (u) => badge(u.role.toUpperCase(), u.role === 'admin' ? 'b-violet' : u.role === 'responder' ? 'b-info' : 'b-muted') },
        { label: 'Active', render: (u) => badge(u.active ? 'ACTIVE' : 'DISABLED', u.active ? 'b-ok' : 'b-bad') },
        { label: 'Created', render: (u) => ago(u.created_at) }], users, editUser));
  }
  async function createUser() {
    const v = await formDialog('Create user', [{ name: 'username', label: 'Username', required: true }, { name: 'display_name', label: 'Display name', required: true },
      { name: 'password', label: 'Initial password (min 10)', type: 'password', required: true },
      { name: 'role', label: 'Role', type: 'select', value: 'user', options: ['user', 'responder', 'admin'].map((r) => ({ value: r, label: r })) }], 'Create');
    if (v) api.post('users', v).then(() => { toast('User created', 'ok'); usersTab(); }).catch((e) => toast(e.message, 'bad'));
  }
  async function editUser(u) {
    const v = await formDialog(`Edit ${u.display_name}`, [
      { name: 'role', label: 'Role', type: 'select', value: u.role, options: ['user', 'responder', 'admin'].map((r) => ({ value: r, label: r })) },
      { name: 'active', label: 'Active', type: 'checkbox', value: u.active },
      { name: 'new_password', label: 'Reset password (leave empty to keep)', type: 'password' }], 'Save',
    u.id === state.me.id ? 'You cannot demote or disable your own account.' : 'Disabling or resetting a password signs the user out everywhere.');
    if (!v) return;
    const body = { role: v.role, active: v.active };
    if (v.new_password) body.new_password = v.new_password;
    api.patch(`users/${u.id}`, body).then(() => { toast('Saved', 'ok'); usersTab(); }).catch((e) => toast(e.message, 'bad'));
  }

  async function settingsTab() {
    const st = await api.get('settings');
    const inputs = {};
    const field = (k) => {
      const v = st[k], info = SETTING_INFO[k] || {};
      if (info.choices) inputs[k] = h('select', {}, info.choices.map(([val, label]) => h('option', { value: val, selected: val === v }, label)));
      else if (typeof v === 'boolean') inputs[k] = h('input', { type: 'checkbox', checked: v });
      else inputs[k] = h('input', { type: 'number', value: v, min: 0, step: info.step || 1 });
      return h('label', { class: 'setting' }, h('span', {}, h('b', {}, info.label || k), info.help ? h('small', { class: 'dim' }, info.help) : null,
        h('code', { class: 'dim' }, k)), inputs[k]);
    };
    const used = new Set();
    const groups = Object.entries(SETTING_GROUPS).map(([title, prefix]) => {
      const keys = Object.keys(st).filter((k) => !used.has(k) && prefix.some((p) => k.startsWith(p)));
      keys.forEach((k) => used.add(k));
      return keys.length ? h('section', { class: 'card stack' }, h('h2', {}, title), keys.map(field)) : null;
    });
    const rest = Object.keys(st).filter((k) => !used.has(k));
    mount(body, h('div', { class: 'stack' }, groups, rest.length ? h('section', { class: 'card stack' }, h('h2', {}, 'Other'), rest.map(field)) : null,
      h('p', { class: 'dim' }, 'Camera person detection runs only when enabled. Use it only where monitoring notices are posted and private areas are excluded.'),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: async () => {
        const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k,
          el.tagName === 'SELECT' ? el.value : el.type === 'checkbox' ? el.checked : Number(el.value)]));
        api.patch('settings', { values }).then(() => toast('Settings saved — badges pick up button changes at their next check-in (≤30 s)', 'ok'))
          .catch((e) => toast(e.message, 'bad'));
      } }, 'Save'), h('button', { class: 'btn', onclick: () => api.post('retention/run').then((r) => toast(`Purged: ${JSON.stringify(r)}`, 'ok')) }, 'Run retention now'))));
  }

  async function auditTab() {
    const rows = await api.get('audit?limit=300');
    const term = h('div', { class: 'term' });
    term.style.maxHeight = '70vh';
    mount(body, term);
    mount(term, rows.map((a) => h('div', {}, h('span', { class: 'ts' }, new Date(a.at).toLocaleString()), '  ', h('span', { class: 'act' }, a.action), ' ',
      a.actor_name || a.actor_device_id || 'anonymous', a.target ? ` → ${a.target}` : '', a.ip ? `  [${a.ip}]` : '', Object.keys(a.detail).length ? `  ${JSON.stringify(a.detail)}` : '')));
  }

  async function moderationTab() {
    const reports = await api.get('chat/reports');
    mount(body, reports.length ? table([{ label: 'Reported', render: (r) => ago(r.created_at) }, { label: 'By', key: 'reporter_name' }, { label: 'Reason', key: 'reason' },
      { label: 'Message', render: (r) => r.deleted_at ? h('span', { class: 'dim' }, '[removed]') : r.body },
      { label: '', render: (r) => r.deleted_at ? badge('RESOLVED', 'b-ok') : h('button', { class: 'btn small danger', onclick: () => api.post(`chat/messages/${r.message_id}/remove`).then(moderationTab) }, 'Remove message') }],
    reports) : h('div', { class: 'empty' }, 'No reported messages.'));
  }

  renderTabs();
  await usersTab();
}
