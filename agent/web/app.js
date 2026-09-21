/* MonitorPad - phone client.
 *
 * Two parallel models drive the UI:
 *   baseline  what the PC currently reports (refreshed by polling)
 *   working   what the user has dialled in but not applied yet
 * Apply sends the diff. Anything risky comes back with a pending id that must
 * be confirmed before the agent's watchdog rolls it back.
 */

'use strict';

const STORE_TOKEN = 'monitorpad.token';
const STORE_COLLAPSED = 'monitorpad.collapsed';
// Remembered so the offline screen can name the PC even when
// it cannot be reached to ask.
const STORE_HOST = 'monitorpad.host';
const POLL_MS = 8000;

const el = (id) => document.getElementById(id);

let token = null;
let state = null;          // last /api/state payload
let baseline = new Map();  // key -> settings as the PC reports them
let working = new Map();   // key -> settings the user wants
let selectedKey = null;
let pollTimer = null;
let countdownTimer = null;
let busy = false;

/* ----------------------------------------------------------------- utils */

function toast(message, bad) {
  const node = el('toast');
  node.textContent = message;
  node.classList.toggle('bad', !!bad);
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, bad ? 5200 : 2600);
}

function footprint(settings) {
  // Panel-space width/height -> the box the monitor occupies on the desktop.
  return settings.rotation % 180
    ? { w: settings.height, h: settings.width }
    : { w: settings.width, h: settings.height };
}

function hzLabel(hz, peers) {
  // Windows floors 59.94 to 59 and 119.88 to 119. When both the floored and
  // the round value exist for a resolution, spell the fractional one out.
  if (peers.has(hz + 1)) {
    return String(Number(((hz + 1) / 1.001).toFixed(3))) + ' Hz';
  }
  return hz + ' Hz';
}

function resKey(mode) { return mode.width + 'x' + mode.height; }

/* ------------------------------------------------------------- collapse */

// Per-display, remembered on this device. A value of true or false is an
// explicit choice by the user; no entry means "use the default".
function collapsedState() {
  try {
    return JSON.parse(localStorage.getItem(STORE_COLLAPSED) || '{}') || {};
  } catch (_) {
    return {};
  }
}

function isCollapsed(monitor) {
  const stored = collapsedState()[monitor.key];
  if (typeof stored === 'boolean') return stored;
  // Folded by default. The header already carries the port, the current
  // mode and the badges, so the list reads as a summary of the whole setup
  // and you open only the display you came to change.
  return true;
}

function setCollapsed(key, value) {
  const state = collapsedState();
  state[key] = value;
  try {
    localStorage.setItem(STORE_COLLAPSED, JSON.stringify(state));
  } catch (_) {
    /* private mode: the choice just will not persist */
  }
}

function setAllCollapsed(value) {
  const stored = collapsedState();
  for (const monitor of (state ? state.monitors : [])) {
    stored[monitor.key] = value;
  }
  try {
    localStorage.setItem(STORE_COLLAPSED, JSON.stringify(stored));
  } catch (_) { /* ignore */ }
}

function anyExpanded() {
  if (!state) return false;
  return state.monitors.some((monitor) => !isCollapsed(monitor));
}

/* ------------------------------------------------------------------- api */

// Tell the agent about a request that failed before it could arrive, so the
// agent's log shows the gap from the phone's side too. Best effort by
// definition -- if the network is still down this goes nowhere.
function reportToAgent(message) {
  if (!token) return;
  fetch('/api/client-log', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ message }),
    keepalive: true,
  }).catch(() => { /* nothing more to be done */ });
}

async function once(path, body) {
  const options = {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Authorization': 'Bearer ' + token },
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* non-JSON */ }
  if (!response.ok) {
    const message = (payload && payload.error) || ('HTTP ' + response.status);
    if (response.status === 401) {
      localStorage.removeItem(STORE_TOKEN);
      token = null;
      showPairing('That token was rejected. Pair again.');
    }
    const error = new Error(message);
    error.fromServer = true;
    throw error;
  }
  return payload;
}

function friendlyNetworkError(error) {
  // Safari words a failed connection as "Load failed", which tells nobody
  // anything. Say what it actually means.
  const raw = (error && error.message) || String(error);
  if (/load failed|network|fetch/i.test(raw)) {
    return 'Could not reach the PC. Check it is awake and on the same Wi-Fi.';
  }
  return raw;
}

async function api(path, body) {
  const isRead = body === undefined;
  try {
    return await once(path, body);
  } catch (error) {
    // A reply that arrived is the agent's answer; repeating would not change
    // it. Nothing arriving means the connection failed -- Safari keeps
    // sockets alive across a lock or a Wi-Fi handover and finds them dead on
    // the next request. A fresh connection usually just works.
    //
    // Only reads are retried. A POST that was received but whose reply was
    // lost would be applied twice, and a second /api/apply would arm its
    // watchdog against the already-changed state -- so a later revert would
    // restore the wrong thing.
    if (error.fromServer || !isRead) {
      if (!error.fromServer) error.message = friendlyNetworkError(error);
      throw error;
    }

    await new Promise((resolve) => setTimeout(resolve, 400));
    let result;
    try {
      result = await once(path, body);
    } catch (second) {
      if (!second.fromServer) second.message = friendlyNetworkError(second);
      throw second;
    }
    reportToAgent('retried GET ' + path + ' after: ' + (error.message || error));
    return result;
  }
}

