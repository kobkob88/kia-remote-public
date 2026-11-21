// Car Remote dashboard: state polling, rendering, commands, auth screens.
import * as diag from './diag.js';
import { t, applyStrings } from './strings.js';
import { $, el, icon, vibrate, onPress } from './dom.js';
import * as api from './api.js';
import * as passkey from './passkey.js';
import { int, dec, temp, num, ago, minutesSince, coords, mapsUrl, DASH } from './format.js';
import { renderCar, climateMode } from './car.js';
import { renderPresets, openClimateSheet, lastOptions, describe, setDial } from './climate.js';
import * as schedule from './schedule.js';
import { openHealth } from './health.js';
import { openActivity } from './activity.js';

const POLL_MS = 30000;
const CMD_POLL_MS = 2000;
const REAUTH_KEY = 'sportage.reauth';
const RELOAD_KEY = 'sportage.autoreload';
const APP_VERSION = 'v25';   // matches sw.js VERSION; shown at the bottom of the menu
const CACHE_KEY = 'sportage.state';
const PRESET_KEY = 'sportage.presets';

let state = null;        // last /api/state
let presets = null;      // last preset list
let pollTimer = null;
let tickTimer = null;
let cmd = null;          // {id, action, startedAt, timer}
let pillHideTimer = null;

const app = $('app');

// ---------------------------------------------------------------- state

let loading = null;      // the /api/state request in flight (resume fires focus and visibility together)
let retries = 0;
let retryTimer = null;
const RETRY_MS = [2000, 5000, 10000, 20000];

// Just after opening or coming back from the background. A request that hangs then is usually a
// connection the phone dropped while asleep; reloading the page (what fixes it by hand) gets a new one.
let wokeAt = Date.now();
const WAKE_WINDOW_MS = 30000;
const WAKE_TIMEOUT_MS = 6000;

function reloadOnce(reason) {
  let last = 0;
  try { last = Number(sessionStorage.getItem(RELOAD_KEY)) || 0; } catch { /* private mode */ }
  if (Date.now() - last < 60000 || navigator.onLine === false) return false;
  try { sessionStorage.setItem(RELOAD_KEY, String(Date.now())); } catch { /* private mode */ }
  diag.note('auto-reload', reason);
  window.location.reload();
  return true;
}

async function load({ quiet = true } = {}) {
  if (loading && quiet) return loading;
  const justWoke = Date.now() - wokeAt < WAKE_WINDOW_MS;
  const run = (async () => {
    try {
      const s = await api.getState(justWoke ? { timeout: WAKE_TIMEOUT_MS } : undefined);
      retries = 0;
      diag.flush();
      setState(s);
      banner(null);
      return s;
    } catch (err) {
      if (err.needs === 'access') { signInAgain(); return null; }
      if (justWoke && err.status === 0 && reloadOnce(`state failed ${Math.round((Date.now() - wokeAt) / 1000)}s after wake`)) return null;
      if (!quiet) throw err;
      banner(navigator.onLine === false ? t('app.offline') : (err.plain || t('app.unreachable')));
      if (!state) {
        const cached = readCache();
        if (cached) { setState(cached, { fromCache: true }); loadPresets(); } else bootMessage(t('app.unreachable'));
      }
      // a dead connection after the background usually works on the next try; don't wait 30 s
      if (retries < RETRY_MS.length && document.visibilityState === 'visible') {
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => load(), RETRY_MS[retries++]);
      }
      return null;
    }
  })();
  if (quiet) { loading = run; run.finally(() => { if (loading === run) loading = null; }); }
  return run;
}

/** The Cloudflare Access sign-in expired: reload so Cloudflare shows its login (at most once a minute). */
function signInAgain() {
  let last = 0;
  try { last = Number(sessionStorage.getItem(REAUTH_KEY)) || 0; } catch { /* private mode */ }
  if (Date.now() - last > 60000) {
    try { sessionStorage.setItem(REAUTH_KEY, String(Date.now())); } catch { /* private mode */ }
    window.location.reload();
    return;
  }
  banner(t('app.signin'));
}

function bootMessage(text) {
  const label = $('boot')?.querySelector('[data-t]');
  if (label) label.textContent = text;
}

