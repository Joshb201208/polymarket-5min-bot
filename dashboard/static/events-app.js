/* ===================================================================
   Events Agent Dashboard — events-app.js
   Fetches events API endpoints every 30s, updates DOM, draws charts.
   =================================================================== */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let categoryChart = null;
let exitChart = null;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
(function init() {
    if (authToken) {
        showDashboard();
        refresh();
    } else {
        showLogin();
    }
    setInterval(() => { if (authToken) refresh(); }, REFRESH_INTERVAL);
})();

// ---------------------------------------------------------------------------
// Main Refresh
// ---------------------------------------------------------------------------
async function refresh() {
    const [positions, trades, status, stats, lifecycle, regime] = await Promise.all([
        fetchJSON("/api/events/positions"),
        fetchJSON("/api/events/trades"),
        fetchJSON("/api/events/status"),
        fetchJSON("/api/events/stats"),
        fetchJSON("/api/events/lifecycle"),
        fetchJSON("/api/events/regime"),
    ]);

    const ok = positions || trades || status;
    setConnected(!!ok);

    if (status) renderStatus(status);
    if (positions) renderPositions(positions, lifecycle, regime);
    if (stats) renderStats(stats);
    if (trades) renderTrades(trades);
    if (lifecycle || regime) renderLifecycleRegime(lifecycle, regime);

    const el = document.getElementById("lastUpdate");
    if (el) el.textContent = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------
function renderStatus(data) {
    const badge = document.getElementById("modeBadge");
    if (badge) {
        const mode = (data.mode || "paper").toUpperCase();
        badge.querySelector(".mode-text").textContent = mode;
        badge.classList.toggle("mode-live", mode === "LIVE");
    }

    const scanEl = document.getElementById("kpiLastScan");
    if (scanEl) scanEl.textContent = data.events_last_scan ? fmt.relative(data.events_last_scan) : "--";
}

// ---------------------------------------------------------------------------
// Positions
// ---------------------------------------------------------------------------
function renderPositions(data, lifecycleData, regimeData) {
    const open = data.open || [];
    const closed = data.closed || [];
    const grid = document.getElementById("positionsGrid");
    const countEl = document.getElementById("openCount");
    const emptyEl = document.getElementById("emptyPositions");

    if (countEl) countEl.textContent = `${open.length} open`;

    // KPIs
    const exposure = open.reduce((sum, p) => sum + (p.cost || 0), 0);
    const el = document.getElementById("kpiExposure");
    if (el) el.textContent = fmt.usd(exposure);
    const subEl = document.getElementById("kpiExposureSub");
    if (subEl) subEl.textContent = `${open.length} open positions`;

    if (open.length === 0) {
        if (emptyEl) emptyEl.style.display = "";
        return;
    }
    if (emptyEl) emptyEl.style.display = "none";

    const lifecycleAssessments = (lifecycleData && lifecycleData.assessments) || {};
    const regimeAssessments = (regimeData && regimeData.assessments) || {};

    const stageColors = {
        early: "#3b82f6", developing: "#8b5cf6", mature: "#f59e0b",
        late: "#f97316", terminal: "#ef4444", unknown: "#71717a",
    };
    const regimeIcons = {
        trending: ">>", volatile: "!!", stale: "--", converging: "><",
    };

    let html = "";
    for (const p of open) {
        const cat = p.category || "other";
        const tpLevel = (p.entry_price * 1.30).toFixed(1);
        const slLevel = (p.entry_price * 0.75).toFixed(1);

        // Lifecycle badge
        const lc = lifecycleAssessments[p.market_id] || {};
        const stage = lc.stage || "";
        const stageColor = stageColors[stage] || "#71717a";
        const stageBadge = stage ? `<span class="badge" style="background:${stageColor}20;color:${stageColor};border:1px solid ${stageColor}40;font-size:10px;padding:2px 6px;border-radius:10px;text-transform:uppercase;font-weight:600;letter-spacing:0.05em;">${stage}</span>` : "";

        // Regime chip
        const rg = regimeAssessments[p.market_id] || {};
        const regime = rg.regime || "";
        const regimeChip = regime ? `<span style="font-size:10px;color:var(--text-muted);font-family:var(--mono);">${regimeIcons[regime] || "??"} ${regime}</span>` : "";

        html += `
        <div class="position-card">
            <div class="position-header">
                <span class="position-market">${escHtml(p.market_question)}</span>
                <div style="display:flex;gap:6px;align-items:center;">
                    ${stageBadge}
                    <span class="badge badge-purple">${escHtml(cat)}</span>
                </div>
            </div>
            <div class="position-details">
                <div class="position-detail">
                    <span class="detail-label">Side</span>
                    <span class="detail-value">${escHtml(p.side)}</span>
                </div>
                <div class="position-detail">
                    <span class="detail-label">Entry</span>
                    <span class="detail-value">${fmt.price(p.entry_price)}</span>
                </div>
                <div class="position-detail">
                    <span class="detail-label">Cost</span>
                    <span class="detail-value">${fmt.usd(p.cost)}</span>
                </div>
                <div class="position-detail">
                    <span class="detail-label">Edge</span>
                    <span class="detail-value">${fmt.edge(p.edge_at_entry)}</span>
                </div>
                <div class="position-detail">
                    <span class="detail-label">Confidence</span>
                    <span class="detail-value">${escHtml(p.confidence)}</span>
                </div>
                <div class="position-detail">
                    <span class="detail-label">TP / SL</span>
                    <span class="detail-value" style="color:#22c55e">${tpLevel}&#162;</span> / <span class="detail-value" style="color:#ef4444">${slLevel}&#162;</span>
                </div>
            </div>
            <div class="position-footer">
                <span class="position-time">${fmt.relative(p.entry_time)}</span>
                <span style="display:flex;gap:8px;align-items:center;">
                    ${regimeChip}
                    <span class="position-edge-source">${escHtml(p.edge_source || "")}</span>
                </span>
            </div>
        </div>`;
    }
    grid.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------
function renderStats(data) {
    const pnlEl = document.getElementById("kpiPnl");
    if (pnlEl) {
        pnlEl.textContent = fmt.usd(data.total_pnl || 0);
        pnlEl.className = "kpi-value " + pnlClass(data.total_pnl || 0);
    }
    const pnlSub = document.getElementById("kpiPnlSub");
    if (pnlSub) pnlSub.textContent = `ROI: ${(data.roi || 0).toFixed(1)}%`;

    const wrEl = document.getElementById("kpiWinRate");
    if (wrEl) wrEl.textContent = fmt.pct(data.win_rate || 0);
    const wEl = document.getElementById("kpiWins");
    if (wEl) wEl.textContent = data.wins || 0;
    const lEl = document.getElementById("kpiLosses");
    if (lEl) lEl.textContent = data.losses || 0;

    // Avg hold time
    const holdEl = document.getElementById("kpiAvgHold");
    if (holdEl) {
        const hours = data.avg_hold_hours;
        if (hours != null && hours > 0) {
            if (hours > 24) {
                holdEl.textContent = `${(hours / 24).toFixed(1)}d`;
            } else {
                holdEl.textContent = `${hours.toFixed(0)}h`;
            }
        } else {
            holdEl.textContent = "--";
        }
    }

    // Category chart
    if (data.category_breakdown) {
        renderCategoryChart(data.category_breakdown);
    }

    // Exit analysis chart
    if (data.exit_reasons) {
        renderExitChart(data.exit_reasons);
    }
}

function renderCategoryChart(catData) {
    const ctx = document.getElementById("categoryChart");
    if (!ctx) return;

    const labels = Object.keys(catData);
    const values = Object.values(catData);

    const colors = {
        politics: "#8b5cf6",
        geopolitics: "#ef4444",
        economics: "#f59e0b",
        crypto: "#00f0ff",
        culture: "#ec4899",
        science: "#22c55e",
        entertainment: "#f97316",
        technology: "#3b82f6",
        other: "#71717a",
    };

    const bgColors = labels.map(l => colors[l] || "#71717a");

    if (categoryChart) categoryChart.destroy();
    categoryChart = new Chart(ctx, {
        type: "doughnut",
        data: {
            labels: labels.map(l => l.charAt(0).toUpperCase() + l.slice(1)),
            datasets: [{
                data: values,
                backgroundColor: bgColors,
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "bottom", labels: { boxWidth: 12, padding: 8, font: { size: 11 } } },
            },
        },
    });
}

function renderExitChart(exitData) {
    const ctx = document.getElementById("exitChart");
    if (!ctx) return;

    const labels = Object.keys(exitData);
    const values = Object.values(exitData);

    const colors = {
        "Take profit": "#22c55e",
        "Stop loss": "#ef4444",
        "Market resolved: WIN": "#00f0ff",
        "Market resolved: LOSS": "#f59e0b",
        "Low liquidity exit": "#71717a",
    };

    const bgColors = labels.map(l => {
        for (const [key, color] of Object.entries(colors)) {
            if (l.includes(key) || l.toLowerCase().includes(key.toLowerCase())) return color;
        }
        return "#71717a";
    });

    if (exitChart) exitChart.destroy();
    exitChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: bgColors,
                borderWidth: 0,
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, ticks: { stepSize: 1 } },
                x: { ticks: { font: { size: 10 } } },
            },
        },
    });
}