/* --------------------------------------------------------------- pairing */

function extractToken(raw) {
  const text = (raw || '').trim();
  if (!text) return null;
  const match = text.match(/[#&?]t=([^&\s]+)/);
  if (match) return decodeURIComponent(match[1]);
  // Allow pasting the bare token.
  if (/^[A-Za-z0-9_-]{16,}$/.test(text)) return text;
  return null;
}

function showPairing(message) {
  el('app').hidden = true;
  el('unreachable').hidden = true;
  el('pair').hidden = false;
  const error = el('pair-error');
  error.hidden = !message;
  error.textContent = message || '';
  clearInterval(pollTimer);
}

async function redeemPin(pin) {
  const response = await fetch('/api/pair', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pin }),
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* non-JSON */ }
  if (!response.ok) {
    throw new Error((payload && payload.error) || ('HTTP ' + response.status));
  }
  return payload.token;
}

async function tryPair(raw) {
  const text = (raw || '').trim();
  const digits = text.replace(/\s|-/g, '');
  const previous = token;

  try {
    let candidate;
    if (/^\d{6}$/.test(digits)) {
      candidate = await redeemPin(digits);
    } else {
      candidate = extractToken(text);
      if (!candidate) {
        showPairing('Enter the 6-digit PIN, or paste the pairing link.');
        return;
      }
    }
    token = candidate;
    await api('/api/state');
    localStorage.setItem(STORE_TOKEN, candidate);
    el('pair').hidden = true;
    el('app').hidden = false;
    start();
  } catch (error) {
    token = previous;
    showPairing(error.message);
  }
}

/* ------------------------------------------------------------ state sync */

function isPresent(monitor) {
  // Older agents did not send the flag; treat its absence as "connected".
  return monitor.present !== false;
}

function settingsFrom(monitor) {
  const rect = monitor.rect || { x: 0, y: 0 };
  const current = monitor.current || { width: 0, height: 0, hz: 0 };
  return {
    x: rect.x,
    y: rect.y,
    width: current.width,
    height: current.height,
    hz: current.hz,
    rotation: monitor.rotation || 0,
    primary: !!monitor.primary,
    enabled: !!monitor.active,
    hdr: !!(monitor.hdr && monitor.hdr.enabled),
    present: isPresent(monitor),
    // '' means extend (its own desktop); otherwise the key of the display
    // it duplicates.
    mirror: monitor.duplicateOf || '',
  };
}

function mirrorTargets(monitor) {
  // Duplicating only works between displays on the same graphics adapter,
  // and only onto one that is actually switched on.
  if (!state) return [];
  return state.monitors.filter((other) => {
    if (other.key === monitor.key) return false;
    if (!isPresent(other)) return false;
    const want = working.get(other.key);
    if (!want || !want.enabled || want.mirror) return false;
    return other.adapterKey && other.adapterKey === monitor.adapterKey;
  });
}

function ago(seconds) {
  if (!seconds) return 'a while ago';
  const delta = Math.max(0, Date.now() / 1000 - seconds);
  if (delta < 90) return 'moments ago';
  if (delta < 5400) return Math.round(delta / 60) + ' minutes ago';
  if (delta < 172800) return Math.round(delta / 3600) + ' hours ago';
  return Math.round(delta / 86400) + ' days ago';
}

function adoptState(payload, keepDraft) {
  state = payload;
  const next = new Map();
  for (const monitor of payload.monitors) {
    next.set(monitor.key, settingsFrom(monitor));
  }
  baseline = next;
  if (!keepDraft || !isDirty()) {
    working = new Map();
    for (const [key, value] of baseline) {
      working.set(key, Object.assign({}, value));
    }
  } else {
    // Drop drafts for monitors that vanished (unplugged while editing).
    for (const key of Array.from(working.keys())) {
      if (!baseline.has(key)) working.delete(key);
    }
    for (const [key, value] of baseline) {
      if (!working.has(key)) working.set(key, Object.assign({}, value));
    }
  }
  render();
}

function monitorsByKey() {
  const map = new Map();
  if (state) for (const monitor of state.monitors) map.set(monitor.key, monitor);
  return map;
}

function changedFields(key) {
  const base = baseline.get(key);
  const want = working.get(key);
  if (!base || !want) return [];
  // "present" tracks the hardware, not the user's intent -- a monitor going
  // to standby mid-edit must not register as an unsaved change.
  return Object.keys(want).filter(
    (field) => field !== 'present' && want[field] !== base[field]);
}

function isDirty() {
  for (const key of working.keys()) {
    if (changedFields(key).length) return true;
  }
  return false;
}