function setState(s, { fromCache = false } = {}) {
  const prevAuth = state?.auth;
  state = s;
  if (s?.dial) setDial(s.dial);
  if (!fromCache && s?.auth === 'ok') {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(s)); } catch { /* storage full or private */ }
  }
  if (s?.auth === 'needs_device') {   // not approved (or removed): keep nothing about the car here
    try { localStorage.removeItem(CACHE_KEY); } catch { /* ignore */ }
  }
  render();
  if (s?.auth === 'ok' && !fromCache) {
    if (prevAuth !== 'ok' || presets === null) loadPresets();
    loadLog();
    if (s.pending?.id && !cmd) trackCommand(s.pending.id, s.pending.action, s.pending.sent_at);
  }
}

function readCache() {
  try { return JSON.parse(localStorage.getItem(CACHE_KEY) || 'null'); } catch { return null; }
}

function banner(text) {
  const b = $('banner');
  b.hidden = !text;
  if (text) b.textContent = text;
}

// ---------------------------------------------------------------- render

function render() {
  $('boot').hidden = true;
  const s = state || {};
  const ok = s.auth === 'ok';
  $('demo-badge').hidden = s.demo !== true;
  $('dash').hidden = !ok;
  $('auth').hidden = ok;
  $('refresh-btn').hidden = !ok;
  $('menu-btn').hidden = !ok;
  if (ok) renderDash(s.vehicle || {});
  else renderAuth(s);
}

function renderDash(v) {
  const name = titleCase(v.name) || t('app.name');
  $('car-name').textContent = name;
  if (document.title !== name) {   // the tab, and the name iOS offers when adding to the home screen
    document.title = name;
    $('app-title')?.setAttribute('content', name);
  }
  renderUpdated(v);
  renderCar(app, v);
  renderActions(v);
  renderWarnings(v);
  renderClimateNote(v);
  renderGauges(v);
  renderLocation(v);
}

function titleCase(s) {
  if (typeof s !== 'string' || !s.trim()) return null;
  return s === s.toUpperCase() ? s.charAt(0) + s.slice(1).toLowerCase() : s;
}

function renderUpdated(v) {
  const when = ago(v.updated_at);
  const u = $('updated');
  u.textContent = when ? t('updated', { when }) : t('updated.never');
  const m = minutesSince(v.updated_at);
  u.classList.toggle('is-stale', m !== null && m > 180);
}

// Something a stop would end: climate on, or the engine running (a remote start can run the engine
// with climate off). While driving Kia refuses the stop, so offering it then is harmless.
const stoppable = (v) => v?.climate?.on === true || v?.engine_on === true;

function renderActions(v) {
  const on = v.climate?.on === true;
  const engineOnly = !on && v.engine_on === true;
  const btn = $('act-climate');
  btn.classList.toggle('is-on', on || engineOnly);
  btn.classList.toggle('is-cool', climateMode(v) === 'cool');
  $('act-climate-label').textContent = t(engineOnly ? 'action.engine_stop' : on ? 'action.climate_stop' : 'action.climate_start');
  $('act-climate-sub').textContent = on ? t('climate.running', { temp: temp(v.climate?.set_temp_c ?? null) })
    : engineOnly ? t('engine.running') : describe(lastOptions());
  $('act-lock').setAttribute('aria-pressed', String(v.locked === true));
  $('act-unlock').setAttribute('aria-pressed', String(v.locked === false));
  setBusy();
}

const WARNINGS = [
  ['washer_fluid', 'drop'], ['brake_fluid', 'warn'], ['oil', 'oil'],
  ['smart_key_battery', 'key'], ['aux_battery', 'battery'],
];

/** The warning lights the car reports now: [{ico, text}]. */
function activeWarnings(v) {
  const found = [];
  for (const [key, ico] of WARNINGS) {
    if (v?.warnings?.[key] === true) found.push({ ico, text: t(`warn.${key}`) });
  }
  const tires = v?.tires || {};
  if (tires.warning === true || ['front_left', 'front_right', 'rear_left', 'rear_right'].some((c) => tires[c] === true)) {
    found.push({ ico: 'tire', text: t('warn.tires') });
  }
  return found;
}

function renderWarnings(v) {
  const items = activeWarnings(v).map((w) => el('li', { class: 'warn' }, icon(w.ico), el('span', { text: w.text })));
  $('warn-list').replaceChildren(...items);
  $('warnings').hidden = items.length === 0;
}

