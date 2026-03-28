/* ===================================================================
   NBA Dashboard — nba-app.js
   Full NBA-only dashboard: positions, charts, trades, research.
   =================================================================== */

// State
let equityChart = null;
let dailyPnlChart = null;
let winRateTypeChart = null;
let sortColumn = "date";
let sortDirection = "desc";
let expandedTradeId = null;
let cachedPositions = null;
let cachedStats = null;
let cachedStatus = null;
let cachedLive = {};

// ---------------------------------------------------------------------------
// Portfolio Value
// ---------------------------------------------------------------------------
function updatePortfolioValue() {
    if (!cachedStatus) return;
    const cash = cachedStatus.bankroll || 0;
    const deployed = (cachedPositions?.open || []).reduce((sum, p) => sum + (p.cost || 0), 0);
    const portfolioValue = cash + deployed;

    document.getElementById("kpiPortfolio").textContent = fmt.usd(portfolioValue);
    document.getElementById("kpiPortfolioSub").textContent =
        `Cash: ${fmt.usd(cash)} \u00b7 Bets: ${fmt.usd(deployed)}`;
    updateUnrealizedPnl();
}

function updateUnrealizedPnl() {
    const openPositions = cachedPositions?.open || [];
    let unrealized = 0;
    let hasLiveData = false;

    for (const p of openPositions) {
        const live = cachedLive[p.id];
        if (live && live.pnl_live != null) {
            unrealized += live.pnl_live;
            hasLiveData = true;
        }
    }

    const el = document.getElementById("kpiUnrealizedPnl");
    if (hasLiveData) {
        el.textContent = (unrealized >= 0 ? "+" : "") + fmt.usd(unrealized);
        el.className = `kpi-value ${pnlClass(unrealized)}`;
    } else {
        el.textContent = "--";
        el.className = "kpi-value";
    }
    document.getElementById("kpiUnrealizedSub").textContent =
        `${openPositions.length} open position${openPositions.length !== 1 ? "s" : ""}`;
}

// ---------------------------------------------------------------------------
// Update Functions
// ---------------------------------------------------------------------------
function updateStatus(data) {
    if (!data) return;
    cachedStatus = data;

    const badge = document.getElementById("modeBadge");
    const modeText = badge.querySelector(".mode-text");
    if (data.mode === "live") {
        badge.classList.add("live");
        modeText.textContent = "LIVE";
    } else {
        badge.classList.remove("live");
        modeText.textContent = "PAPER";
    }

    updatePortfolioValue();
    document.getElementById("lastUpdate").textContent = data.last_scan
        ? fmt.relative(data.last_scan) : "--";
}

function updateStats(data) {
    if (!data) return;
    cachedStats = data;

    const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
    const todayEntry = (data.daily_pnl || []).find((d) => d.date === today);
    const dailyPnl = todayEntry ? todayEntry.pnl : 0;
    const dailyEl = document.getElementById("kpiDailyPnl");
    dailyEl.textContent = (dailyPnl >= 0 ? "+" : "") + fmt.usd(dailyPnl);
    dailyEl.className = `kpi-value ${pnlClass(dailyPnl)}`;

    const todayTrades = todayEntry ? (todayEntry.trades || 1) : 0;
    document.getElementById("kpiDailyPnlSub").textContent =
        `${todayTrades} trade${todayTrades !== 1 ? "s" : ""} today`;

    const realizedEl = document.getElementById("kpiRealizedPnl");
    const realizedPnl = data.total_pnl || 0;
    realizedEl.textContent = (realizedPnl >= 0 ? "+" : "") + fmt.usd(realizedPnl);
    realizedEl.className = `kpi-value ${pnlClass(realizedPnl)}`;
    document.getElementById("kpiRealizedSub").textContent =
        `ROI: ${(data.roi >= 0 ? "+" : "")}${fmt.pct(data.roi)}`;

    document.getElementById("kpiWinRate").textContent = fmt.pct(data.win_rate);
    document.getElementById("kpiTotalTrades").textContent = data.total_trades || 0;
    document.getElementById("kpiWins").textContent = data.wins || 0;
    document.getElementById("kpiLosses").textContent = data.losses || 0;

    document.getElementById("drawdownBadge").textContent = `Max DD: ${fmt.pct(data.max_drawdown)}`;
    document.getElementById("profitFactorBadge").textContent =
        `PF: ${data.profit_factor === Infinity ? "\u221e" : data.profit_factor}`;

    const streak = data.streak || {};
    const csEl = document.getElementById("currentStreak");
    csEl.textContent = `${streak.current || 0}${streak.type || ""}`;
    csEl.className = `stat-mini-value ${streak.type === "W" ? "streak-win" : "streak-loss"}`;
    document.getElementById("bestStreak").textContent = `${streak.best || 0}W`;
    document.getElementById("worstStreak").textContent = `${streak.worst || 0}L`;
    document.getElementById("avgWin").textContent = fmt.usd(data.avg_win);
    document.getElementById("avgLoss").textContent = fmt.usd(data.avg_loss);
    document.getElementById("avgEdge").textContent = `${data.avg_edge || 0}%`;

    drawEquityChart(data.daily_pnl || []);
    drawDailyPnlChart(data.daily_pnl || []);
    drawWinRateTypeChart(data.win_rate_by_type || {});
}

