/* FREYJA QUANT ENGINE v2 - Dashboard Application */

const API_BASE = '';
const API_TOKEN = 'freyja-ctrl-2026';
const POLL_STATE_MS = 5000;
const POLL_STATUS_MS = 10000;
const POLL_LOG_MS = 10000;

const state = {
    connected: false, botStatus: 'unknown', botPid: null, activeSince: null,
    latencyMs: null, tradingMode: 'PAPER', config: {}, configDirty: false,
    pendingConfig: {}, positions: [], trades: [],
    metrics: { portfolio_value: 0, cash_balance: 0, open_positions: 0, win_rate: 0, total_pnl: 0, session_pnl: 0 },
    signals: { rsi: 50, vpin: 0.5, confidence: 0, direction: 'NEUTRAL', kelly_size: 0 },
    perfStats: { total_trades: 0, avg_hold_time: '0m', best_trade: 0, worst_trade: 0 },
    logLines: [], logFilter: '', logAutoScroll: true, previousMetrics: {}
};

async function apiCall(endpoint, method = 'GET', body = null) {
    const start = performance.now();
    const opts = { method, headers: { 'Authorization': `Bearer ${API_TOKEN}`, 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    try {
        const resp = await fetch(`${API_BASE}${endpoint}`, opts);
        const elapsed = Math.round(performance.now() - start);
        state.latencyMs = elapsed; state.connected = true; updateConnectionUI();
        if (!resp.ok) { const err = await resp.json().catch(() => ({ error: resp.statusText })); throw new Error(err.error || `HTTP ${resp.status}`); }
        const contentType = resp.headers.get('content-type') || '';
        if (contentType.includes('application/json')) return await resp.json();
        return await resp.text();
    } catch (err) {
        if (err.message === 'Failed to fetch' || err.name === 'TypeError') { state.connected = false; state.latencyMs = null; updateConnectionUI(); }
        throw err;
    }
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`; toast.textContent = message;
    container.appendChild(toast); setTimeout(() => toast.remove(), 4000);
}

function updateConnectionUI() {
    const badge = document.getElementById('status-badge');
    const latencyEl = document.getElementById('latency-value');
    if (!state.connected) { badge.className = 'status-badge disconnected'; badge.innerHTML = '<span class="dot"></span>DISCONNECTED'; latencyEl.textContent = '---'; latencyEl.className = 'value'; return; }
    if (state.botStatus === 'active') {
        const mode = state.tradingMode === 'LIVE' ? 'running' : 'paper';
        badge.className = `status-badge ${mode}`;
        const label = state.tradingMode === 'LIVE' ? 'LIVE' : 'PAPER';
        badge.innerHTML = `<span class="dot"></span>${label} — RUNNING`;
    } else { badge.className = 'status-badge stopped'; badge.innerHTML = '<span class="dot"></span>STOPPED'; }
    if (state.latencyMs !== null) {
        latencyEl.textContent = `${state.latencyMs}ms`;
        if (state.latencyMs < 200) latencyEl.className = 'value latency-good';
        else if (state.latencyMs < 500) latencyEl.className = 'value latency-warn';
        else latencyEl.className = 'value latency-bad';
    }
}

function updateClock() {
    const el = document.getElementById('system-time'); const now = new Date();
    const h = String(now.getHours()).padStart(2,'0'); const m = String(now.getMinutes()).padStart(2,'0');
    const s = String(now.getSeconds()).padStart(2,'0'); const ms = String(now.getMilliseconds()).padStart(3,'0');
    el.textContent = `${h}:${m}:${s}.${ms}`;
}

async function pollStatus() {
    try { const data = await apiCall('/api/status'); state.botStatus = data.status||'unknown'; state.botPid = data.pid||null; state.activeSince = data.active_since||null; updateConnectionUI(); updateControlsTab(); } catch(e) {}
}

async function pollState() {
    try {
        const [rawState, balance] = await Promise.all([apiCall('/api/state').catch(()=>null), apiCall('/api/balance').catch(()=>null)]);
        state.previousMetrics = {...state.metrics};
        if (balance && !balance.error) { state.metrics.portfolio_value = balance.portfolio_value??0; state.metrics.cash_balance = balance.cash_balance??0; state.metrics.total_pnl = balance.total_pnl??0; state.metrics.session_pnl = balance.session_pnl??0; state.metrics.win_rate = balance.win_rate??0; state.metrics.open_positions = balance.open_positions??0; }
        if (rawState && !rawState.error) {
            const positionsObj = rawState.positions||{};
            state.positions = Object.values(positionsObj).map(p => ({ ticker: p.ticker||'—', side: p.side||'—', entry_price: (p.entry_price||0)/100, current_price: (p.current_price||p.entry_price||0)/100, pnl: p.current_price&&p.entry_price ? ((p.current_price-p.entry_price)/100)*(p.contracts||1) : 0, contracts: p.contracts||0, settlement: p.entry_time ? new Date(p.entry_time*1000).toLocaleTimeString() : '—', vpin: p.vpin_at_entry||0 }));
            state.metrics.open_positions = state.positions.length;
            const rawTrades = rawState.trades||[];
            state.trades = rawTrades.map(t => ({ time: t.exit_time ? new Date(t.exit_time*1000).toLocaleTimeString() : '—', action: t.pnl_dollars>=0?'WIN':'LOSS', ticker: t.ticker||'—', side: (t.side||'—').toUpperCase(), price: (t.exit_price||0)/100, quantity: t.contracts||0, pnl: t.pnl_dollars||0 }));
            state.perfStats.total_trades = rawTrades.length;
            if (rawTrades.length > 0) {
                const pnls = rawTrades.map(t=>t.pnl_dollars||0);
                state.perfStats.best_trade = Math.max(...pnls); state.perfStats.worst_trade = Math.min(...pnls);
                const holdTimes = rawTrades.filter(t=>t.entry_time&&t.exit_time).map(t=>t.exit_time-t.entry_time);
                if (holdTimes.length>0) { const avgSecs=holdTimes.reduce((a,b)=>a+b,0)/holdTimes.length; if(avgSecs<60)state.perfStats.avg_hold_time=Math.round(avgSecs)+'s'; else if(avgSecs<3600)state.perfStats.avg_hold_time=Math.round(avgSecs/60)+'m'; else state.perfStats.avg_hold_time=(avgSecs/3600).toFixed(1)+'h'; }
            }
        }
        updateDashboardTab();
    } catch(e) {}
}

async function pollLogs() {
    if (document.querySelector('.tab-content[data-tab="logs"]').classList.contains('active')) {
        try { const text = await apiCall('/api/log'); state.logLines = text.split('\n').filter(l=>l.trim()); updateLogsTab(); } catch(e) {}
    }
}

async function loadConfig() {
    try {
        const data = await apiCall('/api/config'); state.config = data; state.configDirty = false; updateSlidersFromConfig();
        const paperMode = (data.PAPER_MODE||'true').toLowerCase(); const useDemoStr = (data.USE_DEMO||'true').toLowerCase();
        if (paperMode==='false' && useDemoStr==='false') state.tradingMode='LIVE'; else state.tradingMode='PAPER';
        updateConnectionUI(); updateControlsTab();
        const modeLabel = document.getElementById('mode-label');
        if (modeLabel) { if(state.tradingMode==='LIVE') modeLabel.innerHTML='<span style="color:var(--red)">⚠ LIVE TRADING — REAL MONEY AT RISK</span>'; else modeLabel.innerHTML='<span class="text-amber">⚠ Paper trading — no real money at risk</span>'; }
    } catch(e) { showToast('Failed to load config','error'); }
}

function updateDashboardTab() {
    const metricDefs = [
        {id:'portfolio-value',value:state.metrics.portfolio_value,prefix:'$',decimals:2,cls:'green'},
        {id:'cash-balance',value:state.metrics.cash_balance,prefix:'$',decimals:2,cls:'cyan'},
        {id:'open-positions',value:state.metrics.open_positions,prefix:'',decimals:0,cls:'amber'},
        {id:'win-rate',value:state.metrics.win_rate,prefix:'',decimals:1,suffix:'%',cls:'green'},
        {id:'total-pnl',value:state.metrics.total_pnl,prefix:'$',decimals:2,cls:state.metrics.total_pnl>=0?'green':'red'},
        {id:'session-pnl',value:state.metrics.session_pnl,prefix:'$',decimals:2,cls:state.metrics.session_pnl>=0?'green':'red'}
    ];
    metricDefs.forEach(def => {
        const el = document.getElementById(`metric-${def.id}`); if(!el) return;
        const sign = def.value>0 && def.id.includes('pnl') ? '+' : '';
        const val = `${sign}${def.prefix}${Number(def.value).toFixed(def.decimals)}${def.suffix||''}`;
        const valueEl = el.querySelector('.metric-value');
        if (valueEl && valueEl.textContent!==val) { valueEl.textContent=val; valueEl.className=`metric-value ${def.cls}`; el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),500); }
    });
    renderPositionsTable(); renderTradesTable(); updateSignals(); updatePerfStats();
}

function renderPositionsTable() {
    const tbody = document.getElementById('positions-tbody'); if(!tbody) return;
    const posCount = document.getElementById('positions-count'); if(posCount) posCount.textContent=`${state.positions.length} open`;
    if(state.positions.length===0) { tbody.innerHTML='<tr><td colspan="7" class="empty-table">No open positions</td></tr>'; return; }
    tbody.innerHTML = state.positions.map(p => { const pnl=p.pnl||0; const pnlClass=pnl>=0?'positive':'negative'; const pnlSign=pnl>=0?'+':''; const sideClass=p.side==='yes'?'text-green':'text-red'; return `<tr><td class="text-bright">${p.ticker}</td><td class="${sideClass}">${(p.side||'').toUpperCase()}</td><td>${p.entry_price.toFixed(2)}¢</td><td>${p.current_price.toFixed(2)}¢</td><td class="${pnlClass}">${pnlSign}$${pnl.toFixed(4)}</td><td class="text-dim">${p.settlement}</td><td>${p.vpin.toFixed(3)}</td></tr>`; }).join('');
}

function renderTradesTable() {
    const tbody = document.getElementById('trades-tbody'); if(!tbody) return;
    if(state.trades.length===0) { tbody.innerHTML='<tr><td colspan="7" class="empty-table">No trades recorded</td></tr>'; return; }
    const recent = state.trades.slice(-50).reverse();
    tbody.innerHTML = recent.map(t => { const pnl=t.pnl||0; const pnlClass=pnl>0?'positive':pnl<0?'negative':'neutral'; const pnlSign=pnl>0?'+':''; return `<tr><td class="text-dim">${t.time||'—'}</td><td class="${pnl>=0?'text-green':'text-red'}">${t.action||'—'}</td><td class="text-bright">${t.ticker||'—'}</td><td>${t.side||'—'}</td><td>$${(t.price||0).toFixed(2)}</td><td>${t.quantity||0}</td><td class="${pnlClass}">${pnlSign}$${pnl.toFixed(4)}</td></tr>`; }).join('');
}

function updateSignals() {
    updateGauge('rsi-gauge',state.signals.rsi,100,state.signals.rsi>70||state.signals.rsi<30?'var(--red)':'var(--green)');
    const rsiVal=document.getElementById('rsi-value'); if(rsiVal) rsiVal.textContent=state.signals.rsi.toFixed(1);
    updateGauge('vpin-gauge',state.signals.vpin*100,100,state.signals.vpin>0.7?'var(--red)':state.signals.vpin>0.5?'var(--amber)':'var(--green)');
    const vpinVal=document.getElementById('vpin-value'); if(vpinVal) vpinVal.textContent=state.signals.vpin.toFixed(3);
    const confFill=document.getElementById('confidence-fill'); if(confFill) confFill.style.width=`${Math.min(state.signals.confidence*100,100)}%`;
    const confVal=document.getElementById('confidence-value'); if(confVal) confVal.textContent=(state.signals.confidence*100).toFixed(1)+'%';
    const dirEl=document.getElementById('direction-arrow');
    if(dirEl) {
        if(state.signals.direction==='YES'||state.signals.direction==='LONG'||state.signals.direction==='UP') dirEl.innerHTML='<span class="text-green" style="font-size:32px">▲</span>';
        else if(state.signals.direction==='NO'||state.signals.direction==='SHORT'||state.signals.direction==='DOWN') dirEl.innerHTML='<span class="text-red" style="font-size:32px">▼</span>';
        else dirEl.innerHTML='<span class="text-dim" style="font-size:32px">◆</span>';
        const dirLabel=document.getElementById('direction-label'); if(dirLabel) dirLabel.textContent=state.signals.direction;
    }
    const kellyVal=document.getElementById('kelly-value'); if(kellyVal) kellyVal.textContent='$'+state.signals.kelly_size.toFixed(2);
}

function updateGauge(id,value,max,color) {
    const el=document.getElementById(id); if(!el) return;
    const circle=el.querySelector('.gauge-fill'); if(!circle) return;
    const circumference=2*Math.PI*24;
    circle.style.strokeDasharray=`${circumference}`; circle.style.strokeDashoffset=`${circumference-(value/max)*circumference}`; circle.style.stroke=color;
}

function updatePerfStats() {
    setTextContent('stat-total-trades',state.perfStats.total_trades); setTextContent('stat-avg-hold',state.perfStats.avg_hold_time);
    const best=state.perfStats.best_trade; const worst=state.perfStats.worst_trade;
    setTextContent('stat-best-trade',`${best>=0?'+':''}$${best.toFixed(2)}`); setTextContent('stat-worst-trade',`${worst>=0?'+':''}$${worst.toFixed(2)}`);
}

function setTextContent(id,val) { const el=document.getElementById(id); if(el) el.textContent=val; }

function updateControlsTab() {
    const statusVal=document.getElementById('ctrl-status-val');
    if(statusVal) { statusVal.textContent=state.botStatus.toUpperCase(); statusVal.className='value '+(state.botStatus==='active'?'text-green glow-green':'text-red'); }
    const uptimeVal=document.getElementById('ctrl-uptime-val');
    if(uptimeVal&&state.activeSince) { const since=new Date(state.activeSince); const diff=Date.now()-since.getTime(); if(diff>0) { const hrs=Math.floor(diff/3600000); const mins=Math.floor((diff%3600000)/60000); const secs=Math.floor((diff%60000)/1000); uptimeVal.textContent=`${hrs}h ${mins}m ${secs}s`; } } else if(uptimeVal) uptimeVal.textContent='—';
    const pidVal=document.getElementById('ctrl-pid-val'); if(pidVal) pidVal.textContent=state.botPid||'—';
    const toggle=document.getElementById('mode-toggle'); if(toggle) toggle.className=`toggle-switch ${state.tradingMode.toLowerCase()}`;
}

async function botAction(action) {
    const btn=document.querySelector(`.ctrl-btn.${action}`); if(btn) { btn.disabled=true; btn.classList.add('loading'); }
    try { const data=await apiCall(`/api/bot/${action}`,'POST'); if(data.success) showToast(`Bot ${action} successful`,'success'); else showToast(`Bot ${action} failed: ${data.stderr||data.error||'Unknown'}`,'error'); setTimeout(pollStatus,1000); }
    catch(e) { showToast(`Bot ${action} failed: ${e.message}`,'error'); }
    finally { if(btn) { btn.disabled=false; btn.classList.remove('loading'); } }
}

function toggleTradingMode() {
    if(state.tradingMode==='PAPER') { document.getElementById('live-modal').classList.add('visible'); document.getElementById('confirm-input').value=''; document.getElementById('confirm-btn').disabled=true; }
    else applyTradingMode('PAPER');
}

function applyTradingMode(mode) {
    state.tradingMode=mode; updateControlsTab();
    apiCall('/api/config','POST',{PAPER_MODE:mode==='PAPER'?'true':'false'}).then(()=>showToast(`Switched to ${mode} mode`,mode==='LIVE'?'warning':'success')).catch(e=>showToast(`Failed to switch mode: ${e.message}`,'error'));
}

const SLIDER_DEFS = {
    MIN_CONFIDENCE:{label:'Signal Confidence Threshold',min:0.01,max:0.20,step:0.01,section:'strategy'},
    MIN_CONTRACT_PRICE:{label:'Min Contract Price',min:5,max:50,step:1,section:'strategy'},
    MAX_CONTRACT_PRICE:{label:'Max Contract Price',min:50,max:99,step:1,section:'strategy'},
    KELLY_FRACTION:{label:'Kelly Fraction',min:0.05,max:0.50,step:0.01,section:'strategy'},
    MIN_EV_CENTS:{label:'Min Expected Value',min:0.5,max:5.0,step:0.1,section:'strategy'},
    PROFIT_TARGET_PCT:{label:'Profit Target %',min:0.10,max:1.00,step:0.05,section:'strategy'},
    STOP_LOSS_PCT:{label:'Stop Loss %',min:0.10,max:0.60,step:0.05,section:'strategy'},
    VPIN_EXIT_THRESHOLD:{label:'VPIN Exit Threshold',min:0.50,max:1.00,step:0.01,section:'strategy'},
    MAX_CONCURRENT_POSITIONS:{label:'Max Positions',min:1,max:10,step:1,section:'risk'},
    MAX_TOTAL_EXPOSURE_DOLLARS:{label:'Max Exposure ($)',min:10,max:500,step:5,section:'risk'},
    MAX_POSITION_SIZE_DOLLARS:{label:'Max Position Size ($)',min:5,max:100,step:1,section:'risk'},
    DAILY_LOSS_LIMIT_DOLLARS:{label:'Daily Loss Limit ($)',min:5,max:100,step:1,section:'risk'},
    LOOP_INTERVAL_SECONDS:{label:'Loop Interval (s)',min:5,max:60,step:1,section:'timing'},
    SCAN_INTERVAL_SECONDS:{label:'Scan Interval (s)',min:15,max:120,step:5,section:'timing'}
};

function updateSlidersFromConfig() {
    for(const [key] of Object.entries(SLIDER_DEFS)) { const slider=document.getElementById(`slider-${key}`); const display=document.getElementById(`display-${key}`); if(slider&&state.config[key]!==undefined) { slider.value=parseFloat(state.config[key]); if(display) display.textContent=formatSliderValue(key,parseFloat(state.config[key])); } }
    state.configDirty=false; updateApplyButton();
}

function formatSliderValue(key,val) { const def=SLIDER_DEFS[key]; if(!def) return val; if(def.step<1) return val.toFixed(2); return val.toString(); }

function onSliderChange(key,value) { const display=document.getElementById(`display-${key}`); if(display) display.textContent=formatSliderValue(key,parseFloat(value)); state.pendingConfig[key]=value; state.configDirty=true; updateApplyButton(); }

function updateApplyButton() {
    const btn=document.getElementById('apply-config-btn'); if(!btn) return;
    if(state.configDirty) { btn.disabled=false; btn.classList.add('pending'); btn.textContent='⚡ APPLY CHANGES'; }
    else { btn.disabled=true; btn.classList.remove('pending'); btn.textContent='NO CHANGES'; }
}

async function applyConfig() {
    if(!state.configDirty) return;
    const btn=document.getElementById('apply-config-btn'); if(btn) { btn.disabled=true; btn.textContent='APPLYING...'; }
    try { await apiCall('/api/config','POST',state.pendingConfig); showToast('Configuration updated & bot restarted','success'); state.pendingConfig={}; state.configDirty=false; updateApplyButton(); setTimeout(pollStatus,2000); }
    catch(e) { showToast(`Config update failed: ${e.message}`,'error'); if(btn) btn.disabled=false; }
}

function updateLogsTab() {
    const viewer=document.getElementById('log-viewer'); if(!viewer) return;
    const filter=state.logFilter.toLowerCase();
    const filtered=filter ? state.logLines.filter(l=>l.toLowerCase().includes(filter)) : state.logLines;
    viewer.innerHTML=filtered.map(line=>{ let cls=''; const lower=line.toLowerCase(); if(lower.includes('error')||lower.includes('exception')||lower.includes('traceback')) cls='error'; else if(lower.includes('warning')||lower.includes('warn')) cls='warning'; else if(lower.includes('debug')) cls='debug'; else cls='info'; if(filter&&lower.includes(filter)) cls+=' highlight'; const escaped=line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); return `<div class="log-line ${cls}">${escaped}</div>`; }).join('');
    if(state.logAutoScroll) viewer.scrollTop=viewer.scrollHeight;
}

function toggleAutoScroll() {
    state.logAutoScroll=!state.logAutoScroll;
    const btn=document.getElementById('autoscroll-btn'); if(btn) { btn.classList.toggle('active',state.logAutoScroll); btn.textContent=state.logAutoScroll?'⤓ AUTO-SCROLL ON':'⤓ AUTO-SCROLL OFF'; }
}

let mainChart=null, mainSeries=null;

async function initBTCChart() {
    const container=document.getElementById('btc-chart'); if(!container||typeof LightweightCharts==='undefined') return;
    mainChart=LightweightCharts.createChart(container,{width:container.clientWidth,height:260,layout:{background:{type:'solid',color:'#0a0e0a'},textColor:'#7a9f7a',fontFamily:'JetBrains Mono',fontSize:10},grid:{vertLines:{color:'#00ff4108'},horzLines:{color:'#00ff4108'}},crosshair:{mode:0,vertLine:{color:'#00ff4140',width:1,style:2},horzLine:{color:'#00ff4140',width:1,style:2}},rightPriceScale:{borderColor:'#00ff4120',scaleMargins:{top:0.1,bottom:0.1}},timeScale:{borderColor:'#00ff4120',timeVisible:true,secondsVisible:false}});
    mainSeries=mainChart.addAreaSeries({topColor:'rgba(0,255,65,0.25)',bottomColor:'rgba(0,255,65,0.01)',lineColor:'#00ff41',lineWidth:2});
    fetchBTCData();
    const observer=new ResizeObserver(()=>mainChart.applyOptions({width:container.clientWidth})); observer.observe(container);
}

async function fetchBTCData() {
    try { const resp=await fetch('https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1'); const data=await resp.json(); if(data.prices&&mainSeries) { const chartData=data.prices.map(([ts,price])=>({time:Math.floor(ts/1000),value:price})); mainSeries.setData(chartData); const latest=chartData[chartData.length-1]; if(latest) setTextContent('btc-price','$'+latest.value.toLocaleString(undefined,{maximumFractionDigits:0})); } } catch(e) { console.warn('BTC chart fetch failed:',e); }
}

async function fetchSparklineData() {
    const coins=['ethereum','solana','ripple']; const ids={ethereum:'eth',solana:'sol',ripple:'xrp'};
    for(const coin of coins) {
        try { const resp=await fetch(`https://api.coingecko.com/api/v3/coins/${coin}/market_chart?vs_currency=usd&days=1`); const data=await resp.json(); if(data.prices) { const ticker=ids[coin]; const latest=data.prices[data.prices.length-1]; const first=data.prices[0]; const change=((latest[1]-first[1])/first[1]*100); setTextContent(`${ticker}-price`,'$'+latest[1].toLocaleString(undefined,{maximumFractionDigits:2})); const changeEl=document.getElementById(`${ticker}-change`); if(changeEl) { changeEl.textContent=`${change>=0?'+':''}${change.toFixed(2)}%`; changeEl.className=`sparkline-price ${change<0?'negative':''}`; } drawMiniSparkline(`${ticker}-sparkline`,data.prices,change>=0?'#00ff41':'#ff0033'); } } catch(e) { console.warn(`Sparkline ${coin} failed:`,e); }
    }
}

function drawMiniSparkline(canvasId,prices,color) {
    const canvas=document.getElementById(canvasId); if(!canvas) return;
    const ctx=canvas.getContext('2d'); const W=canvas.width=canvas.clientWidth*2; const H=canvas.height=canvas.clientHeight*2; ctx.scale(2,2);
    const w=canvas.clientWidth; const h=canvas.clientHeight; const vals=prices.map(p=>p[1]); const min=Math.min(...vals); const max=Math.max(...vals); const range=max-min||1;
    ctx.clearRect(0,0,w,h); ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=1.5;
    vals.forEach((v,i)=>{ const x=(i/(vals.length-1))*w; const y=h-((v-min)/range)*h*0.8-h*0.1; if(i===0)ctx.moveTo(x,y); else ctx.lineTo(x,y); }); ctx.stroke();
    const gradient=ctx.createLinearGradient(0,0,0,h); gradient.addColorStop(0,color.replace(')',', 0.15)').replace('rgb','rgba')); gradient.addColorStop(1,'rgba(0,0,0,0)');
    ctx.lineTo(w,h); ctx.lineTo(0,h); ctx.closePath(); ctx.fillStyle=gradient; ctx.fill();
}

function switchTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach(btn=>btn.classList.toggle('active',btn.dataset.tab===tabName));
    document.querySelectorAll('.tab-content').forEach(content=>content.classList.toggle('active',content.dataset.tab===tabName));
    if(tabName==='controls') loadConfig();
    if(tabName==='logs') pollLogs();
    if(tabName==='weather') { pollWeather(); if(!weatherState.pollTimer) weatherState.pollTimer=setInterval(pollWeather,30000); }
    else { if(weatherState.pollTimer) { clearInterval(weatherState.pollTimer); weatherState.pollTimer=null; } }
}