function buildDiff() {
  const enable = {};
  const layout = {};
  const hdr = {};
  const mirror = {};

  for (const key of working.keys()) {
    const base = baseline.get(key);
    const want = working.get(key);
    const fields = changedFields(key);
    if (!fields.length) continue;

    if (fields.includes('enabled')) enable[key] = want.enabled;
    if (fields.includes('hdr') && want.enabled && base.enabled) {
      hdr[key] = want.hdr;
    }
    if (fields.includes('mirror') && want.enabled && base.enabled) {
      mirror[key] = want.mirror;
    }

    // Geometry can only be applied to a monitor that is already lit and is
    // staying lit; anything else is positioned by Windows on the next pass.
    if (base.enabled && want.enabled) {
      const geometry = {};
      for (const field of ['x', 'y', 'width', 'height', 'hz', 'rotation',
                           'primary']) {
        if (fields.includes(field)) geometry[field] = want[field];
      }
      if (Object.keys(geometry).length) {
        // Position is only meaningful as a pair, and the agent needs the
        // primary flag of whichever monitor claims it.
        if ('x' in geometry || 'y' in geometry) {
          geometry.x = want.x;
          geometry.y = want.y;
        }
        layout[key] = geometry;
      }
    }
  }
  return { enable, layout, hdr, mirror };
}

/* ----------------------------------------------------------------- stage */

function activeEntries() {
  const monitors = monitorsByKey();
  const out = [];
  for (const [key, want] of working) {
    if (!want.enabled || !want.present) continue;
    // A duplicated display shares its rectangle with the one it mirrors, so
    // it does not get a box of its own on the map.
    if (want.mirror) continue;
    const monitor = monitors.get(key);
    if (!monitor) continue;
    out.push({ key, monitor, want, box: footprint(want) });
  }
  return out;
}

function stageGeometry(entries) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const entry of entries) {
    minX = Math.min(minX, entry.want.x);
    minY = Math.min(minY, entry.want.y);
    maxX = Math.max(maxX, entry.want.x + entry.box.w);
    maxY = Math.max(maxY, entry.want.y + entry.box.h);
  }
  const stage = el('stage');
  const pad = 14;
  const availW = stage.clientWidth - pad * 2;
  const availH = stage.clientHeight - pad * 2;
  const spanW = Math.max(1, maxX - minX);
  const spanH = Math.max(1, maxY - minY);
  const scale = Math.min(availW / spanW, availH / spanH);
  return {
    scale,
    offsetX: pad + (availW - spanW * scale) / 2 - minX * scale,
    offsetY: pad + (availH - spanH * scale) / 2 - minY * scale,
  };
}

function renderStage() {
  const stage = el('stage');
  const entries = activeEntries();
  stage.textContent = '';

  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'stage-empty';
    empty.textContent = 'No displays enabled';
    stage.appendChild(empty);
    el('stage-warning').hidden = true;
    return;
  }

  const geom = stageGeometry(entries);
  entries.forEach((entry, index) => {
    const box = document.createElement('div');
    box.className = 'mon-box';
    if (entry.want.primary) box.classList.add('primary');
    if (entry.key === selectedKey) box.classList.add('selected');
    box.style.left = (geom.offsetX + entry.want.x * geom.scale) + 'px';
    box.style.top = (geom.offsetY + entry.want.y * geom.scale) + 'px';
    box.style.width = Math.max(26, entry.box.w * geom.scale) + 'px';
    box.style.height = Math.max(22, entry.box.h * geom.scale) + 'px';
    box.dataset.key = entry.key;

    const idx = document.createElement('div');
    idx.className = 'idx';
    idx.textContent = String(index + 1);
    box.appendChild(idx);

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = entry.want.width + ' x ' + entry.want.height;
    box.appendChild(meta);

    attachDrag(box, entry.key, geom);
    stage.appendChild(box);
  });

  const gap = findDisconnected(entries);
  const warning = el('stage-warning');
  warning.hidden = !gap;
  if (gap) {
    warning.textContent =
      'These displays do not touch. Windows will shuffle them together when ' +
      'you apply — drag them edge to edge to control where they land.';
  }
}

function findDisconnected(entries) {
  if (entries.length < 2) return false;
  const touching = (a, b) => {
    const ax2 = a.want.x + a.box.w, ay2 = a.want.y + a.box.h;
    const bx2 = b.want.x + b.box.w, by2 = b.want.y + b.box.h;
    const overlapX = Math.min(ax2, bx2) - Math.max(a.want.x, b.want.x);
    const overlapY = Math.min(ay2, by2) - Math.max(a.want.y, b.want.y);
    if (overlapX > 0 && overlapY > 0) return true;           // overlapping
    if (overlapX > 0 && (ay2 === b.want.y || by2 === a.want.y)) return true;
    if (overlapY > 0 && (ax2 === b.want.x || bx2 === a.want.x)) return true;
    return false;
  };
  const seen = new Set([0]);
  const queue = [0];
  while (queue.length) {
    const current = queue.shift();
    entries.forEach((entry, index) => {
      if (seen.has(index)) return;
      if (touching(entries[current], entry)) { seen.add(index); queue.push(index); }
    });
  }
  return seen.size !== entries.length;
}