function renderClimateNote(v) {
  const n = $('climate-note');
  const on = v.climate?.on === true;
  const ended = !on && v.climate?.ended_at ? new Date(v.climate.ended_at) : null;
  n.textContent = on ? t('climate.running', { temp: temp(v.climate?.set_temp_c ?? null) })
    : ended && !Number.isNaN(ended.getTime()) ? t('climate.ended', { time: ended.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) })
      : t('climate.off');
  n.classList.toggle('is-on', on);
}

function renderGauges(v) {
  const fuel = num(v.fuel_percent);
  $('fuel-pct').textContent = int(fuel);
  $('fuel-arc').style.setProperty('--fuel', String(fuel === null ? 0 : Math.max(0, Math.min(100, fuel))));
  const fuelBox = $('fuel-arc').closest('.fuel');
  fuelBox.classList.toggle('is-low', fuel !== null && fuel < 15);
  fuelBox.classList.toggle('is-unknown', fuel === null);
  $('range-km').textContent = int(v.range_km);

  const batt = num(v.battery_12v_percent);
  $('batt-pct').textContent = int(batt);
  $('batt-bar').style.setProperty('--batt', String(batt === null ? 0 : Math.max(0, Math.min(100, batt))));
  $('batt-pct').closest('.tile').classList.toggle('is-warn', v.warnings?.aux_battery === true || (batt !== null && batt < 50));

  $('outside-c').textContent = dec(v.outside_temp_c);
  $('odo-km').textContent = int(v.odometer_km);
  $('service-km').textContent = int(v.next_service_km);
  const nulls = [['fuel-pct', fuel], ['range-km', v.range_km], ['batt-pct', batt], ['outside-c', v.outside_temp_c],
    ['odo-km', v.odometer_km], ['service-km', v.next_service_km]];
  for (const [id, val] of nulls) $(id).parentElement.classList.toggle('is-null', num(val) === null);
  // Kia Canada never reports the outside temperature: drop the tile rather than show a dash.
  $('outside-c').closest('.tile').hidden = num(v.outside_temp_c) === null;
  $('service-km').closest('.tile').classList.toggle('is-warn', num(v.next_service_km) !== null && v.next_service_km < 1000);
}

function renderLocation(v) {
  const loc = v.location;
  const text = coords(loc?.lat, loc?.lon);
  $('loc-coords').textContent = text || t('loc.unknown');
  $('loc-coords').classList.toggle('is-empty', !text);
  const seen = ago(loc?.updated_at);
  $('loc-seen').textContent = seen ? t('loc.updated', { when: seen }) : '';
  $('loc-seen').hidden = !seen;
  const url = mapsUrl(loc?.lat, loc?.lon);
  const a = $('loc-open');
  a.hidden = !url;
  if (url) a.href = url;
}

// ---------------------------------------------------------------- activity log

async function loadLog() {
  try {
    const res = await api.getLog();
    renderLog(Array.isArray(res?.entries) ? res.entries : []);
  } catch { /* keep what we have */ }
}

function renderLog(entries) {
  const rows = entries.slice(0, 8).map((e) => {
    const status = ['pending', 'success', 'failed', 'timeout', 'error'].includes(e?.status) ? e.status : 'error';
    const base = t(`action.${e?.action}`);
    const what = typeof e?.who === 'string' && e.who.startsWith('schedule') && !base.startsWith('action.')
      ? `${base} (${t('log.by.schedule').toLowerCase()})` : base;
    const statusText = e?.message ? `${t(`log.status.${status}`)}. ${e.message}` : t(`log.status.${status}`);
    return el('li', { class: `log-item log-${status}` },
      el('span', { class: 'log-dot' }),
      el('span', { class: 'log-what', text: what.startsWith('action.') ? DASH : what }),
      el('span', { class: 'log-when', text: ago(e?.sent_at) || DASH }),
      el('span', { class: 'log-status', text: statusText }));
  });
  $('log').replaceChildren(...rows);
  $('log-empty').hidden = rows.length > 0;
}

// ---------------------------------------------------------------- presets

async function loadPresets() {
  try {
    const res = await api.getPresets();
    presets = Array.isArray(res?.presets) ? res.presets : [];
    try { localStorage.setItem(PRESET_KEY, JSON.stringify(presets)); } catch { /* ignore */ }
  } catch {
    if (!presets) {
      try { presets = JSON.parse(localStorage.getItem(PRESET_KEY) || '[]'); } catch { presets = []; }
    }
  }
  paintPresets();
  schedule.load(presets);
}

const sheetHandlers = {
  onStart: (options) => command('climate_start', options),
  onSaved: (list) => { if (list) { presets = list; paintPresets(); schedule.load(presets); } else loadPresets(); },
};

