/* ═══════════════════════════════════════════════════════════════════════════
   FREYJA QUANT ENGINE v2 — Dashboard Application
   ═══════════════════════════════════════════════════════════════════════════ */

// ── Configuration ──────────────────────────────────────────────────────────
const API_BASE = '';
const API_TOKEN = 'freyja-ctrl-2026';

const POLL_STATE_MS    = 5000;
const POLL_STATUS_MS   = 10000;
const POLL_LOG_MS      = 10000;

// ── In-Memory State ────────────────────────────────────────────────────────
const state = {
    connected: false,
    botStatus: 'unknown',
    botPid: null,
    activeSince: null,
    latencyMs: null,
    tradingMode: 'PAPER',
    config: {},
    configDirty: false,
    pendingConfig: {},
    positions: [],
    trades: [],
    metrics: {
        portfolio_value: 0,
        cash_balance: 0,
        open_positions: 0,
        win_rate: 0,
        total_pnl: 0,
        session_pnl: 0,
    },
    signals: {
        rsi: 50,
        vpin: 0.5,
        confidence: 0,
        direction: 'NEUTRAL',
        kelly_size: 0,
    },
    perfStats: {
        total_trades: 0,
        avg_hold_time: '0m',
        best_trade: 0,
        worst_trade: 0,
    },
    logLines: [],
    logFilter: '',
    logAutoScroll: true,
    previousMetrics: {},
};

// ── API Helpers ────────────────────────────────────────────────────────────
async function apiCall(endpoint, method = 'GET', body = null) {
    const start = performance.now();
    const opts = {
        method,
        headers: {
            'Authorization': `Bearer ${API_TOKEN}`,
            'Content-Type': 'application/json',
        },
    };
    if (body) opts.body = JSON.stringify(body);

    try {
        const resp = await fetch(`${API_BASE}${endpoint}`, opts);
        const elapsed = Math.round(performance.now() - start);
        state.latencyMs = elapsed;
        state.connected = true;
        updateConnectionUI();

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ error: resp.statusText }));
            throw new Error(err.error || `HTTP ${resp.status}`);
        }

        const contentType = resp.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            return await resp.json();
        }
        return await resp.text();
    } catch (err) {
        if (err.message === 'Failed to fetch' || err.name === 'TypeError') {
            state.connected = false;
            state.latencyMs = null;
            updateConnectionUI();
        }
        throw err;
    }
}

// ── Toast Notifications ────────────────────────────────────────────────────
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

// ── Connection & Header UI ─────────────────────────────────────────────────
function updateConnectionUI() {
    const badge = document.getElementById('status-badge');
    const latencyEl = document.getElementById('latency-value');

    if (!state.connected) {
        badge.className = 'status-badge disconnected';
        badge.innerHTML = '<span class="dot"></span>DISCONNECTED';
        latencyEl.textContent = '---';
        latencyEl.className = 'value';
        return;
    }

    if (state.botStatus === 'active') {
        const mode = state.tradingMode === 'LIVE' ? 'running' : 'paper';
        badge.className = `status-badge ${mode}`;
        const label = state.tradingMode === 'LIVE' ? 'LIVE' : 'PAPER';
        badge.innerHTML = `<span class="dot"></span>${label} — RUNNING`;
    } else {
        badge.className = 'status-badge stopped';
        badge.innerHTML = '<span class="dot"></span>STOPPED';
    }

    if (state.latencyMs !== null) {
        latencyEl.textContent = `${state.latencyMs}ms`;
        if (state.latencyMs < 200) {
            latencyEl.className = 'value latency-good';
        } else if (state.latencyMs < 500) {
            latencyEl.className = 'value latency-warn';
        } else {
            latencyEl.className = 'value latency-bad';
        }
    }
}

// ── System Clock ───────────────────────────────────────────────────────────
function updateClock() {
    const el = document.getElementById('system-time');
    const now = new Date();
    const h = String(now.getHours()).padStart(2, '0');
    const m = String(now.getMinutes()).padStart(2, '0');
    const s = String(now.getSeconds()).padStart(2, '0');
    const ms = String(now.getMilliseconds()).padStart(3, '0');
    el.textContent = `${h}:${m}:${s}.${ms}`;
}

// ── Polling: Bot Status ────────────────────────────────────────────────────
async function pollStatus() {
    try {
        const data = await apiCall('/api/status');
        state.botStatus = data.status || 'unknown';
        state.botPid = data.pid || null;
        state.activeSince = data.active_since || null;
        updateConnectionUI();
        updateControlsTab();
    } catch (e) {
        // Handled by apiCall
    }
}