function attachDrag(box, key, geom) {
  let startX = 0, startY = 0, originX = 0, originY = 0, moved = false;

  box.addEventListener('pointerdown', (event) => {
    if (busy) return;
    event.preventDefault();
    box.setPointerCapture(event.pointerId);
    box.classList.add('dragging');
    const want = working.get(key);
    startX = event.clientX;
    startY = event.clientY;
    originX = want.x;
    originY = want.y;
    moved = false;
    selectedKey = key;
    renderCards();
  });

  box.addEventListener('pointermove', (event) => {
    if (!box.classList.contains('dragging')) return;
    const dx = (event.clientX - startX) / geom.scale;
    const dy = (event.clientY - startY) / geom.scale;
    if (Math.abs(event.clientX - startX) > 3 ||
        Math.abs(event.clientY - startY) > 3) {
      moved = true;
    }
    const want = working.get(key);
    want.x = Math.round(originX + dx);
    want.y = Math.round(originY + dy);
    box.style.left = (geom.offsetX + want.x * geom.scale) + 'px';
    box.style.top = (geom.offsetY + want.y * geom.scale) + 'px';
  });

  const finish = (event) => {
    if (!box.classList.contains('dragging')) return;
    box.classList.remove('dragging');
    try { box.releasePointerCapture(event.pointerId); } catch (_) { /* gone */ }
    if (moved) {
      snap(key);
      normaliseOrigin();
    }
    render();
  };
  box.addEventListener('pointerup', finish);
  box.addEventListener('pointercancel', finish);
}

function snap(key) {
  const entries = activeEntries();
  const me = entries.find((entry) => entry.key === key);
  if (!me || entries.length < 2) return;

  // Snap threshold in desktop pixels, scaled so it feels the same on screen.
  const geom = stageGeometry(entries);
  const threshold = Math.max(40, 26 / geom.scale);

  const myW = me.box.w, myH = me.box.h;
  let bestX = null, bestY = null;

  for (const other of entries) {
    if (other.key === key) continue;
    const ox = other.want.x, oy = other.want.y;
    const ow = other.box.w, oh = other.box.h;

    const xCandidates = [ox - myW, ox + ow, ox, ox + ow - myW];
    for (const candidate of xCandidates) {
      const distance = Math.abs(candidate - me.want.x);
      if (distance <= threshold && (!bestX || distance < bestX.distance)) {
        bestX = { value: candidate, distance };
      }
    }
    const yCandidates = [oy - myH, oy + oh, oy, oy + oh - myH];
    for (const candidate of yCandidates) {
      const distance = Math.abs(candidate - me.want.y);
      if (distance <= threshold && (!bestY || distance < bestY.distance)) {
        bestY = { value: candidate, distance };
      }
    }
  }

  const want = working.get(key);
  if (bestX) want.x = bestX.value;
  if (bestY) want.y = bestY.value;
}

function normaliseOrigin() {
  // Windows pins the primary display to (0,0); mirror that so the numbers we
  // send match what the PC will actually store.
  const entries = activeEntries();
  const primary = entries.find((entry) => entry.want.primary);
  if (!primary) return;
  const dx = primary.want.x;
  const dy = primary.want.y;
  if (!dx && !dy) return;
  for (const entry of entries) {
    entry.want.x -= dx;
    entry.want.y -= dy;
  }
}

/* ----------------------------------------------------------------- cards */

function makeRow(labelText, noteText, control, wide) {
  const row = document.createElement('div');
  // "wide" rows span both columns of the compact desktop grid.
  row.className = 'row' + (wide ? ' wide' : '');
  const left = document.createElement('div');
  const label = document.createElement('span');
  label.className = 'row-label';
  label.textContent = labelText;
  left.appendChild(label);
  if (noteText) {
    const note = document.createElement('span');
    note.className = 'row-note';
    note.textContent = noteText;
    left.appendChild(note);
  }
  row.appendChild(left);
  row.appendChild(control);
  return row;
}

function makeSwitch(checked, disabled, onToggle) {
  const button = document.createElement('button');
  button.className = 'switch';
  button.type = 'button';
  button.setAttribute('role', 'switch');
  button.setAttribute('aria-checked', String(!!checked));
  button.disabled = !!disabled;
  button.addEventListener('click', () => onToggle(!checked));
  return button;
}

function makeSelect(options, value, disabled, onChange) {
  // Wrapped so the chevron can be a themed mask rather than a background
  // image with one theme's colour baked into it.
  const field = document.createElement('span');
  field.className = 'field';
  const select = document.createElement('select');
  select.disabled = !!disabled;
  for (const option of options) {
    const node = document.createElement('option');
    node.value = option.value;
    node.textContent = option.label;
    if (option.value === value) node.selected = true;
    select.appendChild(node);
  }
  select.addEventListener('change', () => onChange(select.value));
  field.appendChild(select);
  return field;
}