function paintPresets() {
  renderPresets($('presets'), presets, {
    onStart: (options) => command('climate_start', options),
    onCustom: () => openClimateSheet(null, sheetHandlers),
    onEdit: (p) => openClimateSheet(null, sheetHandlers, p),
  });
  setBusy();
}

// ---------------------------------------------------------------- commands

/** extra: {pin} or {assertion} for unlock; note: a line for the pending pill. */
async function command(action, options, extra = null, note = null) {
  if (cmd) { showPill('failed', t('cmd.busy')); return; }
  vibrate(10);
  cmd = { id: null, action, startedAt: Date.now(), note };
  showPending();
  setBusy();
  try {
    const res = await api.sendCommand(action, options, extra);
    if (!res?.id) throw new api.ApiError(500, null);
    cmd.id = res.id;
    loadLog();
    if (res.status && res.status !== 'pending') return finish(res);
    schedulePoll();
  } catch (err) {
    cmd = null;
    setBusy();
    if (action === 'unlock' && err.status === 403 && err.needs === 'verification') {
      hidePill();
      vibrate([60, 60, 60]);
      openPinSheet('unlock', { error: err.plain });
      return;
    }
    if (action === 'unlock' && err.status === 429) { showPill('failed', t('pin.locked.title'), err.plain); vibrate([60, 60, 60]); return; }
    if (err.status === 409) showPill('failed', t('cmd.busy'));
    else showPill('failed', t('cmd.failed'), err.plain || t('app.unreachable'));
    vibrate([60, 60, 60]);
    if (err.status === 409) load();
  }
}

function trackCommand(id, action, sentAt) {
  const started = Date.parse(sentAt || '');
  cmd = { id, action, startedAt: Number.isFinite(started) ? started : Date.now() };
  showPending();
  setBusy();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(cmd?.timer);
  if (!cmd) return;
  cmd.timer = setTimeout(pollCommand, CMD_POLL_MS);
}

async function pollCommand() {
  if (!cmd) return;
  try {
    const res = await api.getCommand(cmd.id);
    if (res?.status && res.status !== 'pending') return finish(res);
  } catch (err) {
    if (err.status === 404) return finish({ status: 'error', message: err.plain });
  }
  updatePendingClock();
  schedulePoll();
}

function finish(res) {
  const action = res.action || cmd?.action;
  clearTimeout(cmd?.timer);
  cmd = null;
  setBusy();
  if (res.status === 'success') {
    showPill('success', t(`cmd.success.${action}`));
    vibrate(30);
  } else {
    const what = t(`action.${action}`);
    const title = res.status === 'timeout' ? t('cmd.timeout')
      : res.status === 'failed' ? (what.startsWith('action.') ? t('cmd.failed') : t('cmd.failed.of', { what })) : t('cmd.error');
    showPill('failed', title, res.message || null);
    vibrate([60, 60, 60]);
  }
  load();
}

const ACTION_BUTTON = {
  lock: 'act-lock', unlock: 'act-unlock', climate_start: 'act-climate',
  climate_stop: 'act-climate', horn_lights: 'act-horn', hazard: 'act-hazard',
};

function setBusy() {
  const busy = !!cmd;
  for (const id of new Set(Object.values(ACTION_BUTTON))) {
    const b = $(id);
    b.disabled = busy;
    b.classList.toggle('is-busy', busy && ACTION_BUTTON[cmd?.action] === id);
  }
  for (const b of $('presets').querySelectorAll('.preset:not(.preset-custom)')) b.disabled = busy;
  $('car-stage').classList.toggle('is-busy', busy && (cmd.action === 'lock' || cmd.action === 'unlock'));
}

// ---------------------------------------------------------------- the status pill

function showPending() {
  clearTimeout(pillHideTimer);
  const pill = $('cmd-status');
  pill.className = 'cmd-status is-pending';
  $('cmd-title').textContent = t(`cmd.pending.${cmd.action}`);
  $('cmd-sub').textContent = cmd.note || t('cmd.pending.note');
  $('cmd-sub').hidden = false;
  $('cmd-close').hidden = true;
  pill.hidden = false;
  updatePendingClock();
  clearInterval(tickTimer);
  tickTimer = setInterval(updatePendingClock, 1000);
}

function updatePendingClock() {
  if (!cmd) return;
  $('cmd-time').textContent = t('cmd.elapsed', { n: Math.max(0, Math.round((Date.now() - cmd.startedAt) / 1000)) });
}

