// Kia activity: every call this computer made to Kia, and which ones reached the car itself
// (those wake it and draw on its 12V battery). Reading Kia's copy of the car doesn't touch it.
import { t } from './strings.js';
import { $, el, icon } from './dom.js';
import * as api from './api.js';
import { int } from './format.js';

const DAY_MS = 24 * 60 * 60 * 1000;

export async function openActivity() {
  $('activity-error').hidden = true;
  $('activity-summary').replaceChildren();
  $('activity-days').replaceChildren();
  $('activity-empty').hidden = true;
  $('activity-sheet').showModal();
  try {
    const res = await api.kiaCalls();
    paint(Array.isArray(res?.entries) ? res.entries : []);
  } catch (err) {
    $('activity-error').textContent = err.plain || t('app.unreachable');
    $('activity-error').hidden = false;
  }
}

function paint(entries) {
  const now = Date.now();
  $('activity-summary').replaceChildren(
    tile(t('activity.day'), entries, now - DAY_MS),
    tile(t('activity.week'), entries, now - 7 * DAY_MS));
  $('activity-empty').hidden = entries.length > 0;

  const days = new Map();
  for (const row of merge(entries)) {
    const d = new Date(row.at);
    if (Number.isNaN(d.getTime())) continue;
    const key = d.toDateString();
    if (!days.has(key)) days.set(key, { date: d, rows: [] });
    days.get(key).rows.push(row);
  }
  const parts = [];
  for (const { date, rows } of days.values()) {
    parts.push(el('h3', { class: 'health-h' }, dayName(date, now)));
    parts.push(el('ul', { class: 'health-list' }, ...rows.map(item)));
  }
  $('activity-days').replaceChildren(...parts);
}

function tile(label, entries, since) {
  const recent = entries.filter((e) => Date.parse(e?.at) >= since);
  const car = recent.filter((e) => e?.reached_car === true).length;
  return el('div', { class: 'health-stat' },
    el('span', { class: 'health-stat-label' }, label),
    el('span', { class: 'health-stat-value' }, int(recent.length)),
    el('span', { class: `activity-car${car ? ' is-car' : ''}` },
      t(car === 1 ? 'activity.car.one' : 'activity.car.many', { n: int(car) })));
}

/** Consecutive status checks on one command become one row with a count. */
function merge(entries) {
  const out = [];
  for (const e of entries) {
    if (!e || typeof e.at !== 'string') continue;
    const last = out[out.length - 1];
    if (e.why === 'check' && last && last.why === 'check' && last.action === e.action && last.ok === e.ok) {
      last.count += 1;
      last.ms += e.ms || 0;
      continue;
    }
    out.push({ ...e, count: 1, ms: e.ms || 0 });
  }
  return out;
}

function item(e) {
  const action = actionName(e.action);
  const label = t(`activity.why.${e.why}`, { action, n: e.count });
  const title = label.startsWith('activity.') ? e.why : label;
  const by = typeof e.who === 'string' && e.who.startsWith('schedule') ? ` (${t('log.by.schedule').toLowerCase()})` : '';
  const endpoints = Array.isArray(e.endpoints) ? e.endpoints : [];
  const notes = [timeText(e.at)];
  if (endpoints.includes('fndmcr')) notes.push(t('activity.location'));
  if (e.ok === false) notes.push(e.message ? t('activity.failed.why', { why: e.message }) : t('activity.failed'));
  const row = el('li', { class: `health-row activity-row${e.ok === false ? ' is-issue' : ''}` },
    el('span', { class: 'activity-text' },
      el('span', { class: 'health-name' }, title + by),
      el('small', { class: 'activity-note' }, notes.join(' · '))),
    el('span', { class: `activity-tag${e.reached_car ? ' is-car' : ''}` },
      e.reached_car ? icon('battery') : null,
      el('span', {}, t(e.reached_car ? 'activity.tag.car' : 'activity.tag.kia'))));
  if (endpoints.length) row.title = endpoints.join(', ');
  return row;
}

function actionName(action) {
  const name = t(`action.${action}`);
  return name.startsWith('action.') ? t('activity.command') : name.toLowerCase();
}

function timeText(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString('en-CA', { hour: 'numeric', minute: '2-digit' });
}

function dayName(date, now) {
  const today = new Date(now).toDateString();
  const yesterday = new Date(now - DAY_MS).toDateString();
  if (date.toDateString() === today) return t('activity.today');
  if (date.toDateString() === yesterday) return t('activity.yesterday');
  return date.toLocaleDateString('en-CA', { weekday: 'short', month: 'short', day: 'numeric' });
}