function renderCard(monitor, index) {
  const want = working.get(monitor.key);
  const base = baseline.get(monitor.key);
  const changed = new Set(changedFields(monitor.key));

  const present = isPresent(monitor);
  const card = document.createElement('div');
  card.className = 'card' + (want.enabled && present ? '' : ' off')
    + (present ? '' : ' absent');

  const head = document.createElement('div');
  head.className = 'card-head';

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'card-toggle';

  const chevron = document.createElement('span');
  chevron.className = 'card-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.innerHTML =
    '<svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>';
  toggle.appendChild(chevron);

  const title = document.createElement('div');
  title.className = 'card-title';
  const name = document.createElement('div');
  name.className = 'card-name';
  name.textContent = monitor.label;
  title.appendChild(name);

  const sub = document.createElement('div');
  sub.className = 'card-sub';
  // Summarise what the card contains, so a collapsed one still tells you
  // the things you would otherwise expand to read.
  const bits = [monitor.port];
  if (present && want.enabled && want.width) {
    bits.push(want.width + ' × ' + want.height +
              (want.hz ? ' @ ' + want.hz + ' Hz' : ''));
  } else if (monitor.native) {
    bits.push('native ' + monitor.native.width + ' × ' +
              monitor.native.height);
  }
  sub.textContent = bits.join('  ·  ');
  title.appendChild(sub);
  toggle.appendChild(title);

  const badges = document.createElement('div');
  badges.className = 'badges';
  if (!present) {
    const badge = document.createElement('span');
    badge.className = 'badge absent';
    badge.textContent = 'Not detected';
    badges.appendChild(badge);
  }
  if (want.enabled && present && !want.mirror && index) {
    const idx = document.createElement('span');
    idx.className = 'badge idx';
    idx.textContent = 'Screen ' + index;
    badges.appendChild(idx);
  }
  if (want.mirror && want.enabled && present) {
    const other = monitorsByKey().get(want.mirror);
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = 'Duplicating ' + (other ? other.label : 'another');
    badges.appendChild(badge);
  }
  if (want.primary && want.enabled && present) {
    const badge = document.createElement('span');
    badge.className = 'badge on';
    badge.textContent = 'Primary';
    badges.appendChild(badge);
  }
  if (!want.enabled && present) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = 'Disabled';
    badges.appendChild(badge);
  }
  if (monitor.hdr && monitor.hdr.enabled) {
    const badge = document.createElement('span');
    badge.className = 'badge on';
    badge.textContent = 'HDR ' + (monitor.hdr.bitsPerChannel || '') +
      (monitor.hdr.bitsPerChannel ? '-bit' : '');
    badges.appendChild(badge);
  }
  // Promoting a display sits in the badge strip rather than taking a whole
  // row of its own: the Primary badge beside it already says which is which.
  if (present && want.enabled && !want.primary && !want.mirror) {
    const promote = document.createElement('button');
    promote.type = 'button';
    promote.className = 'badge action';
    promote.textContent = 'Make primary';
    promote.addEventListener('click', () => {
      for (const settings of working.values()) settings.primary = false;
      want.primary = true;
      normaliseOrigin();
      render();
    });
    badges.appendChild(promote);
  }
  head.appendChild(toggle);

  head.appendChild(makeSwitch(want.enabled && present, !present, (next) => {
    if (!next && activeEntries().length <= 1) {
      toast('You need at least one display switched on.', true);
      return;
    }
    want.enabled = next;
    if (next && !want.width && monitor.native) {
      want.width = monitor.native.width;
      want.height = monitor.native.height;
    }
    if (!next && want.primary) {
      // Hand primary to whichever other display is still on.
      const other = activeEntries().find((entry) => entry.key !== monitor.key);
      if (other) {
        for (const settings of working.values()) settings.primary = false;
        other.want.primary = true;
      }
    }
    render();
  }));
  card.appendChild(head);
  // Badges sit outside the toggle: one of them is itself a button, and a
  // button inside a button is invalid and unreachable by keyboard.
  card.appendChild(badges);

  const rows = document.createElement('div');
  rows.className = 'rows';
  rows.id = 'rows-' + monitor.key;

  const shut = isCollapsed(monitor);
  card.classList.toggle('collapsed', shut);
  toggle.setAttribute('aria-expanded', String(!shut));
  toggle.setAttribute('aria-controls', rows.id);
  toggle.addEventListener('click', () => {
    setCollapsed(monitor.key, !isCollapsed(monitor));
    render();
  });

  if (!present) {
    // Windows drops a monitor from its display config entirely once the
    // output goes quiet, so there is nothing to switch on until it is back.
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent =
      'Windows cannot see this display, so there is nothing to switch on '
      + 'yet. Check it is powered on and set to the input it is plugged '
      + 'into — a TV on standby or showing another source disappears from '
      + 'Windows completely. It will come back here on its own.';
    rows.appendChild(note);

    const meta = document.createElement('div');
    meta.className = 'empty subtle';
    meta.textContent = 'Last seen ' + ago(monitor.lastSeen) + '.';
    rows.appendChild(meta);

    const forget = document.createElement('button');
    forget.className = 'linkbtn';
    forget.type = 'button';
    forget.textContent = 'Forget this display';
    forget.addEventListener('click', () => guard(async () => {
      adoptState(await api('/api/forget', { key: monitor.key }), false);
      toast('Removed from the list.');
    }));
    rows.appendChild(makeRow('Gone for good?', null, forget, true));

    card.appendChild(rows);
    return card;
  }

  if (!want.enabled) {
    const note = document.createElement('div');
    note.className = 'empty';
    note.textContent = base.enabled
      ? 'Turn it back on to change resolution, refresh rate or HDR.'
      : 'Switch it on and apply, then its modes will appear here.';
    rows.appendChild(note);
    card.appendChild(rows);
    return card;
  }

  // --- resolution -----------------------------------------------------
  const modes = monitor.modes || [];
  const byRes = new Map();
  for (const mode of modes) {
    const key = resKey(mode);
    if (!byRes.has(key)) byRes.set(key, []);
    byRes.get(key).push(mode.hz);
  }
  const resOptions = Array.from(byRes.keys()).map((key) => {
    const [width, height] = key.split('x').map(Number);
    const isNative = monitor.native && monitor.native.width === width &&
      monitor.native.height === height;
    return {
      value: key,
      label: key.replace('x', ' x ') + (isNative ? '  (native)' : ''),
      width, height,
    };
  });

  const currentRes = want.width + 'x' + want.height;
  if (!byRes.has(currentRes) && want.width) {
    resOptions.unshift({ value: currentRes,
                         label: currentRes.replace('x', ' x ') });
  }

  const resSelect = makeSelect(resOptions, currentRes, !resOptions.length,
    (value) => {
      const [width, height] = value.split('x').map(Number);
      want.width = width;
      want.height = height;
      const rates = byRes.get(value) || [];
      if (rates.length && !rates.includes(want.hz)) {
        want.hz = Math.max.apply(null, rates);
      }
      render();
    });
  if (changed.has('width') || changed.has('height')) {
    resSelect.classList.add('dirty');
  }
  rows.appendChild(makeRow('Resolution', null, resSelect));

  // --- refresh rate ---------------------------------------------------
  const rates = (byRes.get(currentRes) || []).slice().sort((a, b) => b - a);
  const rateSet = new Set(rates);
  const rateOptions = rates.map((hz) => ({
    value: String(hz), label: hzLabel(hz, rateSet),
  }));
  if (!rateSet.has(want.hz) && want.hz) {
    rateOptions.unshift({ value: String(want.hz), label: want.hz + ' Hz' });
  }
  const exact = monitor.current && monitor.current.exactHz;
  const rateNote = (!changed.has('hz') && exact &&
                    Math.abs(exact - want.hz) > 0.005)
    ? 'Running at ' + exact + ' Hz'
    : null;
  const rateSelect = makeSelect(rateOptions, String(want.hz),
    !rateOptions.length, (value) => { want.hz = Number(value); render(); });
  if (changed.has('hz')) rateSelect.classList.add('dirty');
  rows.appendChild(makeRow('Refresh rate', rateNote, rateSelect));

  // --- extend or duplicate --------------------------------------------
  const targets = mirrorTargets(monitor);
  if (targets.length || want.mirror) {
    const labelFor = (k) => {
      const other = monitorsByKey().get(k);
      return other ? other.label : 'another display';
    };
    const options = [{ value: '', label: 'Extend  (own desktop)' }];
    for (const other of targets) {
      options.push({ value: other.key, label: 'Duplicate ' + other.label });
    }
    if (want.mirror && !targets.some((t) => t.key === want.mirror)) {
      options.push({ value: want.mirror,
                     label: 'Duplicate ' + labelFor(want.mirror) });
    }
    const mirrorSelect = makeSelect(options, want.mirror, false, (value) => {
      want.mirror = value;
      if (value) {
        // A duplicated display has no desktop position of its own, and
        // cannot be the primary.
        want.primary = false;
        if (!activeEntries().some((entry) => entry.want.primary)) {
          const first = activeEntries()[0];
          if (first) first.want.primary = true;
        }
      }
      render();
    });
    if (changed.has('mirror')) mirrorSelect.classList.add('dirty');
    rows.appendChild(makeRow('Desktop', null, mirrorSelect));
  }

  // --- rotation -------------------------------------------------------
  const rotSelect = makeSelect(
    [{ value: '0', label: 'Landscape' },
     { value: '90', label: 'Portrait' },
     { value: '180', label: 'Landscape (flipped)' },
     { value: '270', label: 'Portrait (flipped)' }],
    String(want.rotation), false,
    (value) => { want.rotation = Number(value); render(); });
  if (changed.has('rotation')) rotSelect.classList.add('dirty');
  rows.appendChild(makeRow('Orientation', null, rotSelect));

  // --- HDR ------------------------------------------------------------
  const hdr = monitor.hdr || {};
  let hdrNote = null;
  if (!hdr.supported) {
    hdrNote = 'Not available on this connection';
  } else if (hdr.forceDisabled) {
    hdrNote = 'Blocked by Windows (driver or cable bandwidth)';
  } else if (hdr.enabled) {
    const parts = [];
    if (hdr.encoding) parts.push(hdr.encoding);
    if (hdr.bitsPerChannel) parts.push(hdr.bitsPerChannel + '-bit');
    if (monitor.sdrWhiteNits) parts.push('SDR white ' + monitor.sdrWhiteNits + ' nits');
    hdrNote = parts.join('  ·  ') || null;
  }
  const hdrSwitch = makeSwitch(want.hdr,
    !hdr.supported || hdr.forceDisabled || !base.enabled,
    (next) => { want.hdr = next; render(); });
  rows.appendChild(makeRow('HDR', hdrNote, hdrSwitch));

  card.appendChild(rows);
  return card;
}