// ---------------------------------------------------------------------------
// Trades
// ---------------------------------------------------------------------------
function renderTrades(data) {
    const trades = data.trades || [];
    const closedPositions = data.closed_positions || [];
    const tbody = document.getElementById("tradeTableBody");
    const badge = document.getElementById("tradeCountBadge");

    if (badge) badge.textContent = `${closedPositions.length} closed`;

    if (closedPositions.length === 0) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="8" class="empty-table-cell">No closed events positions yet</td></tr>';
        return;
    }

    // Sort by exit time descending
    const sorted = [...closedPositions].sort((a, b) => (b.exit_time || "").localeCompare(a.exit_time || ""));

    let html = "";
    for (const p of sorted) {
        const pnl = p.pnl || 0;
        const pnlCls = pnlClass(pnl);
        const sign = pnl >= 0 ? "+" : "";

        html += `<tr>
            <td>${fmt.datetime(p.exit_time)}</td>
            <td class="trade-market">${escHtml((p.market_question || "").slice(0, 60))}</td>
            <td><span class="badge badge-purple">${escHtml(p.category || "other")}</span></td>
            <td>${fmt.price(p.entry_price)}</td>
            <td>${fmt.price(p.exit_price)}</td>
            <td class="${pnlCls}">${sign}${fmt.usd(pnl)}</td>
            <td>${fmt.edge(p.edge_at_entry)}</td>
            <td class="trade-exit-reason">${escHtml(p.exit_reason || "")}</td>
        </tr>`;
    }
    if (tbody) tbody.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Lifecycle & Regime Panel