/** kind: 'pending' | 'success' | 'failed' */
function showPill(kind, title, sub = null) {
  clearTimeout(pillHideTimer);
  clearInterval(tickTimer);
  const pill = $('cmd-status');
  pill.className = `cmd-status is-${kind}`;
  $('cmd-title').textContent = title;
  $('cmd-sub').textContent = sub || '';
  $('cmd-sub').hidden = !sub;
  $('cmd-time').textContent = '';
  $('cmd-close').hidden = kind === 'pending';
  pill.hidden = false;
  if (kind === 'success') pillHideTimer = setTimeout(hidePill, 4000);
}

function hidePill() {
  if (cmd) return;
  $('cmd-status').hidden = true;
  $('cmd-sub').hidden = false;
}

// ---------------------------------------------------------------- refresh and wake

let refreshing = false;

async function cheapRefresh() {
  if (!state) { window.location.reload(); return; }   // still on "Connecting": start over
  if (refreshing) return;
  refreshing = true;
  $('refresh-btn').classList.add('is-spinning');
  try {
    setState(await api.refresh(false));
    banner(null);
  } catch (err) {
    showPill('failed', t('refresh.failed'), err.plain || t('app.unreachable'));
  } finally {
    refreshing = false;
    $('refresh-btn').classList.remove('is-spinning');
  }
}

async function wakeCar() {
  if (refreshing || cmd) return;
  refreshing = true;
  $('refresh-btn').classList.add('is-spinning');
  clearTimeout(pillHideTimer);
  const pill = $('cmd-status');
  pill.className = 'cmd-status is-pending';
  $('cmd-title').textContent = t('wake.busy');
  $('cmd-sub').textContent = '';
  $('cmd-sub').hidden = true;
  $('cmd-close').hidden = true;
  pill.hidden = false;
  const started = Date.now();
  clearInterval(tickTimer);
  const tick = () => { $('cmd-time').textContent = t('cmd.elapsed', { n: Math.round((Date.now() - started) / 1000) }); };
  tick();
  tickTimer = setInterval(tick, 1000);
  try {
    setState(await api.refresh(true));
    showPill('success', t('wake.done'));
    vibrate(30);
  } catch (err) {
    if (err.status === 429) showPill('failed', t('wake.limited.title'), t('wake.limited'));
    else showPill('failed', t('refresh.failed'), err.plain || t('app.unreachable'));
    vibrate([60, 60, 60]);
  } finally {
    refreshing = false;
    $('refresh-btn').classList.remove('is-spinning');
  }
}

// ---------------------------------------------------------------- auth screens

function renderAuth(s) {
  const device = s.auth === 'needs_device';
  const mode = s.auth === 'needs_otp' ? 'otp' : s.auth === 'error' ? 'error' : 'login';
  $('login-form').hidden = mode !== 'login';
  // The same form approves a new browser: the Kia account, checked on the PC, not sent to Kia.
  $('login-form').dataset.mode = device ? 'device' : 'login';
  $('login-title').textContent = t(device ? 'auth.device.title' : 'auth.title');
  $('login-lead').textContent = t(device ? 'auth.device.lead' : 'auth.lead');
  $('login-saved').textContent = t(device ? 'auth.device.saved' : 'auth.saved');
  $('login-lockout').textContent = t(device ? 'auth.device.lockout' : 'auth.lockout');
  $('login-submit').textContent = t(device ? 'auth.device.submit' : 'auth.submit');
  $('otp-form').hidden = mode !== 'otp';
  $('autherr').hidden = mode !== 'error';
  $('car-name').textContent = t('app.name');
  $('updated').textContent = '';
  if (mode === 'otp') {
    const hasEmail = s.otp?.has_email !== false;
    const hasSms = s.otp?.has_sms === true;
    $('otp-email').disabled = !hasEmail;
    $('otp-sms').disabled = !hasSms;
    if (!$('otp-email').checked && !$('otp-sms').checked) {
      if (hasEmail) $('otp-email').checked = true; else if (hasSms) $('otp-sms').checked = true;
    }
  }
  if (mode === 'error') $('autherr-msg').textContent = s.auth_error || t('autherr.fallback');
}

function formError(id, text) {
  const p = $(id);
  p.hidden = !text;
  p.textContent = text || '';
}

async function busyButton(btn, busyKey, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = t(busyKey);
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = label; }
}