function renderCards() {
  const container = el('monitors');
  container.textContent = '';
  if (!state) return;

  const order = activeEntries().map((entry) => entry.key);
  let index = 0;
  for (const monitor of state.monitors) {
    const position = order.indexOf(monitor.key);
    index = position >= 0 ? position + 1 : 0;
    container.appendChild(renderCard(monitor, index));
  }
}

/* -------------------------------------------------------------- profiles */

function renderProfiles() {
  const container = el('profiles');
  container.textContent = '';
  const names = (state && state.profiles) || [];
  if (!names.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No saved setups yet. Get things how you like them, ' +
      'then save.';
    container.appendChild(empty);
    return;
  }
  for (const name of names) {
    const row = document.createElement('div');
    row.className = 'profile';
    const label = document.createElement('span');
    label.className = 'profile-name';
    label.textContent = name;
    row.appendChild(label);

    const actions = document.createElement('div');
    actions.className = 'profile-actions';

    const apply = document.createElement('button');
    apply.className = 'ghost small';
    apply.textContent = 'Apply';
    apply.addEventListener('click', () => runProfile(name));
    actions.appendChild(apply);

    const remove = document.createElement('button');
    remove.className = 'ghost small danger';
    remove.textContent = 'Delete';
    remove.addEventListener('click', async () => {
      if (!confirm('Delete the "' + name + '" setup?')) return;
      await guard(async () => {
        adoptState(await api('/api/profile/delete', { name }));
        toast('Deleted.');
      });
    });
    actions.appendChild(remove);

    row.appendChild(actions);
    container.appendChild(row);
  }
}