function init() {
    setInterval(updateClock,50); updateClock();
    document.querySelectorAll('.tab-btn').forEach(btn=>btn.addEventListener('click',()=>switchTab(btn.dataset.tab)));
    document.getElementById('btn-start')?.addEventListener('click',()=>botAction('start'));
    document.getElementById('btn-stop')?.addEventListener('click',()=>botAction('stop'));
    document.getElementById('btn-restart')?.addEventListener('click',()=>botAction('restart'));
    document.getElementById('mode-toggle')?.addEventListener('click',toggleTradingMode);
    const confirmInput=document.getElementById('confirm-input');
    if(confirmInput) confirmInput.addEventListener('input',()=>{ const btn=document.getElementById('confirm-btn'); if(btn) btn.disabled=confirmInput.value!=='CONFIRM'; });
    document.getElementById('confirm-btn')?.addEventListener('click',()=>{ document.getElementById('live-modal').classList.remove('visible'); applyTradingMode('LIVE'); });
    document.getElementById('cancel-modal')?.addEventListener('click',()=>document.getElementById('live-modal').classList.remove('visible'));
    document.getElementById('apply-config-btn')?.addEventListener('click',applyConfig);
    document.getElementById('log-filter')?.addEventListener('input',(e)=>{ state.logFilter=e.target.value; updateLogsTab(); });
    document.getElementById('autoscroll-btn')?.addEventListener('click',toggleAutoScroll);
    document.getElementById('refresh-log-btn')?.addEventListener('click',pollLogs);
    document.getElementById('clear-log-btn')?.addEventListener('click',()=>{ state.logLines=[]; updateLogsTab(); });
    const logViewer=document.getElementById('log-viewer');
    if(logViewer) logViewer.addEventListener('scroll',()=>{ const atBottom=logViewer.scrollTop+logViewer.clientHeight>=logViewer.scrollHeight-30; if(!atBottom&&state.logAutoScroll) { state.logAutoScroll=false; const btn=document.getElementById('autoscroll-btn'); if(btn) { btn.classList.remove('active'); btn.textContent='⤓ AUTO-SCROLL OFF'; } } });
    for(const [key] of Object.entries(SLIDER_DEFS)) { const slider=document.getElementById(`slider-${key}`); if(slider) slider.addEventListener('input',(e)=>onSliderChange(key,e.target.value)); }
    initBTCChart(); setTimeout(fetchSparklineData,1000);
    pollStatus(); pollState(); pollLogs(); loadConfig();
    setInterval(pollState,POLL_STATE_MS); setInterval(pollStatus,POLL_STATUS_MS); setInterval(pollLogs,POLL_LOG_MS);
    setInterval(fetchSparklineData,60000); setInterval(fetchBTCData,60000);
}

