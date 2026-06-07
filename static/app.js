/**
 * app.js — MDT frontend
 * WebSocket clients (screen + logs + state), input handling, control buttons.
 * Vanilla JS only — no frameworks, no build step.
 */

'use strict';

// ── Constants ─────────────────────────────────────────────────────────────────
const WS_RECONNECT_BASE_MS = 1500;
const WS_RECONNECT_MAX_MS  = 15000;
const MAX_LOG_LINES        = 1000; // DOM line limit per device (trim oldest)
const TEST_LABELS = {
  launch: 'Launch',
  crash_detection: 'Crash',
  anr_detection: 'ANR',
  permission_audit: 'Permissions',
  activity_smoke: 'Activity',
  memory_baseline: 'Memory',
  network_connectivity: 'Network',
  ui_responsiveness: 'UI Tap',
};
const TAP_THRESHOLD         = 0.004;

// ── State ─────────────────────────────────────────────────────────────────────
/** @type {Map<number, DeviceUI>} index → DeviceUI */
const devices = new Map();
let globalConfig = {};
let folderPicker = null;
let activityPanel = null;

function downloadBlob(blob, filename) {
  if (!blob) return;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

class H264Player {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.reset();
  }

  reset() {
    try { if (this.decoder && this.decoder.state !== 'closed') this.decoder.close(); } catch (e) {}
    this.decoder = null; this.configured = false; this.gotKey = false;
    this.sps = null; this.pps = null;
    this.buf = new Uint8Array(0);
    this.pendingNALs = []; this.currentAU = []; this.seenVCL = false; this.ts = 0;
  }

  push(arrayBuffer) {
    const inc = new Uint8Array(arrayBuffer);
    const merged = new Uint8Array(this.buf.length + inc.length);
    merged.set(this.buf, 0); merged.set(inc, this.buf.length);
    this.buf = merged;
    this._parse();
  }

  _parse() {
    const d = this.buf;
    const starts = [];
    let p = 0;
    while (p + 3 < d.length) {
      if (d[p] === 0 && d[p + 1] === 0 && d[p + 2] === 1) {
        starts.push(p); p += 3; continue;
      }
      if (p + 3 < d.length && d[p] === 0 && d[p + 1] === 0 && d[p + 2] === 0 && d[p + 3] === 1) {
        starts.push(p); p += 4; continue;
      }
      p += 1;
    }
    if (!starts.length) return;
    if (starts[0] > 0) {
      this.buf = d.slice(starts[0]);
      this._parse();
      return;
    }

    for (let i = 0; i < starts.length - 1; i++) {
      const start = starts[i];
      const next = starts[i + 1];
      const prefixLen = (d[start + 2] === 1) ? 3 : 4;
      const nal = d.slice(start + prefixLen, next);
      this._handleNAL(nal);
    }

    const lastStart = starts[starts.length - 1];
    this.buf = d.slice(lastStart);
  }

  _handleNAL(nal) {
    if (!nal.length) return;
    const type = nal[0] & 0x1f;

    if (type === 7) { this.sps = nal.slice(); if (this.pps) this._configure(); return; }
    if (type === 8) { this.pps = nal.slice(); if (this.sps) this._configure(); return; }

    if (type === 9) { this._flushAU(); return; }

    if (type >= 1 && type <= 5) {
      if (this.currentAU && this.currentAU.length) {
        const hasVcl = this.currentAU.some(n => {
          const t = n[0] & 0x1f;
          return t >= 1 && t <= 5;
        });
        if (hasVcl) this._flushAU();
      }
      this.currentAU = this.currentAU || [];
      this.currentAU.push(nal.slice());
      this.seenVCL = true;
      return;
    }

    if (this.currentAU && this.currentAU.length) {
      this.currentAU.push(nal.slice());
    }
  }

  _flushAU() {
    if (!this.currentAU || !this.currentAU.length) return;
    const au = this.currentAU;
    this.currentAU = [];
    this._emitAU(au);
  }

  _emitAU(nals) {
    const isKey = nals.some(n => (n[0] & 0x1f) === 5);
    if (isKey) this.gotKey = true;
    if (!this.gotKey && !isKey) return;
    if (!this.configured) this._configure();
    if (!this.decoder || this.decoder.state !== 'configured') return;

    let total = 0;
    for (const nal of nals) total += 4 + nal.length;
    const out = new Uint8Array(total);
    let o = 0;
    for (const nal of nals) {
      out[o++] = (nal.length >>> 24) & 0xff;
      out[o++] = (nal.length >>> 16) & 0xff;
      out[o++] = (nal.length >>> 8) & 0xff;
      out[o++] = nal.length & 0xff;
      out.set(nal, o);
      o += nal.length;
    }

    const chunk = new EncodedVideoChunk({ type: isKey ? 'key' : 'delta', timestamp: this.ts, data: out });
    this.ts += 33333;
    try {
      this.decoder.decode(chunk);
    } catch (e) {
      this.reset();
    }
  }

  _configure() {
    const sps = this.sps, pps = this.pps;
    if (!sps || !pps) return;
    const codec = 'avc1.' + [sps[1], sps[2], sps[3]].map(b => b.toString(16).padStart(2, '0')).join('');
    const avcc = new Uint8Array(11 + sps.length + pps.length);
    let o = 0;
    avcc[o++] = 1; avcc[o++] = sps[1]; avcc[o++] = sps[2]; avcc[o++] = sps[3];
    avcc[o++] = 0xff; avcc[o++] = 0xe1;
    avcc[o++] = (sps.length >> 8) & 0xff; avcc[o++] = sps.length & 0xff;
    avcc.set(sps, o); o += sps.length;
    avcc[o++] = 1;
    avcc[o++] = (pps.length >> 8) & 0xff; avcc[o++] = pps.length & 0xff;
    avcc.set(pps, o);
    this.decoder = new VideoDecoder({
      output: (frame) => {
        if (this.canvas.width !== frame.displayWidth) this.canvas.width = frame.displayWidth;
        if (this.canvas.height !== frame.displayHeight) this.canvas.height = frame.displayHeight;
        this.ctx.drawImage(frame, 0, 0);
        const overlay = this.canvas.closest('.phone-viewport')?.querySelector('.screen-status-overlay');
        if (overlay) overlay.classList.add('hidden');
        frame.close();
      },
      error: () => this.reset(),
    });
    try {
      this.decoder.configure({ codec, description: avcc, optimizeForLatency: true });
      this.configured = true;
      this.currentAU = [];
    } catch (e) {
      this.reset();
    }
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);

async function init() {
  folderPicker = new FolderPicker();
  activityPanel = new ActivityPanel();
  activityPanel.connect();

  try {
    const [cfgRes, devRes] = await Promise.all([
      fetch('/api/config'),
      fetch('/api/devices'),
    ]);
    globalConfig = await cfgRes.json();
    const devList = await devRes.json();

    const logNote = document.getElementById('log-dir-note');
    logNote.querySelector('.chip-text').textContent = _shortPath(globalConfig.log_dir);
    logNote.title = globalConfig.log_dir;
    const apkNote = document.getElementById('apk-dir-note');
    apkNote.querySelector('.chip-text').textContent = _shortPath(globalConfig.apk_dir);
    apkNote.title = globalConfig.apk_dir;

    setupTopBarButtons();

    if (devList.every(d => !d.active) && (globalConfig.apk_count || 0) === 0) {
      renderEmptyState();
    } else {
      await syncDevicesFromServer(devList);
      updateGlobalStatus();
    }
  } catch (e) {
    showToast('Failed to connect to MDT server. Is it running?', 'error');
  }

  document.body.classList.remove('page-loading');
  document.body.classList.add('page-ready');

  setupResponsiveLayout();
  setInterval(pollDevices, 4000);
}

/** Debounced resize + ResizeObserver so canvas tap coords stay accurate after layout changes. */
function setupResponsiveLayout() {
  let resizeTimer = null;
  const onResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      devices.forEach(d => d._onLayoutResize?.());
    }, 100);
  };
  window.addEventListener('resize', onResize, { passive: true });
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(onResize);
    const grid = document.getElementById('main-grid');
    if (grid) ro.observe(grid);
  }
}

