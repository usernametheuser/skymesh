import { api } from '../api.js';
import { h, mount, clock, ago, toast, formDialog, openDialog, emptyState } from '../ui.js';
import { state, on, queueChat, chatOutbox, flushChatOutbox, dropOutbox } from '../state.js';

export async function render(page, ctx = {}) {
  const st = { convs: [], active: null, msgs: [], users: [] };
  const list = h('div', { class: 'chat-list' });
  const head = h('div', { class: 'chat-head' });
  const msgsEl = h('div', { class: 'chat-msgs', 'aria-live': 'polite' });
  const input = h('textarea', { rows: 1, placeholder: 'Message… (Enter to send, Shift+Enter for a new line)', maxlength: 4000, 'aria-label': 'Message' });
  const sendBtn = h('button', { class: 'btn primary', onclick: send }, 'Send');
  const compose = h('div', { class: 'chat-compose' }, input, sendBtn);
  const main = h('div', { class: 'chat-main' }, head, msgsEl, compose);
  const layout = h('div', { class: 'chat-layout' }, list, main);
  mount(page, layout);

  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

  async function loadConvs() {
    try { st.convs = await api.get('chat/conversations'); } catch (e) { toast(e.message, 'bad'); }
    renderList();
  }

  function renderList() {
    const online = new Set(st.users.filter((u) => u.online).map((u) => u.id));
    mount(list, h('div', { class: 'row', style: null }, h('div', { class: 'stack' },
      h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: newDirect }, '+ Direct'), h('button', { class: 'btn small', onclick: newGroup }, '+ Group'),
        h('button', { class: 'btn small', onclick: doSearch }, 'Search')))),
    st.convs.length ? st.convs.map((c) => {
      const other = c.kind === 'direct' ? c.members.find((m) => m.id !== state.me.id) : null;
      return h('div', { class: ['conv', st.active?.id === c.id && 'active'], onclick: () => open(c.id), tabindex: 0,
        onkeydown: (e) => e.key === 'Enter' && open(c.id) },
      h('div', { class: 'row' }, h('b', {}, c.name || '(unnamed)'),
        other ? h('span', { class: 'badge ' + (online.has(other.id) ? 'b-ok' : 'b-muted') }, online.has(other.id) ? 'online' : 'offline') : h('span', { class: 'dim mono' }, c.kind),
        c.unread ? h('span', { class: 'badge b-accent' }, String(c.unread)) : null),
      h('div', { class: 'dim' }, c.last_message ? `${c.last_message.sender_name}: ${c.last_message.body.slice(0, 60)}` : 'No messages yet'));
    }) : h('div', { class: 'empty' }, 'No conversations yet.'));
    layout.classList.toggle('has-active', !!st.active);
  }

  async function open(id) {
    st.active = st.convs.find((c) => c.id === id) || null;
    renderList();
    if (!st.active) return;
    const announce = st.active.kind === 'announcement';
    compose.classList.toggle('hidden', announce && !state.perms.has('chat.announce'));
    mount(head, h('button', { class: 'btn small menu-only', onclick: () => { st.active = null; renderList(); } }, '←'),
      h('b', {}, st.active.name), h('span', { class: 'dim' }, `${st.active.members.length} member(s)`));
    try { st.msgs = await api.get(`chat/conversations/${id}/messages`); } catch (e) { toast(e.message, 'bad'); st.msgs = []; }
    renderMsgs(true);
    markRead();
  }

  function renderMsgs(scroll) {
    const atBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 60;
    const pending = chatOutbox().filter((o) => o.conv === st.active?.id);
    const sentIds = new Set(st.msgs.map((m) => m.client_msg_id));
    mount(msgsEl, st.msgs.length || pending.length ? [
      ...st.msgs.map((m) => {
        const mine = m.sender_id === state.me.id;
        let status = '';
        if (mine) status = m.read && m.read === m.recipients ? 'read' : m.delivered && m.delivered === m.recipients ? 'delivered' : 'sent';
        return h('div', { class: ['msg', mine && 'mine'] }, mine ? null : h('div', { class: 'dim' }, m.sender_name),
          h('div', { class: 'body' }, m.body),
          h('div', { class: 'meta' }, h('span', {}, clock(m.created_at)), mine ? h('span', {}, status) : h('button', {
            class: 'btn small', title: 'Report this message', onclick: () => report(m) }, '⚑')));
      }),
      ...pending.filter((o) => !sentIds.has(o.client_msg_id)).map((o) => h('div', { class: ['msg', 'mine', o.state === 'failed' && 'failed'] },
        h('div', { class: 'body' }, o.body),
        h('div', { class: 'meta' }, h('span', {}, o.state === 'failed' ? `failed: ${o.error}` : 'queued — waiting for the backend'),
          o.state === 'failed' ? h('button', { class: 'btn small', onclick: () => dropOutbox(o.client_msg_id) }, 'Discard') : null))),
    ] : emptyState('No messages yet. Say hello.'));
    if (scroll || atBottom) msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  function send() {
    const body = input.value.trim();
    if (!body || !st.active) return;
    input.value = '';
    queueChat(st.active.id, body);
    renderMsgs(true);
  }

  async function markRead() {
    const ids = st.msgs.filter((m) => m.sender_id !== state.me.id).map((m) => m.id).slice(-200);
    if (ids.length && document.visibilityState === 'visible') {
      await api.post('chat/receipts', { message_ids: ids, status: 'read' }).catch(() => {});
      const c = st.convs.find((x) => x.id === st.active?.id);
      if (c) { c.unread = 0; renderList(); }
    }
  }

  async function ensureUsers() { st.users = await api.get('users').catch(() => []); }

  async function newDirect() {
    await ensureUsers();
    const others = st.users.filter((u) => u.id !== state.me.id);
    if (!others.length) return toast('No other users yet');
    const v = await formDialog('New direct message', [{ name: 'user', label: 'Person', type: 'select', value: others[0].id,
      options: others.map((u) => ({ value: u.id, label: `${u.display_name} (@${u.username})${u.online ? ' · online' : ''}` })) }], 'Open');
    if (!v) return;
    const c = await api.post('chat/direct', { user_id: v.user });
    await loadConvs(); open(c.id);
  }

  async function newGroup() {
    await ensureUsers();
    const boxes = st.users.filter((u) => u.id !== state.me.id).map((u) => ({ u, el: h('input', { type: 'checkbox' }) }));
    const name = h('input', { type: 'text', placeholder: 'Group name', maxlength: 60 });
    const ok = await openDialog('New group', h('div', { class: 'stack' }, name,
      boxes.map((b) => h('label', { class: 'row' }, b.el, `${b.u.display_name} (@${b.u.username})`))),
    [{ label: 'Cancel', value: false }, { label: 'Create', value: true, kind: 'primary' }]);
    if (!ok || !name.value.trim()) return;
    try {
      const c = await api.post('chat/groups', { name: name.value.trim(), member_ids: boxes.filter((b) => b.el.checked).map((b) => b.u.id) });
      await loadConvs(); open(c.id);
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function doSearch() {
    const v = await formDialog('Search messages', [{ name: 'q', label: 'Text', required: true }], 'Search');
    if (!v) return;
    try {
      const res = await api.get(`chat/search?q=${encodeURIComponent(v.q)}`);
      await openDialog(`Results for “${v.q}”`, res.length ? h('div', { class: 'stack' }, res.map((m) => h('div', { class: 'list-item', onclick: () => open(m.conversation_id) },
        h('b', {}, m.sender_name), ' ', h('span', { class: 'dim' }, ago(m.created_at)), h('div', {}, m.body)))) : emptyState('No matches in your conversations.'),
      [{ label: 'Close', value: true }]);
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function report(m) {
    const v = await formDialog('Report message', [{ name: 'reason', label: 'Reason', required: true }], 'Report', 'Administrators will review this message.');
    if (v) api.post(`chat/messages/${m.id}/report`, v).then(() => toast('Reported', 'ok')).catch((e) => toast(e.message, 'bad'));
  }

  await Promise.all([loadConvs(), ensureUsers()]);
  renderList();
  if (ctx.params?.c) open(ctx.params.c);

  const offs = [
    on('ws:message', (e) => {
      const m = e.detail;
      if (st.active?.id === m.conversation_id) {
        if (!st.msgs.some((x) => x.id === m.id)) st.msgs.push(m);
        renderMsgs(); if (m.sender_id !== state.me.id) markRead();
      } else if (m.sender_id !== state.me.id) {
        api.post('chat/receipts', { message_ids: [m.id], status: 'delivered' }).catch(() => {});
      }
      loadConvs();
    }),
    on('ws:message_status', (e) => {
      const i = st.msgs.findIndex((x) => x.id === e.detail.id);
      if (i >= 0) { st.msgs[i] = e.detail; renderMsgs(); }
    }),
    on('chat:sent', (e) => { if (st.active?.id === e.detail.conversation_id && !st.msgs.some((x) => x.id === e.detail.id)) st.msgs.push(e.detail); renderMsgs(); }),
    on('outbox', () => renderMsgs()),
    on('ws:conversation', loadConvs),
    on('ws:presence', async () => { await ensureUsers(); renderList(); }),
    on('reconnected', async () => { flushChatOutbox(); await loadConvs(); if (st.active) open(st.active.id); }),
  ];
  return () => offs.forEach((f) => f());
}
