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
const TAP_THRESHOLD         = 0.004;

// ── State ─────────────────────────────────────────────────────────────────────
/** @type {Map<number, DeviceUI>} index → DeviceUI */
const devices = new Map();
let globalConfig = {};

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
  try {
    const [cfgRes, devRes] = await Promise.all([
      fetch('/api/config'),
      fetch('/api/devices'),
    ]);
    globalConfig = await cfgRes.json();
    const devList = await devRes.json();

    document.getElementById('log-dir-note').textContent = `Logs: ${globalConfig.log_dir}`;
    document.getElementById('log-dir-note').title        = globalConfig.log_dir;
    document.getElementById('apk-dir-note').textContent  = `APKs: ${globalConfig.apk_dir}`;
    document.getElementById('apk-dir-note').title        = globalConfig.apk_dir;

    setupTopBarButtons();

    if (devList.length === 0) {
      renderEmptyState();
    } else {
      devList.forEach(ds => createDeviceCard(ds));
      updateGlobalStatus();
    }
  } catch (e) {
    showToast('Failed to connect to MDT server. Is it running?', 'error');
  }

  // Poll for new devices (e.g., user added APKs, page loaded before orchestration)
  setInterval(pollDevices, 4000);
}

async function pollDevices() {
  try {
    const res  = await fetch('/api/devices');
    const list = await res.json();
    if (list.length > devices.size) {
      // Remove empty state if present
      const empty = document.getElementById('empty-state');
      if (empty) empty.remove();
      list.forEach(ds => {
        if (!devices.has(ds.index)) createDeviceCard(ds);
      });
    }
  } catch (_) {}
}

// ── Empty state ───────────────────────────────────────────────────────────────
function renderEmptyState() {
  const grid = document.getElementById('main-grid');
  grid.innerHTML = `
    <div id="empty-state">
      <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="5" y="2" width="14" height="20" rx="2"/>
        <line x1="12" y1="18" x2="12.01" y2="18"/>
        <line x1="9" y1="7" x2="15" y2="7"/>
        <line x1="9" y1="11" x2="13" y2="11"/>
      </svg>
      <h2>No APKs detected</h2>
      <p>Drop up to <strong>3 APK files</strong> into the <code>apk_input/</code> folder, then refresh or restart MDT.</p>
      <p style="font-size:12px; color: var(--text-muted);">The server is running and waiting.</p>
    </div>`;
  document.getElementById('global-status').textContent = 'No APKs';
}

// ── Top-bar button wiring ─────────────────────────────────────────────────────
function setupTopBarButtons() {
  document.getElementById('btn-set-apk-dir').addEventListener('click', async () => {
    const current = globalConfig.apk_dir || '';
    const entered = window.prompt('Enter APK folder path', current);
    if (!entered) return;
    const res = await fetch('/api/apk_dir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: entered }),
    });
    if (!res.ok) {
      showToast('Failed to update APK folder', 'error');
      return;
    }
    const data = await res.json();
    globalConfig.apk_dir = data.apk_dir;
    const note = document.getElementById('apk-dir-note');
    note.textContent = `APKs: ${data.apk_dir}`;
    note.title = data.apk_dir;
    showToast('APK folder updated');
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
  const states = [...devices.values()].map(d => d.state);
  if (states.every(s => s === 'running')) {
    pill.textContent = `${states.length} device${states.length > 1 ? 's' : ''} running`;
    pill.className   = 'status-pill running';
  } else if (states.some(s => s === 'error')) {
    pill.textContent = 'Error on one or more devices';
    pill.className   = 'status-pill';
  } else {
    const booting = states.filter(s => s === 'booting' || s === 'installing').length;
    pill.textContent = booting ? `${booting} device${booting > 1 ? 's' : ''} starting…` : 'Idle';
    pill.className   = 'status-pill';
  }
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
    this.ws      = null;
    this.wsRetry = 0;

    // Log state
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
          <span class="apk-name" title="${apkName}">${apkName}</span>
          <span class="state-badge badge-${this.state}" id="badge-${this.index}">${this.state}</span>
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
        <button                 data-view="split"  id="vbtn-split-${this.index}">Split</button>
      </div>

      <!-- Body: content area swapped by view toggle -->
      <div class="card-body" id="card-body-${this.index}"></div>
    `;

    grid.appendChild(this.card);

    // Build the three view panes (hidden until toggled)
    this._buildScreenPane();
    this._buildLogPane();
    this._setView('screen');

    // View toggle wiring
    this.card.querySelectorAll('.view-toggle button').forEach(btn => {
      btn.addEventListener('click', () => {
        this._setView(btn.dataset.view);
        this.card.querySelectorAll('.view-toggle button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });
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

  // ── Helpers ────────────────────────────────────────────────────────────────
  _normalizeCoords(e) {
    const rect = this.canvas.getBoundingClientRect();
    const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const ny = Math.max(0, Math.min(1, (e.clientY - rect.top)  / rect.height));
    return { nx, ny };
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

function _now() {
  return new Date().toISOString().replace('T', ' ').slice(0, 23);
}