async function pollDevices() {
  try {
    const res  = await fetch('/api/devices');
    const list = await res.json();
    if (list.every(d => !d.active) && (globalConfig.apk_count || 0) === 0) {
      if (!document.getElementById('empty-state')) {
        devices.forEach((ui, idx) => {
          ui.destroy?.();
          devices.delete(idx);
        });
        renderEmptyState();
      }
      return;
    }
    const empty = document.getElementById('empty-state');
    if (empty) empty.remove();
    await syncDevicesFromServer(list);
  } catch (_) {}
}

async function syncDevicesFromServer(list) {
  if (!list) {
    const res = await fetch('/api/devices');
    list = await res.json();
  }

  const cfgRes = await fetch('/api/config');
  globalConfig = await cfgRes.json();

  const indices = new Set(list.map(d => d.index));

  for (const [idx, ui] of devices) {
    if (!indices.has(idx)) {
      ui.destroy?.();
      devices.delete(idx);
    }
  }

  list.forEach(ds => {
    if (!ds.active) {
      if (devices.has(ds.index)) {
        const ui = devices.get(ds.index);
        if (ui.isClosedSlot) return;
        ui.destroy?.();
        devices.delete(ds.index);
      }
      createClosedSlotCard(ds);
      return;
    }

    if (devices.has(ds.index)) {
      const ui = devices.get(ds.index);
      if (ui.isClosedSlot) {
        ui.destroy?.();
        devices.delete(ds.index);
        createDeviceCard(ds);
      } else {
        ui.updateFromServer?.(ds);
      }
    } else {
      createDeviceCard(ds);
    }
  });

  updateGlobalStatus();
  updateAddDeviceHint(list);
}

// ── Empty state ───────────────────────────────────────────────────────────────
function renderEmptyState() {
  const grid = document.getElementById('main-grid');
  grid.innerHTML = `
    <div id="empty-state">
      <div class="empty-orb">
        <div class="empty-orb-inner">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="5" y="2" width="14" height="20" rx="2"/>
            <line x1="12" y1="18" x2="12.01" y2="18"/>
          </svg>
        </div>
      </div>
      <h2>No APKs detected</h2>
      <p>Browse to a folder containing up to <strong>2 APK files</strong>, or drop APKs into your APK folder. MDT will detect them automatically.</p>
      <div class="empty-actions">
        <button id="empty-browse-btn" class="btn btn-primary">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h5l2 2h11v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>
          Browse APK Folder
        </button>
      </div>
      <div class="drop-hint">Server is running and waiting for APKs</div>
    </div>`;
  const pill = document.getElementById('global-status');
  pill.querySelector('.status-label').textContent = 'No APKs';
  pill.className = 'status-pill';
  document.getElementById('empty-browse-btn').addEventListener('click', () => {
    folderPicker.open({ mode: 'folder', startPath: globalConfig.apk_dir, onSelect: setApkFolder });
  });
}

// ── Top-bar button wiring ─────────────────────────────────────────────────────
async function setApkFolder(path) {
  const res = await fetch('/api/apk_dir', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showToast(err.detail || 'Failed to update APK folder', 'error');
    return false;
  }
  const data = await res.json();
  globalConfig.apk_dir = data.apk_dir;
  const note = document.getElementById('apk-dir-note');
  note.querySelector('.chip-text').textContent = _shortPath(data.apk_dir);
  note.title = data.apk_dir;
  showToast('APK folder updated — resyncing devices…', 'success');

  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();

  if (data.devices) {
    globalConfig.apk_count = data.devices.filter(d => d.apk_path).length;
    await syncDevicesFromServer(data.devices);
  } else {
    await syncDevicesFromServer();
  }
  return true;
}

function setupTopBarButtons() {
  document.getElementById('btn-set-apk-dir').addEventListener('click', () => {
    folderPicker.open({
      mode: 'folder',
      startPath: globalConfig.apk_dir,
      onSelect: setApkFolder,
    });
  });

  document.getElementById('btn-toggle-activity').addEventListener('click', () => {
    activityPanel.toggleCollapse();
  });

  document.getElementById('btn-restart-all').addEventListener('click', async () => {
    await fetch('/api/restart_all', { method: 'POST' });
    showToast('Restarting all apps…');
  });
  document.getElementById('btn-reinstall-all').addEventListener('click', async () => {
    await fetch('/api/reinstall_all', { method: 'POST' });
    showToast('Reinstalling all apps…');
  });
  document.getElementById('btn-clear-logs').addEventListener('click', () => {
    devices.forEach(d => d.clearLogPane());
    showToast('Log panes cleared (saved trails untouched)');
  });
}

// ── Global status ─────────────────────────────────────────────────────────────
function updateGlobalStatus() {
  const pill = document.getElementById('global-status');
  const label = pill.querySelector('.status-label');
  const active = [...devices.values()].filter(d => !d.isClosedSlot);
  if (!active.length) {
    const hasApks = (globalConfig.apk_count || 0) > 0;
    label.textContent = hasApks ? 'No active devices' : 'No APKs';
    pill.className = 'status-pill';
    return;
  }
  const states = active.map(d => d.state);
  if (states.every(s => s === 'running')) {
    label.textContent = `${states.length} device${states.length > 1 ? 's' : ''} running`;
    pill.className = 'status-pill running';
  } else if (states.some(s => s === 'error')) {
    label.textContent = 'Error on one or more devices';
    pill.className = 'status-pill error';
  } else {
    const booting = states.filter(s => s === 'booting' || s === 'installing').length;
    label.textContent = booting ? `${booting} device${booting > 1 ? 's' : ''} starting…` : 'Idle';
    pill.className = booting ? 'status-pill booting' : 'status-pill';
  }
}

function updateAddDeviceHint(list) {
  const activeCount = list.filter(d => d.active).length;
  const canAdd = activeCount < (globalConfig.max_devices || 2) && (globalConfig.apk_count || 0) > activeCount;
  document.querySelectorAll('.closed-slot-card .btn-add-device').forEach(btn => {
    btn.disabled = !canAdd;
    btn.title = canAdd ? 'Start emulator in this slot' : 'No APK available or max devices reached';
  });
}