function wireAuth() {
  $('pw-toggle').addEventListener('click', () => {
    const pw = $('login-password');
    const show = pw.type === 'password';
    pw.type = show ? 'text' : 'password';
    $('pw-toggle').textContent = t(show ? 'auth.hide' : 'auth.show');
  });
  $('login-pin').addEventListener('input', (e) => { e.target.value = e.target.value.replace(/\D/g, '').slice(0, 4); });

  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = $('login-email').value.trim();
    const password = $('login-password').value;
    const pin = $('login-pin').value.trim();
    if (!email || !password || !/^\d{4}$/.test(pin)) { formError('login-error', t('auth.missing')); return; }
    formError('login-error', null);
    const device = $('login-form').dataset.mode === 'device';
    await busyButton($('login-submit'), device ? 'auth.device.busy' : 'auth.busy', async () => {
      try {
        const label = passkey.deviceLabel();
        const s = device ? await api.approveDevice(email, password, pin, label)
          : await api.login(email, password, pin, label);
        $('login-password').value = '';
        $('login-pin').value = '';
        setState(s);
      } catch (err) {
        formError('login-error', err.plain || t('app.unreachable'));
        vibrate([60, 60, 60]);
      }
    });
  });

  $('otp-send').addEventListener('click', async () => {
    const method = $('otp-sms').checked ? 'SMS' : 'EMAIL';
    formError('otp-error', null);
    await busyButton($('otp-send'), 'otp.sending', async () => {
      try {
        await api.otpSend(method);
        $('otp-sent').hidden = false;
        $('otp-code').focus();
      } catch (err) {
        formError('otp-error', err.plain || t('app.unreachable'));
      }
    });
  });

  $('otp-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('otp-code').value.trim();
    if (!code) { formError('otp-error', t('otp.missing')); return; }
    formError('otp-error', null);
    await busyButton($('otp-submit'), 'otp.verifying', async () => {
      try {
        const s = await api.otpVerify(code);
        $('otp-code').value = '';
        $('otp-sent').hidden = true;
        setState(s);
      } catch (err) {
        formError('otp-error', err.plain || t('app.unreachable'));
        vibrate([60, 60, 60]);
      }
    });
  });

  $('autherr-retry').addEventListener('click', () => busyButton($('autherr-retry'), 'auth.busy', () => load()));
  $('autherr-relogin').addEventListener('click', async () => {
    try { setState(await api.logout()); } catch (err) { $('autherr-msg').textContent = err.plain || t('app.unreachable'); }
  });
}

// ---------------------------------------------------------------- unlock: fingerprint or PIN

let unlockOptions = null;    // a challenge fetched while the confirm sheet is open
let unlockPrefetch = null;
let regOptions = null;       // passkey setup options, fetched before the tap that uses them
let pinMode = 'unlock';      // 'unlock' | 'setup'

function askUnlock() {
  vibrate(8);
  unlockOptions = null;
  unlockPrefetch = passkey.savedId() ? passkey.prefetchUnlock().then((o) => { unlockOptions = o; return o; }) : null;
  $('confirm-sheet').showModal();
}

async function confirmUnlock(e) {
  e.preventDefault();
  $('confirm-sheet').close('ok');
  let options = unlockOptions;
  unlockOptions = null;
  // Normally ready: then the fingerprint prompt starts inside this tap, as Safari wants.
  if (!options && unlockPrefetch) options = await unlockPrefetch;
  unlockPrefetch = null;
  if (!options || !passkey.savedId()) { openPinSheet('unlock'); return; }
  let assertion;
  try {
    assertion = await passkey.assertion(options);
  } catch {
    // cancelled, timed out, or no fingerprint on this phone any more
    openPinSheet('unlock', { text: t('pin.text.fallback') });
    return;
  }
  command('unlock', null, { assertion });
}

function openPinSheet(mode, { text = null, error = null } = {}) {
  pinMode = mode;
  regOptions = null;
  $('pin-title').textContent = t(`pin.title.${mode}`);
  $('pin-text').textContent = text || t(`pin.text.${mode}`);
  $('pin-ok').textContent = t(`pin.ok.${mode}`);
  $('pin-input').value = '';
  $('pin-remember').checked = false;
  $('pin-remember-row').hidden = true;
  formError('pin-error', error);
  const sheet = $('pin-sheet');
  if (!sheet.open) sheet.showModal();
  $('pin-input').focus();
  passkey.isAvailable().then((ok) => {
    if (!ok) return;
    if (mode === 'setup') fetchRegOptions();
    else if (!passkey.savedId()) $('pin-remember-row').hidden = false;
  });
}

function fetchRegOptions() {
  passkey.prefetchRegister().then((o) => { regOptions = o; });
}

function submitPin(e) {
  e.preventDefault();
  const pin = $('pin-input').value.trim();
  if (!/^\d{4}$/.test(pin)) { formError('pin-error', t('pin.missing')); vibrate([60, 60, 60]); return; }
  formError('pin-error', null);
  const wantPasskey = pinMode === 'setup' || (!$('pin-remember-row').hidden && $('pin-remember').checked);
  let made = null;
  if (wantPasskey && regOptions) {
    made = passkey.create(regOptions);   // inside the tap: the phone asks for the fingerprint now
    regOptions = null;
  } else if (pinMode === 'setup') {
    formError('pin-error', t('passkey.failed'));
    fetchRegOptions();
    return;
  }
  finishPin(pin, made, wantPasskey);
}

async function finishPin(pin, made, wantPasskey) {
  await busyButton($('pin-ok'), 'pin.busy', async () => {
    let skipped = wantPasskey && !made;
    if (made) {
      let cred = null;
      try { cred = await made; } catch { skipped = true; }
      if (cred) {
        try {
          await passkey.register(cred, pin);
        } catch (err) {
          if ([401, 403, 429].includes(err.status)) {   // wrong PIN, or the PIN is paused
            formError('pin-error', err.plain || t('app.unreachable'));
            vibrate([60, 60, 60]);
            if (pinMode === 'unlock') $('pin-remember').checked = false;
            else fetchRegOptions();
            return;
          }
          skipped = true;
        }
      }
      if (skipped && pinMode === 'setup') {
        formError('pin-error', t('passkey.failed'));
        fetchRegOptions();
        return;
      }
    }
    $('pin-sheet').close('ok');
    if (pinMode === 'setup') { showPill('success', t('passkey.on')); vibrate(30); paintPasskeyRow(); return; }
    command('unlock', null, { pin }, skipped ? t('passkey.skipped') : null);
  });
}

function paintPasskeyRow() {
  passkey.isAvailable().then((ok) => {
    $('menu-passkey').hidden = !ok;
    if (!ok) return;
    const on = !!passkey.savedId();
    $('menu-passkey').classList.toggle('is-on', on);
    $('menu-passkey-state').textContent = t(on ? 'menu.passkey.on' : 'menu.passkey.off');
    $('menu-passkey-note').textContent = t(on ? 'menu.passkey.on.note' : 'menu.passkey.off.note');
  });
}

function openMenu() {
  paintPasskeyRow();
  $('menu-sheet').showModal();
  if (passkey.savedId()) passkey.sync().then(paintPasskeyRow);
}

async function togglePasskey() {
  if (!passkey.savedId()) { openPinSheet('setup'); return; }
  try {
    await passkey.turnOff();
    showPill('success', t('passkey.off'));
  } catch (err) {
    showPill('failed', t('cmd.error'), err.plain || t('app.unreachable'));
  }
  paintPasskeyRow();
}

// ---------------------------------------------------------------- approved devices

async function openDevices() {
  formError('devices-error', null);
  $('device-list').replaceChildren();
  $('devices-sheet').showModal();
  try {
    paintDevices((await api.listDevices()).devices);
  } catch (err) {
    formError('devices-error', err.plain || t('app.unreachable'));
  }
}

function paintDevices(devices) {
  const list = $('device-list');
  list.replaceChildren();
  if (!devices.length) {
    list.append(el('li', { class: 'sheet-text' }, t('devices.empty')));
    return;
  }
  for (const d of devices) {
    const sub = [d.who, d.last_used_at ? t('devices.used', { ago: ago(d.last_used_at) }) : null].filter(Boolean).join(' · ');
    const remove = el('button', { class: 'device-remove', type: 'button' }, t('devices.remove'));
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      try {
        paintDevices((await api.deleteDevice(d.id)).devices);
        showPill('success', t('devices.removed'));
        if (d.this_device) { $('devices-sheet').close(); load({ quiet: false }).catch(() => {}); }
      } catch (err) {
        remove.disabled = false;
        formError('devices-error', err.plain || t('app.unreachable'));
      }
    });
    list.append(el('li', { class: 'device-item' },
      el('span', { class: 'device-text' },
        el('span', {}, d.label, d.this_device ? el('span', { class: 'device-this' }, ` · ${t('devices.this')}`) : ''),
        el('small', {}, sub)),
      remove));
  }
}