// ---------------------------------------------------------------------------
function renderLifecycleRegime(lifecycleData, regimeData) {
    const section = document.getElementById("lifecycleSection");
    const grid = document.getElementById("lifecycleGrid");
    const countEl = document.getElementById("lifecycleCount");
    if (!section || !grid) return;

    const lcAssessments = (lifecycleData && lifecycleData.assessments) || {};
    const rgAssessments = (regimeData && regimeData.assessments) || {};

    const allIds = new Set([...Object.keys(lcAssessments), ...Object.keys(rgAssessments)]);
    if (allIds.size === 0) {
        section.style.display = "none";
        return;
    }
    section.style.display = "";
    if (countEl) countEl.textContent = `${allIds.size} markets`;

    const stageColors = {
        early: "#3b82f6", developing: "#8b5cf6", mature: "#f59e0b",
        late: "#f97316", terminal: "#ef4444", unknown: "#71717a",
    };

    let html = "";
    for (const id of allIds) {
        const lc = lcAssessments[id] || {};
        const rg = rgAssessments[id] || {};
        const question = (lc.market_question || rg.market_question || id).slice(0, 55);
        const stage = lc.stage || "unknown";
        const stageColor = stageColors[stage] || "#71717a";
        const days = lc.days_remaining != null ? `${Math.round(lc.days_remaining)}d` : "--";
        const regime = rg.regime || "--";
        const rec = rg.recommendation || "--";

        html += `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px;">
            <div style="font-size:12px;color:var(--text);font-weight:500;margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(question)}</div>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <span style="background:${stageColor}20;color:${stageColor};border:1px solid ${stageColor}40;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600;text-transform:uppercase;">${stage}</span>
                <span style="font-size:11px;color:var(--text-muted);font-family:var(--mono);">${days} left</span>
                <span style="font-size:10px;color:var(--text-dim);font-family:var(--mono);">regime: ${regime}</span>
                <span style="font-size:10px;color:var(--text-dim);font-family:var(--mono);">${rec}</span>
            </div>
        </div>`;
    }
    grid.innerHTML = html;
}
