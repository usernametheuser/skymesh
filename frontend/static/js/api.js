// Thin fetch wrapper. Session auth uses the HttpOnly cookie; nothing secret is stored in JS.

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status; this.code = code; this.details = details;
  }
  get offline() { return this.status === 0; }
}

async function request(method, path, body, opts = {}) {
  const init = { method, credentials: 'same-origin', headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
  if (opts.signal) init.signal = opts.signal;
  let res;
  try {
    res = await fetch(path.startsWith('/') ? path : `/api/v1/${path}`, init);
  } catch (e) {
    throw new ApiError(0, 'network', 'Backend unreachable — check the local network connection');
  }
  if (res.status === 401 && !opts.allow401) window.dispatchEvent(new CustomEvent('skymesh:unauthenticated'));
  const type = res.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const err = data?.error || {};
    const detail = err.details?.map((d) => `${d.loc.slice(1).join('.')}: ${d.msg}`).join('; ');
    throw new ApiError(res.status, err.code || 'error', detail ? `${err.message}: ${detail}` : (err.message || res.statusText), err.details);
  }
  return opts.raw ? res : data;
}

export const api = {
  get: (p, o) => request('GET', p, undefined, o),
  post: (p, b, o) => request('POST', p, b ?? {}, o),
  put: (p, b, o) => request('PUT', p, b, o),
  patch: (p, b, o) => request('PATCH', p, b, o),
  del: (p, o) => request('DELETE', p, undefined, o),
  raw: (p) => request('GET', p, undefined, { raw: true }),
};