// ── Polling: State ─────────────────────────────────────────────────────────
async function pollState() {
    try {
        // Fetch both state.json and computed balance in parallel
        const [rawState, balance] = await Promise.all([
            apiCall('/api/state').catch(() => null),
            apiCall('/api/balance').catch(() => null),
        ]);

        // Save previous metrics for flash detection
        state.previousMetrics = { ...state.metrics };

        // Balance data (computed from trade history)
        if (balance && !balance.error) {
            state.metrics.portfolio_value = balance.portfolio_value ?? 0;
            state.metrics.cash_balance = balance.cash_balance ?? 0;
            state.metrics.total_pnl = balance.total_pnl ?? 0;
            state.metrics.session_pnl = balance.session_pnl ?? 0;
            state.metrics.win_rate = balance.win_rate ?? 0;
            state.metrics.open_positions = balance.open_positions ?? 0;
        }

        // Raw state data (positions dict, trades array)
        if (rawState && !rawState.error) {
            // Positions: state.json stores as {ticker: {...}} dict
            const positionsObj = rawState.positions || {};
            state.positions = Object.values(positionsObj).map(p => ({
                ticker: p.ticker || '—',
                side: p.side || '—',
                entry_price: (p.entry_price || 0) / 100,  // cents → dollars
                current_price: (p.current_price || p.entry_price || 0) / 100,
                pnl: p.current_price && p.entry_price
                    ? ((p.current_price - p.entry_price) / 100) * (p.contracts || 1)
                    : 0,
                contracts: p.contracts || 0,
                settlement: p.entry_time ? new Date(p.entry_time * 1000).toLocaleTimeString() : '—',
                vpin: p.vpin_at_entry || 0,
            }));
            state.metrics.open_positions = state.positions.length;

            // Trades: state.json stores as [{pnl_dollars, ...}] array
            const rawTrades = rawState.trades || [];
            state.trades = rawTrades.map(t => ({
                time: t.exit_time ? new Date(t.exit_time * 1000).toLocaleTimeString() : '—',
                action: t.pnl_dollars >= 0 ? 'WIN' : 'LOSS',
                ticker: t.ticker || '—',
                side: (t.side || '—').toUpperCase(),
                price: (t.exit_price || 0) / 100,
                quantity: t.contracts || 0,
                pnl: t.pnl_dollars || 0,
            }));

            // Determine trading mode from .env
            // (will be updated when config is loaded)

            // Perf stats
            state.perfStats.total_trades = rawTrades.length;
            if (rawTrades.length > 0) {
                const pnls = rawTrades.map(t => t.pnl_dollars || 0);
                state.perfStats.best_trade = Math.max(...pnls);
                state.perfStats.worst_trade = Math.min(...pnls);
                // Average hold time
                const holdTimes = rawTrades
                    .filter(t => t.entry_time && t.exit_time)
                    .map(t => t.exit_time - t.entry_time);
                if (holdTimes.length > 0) {
                    const avgSecs = holdTimes.reduce((a, b) => a + b, 0) / holdTimes.length;
                    if (avgSecs < 60) state.perfStats.avg_hold_time = Math.round(avgSecs) + 's';
                    else if (avgSecs < 3600) state.perfStats.avg_hold_time = Math.round(avgSecs / 60) + 'm';
                    else state.perfStats.avg_hold_time = (avgSecs / 3600).toFixed(1) + 'h';
                }
            }
        }

        updateDashboardTab();
    } catch (e) {
        // Handled by apiCall
    }
}

// ── Polling: Logs ──────────────────────────────────────────────────────────
async function pollLogs() {
    if (document.querySelector('.tab-content[data-tab="logs"]').classList.contains('active')) {
        try {
            const text = await apiCall('/api/log');
            state.logLines = text.split('\n').filter(l => l.trim());
            updateLogsTab();
        } catch (e) {
            // silent
        }
    }
}

// ── Polling: Config ────────────────────────────────────────────────────────
async function loadConfig() {
    try {
        const data = await apiCall('/api/config');
        state.config = data;
        state.configDirty = false;
        updateSlidersFromConfig();

        // Set trading mode from config
        const paperMode = (data.PAPER_MODE || 'true').toLowerCase();
        const useDemoStr = (data.USE_DEMO || 'true').toLowerCase();
        if (paperMode === 'false' && useDemoStr === 'false') {
            state.tradingMode = 'LIVE';
        } else {
            state.tradingMode = 'PAPER';
        }
        updateConnectionUI();
        updateControlsTab();

        const modeLabel = document.getElementById('mode-label');
        if (modeLabel) {
            if (state.tradingMode === 'LIVE') {
                modeLabel.innerHTML = '<span style="color:var(--red)">⚠ LIVE TRADING — REAL MONEY AT RISK</span>';
            } else {
                modeLabel.innerHTML = '<span class="text-amber">⚠ Paper trading — no real money at risk</span>';
            }
        }
    } catch (e) {
        showToast('Failed to load config', 'error');
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  TAB 1: DASHBOARD RENDERING
// ══════════════════════════════════════════════════════════════════════════

function updateDashboardTab() {
    // Metric cards
    const metricDefs = [
        { id: 'portfolio-value',  value: state.metrics.portfolio_value, prefix: '$', decimals: 2, cls: 'green' },
        { id: 'cash-balance',     value: state.metrics.cash_balance,    prefix: '$', decimals: 2, cls: 'cyan' },
        { id: 'open-positions',   value: state.metrics.open_positions,  prefix: '',  decimals: 0, cls: 'amber' },
        { id: 'win-rate',         value: state.metrics.win_rate,        prefix: '',  decimals: 1, suffix: '%', cls: 'green' },
        { id: 'total-pnl',       value: state.metrics.total_pnl,       prefix: '$', decimals: 2, cls: state.metrics.total_pnl >= 0 ? 'green' : 'red' },
        { id: 'session-pnl',     value: state.metrics.session_pnl,     prefix: '$', decimals: 2, cls: state.metrics.session_pnl >= 0 ? 'green' : 'red' },
    ];

    metricDefs.forEach(def => {
        const el = document.getElementById(`metric-${def.id}`);
        if (!el) return;
        const sign = def.value > 0 && def.id.includes('pnl') ? '+' : '';
        const val = `${sign}${def.prefix}${Number(def.value).toFixed(def.decimals)}${def.suffix || ''}`;
        const valueEl = el.querySelector('.metric-value');
        if (valueEl && valueEl.textContent !== val) {
            valueEl.textContent = val;
            valueEl.className = `metric-value ${def.cls}`;
            el.classList.add('flash');
            setTimeout(() => el.classList.remove('flash'), 500);
        }
    });

    // Positions table
    renderPositionsTable();

    // Trades table
    renderTradesTable();

    // Signals
    updateSignals();

    // Performance stats
    updatePerfStats();
}

function renderPositionsTable() {
    const tbody = document.getElementById('positions-tbody');
    if (!tbody) return;

    const posCount = document.getElementById('positions-count');
    if (posCount) posCount.textContent = `${state.positions.length} open`;

    if (state.positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-table">No open positions</td></tr>';
        return;
    }

    tbody.innerHTML = state.positions.map(p => {
        const pnl = p.pnl || 0;
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';
        const pnlSign = pnl >= 0 ? '+' : '';
        const sideClass = p.side === 'yes' ? 'text-green' : 'text-red';
        return `<tr>
            <td class="text-bright">${p.ticker}</td>
            <td class="${sideClass}">${(p.side || '').toUpperCase()}</td>
            <td>${p.entry_price.toFixed(2)}¢</td>
            <td>${p.current_price.toFixed(2)}¢</td>
            <td class="${pnlClass}">${pnlSign}$${pnl.toFixed(4)}</td>
            <td class="text-dim">${p.settlement}</td>
            <td>${p.vpin.toFixed(3)}</td>
        </tr>`;
    }).join('');
}

function renderTradesTable() {
    const tbody = document.getElementById('trades-tbody');
    if (!tbody) return;

    if (state.trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-table">No trades recorded</td></tr>';
        return;
    }

    const recent = state.trades.slice(-50).reverse();
    tbody.innerHTML = recent.map(t => {
        const pnl = t.pnl || 0;
        const pnlClass = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral';
        const pnlSign = pnl > 0 ? '+' : '';
        return `<tr>
            <td class="text-dim">${t.time || '—'}</td>
            <td class="${pnl >= 0 ? 'text-green' : 'text-red'}">${t.action || '—'}</td>
            <td class="text-bright">${t.ticker || '—'}</td>
            <td>${t.side || '—'}</td>
            <td>$${(t.price || 0).toFixed(2)}</td>
            <td>${t.quantity || 0}</td>
            <td class="${pnlClass}">${pnlSign}$${pnl.toFixed(4)}</td>
        </tr>`;
    }).join('');
}

function updateSignals() {
    // RSI gauge
    updateGauge('rsi-gauge', state.signals.rsi, 100, state.signals.rsi > 70 || state.signals.rsi < 30 ? 'var(--red)' : 'var(--green)');
    const rsiVal = document.getElementById('rsi-value');
    if (rsiVal) rsiVal.textContent = state.signals.rsi.toFixed(1);

    // VPIN gauge
    updateGauge('vpin-gauge', state.signals.vpin * 100, 100, state.signals.vpin > 0.7 ? 'var(--red)' : state.signals.vpin > 0.5 ? 'var(--amber)' : 'var(--green)');
    const vpinVal = document.getElementById('vpin-value');
    if (vpinVal) vpinVal.textContent = state.signals.vpin.toFixed(3);

    // Confidence bar
    const confFill = document.getElementById('confidence-fill');
    if (confFill) confFill.style.width = `${Math.min(state.signals.confidence * 100, 100)}%`;
    const confVal = document.getElementById('confidence-value');
    if (confVal) confVal.textContent = (state.signals.confidence * 100).toFixed(1) + '%';

    // Direction arrow
    const dirEl = document.getElementById('direction-arrow');
    if (dirEl) {
        if (state.signals.direction === 'YES' || state.signals.direction === 'LONG' || state.signals.direction === 'UP') {
            dirEl.innerHTML = '<span class="text-green" style="font-size:32px">▲</span>';
        } else if (state.signals.direction === 'NO' || state.signals.direction === 'SHORT' || state.signals.direction === 'DOWN') {
            dirEl.innerHTML = '<span class="text-red" style="font-size:32px">▼</span>';
        } else {
            dirEl.innerHTML = '<span class="text-dim" style="font-size:32px">◆</span>';
        }
        const dirLabel = document.getElementById('direction-label');
        if (dirLabel) dirLabel.textContent = state.signals.direction;
    }

    // Kelly Size
    const kellyVal = document.getElementById('kelly-value');
    if (kellyVal) kellyVal.textContent = '$' + state.signals.kelly_size.toFixed(2);
}

function updateGauge(id, value, max, color) {
    const el = document.getElementById(id);
    if (!el) return;
    const circle = el.querySelector('.gauge-fill');
    if (!circle) return;
    const radius = 24;
    const circumference = 2 * Math.PI * radius;
    const offset = circumference - (value / max) * circumference;
    circle.style.strokeDasharray = `${circumference}`;
    circle.style.strokeDashoffset = `${offset}`;
    circle.style.stroke = color;
}

function updatePerfStats() {
    setTextContent('stat-total-trades', state.perfStats.total_trades);
    setTextContent('stat-avg-hold', state.perfStats.avg_hold_time);
    const best = state.perfStats.best_trade;
    const worst = state.perfStats.worst_trade;
    setTextContent('stat-best-trade', `${best >= 0 ? '+' : ''}$${best.toFixed(2)}`);
    setTextContent('stat-worst-trade', `${worst >= 0 ? '+' : ''}$${worst.toFixed(2)}`);
}

function setTextContent(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

// ══════════════════════════════════════════════════════════════════════════
//  TAB 2: CONTROLS
// ══════════════════════════════════════════════════════════════════════════

function updateControlsTab() {
    const statusVal = document.getElementById('ctrl-status-val');
    if (statusVal) {
        statusVal.textContent = state.botStatus.toUpperCase();
        statusVal.className = 'value ' + (state.botStatus === 'active' ? 'text-green glow-green' : 'text-red');
    }

    const uptimeVal = document.getElementById('ctrl-uptime-val');
    if (uptimeVal && state.activeSince) {
        const since = new Date(state.activeSince);
        const diff = Date.now() - since.getTime();
        if (diff > 0) {
            const hrs = Math.floor(diff / 3600000);
            const mins = Math.floor((diff % 3600000) / 60000);
            const secs = Math.floor((diff % 60000) / 1000);
            uptimeVal.textContent = `${hrs}h ${mins}m ${secs}s`;
        }
    } else if (uptimeVal) {
        uptimeVal.textContent = '—';
    }

    const pidVal = document.getElementById('ctrl-pid-val');
    if (pidVal) {
        pidVal.textContent = state.botPid || '—';
    }

    // Toggle state
    const toggle = document.getElementById('mode-toggle');
    if (toggle) {
        toggle.className = `toggle-switch ${state.tradingMode.toLowerCase()}`;
    }
}

async function botAction(action) {
    const btn = document.querySelector(`.ctrl-btn.${action}`);
    if (btn) {
        btn.disabled = true;
        btn.classList.add('loading');
    }

    try {
        const data = await apiCall(`/api/bot/${action}`, 'POST');
        if (data.success) {
            showToast(`Bot ${action} successful`, 'success');
        } else {
            showToast(`Bot ${action} failed: ${data.stderr || data.error || 'Unknown'}`, 'error');
        }
        setTimeout(pollStatus, 1000);
    } catch (e) {
        showToast(`Bot ${action} failed: ${e.message}`, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('loading');
        }
    }
}

function toggleTradingMode() {
    if (state.tradingMode === 'PAPER') {
        // Show confirmation modal for switching to LIVE
        document.getElementById('live-modal').classList.add('visible');
        document.getElementById('confirm-input').value = '';
        document.getElementById('confirm-btn').disabled = true;
    } else {
        // Switch back to paper — no confirmation needed
        applyTradingMode('PAPER');
    }
}

function applyTradingMode(mode) {
    state.tradingMode = mode;
    updateControlsTab();

    const configUpdate = {
        PAPER_MODE: mode === 'PAPER' ? 'true' : 'false',
    };

    apiCall('/api/config', 'POST', configUpdate)
        .then(() => showToast(`Switched to ${mode} mode`, mode === 'LIVE' ? 'warning' : 'success'))
        .catch(e => showToast(`Failed to switch mode: ${e.message}`, 'error'));
}

// ── Slider Definitions ─────────────────────────────────────────────────────
const SLIDER_DEFS = {
    // Strategy
    MIN_CONFIDENCE:         { label: 'Signal Confidence Threshold', min: 0.01, max: 0.20, step: 0.01, section: 'strategy' },
    MIN_CONTRACT_PRICE:     { label: 'Min Contract Price (¢)',      min: 5,    max: 50,   step: 1,    section: 'strategy' },
    MAX_CONTRACT_PRICE:     { label: 'Max Contract Price (¢)',      min: 50,   max: 99,   step: 1,    section: 'strategy' },
    KELLY_FRACTION:         { label: 'Kelly Fraction',              min: 0.05, max: 0.50, step: 0.01, section: 'strategy' },
    MIN_EV_CENTS:           { label: 'Min Expected Value (¢)',      min: 0.5,  max: 5.0,  step: 0.1,  section: 'strategy' },
    PROFIT_TARGET_PCT:      { label: 'Profit Target %',             min: 0.10, max: 1.00, step: 0.05, section: 'strategy' },
    STOP_LOSS_PCT:          { label: 'Stop Loss %',                 min: 0.10, max: 0.60, step: 0.05, section: 'strategy' },
    VPIN_EXIT_THRESHOLD:    { label: 'VPIN Exit Threshold',         min: 0.50, max: 1.00, step: 0.01, section: 'strategy' },
    // Risk
    MAX_CONCURRENT_POSITIONS:   { label: 'Max Positions',           min: 1,    max: 10,   step: 1,    section: 'risk' },
    MAX_TOTAL_EXPOSURE_DOLLARS: { label: 'Max Exposure ($)',        min: 10,   max: 500,  step: 5,    section: 'risk' },
    MAX_POSITION_SIZE_DOLLARS:  { label: 'Max Position Size ($)',   min: 5,    max: 100,  step: 1,    section: 'risk' },
    DAILY_LOSS_LIMIT_DOLLARS:   { label: 'Daily Loss Limit ($)',    min: 5,    max: 100,  step: 1,    section: 'risk' },
    // Timing
    LOOP_INTERVAL_SECONDS:  { label: 'Loop Interval (s)',           min: 5,    max: 60,   step: 1,    section: 'timing' },
    SCAN_INTERVAL_SECONDS:  { label: 'Scan Interval (s)',           min: 15,   max: 120,  step: 5,    section: 'timing' },
};

function updateSlidersFromConfig() {
    for (const [key, def] of Object.entries(SLIDER_DEFS)) {
        const slider = document.getElementById(`slider-${key}`);
        const display = document.getElementById(`display-${key}`);
        if (slider && state.config[key] !== undefined) {
            slider.value = parseFloat(state.config[key]);
            if (display) display.textContent = formatSliderValue(key, parseFloat(state.config[key]));
        }
    }
    state.configDirty = false;
    updateApplyButton();
}

function formatSliderValue(key, val) {
    const def = SLIDER_DEFS[key];
    if (!def) return val;
    if (def.step < 1) return val.toFixed(2);
    return val.toString();
}

function onSliderChange(key, value) {
    const display = document.getElementById(`display-${key}`);
    if (display) display.textContent = formatSliderValue(key, parseFloat(value));
    state.pendingConfig[key] = value;
    state.configDirty = true;
    updateApplyButton();
}

function updateApplyButton() {
    const btn = document.getElementById('apply-config-btn');
    if (!btn) return;
    if (state.configDirty) {
        btn.disabled = false;
        btn.classList.add('pending');
        btn.textContent = '⚡ APPLY CHANGES';
    } else {
        btn.disabled = true;
        btn.classList.remove('pending');
        btn.textContent = 'NO CHANGES';
    }
}

async function applyConfig() {
    if (!state.configDirty) return;
    const btn = document.getElementById('apply-config-btn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'APPLYING...';
    }

    try {
        await apiCall('/api/config', 'POST', state.pendingConfig);
        showToast('Configuration updated & bot restarted', 'success');
        state.pendingConfig = {};
        state.configDirty = false;
        updateApplyButton();
        setTimeout(pollStatus, 2000);
    } catch (e) {
        showToast(`Config update failed: ${e.message}`, 'error');
        if (btn) btn.disabled = false;
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  TAB 3: LOGS
// ══════════════════════════════════════════════════════════════════════════

function updateLogsTab() {
    const viewer = document.getElementById('log-viewer');
    if (!viewer) return;

    const filter = state.logFilter.toLowerCase();
    const filtered = filter
        ? state.logLines.filter(l => l.toLowerCase().includes(filter))
        : state.logLines;

    viewer.innerHTML = filtered.map(line => {
        let cls = '';
        const lower = line.toLowerCase();
        if (lower.includes('error') || lower.includes('exception') || lower.includes('traceback')) cls = 'error';
        else if (lower.includes('warning') || lower.includes('warn')) cls = 'warning';
        else if (lower.includes('debug')) cls = 'debug';
        else cls = 'info';

        if (filter && lower.includes(filter)) cls += ' highlight';

        const escaped = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        return `<div class="log-line ${cls}">${escaped}</div>`;
    }).join('');

    if (state.logAutoScroll) {
        viewer.scrollTop = viewer.scrollHeight;
    }
}

function toggleAutoScroll() {
    state.logAutoScroll = !state.logAutoScroll;
    const btn = document.getElementById('autoscroll-btn');
    if (btn) {
        btn.classList.toggle('active', state.logAutoScroll);
        btn.textContent = state.logAutoScroll ? '⤓ AUTO-SCROLL ON' : '⤓ AUTO-SCROLL OFF';
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  CHARTS (TradingView Lightweight Charts)
// ══════════════════════════════════════════════════════════════════════════

let mainChart = null;
let mainSeries = null;

async function initBTCChart() {
    const container = document.getElementById('btc-chart');
    if (!container || typeof LightweightCharts === 'undefined') return;

    mainChart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 260,
        layout: {
            background: { type: 'solid', color: '#0a0e0a' },
            textColor: '#7a9f7a',
            fontFamily: 'JetBrains Mono',
            fontSize: 10,
        },
        grid: {
            vertLines: { color: '#00ff4108' },
            horzLines: { color: '#00ff4108' },
        },
        crosshair: {
            mode: 0,
            vertLine: { color: '#00ff4140', width: 1, style: 2 },
            horzLine: { color: '#00ff4140', width: 1, style: 2 },
        },
        rightPriceScale: {
            borderColor: '#00ff4120',
            scaleMargins: { top: 0.1, bottom: 0.1 },
        },
        timeScale: {
            borderColor: '#00ff4120',
            timeVisible: true,
            secondsVisible: false,
        },
    });

    mainSeries = mainChart.addAreaSeries({
        topColor: 'rgba(0, 255, 65, 0.25)',
        bottomColor: 'rgba(0, 255, 65, 0.01)',
        lineColor: '#00ff41',
        lineWidth: 2,
    });

    fetchBTCData();

    // Resize observer
    const observer = new ResizeObserver(() => {
        mainChart.applyOptions({ width: container.clientWidth });
    });
    observer.observe(container);
}

async function fetchBTCData() {
    try {
        const resp = await fetch('https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1');
        const data = await resp.json();
        if (data.prices && mainSeries) {
            const chartData = data.prices.map(([ts, price]) => ({
                time: Math.floor(ts / 1000),
                value: price,
            }));
            mainSeries.setData(chartData);

            // Update BTC price display
            const latest = chartData[chartData.length - 1];
            if (latest) {
                setTextContent('btc-price', '$' + latest.value.toLocaleString(undefined, { maximumFractionDigits: 0 }));
            }
        }
    } catch (e) {
        console.warn('BTC chart fetch failed:', e);
    }
}

async function fetchSparklineData() {
    const coins = ['ethereum', 'solana', 'ripple'];
    const ids = { ethereum: 'eth', solana: 'sol', ripple: 'xrp' };
    
    for (const coin of coins) {
        try {
            const resp = await fetch(`https://api.coingecko.com/api/v3/coins/${coin}/market_chart?vs_currency=usd&days=1`);
            const data = await resp.json();
            if (data.prices) {
                const ticker = ids[coin];
                const latest = data.prices[data.prices.length - 1];
                const first = data.prices[0];
                const change = ((latest[1] - first[1]) / first[1] * 100);
                
                setTextContent(`${ticker}-price`, '$' + latest[1].toLocaleString(undefined, { maximumFractionDigits: 2 }));
                const changeEl = document.getElementById(`${ticker}-change`);
                if (changeEl) {
                    changeEl.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
                    changeEl.className = `sparkline-price ${change < 0 ? 'negative' : ''}`;
                }

                drawMiniSparkline(`${ticker}-sparkline`, data.prices, change >= 0 ? '#00ff41' : '#ff0033');
            }
        } catch (e) {
            console.warn(`Sparkline ${coin} failed:`, e);
        }
    }
}

function drawMiniSparkline(canvasId, prices, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth * 2;
    const H = canvas.height = canvas.clientHeight * 2;
    ctx.scale(2, 2);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;

    const vals = prices.map(p => p[1]);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 1;

    ctx.clearRect(0, 0, w, h);
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;

    prices.forEach(([, val], i) => {
        const x = (i / (prices.length - 1)) * w;
        const y = h - ((val - min) / range) * h;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();
}

// ══════════════════════════════════════════════════════════════════════════
//  TAB NAVIGATION
// ══════════════════════════════════════════════════════════════════════════

function switchTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.dataset.tab === tabName);
    });

    if (tabName === 'logs') {
        pollLogs();
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  MODAL HANDLING
// ══════════════════════════════════════════════════════════════════════════

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('visible');
}

document.addEventListener('DOMContentLoaded', () => {
    const confirmInput = document.getElementById('confirm-input');
    const confirmBtn = document.getElementById('confirm-btn');
    if (confirmInput && confirmBtn) {
        confirmInput.addEventListener('input', () => {
            confirmBtn.disabled = confirmInput.value.trim().toUpperCase() !== 'CONFIRM';
        });
        confirmBtn.addEventListener('click', () => {
            applyTradingMode('LIVE');
            closeModal('live-modal');
        });
    }
});

// ══════════════════════════════════════════════════════════════════════════
//  SPORTS TRADING
// ══════════════════════════════════════════════════════════════════════════

async function fetchSportsData() {
    try {
        const data = await apiCall('/api/sports');
        if (!data || data.error) return;

        // Update header stats
        setElText('sports-live-count', data.live_games_count || 0);
        setElText('sports-total-games', data.total_games || 0);
        setElText('sports-spread-count', data.spread_markets_count || 0);
        setElText('sports-total-mkts', data.total_markets_count || 0);

        // Status dot
        const dot = document.getElementById('sports-status-dot');
        const statusText = document.getElementById('sports-status-text');
        if (dot && statusText) {
            if (data.live_games_count > 0) {
                dot.className = 'sports-dot live';
                statusText.textContent = `${data.live_games_count} LIVE GAMES`;
            } else if (data.total_games > 0) {
                dot.className = 'sports-dot idle';
                statusText.textContent = 'Pre-game';
            } else {
                dot.className = 'sports-dot';
                statusText.textContent = 'No games today';
            }
        }

        // Render game cards
        renderSportsGames(data.games || []);

        // Render markets table
        renderSportsMarkets(data.top_markets || []);
    } catch (e) {
        console.warn('Sports fetch failed:', e);
    }
}

function renderSportsGames(games) {
    const container = document.getElementById('sports-games-container');
    if (!container) return;

    if (games.length === 0) {
        container.innerHTML = '<div class="sports-empty">No NBA games scheduled today</div>';
        return;
    }

    container.innerHTML = games.map(g => {
        const isLive = g.status === 'in';
        const isPre = g.status === 'pre';
        const homeWp = g.home_win_prob != null ? (g.home_win_prob * 100).toFixed(0) : null;
        const awayWp = g.away_win_prob != null ? (g.away_win_prob * 100).toFixed(0) : null;

        return `
        <div class="sports-game-card ${isLive ? 'live' : ''}">
            <div class="sports-game-header">
                <span class="sports-game-status ${isLive ? 'live' : isPre ? 'pre' : 'post'}">
                    ${isLive ? '● LIVE' : isPre ? 'PRE' : 'FINAL'}
                </span>
                <span class="sports-game-time">${g.start_time_display || g.period || ''} ${g.clock || ''}</span>
            </div>
            <div class="sports-matchup">
                <div class="sports-team">
                    <div class="sports-team-abbr">${g.away_abbr || '?'}</div>
                    <div class="sports-team-name">${g.away_team || ''}</div>
                    ${g.away_record ? `<div class="sports-record">${g.away_record}</div>` : ''}
                </div>
                <div class="sports-score-display">
                    ${isLive || g.status === 'post'
                        ? `${g.away_score ?? '?'} – ${g.home_score ?? '?'}`
                        : '<span class="sports-vs">@</span>'}
                </div>
                <div class="sports-team">
                    <div class="sports-team-abbr">${g.home_abbr || '?'}</div>
                    <div class="sports-team-name">${g.home_team || ''}</div>
                    ${g.home_record ? `<div class="sports-record">${g.home_record}</div>` : ''}
                </div>
            </div>
            ${homeWp && awayWp ? `
            <div class="sports-wp">
                <div class="sports-wp-bar" style="width:${awayWp}%" title="${g.away_abbr}: ${awayWp}% win"></div>
                <div class="sports-wp-bar home" style="width:${homeWp}%" title="${g.home_abbr}: ${homeWp}% win"></div>
            </div>` : ''}
            <div class="sports-game-footer">
                <span>${g.venue || ''}</span>
                <span>${g.markets_count ? g.markets_count + ' markets' : ''}</span>
            </div>
        </div>`;
    }).join('');
}

function renderSportsMarkets(markets) {
    const tbody = document.getElementById('sports-markets-tbody');
    if (!tbody) return;

    if (markets.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-table">No markets available</td></tr>';
        return;
    }

    tbody.innerHTML = markets.slice(0, 15).map(m => {
        const bid = m.best_bid != null ? (m.best_bid * 100).toFixed(0) + '¢' : '--';
        const ask = m.best_ask != null ? (m.best_ask * 100).toFixed(0) + '¢' : '--';
        const vol = m.volume != null ? m.volume.toLocaleString() : '--';
        const last = m.last_price != null ? (m.last_price * 100).toFixed(0) + '¢' : '--';
        return `<tr>
            <td class="text-bright" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${m.title || ''}">${m.title || '--'}</td>
            <td class="text-dim">${m.market_type || '--'}</td>
            <td>${bid}</td>
            <td>${ask}</td>
            <td style="color:var(--cyan);font-weight:600;">${vol}</td>
            <td>${last}</td>
        </tr>`;
    }).join('');
}

// ══════════════════════════════════════════════════════════════════════════
//  INITIALIZATION
// ══════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', async () => {
    // Start clock
    setInterval(updateClock, 100);
    updateClock();

    // Initial data load
    await Promise.all([
        loadConfig(),
        pollStatus(),
        pollState(),
        fetchSportsData(),
    ]);

    // Init BTC chart
    initBTCChart();
    fetchSparklineData();

    // Polling intervals
    setInterval(pollState,  POLL_STATE_MS);
    setInterval(pollStatus, POLL_STATUS_MS);
    setInterval(pollLogs,   POLL_LOG_MS);
    setInterval(fetchBTCData, 60000);
    setInterval(fetchSparklineData, 60000);
    setInterval(fetchSportsData, 30000);
});

function setElText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}