/* ---------------------------------------------------------------- render */

function render() {
  if (!state) return;
  el('host-name').textContent = state.host || 'PC';
  renderStage();
  renderCards();
  renderProfiles();

  const expanded = anyExpanded();
  const collapseAll = el('collapse-all');
  collapseAll.textContent = expanded ? 'Collapse all' : 'Expand all';
  collapseAll.hidden = !state.monitors.length;

  const diff = buildDiff();
  // Count every kind of pending change, or one made only through a section
  // not listed here would leave Apply hidden and look like nothing happened.
  const changedKeys = new Set();
  for (const section of [diff.enable, diff.layout, diff.hdr, diff.mirror]) {
    for (const key of Object.keys(section)) changedKeys.add(key);
  }
  const count = changedKeys.size;
  const bar = el('actionbar');
  bar.hidden = count === 0;
  el('change-summary').textContent =
    count === 1 ? '1 display changed' : count + ' displays changed';
  el('apply').disabled = busy;

  const banner = el('banner');
  const revert = state.lastRevert;
  if (revert && Date.now() / 1000 - revert.at < 30) {
    banner.hidden = false;
    banner.classList.toggle('bad', !!(revert.errors && revert.errors.length));
    banner.textContent = (revert.errors && revert.errors.length)
      ? 'Rolled back, but some steps failed: ' + revert.errors.join('; ')
      : 'No confirmation came through, so the previous setup was restored.';
  } else {
    banner.hidden = true;
  }
}

/* --------------------------------------------------------------- actions */

async function guard(work) {
  if (busy) return;
  busy = true;
  el('apply').disabled = true;
  try {
    await work();
  } catch (error) {
    toast(error.message, true);
  } finally {
    busy = false;
    el('apply').disabled = false;
    render();
  }
}

function startCountdown(pending) {
  clearInterval(countdownTimer);
  const overlay = el('confirm');
  overlay.hidden = false;
  overlay.dataset.id = pending.id;
  let left = pending.secondsLeft;
  el('countdown').textContent = String(left);
  countdownTimer = setInterval(() => {
    left -= 1;
    el('countdown').textContent = String(Math.max(0, left));
    if (left <= 0) {
      clearInterval(countdownTimer);
      overlay.hidden = true;
      refresh();
    }
  }, 1000);
}

async function applyChanges() {
  const diff = buildDiff();
  await guard(async () => {
    const result = await api('/api/apply', diff);
    adoptState(result, false);
    if (result.pending) {
      startCountdown(result.pending);
    } else {
      toast('Applied.');
    }
  });
}