if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init); else init();

const weatherState = { selectedCity:null, data:null, lastFetch:null, pollTimer:null, minEdge:0.08 };

async function pollWeather() {
    try {
        const data=await apiCall('/api/weather'); weatherState.data=data; weatherState.lastFetch=new Date();
        if(data.config&&typeof data.config.min_edge==='number') weatherState.minEdge=data.config.min_edge;
        if(!weatherState.selectedCity&&data.cities) { const codes=Object.keys(data.cities); if(codes.length>0) weatherState.selectedCity=codes[0]; }
        updateWeatherTab();
    } catch(e) { const dot=document.getElementById('weather-status-dot'); const txt=document.getElementById('weather-status-text'); if(dot)dot.className='weather-status-dot error'; if(txt){txt.textContent='FETCH ERROR';txt.style.color='var(--red)';} }
}

function selectCity(code) { weatherState.selectedCity=code; updateWeatherTab(); }

function updateWeatherTab() {
    const data=weatherState.data;
    const dot=document.getElementById('weather-status-dot'); const txt=document.getElementById('weather-status-text');
    const cityCountEl=document.getElementById('weather-city-count'); const lastUpdateEl=document.getElementById('weather-last-update');
    if(data) {
        if(dot)dot.className='weather-status-dot active';
        if(txt){txt.textContent='LIVE';txt.style.color='var(--cyan)';}
        const cityKeys=data.cities?Object.keys(data.cities):[];
        if(cityCountEl)cityCountEl.textContent=`${cityKeys.length} CITIES`;
        if(lastUpdateEl&&weatherState.lastFetch) { const ts=weatherState.lastFetch; const hh=String(ts.getHours()).padStart(2,'0'); const mm=String(ts.getMinutes()).padStart(2,'0'); const ss=String(ts.getSeconds()).padStart(2,'0'); lastUpdateEl.textContent=`LAST: ${hh}:${mm}:${ss}`; }
        renderCityChips(data.cities);
        const city=weatherState.selectedCity&&data.cities?data.cities[weatherState.selectedCity]:null;
        renderForecastCards(city); renderMarketsTable(city); renderModelViz(city); renderWeatherConfig(data.config,data.ts);
    } else { if(dot)dot.className='weather-status-dot'; if(txt){txt.textContent='AWAITING DATA';txt.style.color='';} }
}