// ---------------------------------------------------------------- wiring

function wire() {
  onPress($('refresh-btn'), { tap: cheapRefresh, long: openMenu });
  $('menu-btn').addEventListener('click', openMenu);
  $('menu-sheet').addEventListener('close', async () => {
    const choice = $('menu-sheet').returnValue;
    $('menu-sheet').returnValue = '';
    if (choice === 'wake') wakeCar();
    if (choice === 'passkey') togglePasskey();
    if (choice === 'devices') openDevices();
    if (choice === 'activity') openActivity();
    if (choice === 'health') {
      const v = state?.vehicle;
      openHealth({ warnings: activeWarnings(v), serviceKm: num(v?.next_service_km) });
    }
    if (choice === 'logout') {
      try {
        setState(await api.logout());
        try { localStorage.removeItem(CACHE_KEY); } catch { /* ignore */ }
      } catch (err) { showPill('failed', t('cmd.error'), err.plain || t('app.unreachable')); }
    }
  });

  $('act-lock').addEventListener('click', () => command('lock'));
  $('act-unlock').addEventListener('click', askUnlock);
  $('confirm-ok').addEventListener('click', confirmUnlock);
  $('confirm-sheet').addEventListener('close', () => { $('confirm-sheet').returnValue = ''; });
  $('pin-form').addEventListener('submit', submitPin);
  $('pin-cancel').addEventListener('click', () => $('pin-sheet').close('cancel'));
  $('pin-input').addEventListener('input', (e) => { e.target.value = e.target.value.replace(/\D/g, '').slice(0, 4); });
  $('pin-remember').addEventListener('change', () => { if ($('pin-remember').checked && !regOptions) fetchRegOptions(); });
  $('act-climate').addEventListener('click', () => {
    if (stoppable(state?.vehicle)) command('climate_stop');
    else command('climate_start', lastOptions());
  });
  $('act-horn').addEventListener('click', () => command('horn_lights'));
  $('act-hazard').addEventListener('click', () => command('hazard'));
  $('cmd-close').addEventListener('click', hidePill);
  schedule.wire({ changed: loadLog });

  // tap on the dimmed backdrop closes a sheet
  for (const d of document.querySelectorAll('dialog.sheet')) {
    d.addEventListener('click', (e) => { if (e.target === d) d.close('cancel'); });
  }

  wireAuth();
  window.addEventListener('needs-device', () => {
    for (const d of document.querySelectorAll('dialog[open]')) d.close('cancel');
    setState({ demo: state?.demo === true, auth: 'needs_device' });
  });

  let hiddenAt = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      diag.note('visible', hiddenAt ? `after ${Math.round((Date.now() - hiddenAt) / 1000)}s` : '');
      wokeAt = Date.now();
      load(); startPolling();
    } else {
      hiddenAt = Date.now();
      diag.note('hidden');
      stopPolling();
    }
  });
  window.addEventListener('pageshow', (e) => { if (e.persisted) { diag.note('pageshow', 'from back-forward cache'); wokeAt = Date.now(); load(); } });
  document.addEventListener('resume', () => diag.note('resume', 'page was frozen'));
  window.addEventListener('focus', () => load());
  window.addEventListener('online', () => load());
  window.addEventListener('offline', () => banner(t('app.offline')));
  // keep "12 min ago" honest between polls
  setInterval(() => { if (state?.auth === 'ok' && document.visibilityState === 'visible') renderUpdated(state.vehicle || {}); }, 20000);
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function registerWorker() {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register('sw.js').catch(() => { /* offline shell is a bonus */ });
}

window.__sportageBooted = true;
diag.note('boot', `${APP_VERSION} cache=${!!readCache()} sw=${!!navigator.serviceWorker?.controller} online=${navigator.onLine} discarded=${!!document.wasDiscarded} nav=${performance.getEntriesByType?.('navigation')?.[0]?.type || '?'}`);
applyStrings();
$('menu-version').textContent = t('menu.version', { v: APP_VERSION });
wire();
paintPasskeyRow();
// Show the last known state at once instead of "Connecting" while the phone wakes its connection.
{
  const cached = readCache();
  if (cached) { setState(cached, { fromCache: true }); loadPresets(); }
}
load();
startPolling();
registerWorker();
