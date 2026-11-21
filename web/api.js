// The one door to the car service. Paths are relative so the app works under any base path.

import { note } from './diag.js';

export class ApiError extends Error {
  constructor(status, message, needs = null) {
    super(message || `HTTP ${status}`);
    this.status = status;
    this.plain = message || null; // the service's plain sentence, when it sent one
    this.needs = needs;           // 'verification' when unlock wants a fingerprint or the PIN
  }
}

// A phone back from the background can hold a dead connection that never answers; give up and
// retry instead of waiting forever. Reads are quick; logins and forced refreshes take Kia ~30 s.
const READ_TIMEOUT_MS = 10000;
const WRITE_TIMEOUT_MS = 75000;

export async function api(path, { method = 'GET', body, timeout } = {}) {
  const ctrl = new AbortController();
  const started = Date.now();
  const timer = setTimeout(() => ctrl.abort(), timeout || (method === 'GET' ? READ_TIMEOUT_MS : WRITE_TIMEOUT_MS));
  // redirect: 'manual': when the Cloudflare Access sign-in has expired, Cloudflare answers with a
  // redirect to its login page; we want to see that, not a CORS failure.
  const init = { method, cache: 'no-store', headers: {}, redirect: 'manual', signal: ctrl.signal };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  let data = null;
  try {
    try {
      res = await fetch(`api/${path}`, init);
    } catch (err) {
      note('api-fail', `${path} ${ctrl.signal.aborted ? 'timeout' : (err && err.name) || 'error'} ${Date.now() - started}ms online=${navigator.onLine}`);
      throw new ApiError(0, null);
    }
    if (res.type === 'opaqueredirect' || (res.status >= 300 && res.status < 400)) {
      note('api-redirect', `${path} ${Date.now() - started}ms`);
      throw new ApiError(401, null, 'access');
    }
    const took = Date.now() - started;
    // log failures, and successes right after a wake (timeout set) or slow ones; not every poll
    if (!res.ok || timeout || took > 1500) note(res.ok ? 'api-ok' : 'api-status', `${path} ${res.status} ${took}ms`);
    try { data = await res.json(); } catch { /* empty or not JSON */ }
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    // Cloudflare let this browser in, but the car service hasn't approved it (or it was removed).
    if (data && data.needs === 'device' && path !== 'device/approve') window.dispatchEvent(new Event('needs-device'));
    throw new ApiError(res.status, data && typeof data.error === 'string' ? data.error : null,
      data && typeof data.needs === 'string' ? data.needs : null);
  }
  return data;
}

export const getState = (opts) => api('state', opts);
export const refresh = (force) => api('refresh', { method: 'POST', body: { force: !!force } });
/** extra: {pin} or {assertion} for unlock */
export const sendCommand = (action, options, extra) =>
  api('command', { method: 'POST', body: { action, ...(options ? { options } : {}), ...(extra || {}) } });
export const getCommand = (id) => api(`command/${encodeURIComponent(id)}`);
export const getLog = () => api('log');
export const getPresets = () => api('presets');
export const putPresets = (presets) => api('presets', { method: 'PUT', body: { presets } });
export const getSchedules = () => api('schedules');
export const putSchedules = (schedules) => api('schedules', { method: 'PUT', body: { schedules } });
export const login = (email, password, pin, label) => api('login', { method: 'POST', body: { email, password, pin, label } });
export const approveDevice = (email, password, pin, label) =>
  api('device/approve', { method: 'POST', body: { email, password, pin, label } });
export const listDevices = () => api('devices');
export const health = () => api('health');
export const kiaCalls = () => api('kia-calls');
export const deleteDevice = (id) => api(`devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const otpSend = (method) => api('otp/send', { method: 'POST', body: { method } });
export const otpVerify = (code) => api('otp/verify', { method: 'POST', body: { code } });
export const logout = () => api('logout', { method: 'POST' });
export const unlockChallenge = () => api('unlock/challenge', { method: 'POST' });
export const passkeyOptions = (label) => api('passkeys/register/options', { method: 'POST', body: { label } });
export const registerPasskey = (credential, pin) => api('passkeys/register', { method: 'POST', body: { credential, pin } });
export const listPasskeys = () => api('passkeys');
export const deletePasskey = (id) => api(`passkeys/${encodeURIComponent(id)}`, { method: 'DELETE' });