function renderCityChips(cities) {
    const container=document.getElementById('weather-city-chips'); if(!container) return;
    if(!cities||Object.keys(cities).length===0) { container.innerHTML='<span class="text-dim" style="font-size:11px">No cities in data</span>'; return; }
    container.innerHTML=Object.values(cities).map(city=>{ const isActive=weatherState.selectedCity===city.code; return `<button class="city-chip${isActive?' active':''}" onclick="selectCity('${city.code}')"><span class="city-chip-code">${city.code}</span><span class="city-chip-name">${city.name}</span></button>`; }).join('');
}

function renderForecastCards(city) {
    const container=document.getElementById('weather-forecast-cards'); const cityLabel=document.getElementById('weather-forecast-city'); if(!container) return;
    if(!city) { container.innerHTML='<div class="weather-empty-state">Select a city to view forecast</div>'; if(cityLabel)cityLabel.textContent='— SELECT A CITY —'; return; }
    if(cityLabel)cityLabel.textContent=`${city.name.toUpperCase()} (${city.code})`;
    const forecast=(city.forecast||[]).slice(0,3);
    if(forecast.length===0) { container.innerHTML='<div class="weather-empty-state">No forecast data available</div>'; return; }
    container.innerHTML=forecast.map(day=>`<div class="weather-forecast-card"><div class="wfc-date"><div class="wfc-day-name">${escapeHtml(day.name)}</div><div class="wfc-date-str">${escapeHtml(day.date)}</div></div><div class="wfc-details"><div class="wfc-condition">${escapeHtml(day.short)}</div><div class="wfc-wind">&#x25ba; ${escapeHtml(day.wind)} ${escapeHtml(day.wind_dir||'')}</div></div><div class="wfc-temp"><div class="wfc-temp-value">${day.high_f}</div><div class="wfc-temp-unit">°F HIGH</div></div></div>`).join('');
}