async function runProfile(name) {
  await guard(async () => {
    const result = await api('/api/profile/apply', { name });
    adoptState(result, false);
    if (result.pending) startCountdown(result.pending);
    else toast('Applied "' + name + '".');
  });
}

// The build this page was served as. The agent stamps it in when it serves
// index.html, so a cached copy carries the build it was cached at.
const APP_BUILD = (() => {
  const meta = document.querySelector('meta[name="monitorpad-build"]');
  return meta ? meta.content : '';
})();

async function refreshIfOutdated(build) {
  // The interface is cached hard so it opens away from home. That must not
  // mean running an old copy once back in range, so when the agent reports a
  // build we do not have, pull a fresh page in and restart.
  if (!build || !APP_BUILD || build === APP_BUILD) return false;
  try {
    await fetch('index.html', { cache: 'reload' });
  } catch (_) {
    return false;  // no point reloading into the same cached copy
  }
  location.reload();
  return true;
}

function showUnreachable(show) {
  const screen = el('unreachable');
  if (show) {
    const host = localStorage.getItem(STORE_HOST);
    el('unreachable-host').textContent = host || 'your PC';
    el('app').hidden = true;
    screen.hidden = false;
  } else {
    screen.hidden = true;
    if (token) el('app').hidden = false;
  }
}

async function refresh() {
  try {
    adoptState(await api('/api/state'), true);
    document.body.classList.remove('offline');
    showUnreachable(false);
    if (state && state.host) localStorage.setItem(STORE_HOST, state.host);
    if (await refreshIfOutdated(state && state.build)) return;
  } catch (error) {
    // Only say something the first time. A polling loop against an agent
    // that has stopped would otherwise fire a toast every few seconds; the
    // dot beside the PC name carries the state from then on.
    const wasOnline = !document.body.classList.contains('offline');
    document.body.classList.add('offline');
    // Never having reached the agent means there is nothing to show behind a
    // toast, so say plainly what is wrong instead of an empty interface.
    if (state === null) {
      showUnreachable(true);
    } else if (token && wasOnline) {
      toast(error.message, true);
    }
  }
}

function start() {
  clearInterval(pollTimer);
  refresh();
  pollTimer = setInterval(() => {
    if (busy || !el('confirm').hidden) return;
    refresh();
  }, POLL_MS);
}

/* ------------------------------------------------------------------ wire */

el('pair-go').addEventListener('click', () => tryPair(el('pair-input').value));
el('pair-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') tryPair(el('pair-input').value);
});

el('refresh').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.classList.add('spinning');
  await refresh();
  setTimeout(() => button.classList.remove('spinning'), 400);
});

el('identify').addEventListener('click', () => guard(async () => {
  await api('/api/identify', {});
  toast('Check your screens.');
}));

el('apply').addEventListener('click', applyChanges);

el('unreachable-retry').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = 'Trying...';
  await refresh();
  button.disabled = false;
  button.textContent = 'Try again';
});

el('collapse-all').addEventListener('click', () => {
  setAllCollapsed(anyExpanded());
  render();
});

el('discard').addEventListener('click', () => {
  working = new Map();
  for (const [key, value] of baseline) {
    working.set(key, Object.assign({}, value));
  }
  render();
});

el('confirm-keep').addEventListener('click', () => guard(async () => {
  const id = el('confirm').dataset.id;
  clearInterval(countdownTimer);
  el('confirm').hidden = true;
  adoptState(await api('/api/confirm', { id }), false);
  toast('Settings kept.');
}));

el('confirm-revert').addEventListener('click', () => guard(async () => {
  const id = el('confirm').dataset.id;
  clearInterval(countdownTimer);
  el('confirm').hidden = true;
  adoptState(await api('/api/revert', { id }), false);
  toast('Reverted.');
}));

el('profile-save').addEventListener('click', () => guard(async () => {
  const input = el('profile-name');
  const name = input.value.trim();
  if (!name) { toast('Give the setup a name first.', true); return; }
  adoptState(await api('/api/profile/save', { name }), true);
  input.value = '';
  toast('Saved "' + name + '".');
}));

window.addEventListener('resize', () => { if (state) renderStage(); });

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && token) refresh();
});

/* ------------------------------------------------------------------ boot */

(function boot() {
  const fromUrl = extractToken(location.hash);
  if (fromUrl) {
    localStorage.setItem(STORE_TOKEN, fromUrl);
    history.replaceState(null, '', location.pathname);
  }
  token = localStorage.getItem(STORE_TOKEN);
  if (!token) {
    showPairing(null);
    return;
  }
  el('app').hidden = false;
  start();

  // No service worker on purpose. Over the LAN the app is served from a
  // plain-HTTP address, which is not a secure context, so one could never
  // register on the phone at all -- and caching the shell would only risk
  // showing a stale interface. The app is useless without the agent
  // reachable anyway, so there is nothing worth having offline.
})();