function renderPositions() {
    const open = cachedPositions?.open || [];
    const grid = document.getElementById("positionsGrid");
    document.getElementById("openCount").textContent = `${open.length} open`;

    if (open.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">
                    <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
                        <circle cx="24" cy="24" r="20" stroke="#71717a" stroke-width="1.5" stroke-dasharray="4 4"/>
                        <path d="M18 24h12M24 18v12" stroke="#71717a" stroke-width="1.5" stroke-linecap="round"/>
                    </svg>
                </div>
                <div class="empty-text">No open positions</div>
                <div class="empty-sub">Waiting for next edge opportunity</div>
            </div>`;
        return;
    }

    grid.innerHTML = open.map((p) => {
        const holdTime = fmt.relative(p.entry_time);
        const confClass = p.confidence === "HIGH" ? "conf-high" :
            p.confidence === "MEDIUM" ? "conf-medium" : "conf-low";
        const live = cachedLive[p.id] || null;
        const cardClass = live && live.pnl_live > 0 ? "pos-profit" :
            live && live.pnl_live < 0 ? "pos-loss" : "pos-neutral";

        let gameDate = "--";
        if (p.game_start_time) {
            gameDate = new Date(p.game_start_time).toLocaleDateString("en-US",
                { weekday: "short", month: "short", day: "numeric", timeZone: TZ });
        } else if (p.market_slug) {
            const parts = p.market_slug.split("-");
            if (parts.length >= 5) {
                gameDate = new Date(parts.slice(-3).join("-") + "T12:00:00Z")
                    .toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: TZ });
            }
        }

        return `
        <div class="position-card ${cardClass}">
            <div class="pos-header">
                <div class="pos-market">${escHtml(p.market_question)}</div>
                <div class="pos-header-right">
                    <span class="pos-game-date">${gameDate}</span>
                    <span class="pos-conf badge ${confClass}">${p.confidence}</span>
                </div>
            </div>
            <div class="pos-details">
                <div class="pos-detail">
                    <span class="pos-detail-label">Side</span>
                    <span class="pos-detail-value">${p.side}</span>
                </div>
                <div class="pos-detail">
                    <span class="pos-detail-label">Edge</span>
                    <span class="pos-detail-value pnl-positive">${fmt.edge(p.edge_at_entry)}</span>
                </div>
                <div class="pos-detail">
                    <span class="pos-detail-label">Entry</span>
                    <span class="pos-detail-value">${fmt.price(p.entry_price)}</span>
                </div>
                <div class="pos-detail">
                    <span class="pos-detail-label">Now</span>
                    <span class="pos-detail-value">${live ? fmt.price(live.live_price) : '--'}</span>
                </div>
                <div class="pos-detail">
                    <span class="pos-detail-label">Cost</span>
                    <span class="pos-detail-value">${fmt.usd(p.cost)}</span>
                </div>
                <div class="pos-detail">
                    <span class="pos-detail-label">Value</span>
                    <span class="pos-detail-value ${live && live.pnl_live > 0 ? 'pnl-positive' : live && live.pnl_live < 0 ? 'pnl-negative' : ''}">${live ? fmt.usd(live.current_value) : '--'}</span>
                </div>
                <div class="pos-detail">
                    <span class="pos-detail-label">P&L</span>
                    <span class="pos-detail-value ${live && live.pnl_live > 0 ? 'pnl-positive' : live && live.pnl_live < 0 ? 'pnl-negative' : ''}">${live ? (live.pnl_live >= 0 ? '+' : '') + fmt.usd(live.pnl_live) : '--'}</span>
                </div>
            </div>
            ${live && live.score ? `
            <div class="pos-score">
                <span class="pos-score-teams">${escHtml(live.score.away_team)} ${live.score.away_score} — ${live.score.home_score} ${escHtml(live.score.home_team)}</span>
                <span class="pos-score-status ${live.score.status === 'STATUS_IN_PROGRESS' ? 'pos-score-live' : live.score.status === 'STATUS_FINAL' ? 'pos-score-final' : ''}">${escHtml(live.score.detail || (live.score.status === 'STATUS_SCHEDULED' ? 'Scheduled' : live.score.status === 'STATUS_FINAL' ? 'Final' : 'Live'))}</span>
            </div>` : ''}
            <div class="pos-line-chart"><canvas id="line-spark-${p.id}" height="36"></canvas></div>
        </div>`;
    }).join("");
}

function renderTrades() {
    const closed = (cachedPositions?.closed || []).slice();

    closed.sort((a, b) => {
        let va, vb;
        switch (sortColumn) {
            case "date": va = a.exit_time || a.entry_time || ""; vb = b.exit_time || b.entry_time || ""; break;
            case "market": va = a.market_question || ""; vb = b.market_question || ""; break;
            case "entry": va = a.entry_price || 0; vb = b.entry_price || 0; break;
            case "exit": va = a.exit_price || 0; vb = b.exit_price || 0; break;
            case "pnl": va = a.pnl || 0; vb = b.pnl || 0; break;
            default: va = a.exit_time || ""; vb = b.exit_time || "";
        }
        if (typeof va === "string") {
            const cmp = va.localeCompare(vb);
            return sortDirection === "asc" ? cmp : -cmp;
        }
        return sortDirection === "asc" ? va - vb : vb - va;
    });

    document.getElementById("tradeCountBadge").textContent = `${closed.length} closed`;

    const tbody = document.getElementById("tradeTableBody");
    tbody.innerHTML = closed.map((p) => {
        const pnl = p.pnl || 0;
        const pnlCls = pnlClass(pnl);
        const confClass = p.confidence === "HIGH" ? "conf-high" :
            p.confidence === "MEDIUM" ? "conf-medium" : "conf-low";
        const isExpanded = expandedTradeId === p.id;

        return `
        <tr onclick="toggleTradeDetail('${p.id}')">
            <td>${fmt.date(p.exit_time || p.entry_time)}</td>
            <td class="td-market" title="${escHtml(p.market_question)}">${escHtml(p.market_question)}</td>
            <td>${p.side}</td>
            <td>${fmt.price(p.entry_price)}</td>
            <td>${fmt.price(p.exit_price)}</td>
            <td class="${pnlCls}">${(pnl >= 0 ? "+" : "") + fmt.usd(pnl)}</td>
            <td>${fmt.edge(p.edge_at_entry)}</td>
            <td><span class="badge ${confClass}" style="font-size:10px">${p.confidence}</span></td>
        </tr>
        <tr class="trade-detail-row ${isExpanded ? "expanded" : ""}" id="detail-${p.id}">
            <td colspan="8" class="trade-detail-cell">
                <div class="trade-detail-grid">
                    <div class="trade-detail-item"><span class="trade-detail-label">Position ID</span><span class="trade-detail-value">${p.id}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Game Date</span><span class="trade-detail-value">${p.game_start_time ? new Date(p.game_start_time).toLocaleDateString("en-US",{weekday:"short",month:"short",day:"numeric",timeZone:TZ}) : "--"}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Entry Time</span><span class="trade-detail-value">${fmt.datetime(p.entry_time)}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Exit Time</span><span class="trade-detail-value">${fmt.datetime(p.exit_time)}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Cost</span><span class="trade-detail-value">${fmt.usd(p.cost)}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Shares</span><span class="trade-detail-value">${p.shares?.toFixed(2) || "--"}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Fair Price</span><span class="trade-detail-value">${fmt.price(p.our_fair_price)}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Exit Reason</span><span class="trade-detail-value">${p.exit_reason || "--"}</span></div>
                    <div class="trade-detail-item"><span class="trade-detail-label">Mode</span><span class="trade-detail-value">${p.mode || "--"}</span></div>
                </div>
            </td>
        </tr>`;
    }).join("");

    // Mobile cards
    document.getElementById("tradeCardsMobile").innerHTML = closed.map((p) => {
        const pnl = p.pnl || 0;
        const pnlCls = pnlClass(pnl);
        const confClass = p.confidence === "HIGH" ? "conf-high" :
            p.confidence === "MEDIUM" ? "conf-medium" : "conf-low";
        return `
        <div class="trade-card-m">
            <div class="trade-card-m-header">
                <div class="trade-card-m-market">${escHtml(p.market_question)}</div>
                <div class="trade-card-m-pnl ${pnlCls}">${(pnl >= 0 ? "+" : "") + fmt.usd(pnl)}</div>
            </div>
            <div class="trade-card-m-details">
                <div class="trade-card-m-detail"><span class="trade-card-m-label">Side</span><span class="trade-card-m-value">${p.side}</span></div>
                <div class="trade-card-m-detail"><span class="trade-card-m-label">Entry</span><span class="trade-card-m-value">${fmt.price(p.entry_price)}</span></div>
                <div class="trade-card-m-detail"><span class="trade-card-m-label">Exit</span><span class="trade-card-m-value">${fmt.price(p.exit_price)}</span></div>
                <div class="trade-card-m-detail"><span class="trade-card-m-label">Edge</span><span class="trade-card-m-value">${fmt.edge(p.edge_at_entry)}</span></div>
                <div class="trade-card-m-detail"><span class="trade-card-m-label">Date</span><span class="trade-card-m-value">${fmt.date(p.exit_time)}</span></div>
                <div class="trade-card-m-detail"><span class="trade-card-m-label">Conf</span><span class="trade-card-m-value"><span class="badge ${confClass}" style="font-size:9px;padding:2px 6px">${p.confidence}</span></span></div>
            </div>
        </div>`;
    }).join("");
}

function updateResearch(data) {
    if (!data) return;
    const research = data.research || [];
    const grid = document.getElementById("researchGrid");
    document.getElementById("researchCount").textContent = `${research.length} analyzed`;

    if (research.length === 0) {
        grid.innerHTML = `<div class="empty-state"><div class="empty-text">No research data yet</div></div>`;
        return;
    }

    grid.innerHTML = research.map((r) => {
        const hasEdge = r.edge >= 0.04;
        const edgeClass = hasEdge ? "has-edge" : "no-edge";
        return `
        <div class="research-card ${edgeClass}">
            <div class="research-header">
                <span class="research-game">${escHtml(r.game)}</span>
                <span class="research-time">${fmt.datetime(r.game_time)}</span>
            </div>
            <div class="research-odds">
                <div class="research-odds-item"><div class="research-odds-label">Fair Price</div><div class="research-odds-value">${fmt.price(r.our_fair_price)}</div></div>
                <div class="research-odds-item"><div class="research-odds-label">Market</div><div class="research-odds-value">${fmt.price(r.market_price)}</div></div>
                <div class="research-odds-item"><div class="research-odds-label">Edge</div><div class="research-odds-value ${hasEdge ? "pnl-positive" : "pnl-neutral"}">${fmt.edge(r.edge)}</div></div>
            </div>
            <div class="research-analysis">${escHtml(r.analysis)}</div>
            <div class="research-footer">
                <span class="badge ${r.confidence === "HIGH" ? "conf-high" : r.confidence === "MEDIUM" ? "conf-medium" : "conf-low"}" style="font-size:10px">${r.confidence}</span>
                <span class="research-bet-status ${r.bet_placed ? "research-bet-placed" : "research-bet-passed"}">${r.bet_placed ? "\u2713 BET PLACED" : "\u2014 PASSED"}</span>
            </div>
        </div>`;
    }).join("");
}

// ---------------------------------------------------------------------------
// Redeem Checklist
// ---------------------------------------------------------------------------
function getRedeemedSet() {
    try { return new Set(JSON.parse(localStorage.getItem('nba_agent_redeemed') || '[]')); }
    catch { return new Set(); }
}
function saveRedeemedSet(set) {
    try { localStorage.setItem('nba_agent_redeemed', JSON.stringify([...set])); } catch {}
}
function toggleRedeemed(posId) {
    const set = getRedeemedSet();
    if (set.has(posId)) set.delete(posId); else set.add(posId);
    saveRedeemedSet(set);
    if (cachedPositions) updateRedeemChecklist(cachedPositions);
}
function updateRedeemChecklist(positions) {
    const closed = positions.closed || [];
    const container = document.getElementById('redeemList');
    const redeemed = getRedeemedSet();

    if (closed.length === 0) {
        container.innerHTML = `<div class="empty-state"><div class="empty-text">No positions to redeem</div><div class="empty-sub">Resolved positions will appear here</div></div>`;
        document.getElementById('redeemWinCount').textContent = '0 to collect';
        document.getElementById('redeemLossCount').textContent = '0 to clear';
        return;
    }

    const sorted = closed.slice().sort((a, b) => {
        const aChecked = redeemed.has(a.id) ? 1 : 0;
        const bChecked = redeemed.has(b.id) ? 1 : 0;
        if (aChecked !== bChecked) return aChecked - bChecked;
        return (b.exit_time || '').localeCompare(a.exit_time || '');
    });

    const wins = sorted.filter(p => (p.pnl || 0) > 0);
    const losses = sorted.filter(p => (p.pnl || 0) <= 0);
    document.getElementById('redeemWinCount').textContent = `${wins.filter(p => !redeemed.has(p.id)).length} to collect`;
    document.getElementById('redeemLossCount').textContent = `${losses.filter(p => !redeemed.has(p.id)).length} to clear`;

    container.innerHTML = sorted.map(p => {
        const pnl = p.pnl || 0;
        const isWin = pnl > 0;
        const isChecked = redeemed.has(p.id);
        const typeClass = isWin ? 'win' : 'loss';
        return `
        <div class="redeem-item ${typeClass} ${isChecked ? 'checked' : ''}" onclick="toggleRedeemed('${p.id}')">
            <div class="redeem-check">
                <svg class="redeem-check-icon" viewBox="0 0 12 12" fill="none">
                    <path d="M2 6l3 3 5-6" stroke="${isWin ? '#0a0b0f' : '#fff'}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            </div>
            <div class="redeem-info">
                <span class="redeem-market">${escHtml(p.market_question)}</span>
                <div class="redeem-meta">
                    <span class="redeem-date">${fmt.date(p.exit_time)}</span>
                    <span class="redeem-side">${p.side}</span>
                    <span class="redeem-amount ${typeClass}">${isWin ? '+' : ''}${fmt.usd(pnl)}</span>
                    <span class="redeem-type ${isWin ? 'collect' : 'clear'}">${isWin ? 'COLLECT' : 'CLEAR'}</span>
                </div>
            </div>
        </div>`;
    }).join('');
}
function clearAllRedeemed() {
    if (!cachedPositions) return;
    const set = getRedeemedSet();
    (cachedPositions.closed || []).forEach(p => set.add(p.id));
    saveRedeemedSet(set);
    updateRedeemChecklist(cachedPositions);
}
window.toggleRedeemed = toggleRedeemed;
window.clearAllRedeemed = clearAllRedeemed;

// ---------------------------------------------------------------------------
// Performance Summary
// ---------------------------------------------------------------------------
let currentPeriod = "today";

function initPeriodTabs() {
    document.querySelectorAll(".period-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".period-tab").forEach((t) => t.classList.remove("active"));
            tab.classList.add("active");
            currentPeriod = tab.dataset.period;
            fetchPerformance(currentPeriod);
        });
    });
}

async function fetchPerformance(period) {
    try {
        const headers = {};
        if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
        const resp = await fetch(`${API}/api/performance/${period}`, { headers });
        if (!resp.ok) return;
        const data = await resp.json();
        updatePerformanceGrid(data);
    } catch (e) { /* silent */ }
}

function updatePerformanceGrid(data) {
    document.getElementById("perfBets").textContent = data.bets || 0;
    document.getElementById("perfWL").textContent = `${data.wins || 0} / ${data.losses || 0}`;
    document.getElementById("perfWinRate").textContent = fmt.pct(data.win_rate);

    const pnlEl = document.getElementById("perfPnl");
    const pnl = data.pnl || 0;
    pnlEl.textContent = (pnl >= 0 ? "+" : "") + fmt.usd(pnl);
    pnlEl.className = `summary-value ${pnlClass(pnl)}`;

    const roiEl = document.getElementById("perfRoi");
    const roi = data.roi || 0;
    roiEl.textContent = (roi >= 0 ? "+" : "") + fmt.pct(roi);
    roiEl.className = `summary-value ${pnlClass(roi)}`;

    document.getElementById("perfAvgWin").textContent = fmt.usd(data.avg_win);
    document.getElementById("perfAvgWin").className = "summary-value pnl-positive";
    document.getElementById("perfAvgLoss").textContent = fmt.usd(data.avg_loss);
    document.getElementById("perfAvgLoss").className = "summary-value pnl-negative";
    document.getElementById("perfPF").textContent = data.profit_factor != null ? data.profit_factor.toFixed(2) + "x" : "--";
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
function drawEquityChart(dailyPnl) {
    const ctx = document.getElementById("equityChart").getContext("2d");
    const labels = dailyPnl.map((d) => d.date);
    const values = dailyPnl.map((d) => d.bankroll);

    if (equityChart) {
        equityChart.data.labels = labels;
        equityChart.data.datasets[0].data = values;
        equityChart.update("none");
        return;
    }

    const gradient = ctx.createLinearGradient(0, 0, 0, 340);
    gradient.addColorStop(0, "rgba(0, 240, 255, 0.25)");
    gradient.addColorStop(0.5, "rgba(0, 240, 255, 0.08)");
    gradient.addColorStop(1, "rgba(0, 240, 255, 0)");

    equityChart = new Chart(ctx, {
        type: "line",
        data: {
            labels,
            datasets: [{
                label: "Bankroll", data: values,
                borderColor: "#00f0ff", borderWidth: 2.5,
                backgroundColor: gradient, fill: true, tension: 0.4,
                pointRadius: 3, pointBackgroundColor: "#00f0ff",
                pointBorderColor: "#0a0b0f", pointBorderWidth: 2,
                pointHoverRadius: 6, pointHoverBackgroundColor: "#00f0ff",
                pointHoverBorderColor: "#fff",
            }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: { duration: 1000, easing: "easeOutQuart" },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18,19,26,0.95)", borderColor: "rgba(0,240,255,0.3)",
                    borderWidth: 1, cornerRadius: 8, padding: 12,
                    titleFont: { family: "'Inter'", size: 12, weight: "600" },
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    titleColor: "#71717a", bodyColor: "#e4e4e7",
                    callbacks: { label: (ctx) => `$${ctx.parsed.y.toFixed(2)}` },
                },
            },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0, maxTicksLimit: 10 } },
                y: { grid: { color: "rgba(255,255,255,0.04)" }, ticks: { font: { family: "'JetBrains Mono'", size: 11 }, callback: (v) => `$${v}` } },
            },
        },
    });
}

function drawDailyPnlChart(dailyPnl) {
    const ctx = document.getElementById("dailyPnlChart").getContext("2d");
    const labels = dailyPnl.map((d) => d.date);
    const values = dailyPnl.map((d) => d.pnl);
    const colors = values.map((v) => (v >= 0 ? "#22c55e" : "#ef4444"));
    const bgColors = values.map((v) => v >= 0 ? "rgba(34,197,94,0.7)" : "rgba(239,68,68,0.7)");

    if (dailyPnlChart) {
        dailyPnlChart.data.labels = labels;
        dailyPnlChart.data.datasets[0].data = values;
        dailyPnlChart.data.datasets[0].backgroundColor = bgColors;
        dailyPnlChart.data.datasets[0].borderColor = colors;
        dailyPnlChart.update("none");
        return;
    }

    dailyPnlChart = new Chart(ctx, {
        type: "bar",
        data: { labels, datasets: [{ label: "P&L", data: values, backgroundColor: bgColors, borderColor: colors, borderWidth: 1, borderRadius: 4, borderSkipped: false }] },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: { duration: 800, easing: "easeOutQuart" },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18,19,26,0.95)", borderColor: "rgba(255,255,255,0.1)",
                    borderWidth: 1, cornerRadius: 8, padding: 12,
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    callbacks: { label: (ctx) => `${ctx.parsed.y >= 0 ? "+" : ""}$${ctx.parsed.y.toFixed(2)}` },
                },
            },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0, maxTicksLimit: 8 } },
                y: { grid: { color: "rgba(255,255,255,0.04)" }, ticks: { font: { family: "'JetBrains Mono'", size: 11 }, callback: (v) => `$${v}` } },
            },
        },
    });
}

function drawWinRateTypeChart(winRateByType) {
    const ctx = document.getElementById("winRateTypeChart").getContext("2d");
    const types = Object.keys(winRateByType);
    const values = Object.values(winRateByType);
    const colorMap = { moneyline: "#00f0ff", spread: "#8b5cf6", total: "#00ff88", futures: "#ff3366" };
    const bgMap = { moneyline: "rgba(0,240,255,0.7)", spread: "rgba(139,92,246,0.7)", total: "rgba(0,255,136,0.7)", futures: "rgba(255,51,102,0.7)" };
    const bgColors = types.map((t) => bgMap[t] || "rgba(255,255,255,0.2)");
    const borderColors = types.map((t) => colorMap[t] || "#71717a");

    if (winRateTypeChart) {
        winRateTypeChart.data.labels = types.map((t) => t.charAt(0).toUpperCase() + t.slice(1));
        winRateTypeChart.data.datasets[0].data = values;
        winRateTypeChart.data.datasets[0].backgroundColor = bgColors;
        winRateTypeChart.data.datasets[0].borderColor = borderColors;
        winRateTypeChart.update("none");
        return;
    }

    winRateTypeChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels: types.map((t) => t.charAt(0).toUpperCase() + t.slice(1)),
            datasets: [{ label: "Win Rate %", data: values, backgroundColor: bgColors, borderColor: borderColors, borderWidth: 1, borderRadius: 6, borderSkipped: false, barThickness: 40 }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: { duration: 800, easing: "easeOutQuart" },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18,19,26,0.95)", borderColor: "rgba(255,255,255,0.1)",
                    borderWidth: 1, cornerRadius: 8, padding: 12,
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    callbacks: { label: (ctx) => `${ctx.parsed.y.toFixed(1)}%` },
                },
            },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 11, weight: "500" }, color: "#e4e4e7" } },
                y: { min: 0, max: 100, grid: { color: "rgba(255,255,255,0.04)" }, ticks: { font: { family: "'JetBrains Mono'", size: 11 }, callback: (v) => `${v}%` } },
            },
        },
    });
}

// ---------------------------------------------------------------------------
// Line Movement Sparklines in Position Cards
// ---------------------------------------------------------------------------
let lineMovementCharts = {};

function renderLineMovements(lineData) {
    if (!lineData || !lineData.movements) return;
    const movements = lineData.movements || [];
    const openPositions = cachedPositions?.open || [];

    for (const p of openPositions) {
        const canvasEl = document.getElementById(`line-spark-${p.id}`);
        if (!canvasEl) continue;

        const match = movements.find(m =>
            m.position_id === p.id || m.market_slug === p.market_slug
        );
        if (!match || !match.prices || match.prices.length < 2) continue;

        const prices = match.prices;
        const lastPrice = prices[prices.length - 1];
        const firstPrice = prices[0];
        const trending = lastPrice >= firstPrice;
        const color = trending ? "#22c55e" : "#ef4444";

        if (lineMovementCharts[p.id]) {
            lineMovementCharts[p.id].data.datasets[0].data = prices;
            lineMovementCharts[p.id].data.labels = prices.map((_, i) => i);
            lineMovementCharts[p.id].update("none");
        } else {
            lineMovementCharts[p.id] = new Chart(canvasEl.getContext("2d"), {
                type: "line",
                data: {
                    labels: prices.map((_, i) => i),
                    datasets: [{
                        data: prices, borderColor: color, borderWidth: 1.5,
                        backgroundColor: "transparent", fill: false,
                        tension: 0.3, pointRadius: 0,
                    }],
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    animation: false,
                    plugins: { legend: { display: false }, tooltip: { enabled: false } },
                    scales: { x: { display: false }, y: { display: false } },
                },
            });
        }
    }
}

// ---------------------------------------------------------------------------
// NBA Odds Comparison Table
// ---------------------------------------------------------------------------
function renderNbaOddsTable(data) {
    const tbody = document.getElementById("nbaOddsTableBody");
    if (!tbody) return;
    const snapshots = ((data && data.snapshots) || []).filter(s =>
        !s.sport || s.sport.toLowerCase() === "nba"
    );
    const countEl = document.getElementById("nbaOddsCount");
    if (countEl) countEl.textContent = `${snapshots.length} games`;

    if (snapshots.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-table-cell">No NBA games being evaluated</td></tr>';
        return;
    }

    tbody.innerHTML = snapshots.map(s => {
        const edge = s.edge || 0;
        const edgePct = (edge * 100).toFixed(1);
        const rowClass = edge > 0.03 ? "odds-row-edge" : "";

        let statusBadge = '<span class="odds-status odds-status-noedge">NO EDGE</span>';
        if (s.status === "bet_placed" || s.bet_placed) {
            statusBadge = '<span class="odds-status odds-status-bet">BET PLACED</span>';
        } else if (edge > 0.03 || s.status === "watching") {
            statusBadge = '<span class="odds-status odds-status-watching">WATCHING</span>';
        }

        return `<tr class="${rowClass}">
            <td class="td-market">${escHtml(s.game || s.matchup || '')}</td>
            <td class="mono">${s.polymarket_price != null ? (s.polymarket_price * 100).toFixed(1) + '¢' : '--'}</td>
            <td class="mono">${s.vegas_price != null ? (s.vegas_price * 100).toFixed(1) + '¢' : '--'}</td>
            <td class="mono">${s.fair_value != null ? (s.fair_value * 100).toFixed(1) + '¢' : '--'}</td>
            <td class="mono ${edge > 0.03 ? 'pnl-positive' : edge < -0.03 ? 'pnl-negative' : ''}">${edgePct}%</td>
            <td>${statusBadge}</td>
        </tr>`;
    }).join("");
}

// ---------------------------------------------------------------------------
// Sort + Expand handlers
// ---------------------------------------------------------------------------
window.toggleTradeDetail = function (id) {
    expandedTradeId = expandedTradeId === id ? null : id;
    renderTrades();
};

document.addEventListener("click", (e) => {
    const th = e.target.closest("th.sortable");
    if (!th) return;
    const col = th.dataset.sort;
    if (sortColumn === col) sortDirection = sortDirection === "asc" ? "desc" : "asc";
    else { sortColumn = col; sortDirection = "desc"; }
    document.querySelectorAll("th.sortable").forEach((el) => {
        el.classList.remove("sort-active");
        el.querySelector(".sort-arrow").textContent = "↕";
    });
    th.classList.add("sort-active");
    th.querySelector(".sort-arrow").textContent = sortDirection === "asc" ? "↑" : "↓";
    renderTrades();
});

// ---------------------------------------------------------------------------
// Main refresh loop
// ---------------------------------------------------------------------------
async function refresh() {
    const [status, positions, stats, research, liveData, lineData, oddsData] = await Promise.all([
        fetchJSON("/api/status"),
        fetchJSON("/api/positions"),
        fetchJSON("/api/stats"),
        fetchJSON("/api/research"),
        fetchJSON("/api/live"),
        fetchJSON("/api/line-movements"),
        fetchJSON("/api/odds-snapshots"),
    ]);

    const anySuccess = status || positions || stats || research;
    setConnected(anySuccess);

    if (status) updateStatus(status);
    if (stats) updateStats(stats);

    if (liveData && liveData.positions) {
        cachedLive = {};
        for (const lp of liveData.positions) cachedLive[lp.id] = lp;
        updateUnrealizedPnl();
    }

    if (positions) {
        cachedPositions = positions;
        updatePortfolioValue();
        renderPositions();
        renderTrades();
        updateRedeemChecklist(positions);
    }

    // Line movement sparklines (after positions rendered)
    if (lineData) renderLineMovements(lineData);

    if (research) updateResearch(research);
    if (oddsData) renderNbaOddsTable(oddsData);
    fetchPerformance(currentPeriod);

    // API health
    fetchJSON("/api/api-health").then((h) => {
        if (!h || !h.sources) return;
        const map = { dotEspn: "espn", dotOdds: "odds_api", dotBdl: "balldontlie", dotPoly: "polymarket" };
        for (const [elId, key] of Object.entries(map)) {
            const el = document.getElementById(elId);
            if (!el) continue;
            const src = h.sources[key];
            el.className = `api-dot ${src && src.status === "ok" ? "ok" : "error"}`;
            el.title = `${key}: ${src ? src.status : "unknown"}`;
        }
    });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
initPeriodTabs();

if (authToken) {
    fetchJSON("/api/status").then((data) => {
        if (data) { showDashboard(); refresh(); }
        else { showLogin(); }
    });
} else {
    showLogin();
}

setInterval(() => { if (authToken) refresh(); }, REFRESH_INTERVAL);