function renderMarketsTable(city) {
    const tbody=document.getElementById('weather-markets-tbody'); const cityLabel=document.getElementById('weather-markets-city'); if(!tbody) return;
    if(!city) { tbody.innerHTML='<tr><td colspan="7" class="empty-table">Select a city to view markets</td></tr>'; if(cityLabel)cityLabel.textContent='— SELECT A CITY —'; return; }
    if(cityLabel)cityLabel.textContent=`${city.name.toUpperCase()} (${city.code})`;
    const markets=city.markets||[];
    if(markets.length===0) { tbody.innerHTML='<tr><td colspan="7" class="empty-table">No markets for this city</td></tr>'; return; }
    const minEdge=weatherState.minEdge;
    tbody.innerHTML=markets.map(m=>{ const edge=typeof m.edge==='number'?m.edge:0; const modelProb=typeof m.model_prob==='number'?m.model_prob:0; const mktImplied=typeof m.market_implied==='number'?m.market_implied:0; const yesAsk=typeof m.yes_ask==='number'?m.yes_ask:0; const volume=typeof m.volume==='number'?m.volume:0; let edgeClass,edgeSignalClass=''; if(edge>=minEdge){edgeClass='edge-high';edgeSignalClass='edge-signal';}else if(edge>=0.03)edgeClass='edge-mid';else edgeClass='edge-low'; let signalHtml; if(edge>=minEdge){if(modelProb>mktImplied)signalHtml='<span class="signal-buy-yes">&#x25b2; BUY YES</span>';else signalHtml='<span class="signal-buy-no">&#x25b2; BUY NO</span>';}else signalHtml='<span class="signal-none">—</span>'; const bracket=m.title||m.ticker||'—'; const tickerFull=m.ticker||'—'; return `<tr><td><div style="font-size:11px;color:var(--text-bright);margin-bottom:2px">${escapeHtml(bracket)}</div><div class="ticker-cell" title="${escapeHtml(tickerFull)}">${escapeHtml(tickerFull)}</div></td><td class="text-green" style="font-family:var(--font-code)">${(modelProb*100).toFixed(1)}%</td><td style="font-family:var(--font-code);color:var(--text-secondary)">${(mktImplied*100).toFixed(1)}%</td><td class="${edgeClass} ${edgeSignalClass}" style="font-family:var(--font-code)">${edge>=0?'+':''}${(edge*100).toFixed(1)}%</td><td style="font-family:var(--font-code);color:var(--text-dim)">${volume}</td><td style="font-family:var(--font-code);color:var(--amber)">${yesAsk}¢</td><td>${signalHtml}</td></tr>`; }).join('');
}