// ── Device card factory ───────────────────────────────────────────────────────

/**
 * All per-device UI and WebSocket state lives in this class.
 */
class DeviceUI {
  constructor(ds) {
    this.index   = ds.index;
    this.state   = ds.state || 'idle';
    this.ds      = ds;
    this.isClosedSlot = false;
    this.ws      = null;
    this.wsRetry = 0;

    // Test state
    this.testResults = [];
    this.testRunning = false;
    this.testPollTimer = null;
    this.logFilter    = 'all';
    this.logAutoScroll = true;
    this.logLines     = [];   // {el, color} for filter toggling
    this.lastStateLogKey = '';

    // Screen drag state
    this.dragStart = null;
    this.pendingFrame = null;
    this.frameInFlight = false;
    this.videoDecoderSupported = ('VideoDecoder' in window);

    // Build DOM
    this._buildCard();
    this._connectWS();
  }

  // ── DOM construction ───────────────────────────────────────────────────────
  _buildCard() {
    const grid = document.getElementById('main-grid');

    this.card = document.createElement('div');
    this.card.className = `device-card state-${this.state}`;
    this.card.id = `device-card-${this.index}`;

    const apkName = this.ds.apk_path
      ? this.ds.apk_path.split(/[\\/]/).pop()
      : `Device ${this.index}`;
    const pkg     = this.ds.package || '—';
    const serial  = this.ds.serial  || '—';

    this.card.innerHTML = `
      <!-- Header -->
      <div class="card-header">
        <div class="card-header-row1">
          <span class="ws-dot disconnected" id="ws-dot-${this.index}" title="WebSocket"></span>
          <span class="apk-name" id="apk-name-${this.index}" title="${apkName}">${apkName}</span>
          <span class="state-badge badge-${this.state}" id="badge-${this.index}">${this.state}</span>
          <button class="btn btn-icon btn-sm btn-close-device" id="btn-close-${this.index}" title="Close device slot" aria-label="Close device">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
        <div class="card-meta">
          <span class="meta-item" id="pkg-${this.index}" title="${pkg}">${_truncate(pkg, 32)}</span>
          <span class="meta-item" id="serial-${this.index}">${serial}</span>
          <div class="counters">
            <span class="counter ok"    id="cnt-ok-${this.index}">   <span class="counter-dot"></span><span id="cnt-ok-val-${this.index}">0</span></span>
            <span class="counter warn"  id="cnt-warn-${this.index}"> <span class="counter-dot"></span><span id="cnt-warn-val-${this.index}">0</span></span>
            <span class="counter error" id="cnt-err-${this.index}">  <span class="counter-dot"></span><span id="cnt-err-val-${this.index}">0</span></span>
          </div>
        </div>
      </div>

      <!-- View toggle -->
      <div class="view-toggle" id="toggle-${this.index}">
        <button class="active"  data-view="screen" id="vbtn-screen-${this.index}">Screen</button>
        <button                 data-view="logs"   id="vbtn-logs-${this.index}">Logs</button>
        <button                 data-view="tests"  id="vbtn-tests-${this.index}">Tests</button>
        <button                 data-view="split"  id="vbtn-split-${this.index}">Split</button>
      </div>

      <!-- Body: content area swapped by view toggle -->
      <div class="card-body" id="card-body-${this.index}"></div>
    `;

    grid.appendChild(this.card);

    // Build the three view panes (hidden until toggled)
    this._buildScreenPane();
    this._buildLogPane();
    this._buildTestPane();
    this._setView('screen');

    // View toggle wiring
    this.card.querySelectorAll('.view-toggle button').forEach(btn => {
      btn.addEventListener('click', () => {
        this._setView(btn.dataset.view);
        this.card.querySelectorAll('.view-toggle button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });

    this.card.querySelector(`#btn-close-${this.index}`)?.addEventListener('click', () => this._closeDevice());
  }

  async _closeDevice() {
    try {
      const res = await fetch(`/api/device/${this.index}/close`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      showToast(`Device ${this.index} closed`);
      await syncDevicesFromServer();
    } catch (e) {
      showToast(`Failed to close device ${this.index}: ${e.message}`, 'error');
    }
  }

  updateFromServer(ds) {
    this.ds = ds;
    const apkName = ds.apk_path
      ? ds.apk_path.split(/[\\/]/).pop()
      : `Device ${ds.index}`;
    const apkEl = document.getElementById(`apk-name-${this.index}`);
    if (apkEl) {
      apkEl.textContent = apkName;
      apkEl.title = apkName;
    }
    if (ds.package) {
      const el = document.getElementById(`pkg-${this.index}`);
      if (el) { el.textContent = _truncate(ds.package, 32); el.title = ds.package; }
    }
  }

  destroy() {
    if (this.testPollTimer) clearInterval(this.testPollTimer);
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close();
      this.ws = null;
    }
    if (this.player) this.player.reset();
    this.card?.remove();
  }

  _buildScreenPane() {
    // Screen pane (shared between 'screen' and 'split' views)
    this.screenPane = document.createElement('div');
    this.screenPane.className = 'screen-pane';
    this.screenPane.innerHTML = `
      <div class="screen-frame" id="sframe-${this.index}">
        <div class="phone-shell">
          <div class="phone-viewport">
            <canvas class="screen-canvas" id="scanvas-${this.index}" aria-label="Device screen"></canvas>
            <div class="webcodecs-notice" id="snotice-${this.index}">Use Chrome or Edge — WebCodecs required</div>
            <div class="screen-overlay" id="soverlay-${this.index}"></div>
            <div class="screen-status-overlay" id="sstatus-${this.index}">
              <div class="boot-spinner"></div>
              <div class="screen-status-text" id="sstatus-txt-${this.index}">Starting…</div>
            </div>
          </div>
        </div>
      </div>
      <!-- Text input strip -->
      <div class="text-input-strip">
        <input class="text-input-field" id="txtinput-${this.index}" type="text"
               placeholder="Type text → Enter to send" autocomplete="off" spellcheck="false" />
        <button class="btn btn-ghost btn-sm" id="btn-send-text-${this.index}">Send</button>
        <button class="btn btn-icon btn-sm" title="Backspace" id="btn-bs-${this.index}">⌫</button>
        <button class="btn btn-icon btn-sm" title="Enter"     id="btn-enter-${this.index}">↵</button>
      </div>
      <!-- Device controls -->
      <div class="device-controls">
        <button class="btn btn-ghost btn-sm" id="btn-back-${this.index}"    title="Back (keyevent 4)">◀ Back</button>
        <button class="btn btn-ghost btn-sm" id="btn-home-${this.index}"    title="Home (keyevent 3)">⌂ Home</button>
        <button class="btn btn-ghost btn-sm" id="btn-rec-${this.index}"     title="Recents (keyevent 187)">▣ Recents</button>
        <button class="btn btn-ghost btn-sm" id="btn-restart-${this.index}" title="Restart app">↺ Restart</button>
        <button class="btn btn-ghost btn-sm" id="btn-reinstall-${this.index}" title="Reinstall APK">⤓ Reinstall</button>
        <button class="btn btn-ghost btn-sm" id="btn-reboot-${this.index}"  title="Reboot device">⏻ Reboot</button>
        <button class="btn btn-ghost btn-sm" id="btn-rotate-${this.index}"  title="Rotate screen">⤾ Rotate</button>
        <button class="btn btn-ghost btn-sm" id="btn-screenshot-${this.index}" title="Save screenshot">📷 Save</button>
      </div>
      <!-- Live reload -->
      <div class="live-reload-strip" id="live-reload-${this.index}">
        <label class="live-reload-toggle" title="Watch build output and auto-install via adb">
          <input type="checkbox" id="lr-toggle-${this.index}" />
          <span class="lr-label">Live Reload</span>
        </label>
        <span class="lr-status" id="lr-status-${this.index}">Off</span>
        <button class="btn btn-ghost btn-sm lr-set-path" id="lr-path-${this.index}" title="Set watch path">📁 Path</button>
        <button class="btn btn-ghost btn-sm lr-sync-now hidden" id="lr-sync-${this.index}" title="Sync now">↻ Sync</button>
      </div>`;

    this.canvas = this.screenPane.querySelector(`#scanvas-${this.index}`);
    this.notice = this.screenPane.querySelector(`#snotice-${this.index}`);
    this.hitTarget = this.screenPane.querySelector(`#sframe-${this.index}`);
    if (this.videoDecoderSupported) {
      this.player = new H264Player(this.canvas);
      this.notice.style.display = 'none';
    } else {
      this.player = null;
      this.canvas.style.display = 'none';
    }

    this._wireScreenInput();
    this._wireDeviceControls();
    this._wireLiveReload();
    this._fetchLiveReloadStatus();
    this._onLayoutResize();
  }

  _buildTestPane() {
    const tests = globalConfig.tests || Object.keys(TEST_LABELS);
    this.testPane = document.createElement('div');
    this.testPane.className = 'test-pane';
    this.testPane.innerHTML = `
      <div class="test-panel">
        <div class="test-panel-header">
          <span class="test-panel-title">Built-in APK Tests</span>
          <button class="test-run-all" id="btn-run-all-tests-${this.index}">Run All</button>
        </div>
        <div class="test-buttons" id="test-buttons-${this.index}">
          ${tests.map(t => `<button class="test-btn" data-test="${t}" id="tbtn-${t}-${this.index}">${TEST_LABELS[t] || t}</button>`).join('')}
        </div>
        <div class="test-results" id="test-results-${this.index}">
          <div class="test-result-line running">No tests run yet.</div>
        </div>
      </div>`;

    this.testPane.querySelector(`#btn-run-all-tests-${this.index}`)
      .addEventListener('click', () => this._runTests(null));
    this.testPane.querySelectorAll('.test-btn').forEach(btn => {
      btn.addEventListener('click', () => this._runTests([btn.dataset.test]));
    });
  }

  async _runTests(testNames) {
    if (this.testRunning) {
      showToast('Tests already running on this device', 'warn');
      return;
    }
    this.testRunning = true;
    this.testResults = [];
    this._renderTestResults([{ test: '…', status: 'running', message: 'Starting…' }]);

    const url = testNames
      ? `/api/device/${this.index}/tests/run`
      : `/api/device/${this.index}/tests/run`;
    const body = testNames ? JSON.stringify({ tests: testNames }) : JSON.stringify({});

    try {
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      this._startTestPoll();
    } catch (e) {
      this.testRunning = false;
      this._renderTestResults([{ test: 'error', status: 'error', message: String(e.message || e) }]);
      showToast(`Device ${this.index}: test failed to start`, 'error');
    }
  }

  _startTestPoll() {
    if (this.testPollTimer) clearInterval(this.testPollTimer);
    this.testPollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/device/${this.index}/tests/status`);
        const data = await res.json();
        if (data.results && data.results.length) {
          this._renderTestResults(data.results, data.current_test);
        }
        if (data.status === 'passed' || data.status === 'failed' || data.status === 'idle') {
          if (data.status !== 'idle') {
            this._renderTestResults(data.results || []);
            showToast(`Device ${this.index}: tests ${data.status}`, data.status === 'passed' ? '' : 'warn');
          }
          this.testRunning = false;
          clearInterval(this.testPollTimer);
          this.testPollTimer = null;
        }
      } catch (_) {}
    }, 800);
  }

  _renderTestResults(results, currentTest) {
    const el = this.testPane?.querySelector(`#test-results-${this.index}`);
    if (!el) return;
    if (!results || !results.length) {
      el.innerHTML = '<div class="test-result-line running">No results yet.</div>';
      return;
    }
    el.innerHTML = results.map(r => {
      const cls = r.status === 'passed' ? 'passed'
        : r.status === 'failed' ? 'failed'
        : r.status === 'error' ? 'error' : 'running';
      const dur = r.duration_ms != null ? ` (${r.duration_ms}ms)` : '';
      return `<div class="test-result-line ${cls}"><span>${_esc(r.test || '?')}</span><span>${_esc(r.message || r.status || '')}${dur}</span></div>`;
    }).join('');
    if (currentTest) {
      el.innerHTML += `<div class="test-result-line running">Running: ${_esc(currentTest)}…</div>`;
    }
    this.testPane.querySelectorAll('.test-btn').forEach(btn => {
      btn.classList.toggle('running', btn.dataset.test === currentTest);
    });
  }

  _onTest(msg) {
    if (msg.event === 'result') {
      const idx = this.testResults.findIndex(r => r.test === msg.test);
      const entry = { test: msg.test, status: msg.status, message: msg.message, duration_ms: msg.duration_ms };
      if (idx >= 0) this.testResults[idx] = entry;
      else this.testResults.push(entry);
      this._renderTestResults(this.testResults, msg.current_test);
    } else if (msg.event === 'progress') {
      this._renderTestResults(this.testResults, msg.current_test);
    } else if (msg.event === 'run_complete') {
      this.testResults = msg.results || [];
      this.testRunning = false;
      this._renderTestResults(this.testResults);
    }
  }

  _buildLogPane() {
    this.logPane = document.createElement('div');
    this.logPane.className = 'log-pane';
    this.logPane.innerHTML = `
      <div class="log-filter-bar">
        <button class="filter-chip active" data-filter="all"   id="fc-all-${this.index}">All</button>
        <button class="filter-chip"        data-filter="ok"    id="fc-ok-${this.index}">OK</button>
        <button class="filter-chip"        data-filter="warn"  id="fc-warn-${this.index}">Warn</button>
        <button class="filter-chip"        data-filter="error" id="fc-error-${this.index}">Error</button>
      </div>
      <div class="log-console" id="log-console-${this.index}" tabindex="0"></div>`;

    // Filter chips
    this.logPane.querySelectorAll('.filter-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        this.logFilter = chip.dataset.filter;
        this.logPane.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        this._applyLogFilter();
      });
    });

    // Auto-scroll: pause if user scrolls up, resume on scroll to bottom
    const console_ = this.logPane.querySelector(`#log-console-${this.index}`);
    console_.addEventListener('scroll', () => {
      const atBottom = console_.scrollHeight - console_.scrollTop - console_.clientHeight < 40;
      this.logAutoScroll = atBottom;
    });
  }

  // ── View layout ────────────────────────────────────────────────────────────
  _setView(view) {
    const body = document.getElementById(`card-body-${this.index}`);
    body.innerHTML = '';

    if (view === 'screen') {
      body.appendChild(this.screenPane);
    } else if (view === 'logs') {
      body.appendChild(this.logPane);
    } else if (view === 'tests') {
      body.appendChild(this.testPane);
    } else {
      // split
      const wrap = document.createElement('div');
      wrap.className = 'split-view';
      wrap.appendChild(this.screenPane);
      wrap.appendChild(this.logPane);
      body.appendChild(wrap);
    }
  }

  // ── Screen input wiring ────────────────────────────────────────────────────
  _wireScreenInput() {
    const overlay  = this.screenPane.querySelector(`#soverlay-${this.index}`);
    const txtInput = this.screenPane.querySelector(`#txtinput-${this.index}`);
    overlay.tabIndex = 0;

    // Tap / drag on overlay
    overlay.addEventListener('pointerdown', e => {
      overlay.setPointerCapture(e.pointerId);
      overlay.focus();
      this.dragStart = this._normalizeCoords(e);
    });

    overlay.addEventListener('pointerup', e => {
      if (!this.dragStart) return;
      const end = this._normalizeCoords(e);
      const dx  = Math.abs(end.nx - this.dragStart.nx);
      const dy  = Math.abs(end.ny - this.dragStart.ny);
      if (dx < TAP_THRESHOLD && dy < TAP_THRESHOLD) {
        // tap
        this._send({ action: 'tap', nx: end.nx, ny: end.ny });
      } else {
        // swipe
        this._send({ action: 'swipe',
          nx1: this.dragStart.nx, ny1: this.dragStart.ny,
          nx2: end.nx, ny2: end.ny, ms: 140 });
      }
      this.dragStart = null;
    });

    overlay.addEventListener('pointercancel', () => {
      this.dragStart = null;
    });

    // Hardware-key style controls while screen is focused
    overlay.addEventListener('keydown', e => {
      const keyMap = {
        Escape: 'back',
        Home: 'home',
        Enter: 'enter',
        Backspace: 'del',
      };
      const key = keyMap[e.key];
      if (key) {
        e.preventDefault();
        this._send({ action: 'key', key });
      }
    });

    // Text input
    const sendText = () => {
      const val = txtInput.value;
      if (val) {
        this._send({ action: 'text', text: val });
        txtInput.value = '';
      }
    };

    txtInput.addEventListener('keydown', e => {
      if (e.key === 'Enter')     { e.preventDefault(); sendText(); }
      if (e.key === 'Backspace' && !txtInput.value) this._send({ action: 'key', key: 'del' });
    });

    this.screenPane.querySelector(`#btn-send-text-${this.index}`)
      .addEventListener('click', sendText);
    this.screenPane.querySelector(`#btn-bs-${this.index}`)
      .addEventListener('click', () => this._send({ action: 'key', key: 'del' }));
    this.screenPane.querySelector(`#btn-enter-${this.index}`)
      .addEventListener('click', () => this._send({ action: 'key', key: 'enter' }));
  }

  _wireDeviceControls() {
    const i = this.index;
    const sp = this.screenPane;

    sp.querySelector(`#btn-back-${i}`)   .addEventListener('click', () => this._send({ action: 'key', key: 'back' }));
    sp.querySelector(`#btn-home-${i}`)   .addEventListener('click', () => this._send({ action: 'key', key: 'home' }));
    sp.querySelector(`#btn-rec-${i}`)    .addEventListener('click', () => this._send({ action: 'key', key: 'recents' }));

    sp.querySelector(`#btn-restart-${i}`).addEventListener('click', async () => {
      await fetch(`/api/device/${i}/restart_app`, { method: 'POST' });
      showToast(`Device ${i}: restarting app`);
    });
    sp.querySelector(`#btn-reinstall-${i}`).addEventListener('click', async () => {
      await fetch(`/api/device/${i}/reinstall`, { method: 'POST' });
      showToast(`Device ${i}: reinstalling`);
    });
    sp.querySelector(`#btn-reboot-${i}`).addEventListener('click', async () => {
      await fetch(`/api/device/${i}/reboot`, { method: 'POST' });
      showToast(`Device ${i}: rebooting device`);
    });
    sp.querySelector(`#btn-rotate-${i}`).addEventListener('click', async () => {
      await fetch(`/api/device/${i}/rotate`, { method: 'POST' });
      showToast(`Device ${i}: rotating`);
    });
    sp.querySelector(`#btn-screenshot-${i}`).addEventListener('click', () => this._saveScreenshot());
  }

  // ── Live reload ────────────────────────────────────────────────────────────
  _wireLiveReload() {
    const i = this.index;
    const toggle = this.screenPane.querySelector(`#lr-toggle-${i}`);
    const pathBtn = this.screenPane.querySelector(`#lr-path-${i}`);
    const syncBtn = this.screenPane.querySelector(`#lr-sync-${i}`);

    this.lrWatchPath = this.ds.apk_path || '';

    toggle.addEventListener('change', async () => {
      if (toggle.checked) {
        await this._enableLiveReload();
      } else {
        await this._disableLiveReload();
      }
    });

    pathBtn.addEventListener('click', () => {
      const defaultPath = this.lrWatchPath || this.ds.apk_path || globalConfig.apk_dir || '';
      folderPicker.open({
        mode: 'path',
        startPath: defaultPath,
        onSelect: async (path) => {
          this.lrWatchPath = path;
          if (toggle.checked) await this._enableLiveReload();
          return true;
        },
      });
    });

    syncBtn.addEventListener('click', async () => {
      try {
        const res = await fetch(`/api/device/${i}/live-reload/sync`, { method: 'POST' });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        showToast(`Device ${i}: synced`);
      } catch (e) {
        showToast(`Device ${i}: sync failed — ${e.message}`, 'error');
      }
    });
  }

  async _fetchLiveReloadStatus() {
    try {
      const res = await fetch(`/api/device/${this.index}/live-reload/status`);
      if (res.ok) this._updateLiveReloadUI(await res.json());
    } catch (_) {}
  }

  async _enableLiveReload() {
    const i = this.index;
    const toggle = this.screenPane.querySelector(`#lr-toggle-${i}`);
    const body = {};
    if (this.lrWatchPath) body.watch_path = this.lrWatchPath;

    try {
      const res = await fetch(`/api/device/${i}/live-reload/enable`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      this.lrWatchPath = data.watch_path || this.lrWatchPath;
      this._updateLiveReloadUI(data);
      showToast(`Device ${i}: live reload watching`);
    } catch (e) {
      if (toggle) toggle.checked = false;
      showToast(`Device ${i}: live reload failed — ${e.message}`, 'error');
    }
  }

  async _disableLiveReload() {
    const i = this.index;
    try {
      const res = await fetch(`/api/device/${i}/live-reload/disable`, { method: 'POST' });
      if (res.ok) this._updateLiveReloadUI(await res.json());
      showToast(`Device ${i}: live reload stopped`);
    } catch (_) {}
  }

  _updateLiveReloadUI(data) {
    const i = this.index;
    const toggle = this.screenPane.querySelector(`#lr-toggle-${i}`);
    const statusEl = this.screenPane.querySelector(`#lr-status-${i}`);
    const syncBtn = this.screenPane.querySelector(`#lr-sync-${i}`);
    const strip = this.screenPane.querySelector(`#live-reload-${i}`);
    if (!statusEl) return;

    if (toggle) toggle.checked = !!data.enabled;

    const status = data.status || 'stopped';
    let label = status;
    if (status === 'watching') label = 'Watching';
    else if (status === 'syncing') label = 'Syncing…';
    else if (status === 'error') label = 'Error';
    else label = 'Off';

    if (data.last_sync_at) {
      const t = new Date(data.last_sync_at).toLocaleTimeString();
      label += ` · ${t}`;
    }
    if (data.last_error) {
      statusEl.title = data.last_error;
    } else {
      statusEl.title = data.watch_path || '';
    }

    statusEl.textContent = label;
    statusEl.className = `lr-status lr-${status}`;

    if (strip) strip.classList.toggle('lr-active', !!data.enabled);
    if (syncBtn) syncBtn.classList.toggle('hidden', !data.enabled);
  }

  _onLiveReload(msg) {
    this._updateLiveReloadUI(msg);
    if (msg.status === 'syncing') {
      showToast(`Device ${this.index}: syncing…`);
    } else if (msg.status === 'watching' && msg.last_sync_at) {
      showToast(`Device ${this.index}: reload complete`);
    } else if (msg.status === 'error') {
      showToast(`Device ${this.index}: reload error — ${msg.last_error || 'unknown'}`, 'error');
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  /** Visible video area inside letterboxed canvas (object-fit: contain). */
  _getCanvasDisplayRect() {
    const canvas = this.canvas;
    const rect = canvas.getBoundingClientRect();
    const iw = canvas.width;
    const ih = canvas.height;
    if (!iw || !ih || !rect.width || !rect.height) return rect;

    const elAspect = rect.width / rect.height;
    const vidAspect = iw / ih;
    let w, h, x, y;

    if (vidAspect > elAspect) {
      w = rect.width;
      h = rect.width / vidAspect;
      x = rect.left;
      y = rect.top + (rect.height - h) / 2;
    } else {
      h = rect.height;
      w = rect.height * vidAspect;
      x = rect.left + (rect.width - w) / 2;
      y = rect.top;
    }
    return { left: x, top: y, width: w, height: h };
  }

  _normalizeCoords(e) {
    const rect = this._getCanvasDisplayRect();
    const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const ny = Math.max(0, Math.min(1, (e.clientY - rect.top)  / rect.height));
    return { nx, ny };
  }

  _onLayoutResize() {
    // Force layout recalc; coords are computed on demand via getBoundingClientRect.
    void this.canvas?.offsetHeight;
  }

  _send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  _saveScreenshot() {
    if (!this.canvas) return;

    const flash = document.createElement('div');
    flash.className = 'screenshot-flash';
    const frame = this.screenPane.querySelector(`#sframe-${this.index}`);
    frame.appendChild(flash);
    flash.addEventListener('animationend', () => flash.remove());

    this.canvas.toBlob(blob => {
      downloadBlob(blob, `mdt_device${this.index}_${Date.now()}.png`);
    }, 'image/png');
  }

  // ── WebSocket ──────────────────────────────────────────────────────────────
  _connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url   = `${proto}://${location.host}/ws/${this.index}`;
    const dot   = document.getElementById(`ws-dot-${this.index}`);

    dot.className = 'ws-dot reconnecting';
    this.ws = new WebSocket(url);
    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => {
      dot.className = 'ws-dot connected';
      this.wsRetry  = 0;
    };

    this.ws.onmessage = e => {
      if (e.data instanceof ArrayBuffer) {
        if (this.player) this.player.push(e.data);
        return;
      }
      if (typeof e.data === 'string') {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'video_reset') {
            if (this.player) this.player.reset();
            return;
          }
          this._handleMsg(msg);
        } catch (_) {}
      }
    };

    this.ws.onclose = this.ws.onerror = () => {
      dot.className = 'ws-dot disconnected';
      const delay = Math.min(WS_RECONNECT_BASE_MS * 2 ** this.wsRetry, WS_RECONNECT_MAX_MS);
      this.wsRetry++;
      setTimeout(() => {
        dot.className = 'ws-dot reconnecting';
        this._connectWS();
      }, delay);
    };
  }

  _handleMsg(msg) {
    switch (msg.type) {
      case 'log':      this._onLog(msg);           break;
      case 'state':    this._onState(msg);         break;
      case 'counters': this._onCounters(msg);      break;
      case 'test':     this._onTest(msg);          break;
      case 'live_reload': this._onLiveReload(msg); break;
      case 'ping':     break; // keepalive
    }
  }


  _onState(msg) {
    this.state = msg.state;
    const badge = document.getElementById(`badge-${this.index}`);
    if (badge) {
      badge.textContent = this.state;
      badge.className = `state-badge badge-${this.state}`;
    }
    this.card.className = `device-card state-${this.state}`;

    // Update overlay text
    const statusTxt = this.screenPane.querySelector(`#sstatus-txt-${this.index}`);
    const overlay   = this.screenPane.querySelector(`#sstatus-${this.index}`);

    if (this.state === 'running') {
      overlay.classList.add('hidden');
    } else if (this.state === 'error') {
      overlay.classList.remove('hidden');
      overlay.querySelector('.boot-spinner').style.display = 'none';
      statusTxt.textContent = msg.error_msg || msg.status_msg || 'Error';
      statusTxt.style.color = 'var(--error)';
      // Inject error into log pane
      this._appendLogLine({ level: 'E', color: 'error', tag: 'MDT', msg: msg.error_msg || msg.status_msg || 'Error', ts: _now() });
    } else {
      overlay.classList.remove('hidden');
      overlay.querySelector('.boot-spinner').style.display = '';
      statusTxt.style.color = '';
      statusTxt.textContent = msg.status_msg
        || (this.state === 'booting' ? 'Booting emulator…'
          : this.state === 'installing' ? 'Installing APK…' : 'Starting…');

      // Log state transitions/progress so boot/install phases are visible in Logs tab.
      const progress = statusTxt.textContent;
      const stateKey = `${this.state}:${progress}`;
      if (progress && stateKey !== this.lastStateLogKey) {
        this.lastStateLogKey = stateKey;
        this._appendLogLine({
          level: 'I',
          color: 'ok',
          tag: 'MDT',
          msg: `[${this.state}] ${progress}`,
          ts: _now(),
        });
      }
    }

    // Update package / serial if now known
    if (msg.package) {
      const el = document.getElementById(`pkg-${this.index}`);
      if (el) { el.textContent = _truncate(msg.package, 32); el.title = msg.package; }
    }
    if (msg.serial) {
      const el = document.getElementById(`serial-${this.index}`);
      if (el) el.textContent = msg.serial;
    }

    updateGlobalStatus();
  }

  _onCounters(msg) {
    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    setVal(`cnt-ok-val-${this.index}`,   msg.cnt_ok);
    setVal(`cnt-warn-val-${this.index}`, msg.cnt_warn);
    setVal(`cnt-err-val-${this.index}`,  msg.cnt_error);
  }

  _onLog(entry) {
    this._appendLogLine(entry);
  }

  _appendLogLine(entry) {
    const console_ = this.logPane.querySelector(`#log-console-${this.index}`);
    if (!console_) return;

    const colorClass = entry.color === 'warn' ? 'level-warn'
                     : entry.color === 'error' ? 'level-error'
                     : 'level-ok';

    const line = document.createElement('div');
    line.className  = `log-line ${colorClass}`;
    line.dataset.color = entry.color || 'ok';
    line.innerHTML  = `<span class="log-ts">${(entry.ts||'').split(' ')[1]||''}</span>`
                    + `<span class="log-lvl">${entry.level||'I'}</span>`
                    + `<span class="log-tag">${_esc(entry.tag||'')}</span>`
                    + `<span class="log-msg">${_esc(entry.msg||entry.raw||'')}</span>`;

    // Apply current filter
    if (this.logFilter !== 'all' && line.dataset.color !== this.logFilter) {
      line.style.display = 'none';
    }

    console_.appendChild(line);
    this.logLines.push(line);

    // Trim DOM if too long
    while (this.logLines.length > MAX_LOG_LINES) {
      const old = this.logLines.shift();
      old.remove();
    }

    if (this.logAutoScroll) {
      console_.scrollTop = console_.scrollHeight;
    }
  }

  _applyLogFilter() {
    this.logLines.forEach(line => {
      line.style.display =
        (this.logFilter === 'all' || line.dataset.color === this.logFilter)
          ? '' : 'none';
    });
  }

  clearLogPane() {
    const console_ = this.logPane.querySelector(`#log-console-${this.index}`);
    if (console_) console_.innerHTML = '';
    this.logLines = [];
  }
}

// ── Device card creation (entry point) ────────────────────────────────────────
function createDeviceCard(ds) {
  if (devices.has(ds.index)) return;
  const ui = new DeviceUI(ds);
  devices.set(ds.index, ui);
}

function createClosedSlotCard(ds) {
  if (devices.has(ds.index)) return;

  const grid = document.getElementById('main-grid');
  const card = document.createElement('div');
  card.className = 'device-card closed-slot-card state-closed';
  card.id = `device-card-${ds.index}`;

  const slotLabel = ds.index === 0 ? 'Device 1' : 'Device 2';
  const hasApks = (globalConfig.apk_count || 0) > 0;

  card.innerHTML = `
    <div class="closed-slot-inner">
      <div class="closed-slot-icon">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>
        </svg>
      </div>
      <h3 class="closed-slot-title">${slotLabel} — Slot closed</h3>
      <p class="closed-slot-desc">${hasApks ? 'An APK is available in the current folder.' : 'Add APKs to the folder, then open this slot.'}</p>
      <button class="btn btn-primary btn-add-device" id="btn-add-${ds.index}" ${hasApks ? '' : 'disabled'}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        Add device
      </button>
    </div>`;

  grid.appendChild(card);

  const ui = {
    index: ds.index,
    state: 'closed',
    isClosedSlot: true,
    card,
    destroy() { card.remove(); },
  };
  devices.set(ds.index, ui);

  card.querySelector(`#btn-add-${ds.index}`).addEventListener('click', async () => {
    try {
      const res = await fetch(`/api/device/${ds.index}/open`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      showToast(`Opening device ${ds.index}…`);
      ui.destroy();
      devices.delete(ds.index);
      const data = await res.json();
      createDeviceCard(data);
      updateGlobalStatus();
    } catch (e) {
      showToast(`Failed to open device ${ds.index}: ${e.message}`, 'error');
    }
  });
}

// ── Activity panel ────────────────────────────────────────────────────────────

class ActivityPanel {
  constructor() {
    this.panel = document.getElementById('activity-panel');
    this.console = document.getElementById('activity-console');
    this.shell = document.getElementById('app-shell');
    this.filter = 'all';
    this.autoScroll = true;
    this.lines = [];
    this.ws = null;
    this.wsRetry = 0;
    this._maxLines = 500;

    document.getElementById('activity-filters').querySelectorAll('.afilter').forEach(btn => {
      btn.addEventListener('click', () => {
        this.filter = btn.dataset.filter;
        document.querySelectorAll('.afilter').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._applyFilter();
      });
    });

    document.getElementById('btn-activity-clear').addEventListener('click', async () => {
      await fetch('/api/activity/clear', { method: 'POST' });
      this.console.innerHTML = '';
      this.lines = [];
      showToast('Activity log cleared');
    });

    document.getElementById('btn-activity-autoscroll').addEventListener('click', (e) => {
      this.autoScroll = !this.autoScroll;
      e.currentTarget.classList.toggle('active', this.autoScroll);
    });

    document.getElementById('btn-activity-collapse').addEventListener('click', () => {
      this.toggleCollapse();
    });

    this.console.addEventListener('scroll', () => {
      const atBottom = this.console.scrollHeight - this.console.scrollTop - this.console.clientHeight < 40;
      this.autoScroll = atBottom;
      document.getElementById('btn-activity-autoscroll').classList.toggle('active', this.autoScroll);
    });

    this._setupResize();
  }

  toggleCollapse() {
    this.shell.classList.toggle('activity-collapsed');
  }

  _setupResize() {
    const handle = document.getElementById('activity-resize-handle');
    let startY = 0;
    let startH = 0;

    const onMove = (e) => {
      const dy = startY - e.clientY;
      const maxH = Math.min(480, Math.floor(window.innerHeight * 0.45));
      const newH = Math.max(120, Math.min(maxH, startH + dy));
      document.documentElement.style.setProperty('--activity-height', `${newH}px`);
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };

    handle.addEventListener('mousedown', (e) => {
      startY = e.clientY;
      startH = this.panel.offsetHeight;
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      e.preventDefault();
    });
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/activity`;
    this.ws = new WebSocket(url);

    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'activity_history') {
          msg.entries.forEach(entry => this._append(entry, false));
          if (this.autoScroll) this.console.scrollTop = this.console.scrollHeight;
        } else if (msg.type === 'activity') {
          this._append(msg);
        }
      } catch (_) {}
    };

    this.ws.onclose = this.ws.onerror = () => {
      const delay = Math.min(WS_RECONNECT_BASE_MS * 2 ** this.wsRetry, WS_RECONNECT_MAX_MS);
      this.wsRetry++;
      setTimeout(() => this.connect(), delay);
    };

    this.ws.onopen = () => { this.wsRetry = 0; };
  }

  _append(entry, animate = true) {
    const level = entry.level || 'info';
    const line = document.createElement('div');
    line.className = `activity-line level-${level}`;
    line.dataset.level = level;
    const dev = entry.device_index != null ? `D${entry.device_index}` : '';
    line.innerHTML =
      `<span class="act-ts">${_esc(entry.ts || '')}</span>` +
      `<span class="act-level">${_esc(level)}</span>` +
      `<span class="act-source">${_esc(entry.source || '')}</span>` +
      `<span class="act-device">${_esc(dev)}</span>` +
      `<span class="act-msg">${_esc(entry.message || '')}</span>`;

    if (this.filter !== 'all' && level !== this.filter) {
      line.style.display = 'none';
    }

    this.console.appendChild(line);
    this.lines.push(line);

    while (this.lines.length > this._maxLines) {
      this.lines.shift().remove();
    }

    if (this.autoScroll) {
      this.console.scrollTop = this.console.scrollHeight;
    }
  }

  _applyFilter() {
    this.lines.forEach(line => {
      line.style.display =
        (this.filter === 'all' || line.dataset.level === this.filter) ? '' : 'none';
    });
  }
}

// ── Folder picker modal ───────────────────────────────────────────────────────

class FolderPicker {
  constructor() {
    this.modal = document.getElementById('folder-modal');
    this.list = document.getElementById('folder-list');
    this.breadcrumbs = document.getElementById('folder-breadcrumbs');
    this.currentPathEl = document.getElementById('folder-current-path');
    this.apkCountEl = document.getElementById('folder-apk-count');
    this.selectBtn = document.getElementById('folder-btn-select');
    this.titleEl = document.getElementById('folder-modal-title');

    this.currentPath = '';
    this.mode = 'folder';
    this.onSelect = null;
    this._data = null;

    document.getElementById('folder-modal-close').addEventListener('click', () => this.close());
    document.getElementById('folder-btn-cancel').addEventListener('click', () => this.close());
    document.getElementById('folder-btn-up').addEventListener('click', () => {
      if (this._data?.parent) this._load(this._data.parent);
    });
    document.getElementById('folder-btn-home').addEventListener('click', () => {
      if (this._data?.home) this._load(this._data.home);
    });
    this.selectBtn.addEventListener('click', () => this._confirm());
    this.modal.addEventListener('click', (e) => {
      if (e.target === this.modal) this.close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.modal.classList.contains('hidden')) this.close();
    });
  }

  open({ mode = 'folder', startPath = '', onSelect }) {
    this.mode = mode;
    this.onSelect = onSelect;
    this.titleEl.textContent = mode === 'path' ? 'Select Watch Path' : 'Browse APK Folder';
    this.selectBtn.textContent = mode === 'path' ? 'Select this path' : 'Select this folder';
    this.modal.classList.remove('hidden');
    this._load(startPath);
  }

  close() {
    this.modal.classList.add('hidden');
    this.onSelect = null;
  }

  async _load(path) {
    this.list.innerHTML = '<div class="folder-loading"><div class="boot-spinner"></div>Loading…</div>';
    try {
      const q = path ? `?path=${encodeURIComponent(path)}` : '';
      const res = await fetch(`/api/browse${q}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      this._data = await res.json();
      this.currentPath = this._data.path;
      this._render();
    } catch (e) {
      this.list.innerHTML = `<div class="folder-empty">Error: ${_esc(e.message)}</div>`;
    }
  }

  _render() {
    const d = this._data;
    this.currentPathEl.textContent = d.path;
    this.currentPathEl.title = d.path;
    this.apkCountEl.textContent = `${d.apk_count} APK${d.apk_count !== 1 ? 's' : ''}`;

    const parts = d.path.split(/[/\\]/).filter(Boolean);
    let acc = d.path.startsWith('/') && parts.length ? '/' : '';
    if (d.path.match(/^[A-Za-z]:/)) acc = '';

    this.breadcrumbs.innerHTML = '';
    parts.forEach((part, i) => {
      if (d.path.match(/^[A-Za-z]:/) && i === 0) {
        acc = part + (d.path.includes('\\') ? '\\' : '/');
      } else {
        acc = acc ? acc + (acc.endsWith('/') || acc.endsWith('\\') ? '' : '/') + part : part;
      }
      if (i > 0) {
        const sep = document.createElement('span');
        sep.className = 'crumb-sep';
        sep.textContent = '/';
        this.breadcrumbs.appendChild(sep);
      }
      const btn = document.createElement('button');
      btn.className = 'crumb';
      btn.textContent = part;
      const target = this._resolveCrumbPath(parts, i, d.path);
      btn.addEventListener('click', () => this._load(target));
      this.breadcrumbs.appendChild(btn);
    });

    const items = [];
    if (this.mode === 'path' && d.apk_files) {
      d.apk_files.forEach(f => items.push({ type: 'apk', ...f }));
    }
    d.directories.forEach(dir => items.push({ type: 'dir', ...dir }));

    if (!items.length) {
      this.list.innerHTML = '<div class="folder-empty">This folder is empty</div>';
      return;
    }

    this.list.innerHTML = '';
    items.forEach(item => {
      const btn = document.createElement('button');
      btn.className = `folder-item${item.type === 'apk' ? ' apk-item' : ''}`;
      const icon = item.type === 'apk'
        ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
        : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h5l2 2h11v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>';
      btn.innerHTML = `${icon}<span class="fi-name">${_esc(item.name)}</span><span class="fi-arrow">→</span>`;
      btn.addEventListener('click', () => {
        if (item.type === 'dir') {
          this._load(item.path);
        } else if (this.mode === 'path' && this.onSelect) {
          this.onSelect(item.path).then(ok => { if (ok !== false) this.close(); });
        }
      });
      if (item.type === 'apk' && this.mode === 'path') {
        btn.addEventListener('dblclick', () => {
          if (this.onSelect) this.onSelect(item.path).then(ok => { if (ok !== false) this.close(); });
        });
      }
      this.list.appendChild(btn);
    });
  }

  _resolveCrumbPath(parts, index, fullPath) {
    const isWin = /^[A-Za-z]:/.test(fullPath);
    const sep = fullPath.includes('\\') ? '\\' : '/';
    if (isWin) {
      return parts.slice(0, index + 1).join(sep);
    }
    return '/' + parts.slice(0, index + 1).join('/');
  }

  async _confirm() {
    if (!this.onSelect || !this.currentPath) return;
    const ok = await this.onSelect(this.currentPath);
    if (ok !== false) this.close();
  }
}

// ── Toast helper ──────────────────────────────────────────────────────────────
function showToast(msg, type = '') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast${type ? ' ' + type : ''}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── Utility ───────────────────────────────────────────────────────────────────
function _esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function _truncate(str, max) {
  return str.length > max ? '…' + str.slice(-(max - 1)) : str;
}

function _shortPath(p) {
  if (!p) return '—';
  const parts = String(p).split(/[/\\]/);
  if (parts.length <= 2) return p;
  return '…/' + parts.slice(-2).join('/');
}

function _now() {
  return new Date().toISOString().replace('T', ' ').slice(0, 23);
}