function renderModelViz(city) {
    const container=document.getElementById('weather-model-viz'); const paramsEl=document.getElementById('weather-model-params'); if(!container||!paramsEl) return;
    if(!city||!city.markets||city.markets.length===0) { container.style.display='none'; return; }
    container.style.display='block';
    const sigma=typeof city.sigma_base==='number'?city.sigma_base:2.5;
    const rows=city.markets.map(m=>({bracket:m.title||m.ticker||'—',prob:typeof m.model_prob==='number'?m.model_prob:0,forecastHigh:typeof m.forecast_high==='number'?m.forecast_high:null}));
    const refForecast=rows.find(r=>r.forecastHigh!==null); const forecastHighVal=refForecast?refForecast.forecastHigh:null;
    const sigmaRow=`<div class="wmp-sigma-row"><div class="wmp-sigma-item"><div class="wmp-sigma-label">Forecast High</div><div class="wmp-sigma-value">${forecastHighVal!==null?forecastHighVal+'°F':'—'}</div></div><div class="wmp-sigma-item"><div class="wmp-sigma-label">σ (Sigma)</div><div class="wmp-sigma-value">${sigma.toFixed(1)}°F</div></div><div class="wmp-sigma-item"><div class="wmp-sigma-label">Distribution</div><div class="wmp-sigma-value" style="color:var(--text-secondary);font-size:10px">NORMAL CDF</div></div></div>`;
    const barsHtml=rows.map(r=>{ const pct=Math.max(0,Math.min(100,r.prob*100)); return `<div class="wmp-row"><div class="wmp-label"><span class="wmp-bracket">${escapeHtml(r.bracket)}</span><span class="wmp-prob">${pct.toFixed(1)}%</span></div><div class="wmp-bar-track"><div class="wmp-bar-fill" style="width:${pct.toFixed(1)}%"></div></div></div>`; }).join('');
    paramsEl.innerHTML=barsHtml+sigmaRow;
}

function renderWeatherConfig(config,ts) {
    if(!config) return;
    const setWCfg=(id,val,cls)=>{ const el=document.getElementById(id); if(!el) return; el.textContent=val; el.className=`weather-config-value ${cls||''}`; };
    setWCfg('wcfg-enabled',config.enabled?'YES':'NO',config.enabled?'text-green glow-green':'text-red');
    setWCfg('wcfg-paper',config.paper_mode?'PAPER':'LIVE',config.paper_mode?'text-amber':'text-red');
    setWCfg('wcfg-min-edge',typeof config.min_edge==='number'?(config.min_edge*100).toFixed(0)+'%':'—','text-cyan');
    setWCfg('wcfg-kelly',typeof config.kelly_fraction==='number'?config.kelly_fraction.toFixed(2):'—','text-green');
    setWCfg('wcfg-sigma',typeof config.sigma_inflation==='number'?config.sigma_inflation.toFixed(1)+'x':'—','text-cyan');
    if(ts) { const d=new Date(ts*1000); setWCfg('wcfg-ts',d.toLocaleTimeString(),'text-dim'); }
}

function escapeHtml(str) { if(str===null||str===undefined) return '—'; return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }