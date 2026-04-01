/* ===================================================================
   Analytics Dashboard — analytics-app.js
   Fetches events API data, computes analytics, renders Chart.js charts.
   =================================================================== */

// ---------------------------------------------------------------------------
// Chart instances
// ---------------------------------------------------------------------------
let winRateByCatChart = null;
let pnlByCatChart = null;
let entryPriceChart = null;
let exitAnalysisChart = null;
let holdTimeChart = null;
let equityCurveChart = null;

// Sort state for trade table
let tradeSortKey = "date";
let tradeSortDir = "desc";
let closedPositionsCache = [];

// ---------------------------------------------------------------------------
// Color palette
// ---------------------------------------------------------------------------
const COLORS = {
    teal: "#00f0ff",
    purple: "#8b5cf6",
    green: "#22c55e",
    red: "#ef4444",
    amber: "#f59e0b",
    blue: "#3b82f6",
    pink: "#ec4899",
    indigo: "#6366f1",
    cyan: "#06b6d4",
    emerald: "#10b981",
};
const CATEGORY_COLORS = [COLORS.teal, COLORS.purple, COLORS.green, COLORS.amber, COLORS.blue, COLORS.pink, COLORS.indigo, COLORS.cyan, COLORS.emerald, COLORS.red];

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
    initTradeSort();
})();

// ---------------------------------------------------------------------------
// Main Refresh
// ---------------------------------------------------------------------------
async function refresh() {
    const [stats, positions, trades, portfolio] = await Promise.all([
        fetchJSON("/api/events/stats"),
        fetchJSON("/api/events/positions"),
        fetchJSON("/api/events/trades"),
        fetchJSON("/api/events/portfolio_value"),
    ]);

    const ok = stats || positions || trades;
    setConnected(!!ok);

    const closed = positions ? positions.closed || [] : [];
    closedPositionsCache = closed;

    if (stats) renderKPIs(stats, portfolio, closed);
    if (closed.length) {
        renderWinRateByCategory(closed);
        renderPnlByCategory(closed);
        renderEntryPriceAnalysis(closed);
        renderExitAnalysis(closed);
        renderHoldTimeAnalysis(closed);
        renderEquityCurve(closed);
    }
    renderTradeTable(closed);

    const el = document.getElementById("lastUpdate");
    if (el) el.textContent = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------
function renderKPIs(stats, portfolio, closed) {
    setText("kpiTotalTrades", stats.total_trades);
    setText("kpiWinsLosses", `${stats.wins}W / ${stats.losses}L`);

    const wrEl = document.getElementById("kpiWinRate");
    if (wrEl) {
        wrEl.textContent = `${stats.win_rate}%`;
        wrEl.className = "kpi-value " + (stats.win_rate >= 50 ? "pnl-positive" : "pnl-negative");
    }
    setText("kpiWinRateSub", `${stats.wins} of ${stats.total_trades} trades`);

    const pnlEl = document.getElementById("kpiRealizedPnl");
    if (pnlEl) {
        pnlEl.textContent = fmt.usd(stats.total_pnl);
        pnlEl.className = "kpi-value " + pnlClass(stats.total_pnl);
    }
    setText("kpiRealizedSub", `ROI ${stats.roi}%`);

    // Profit factor
    const grossWin = closed.filter(p => (p.pnl || 0) > 0).reduce((s, p) => s + (p.pnl || 0), 0);
    const grossLoss = Math.abs(closed.filter(p => (p.pnl || 0) < 0).reduce((s, p) => s + (p.pnl || 0), 0));
    const pf = grossLoss > 0 ? (grossWin / grossLoss).toFixed(2) : grossWin > 0 ? "∞" : "--";
    setText("kpiProfitFactor", pf);

    // Avg win / loss
    const wins = closed.filter(p => (p.pnl || 0) > 0);
    const losses = closed.filter(p => (p.pnl || 0) < 0);
    const avgWin = wins.length > 0 ? wins.reduce((s, p) => s + (p.pnl || 0), 0) / wins.length : 0;
    const avgLoss = losses.length > 0 ? losses.reduce((s, p) => s + (p.pnl || 0), 0) / losses.length : 0;
    setText("kpiAvgWin", fmt.usd(avgWin));
    setText("kpiAvgWinSub", `${wins.length} winning trades`);
    setText("kpiAvgLoss", fmt.usd(avgLoss));
    setText("kpiAvgLossSub", `${losses.length} losing trades`);

    const roiEl = document.getElementById("kpiROI");
    if (roiEl) {
        roiEl.textContent = `${stats.roi}%`;
        roiEl.className = "kpi-value " + pnlClass(stats.roi);
    }
    if (portfolio) {
        setText("kpiROISub", `${fmt.usd(portfolio.starting_bankroll)} starting`);
    }
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

// ---------------------------------------------------------------------------
// Win Rate by Category (bar chart)
// ---------------------------------------------------------------------------
function renderWinRateByCategory(closed) {
    const cats = {};
    for (const p of closed) {
        const cat = p.category || "other";
        if (!cats[cat]) cats[cat] = { wins: 0, total: 0 };
        cats[cat].total++;
        if ((p.pnl || 0) > 0) cats[cat].wins++;
    }

    const labels = Object.keys(cats).sort((a, b) => cats[b].total - cats[a].total);
    const winRates = labels.map(c => Math.round((cats[c].wins / cats[c].total) * 100));
    const counts = labels.map(c => cats[c].total);
    const colors = winRates.map(wr => wr >= 50 ? COLORS.green : COLORS.red);

    const ctx = document.getElementById("winRateByCatChart");
    if (winRateByCatChart) winRateByCatChart.destroy();
    winRateByCatChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "Win Rate %",
                data: winRates,
                backgroundColor: colors.map(c => c + "cc"),
                borderColor: colors,
                borderWidth: 1,
                borderRadius: 4,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.raw}% win rate (${counts[ctx.dataIndex]} trades)`
                    }
                }
            },
            scales: {
                y: { beginAtZero: true, max: 100, ticks: { callback: v => v + "%" }, grid: { color: "rgba(255,255,255,0.06)" } },
                x: { grid: { display: false } }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// P&L by Category (bar chart)
// ---------------------------------------------------------------------------
function renderPnlByCategory(closed) {
    const cats = {};
    for (const p of closed) {
        const cat = p.category || "other";
        cats[cat] = (cats[cat] || 0) + (p.pnl || 0);
    }

    const labels = Object.keys(cats).sort((a, b) => cats[b] - cats[a]);
    const values = labels.map(c => Math.round(cats[c] * 100) / 100);
    const colors = values.map(v => v >= 0 ? COLORS.green : COLORS.red);

    const ctx = document.getElementById("pnlByCatChart");
    if (pnlByCatChart) pnlByCatChart.destroy();
    pnlByCatChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "P&L ($)",
                data: values,
                backgroundColor: colors.map(c => c + "cc"),
                borderColor: colors,
                borderWidth: 1,
                borderRadius: 4,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `$${ctx.raw.toFixed(2)}`
                    }
                }
            },
            scales: {
                y: { ticks: { callback: v => "$" + v }, grid: { color: "rgba(255,255,255,0.06)" } },
                x: { grid: { display: false } }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Entry Price Analysis (histogram: trade count + avg P&L by entry price bucket)
// ---------------------------------------------------------------------------
function renderEntryPriceAnalysis(closed) {
    // Bucket by entry price in 5¢ increments
    const buckets = {};
    for (const p of closed) {
        const price = p.entry_price || 0;
        const bucket = Math.floor(price * 20) * 5; // 5¢ buckets
        const label = `${bucket}¢-${bucket + 5}¢`;
        if (!buckets[label]) buckets[label] = { pnlSum: 0, count: 0, order: bucket };
        buckets[label].pnlSum += (p.pnl || 0);
        buckets[label].count++;
    }

    const sorted = Object.entries(buckets).sort((a, b) => a[1].order - b[1].order);
    const labels = sorted.map(([l]) => l);
    const avgPnls = sorted.map(([, b]) => Math.round((b.pnlSum / b.count) * 100) / 100);
    const counts = sorted.map(([, b]) => b.count);
    const pnlColors = avgPnls.map(v => v >= 0 ? COLORS.green + "cc" : COLORS.red + "cc");

    const ctx = document.getElementById("entryPriceChart");
    if (entryPriceChart) entryPriceChart.destroy();
    entryPriceChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [
                {
                    label: "Avg P&L ($)",
                    data: avgPnls,
                    backgroundColor: pnlColors,
                    borderRadius: 4,
                    yAxisID: "y",
                },
                {
                    label: "Trade Count",
                    data: counts,
                    type: "line",
                    borderColor: COLORS.teal,
                    backgroundColor: COLORS.teal + "33",
                    pointBackgroundColor: COLORS.teal,
                    pointRadius: 4,
                    fill: false,
                    tension: 0.3,
                    yAxisID: "y1",
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { usePointStyle: true, pointStyle: "circle", padding: 16 } }
            },
            scales: {
                y: { position: "left", ticks: { callback: v => "$" + v }, grid: { color: "rgba(255,255,255,0.06)" } },
                y1: { position: "right", ticks: { stepSize: 1 }, grid: { display: false } },
                x: { grid: { display: false } }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Exit Analysis (grouped bar: win rate + avg P&L by exit reason)
// ---------------------------------------------------------------------------
function renderExitAnalysis(closed) {
    const reasons = {};
    for (const p of closed) {
        const reason = simplifyExitReason(p.exit_reason || "Unknown");
        if (!reasons[reason]) reasons[reason] = { wins: 0, total: 0, pnlSum: 0 };
        reasons[reason].total++;
        reasons[reason].pnlSum += (p.pnl || 0);
        if ((p.pnl || 0) > 0) reasons[reason].wins++;
    }

    const labels = Object.keys(reasons).sort((a, b) => reasons[b].total - reasons[a].total);
    const winRates = labels.map(r => Math.round((reasons[r].wins / reasons[r].total) * 100));
    const avgPnls = labels.map(r => Math.round((reasons[r].pnlSum / reasons[r].total) * 100) / 100);

    const ctx = document.getElementById("exitAnalysisChart");
    if (exitAnalysisChart) exitAnalysisChart.destroy();
    exitAnalysisChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [
                {
                    label: "Win Rate %",
                    data: winRates,
                    backgroundColor: COLORS.teal + "cc",
                    borderColor: COLORS.teal,
                    borderWidth: 1,
                    borderRadius: 4,
                    yAxisID: "y",
                },
                {
                    label: "Avg P&L ($)",
                    data: avgPnls,
                    backgroundColor: COLORS.purple + "cc",
                    borderColor: COLORS.purple,
                    borderWidth: 1,
                    borderRadius: 4,
                    yAxisID: "y1",
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { usePointStyle: true, pointStyle: "circle", padding: 16 } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            if (ctx.datasetIndex === 0) return `${ctx.raw}% win rate (${reasons[labels[ctx.dataIndex]].total} trades)`;
                            return `$${ctx.raw.toFixed(2)} avg P&L`;
                        }
                    }
                }
            },
            scales: {
                y: { position: "left", beginAtZero: true, max: 100, ticks: { callback: v => v + "%" }, grid: { color: "rgba(255,255,255,0.06)" } },
                y1: { position: "right", ticks: { callback: v => "$" + v }, grid: { display: false } },
                x: { grid: { display: false } }
            }
        }
    });
}

function simplifyExitReason(reason) {
    if (!reason) return "Unknown";
    const r = reason.toLowerCase();
    if (r.includes("take profit")) return "Take Profit";
    if (r.includes("stop loss")) return "Stop Loss";
    if (r.includes("win")) return "Market: WIN";
    if (r.includes("loss")) return "Market: LOSS";
    if (r.includes("liquidity")) return "Low Liquidity";
    if (r.includes("manual") || r.includes("force")) return "Manual Close";
    return reason.length > 20 ? reason.slice(0, 20) + "…" : reason;
}

// ---------------------------------------------------------------------------
// Hold Time Analysis (scatter: hold time vs P&L, colored win/loss)
// ---------------------------------------------------------------------------
function renderHoldTimeAnalysis(closed) {
    const wins = [];
    const losses = [];

    for (const p of closed) {
        const entry = p.entry_time ? new Date(p.entry_time) : null;
        const exit = p.exit_time ? new Date(p.exit_time) : null;
        if (!entry || !exit) continue;
        const holdHours = Math.round((exit - entry) / 3600000 * 10) / 10;
        const pnl = p.pnl || 0;
        const point = { x: holdHours, y: pnl };
        if (pnl > 0) wins.push(point);
        else losses.push(point);
    }

    const ctx = document.getElementById("holdTimeChart");
    if (holdTimeChart) holdTimeChart.destroy();
    holdTimeChart = new Chart(ctx, {
        type: "scatter",
        data: {
            datasets: [
                {
                    label: "Wins",
                    data: wins,
                    backgroundColor: COLORS.green + "99",
                    borderColor: COLORS.green,
                    pointRadius: 5,
                    pointHoverRadius: 7,
                },
                {
                    label: "Losses",
                    data: losses,
                    backgroundColor: COLORS.red + "99",
                    borderColor: COLORS.red,
                    pointRadius: 5,
                    pointHoverRadius: 7,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { usePointStyle: true, pointStyle: "circle", padding: 16 } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `Hold: ${ctx.raw.x}h | P&L: $${ctx.raw.y.toFixed(2)}`
                    }
                }
            },
            scales: {
                x: { title: { display: true, text: "Hold Time (hours)", color: "#a1a1aa" }, grid: { color: "rgba(255,255,255,0.06)" } },
                y: { title: { display: true, text: "P&L ($)", color: "#a1a1aa" }, ticks: { callback: v => "$" + v }, grid: { color: "rgba(255,255,255,0.06)" } }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Equity Curve (cumulative P&L over time)
// ---------------------------------------------------------------------------
function renderEquityCurve(closed) {
    // Sort by exit time ascending
    const sorted = [...closed]
        .filter(p => p.exit_time)
        .sort((a, b) => new Date(a.exit_time) - new Date(b.exit_time));

    let cumPnl = 0;
    const data = sorted.map(p => {
        cumPnl += (p.pnl || 0);
        return { x: new Date(p.exit_time), y: Math.round(cumPnl * 100) / 100 };
    });

    // Add starting point
    if (data.length > 0) {
        data.unshift({ x: new Date(data[0].x.getTime() - 86400000), y: 0 });
    }

    const ctx = document.getElementById("equityCurveChart");
    if (equityCurveChart) equityCurveChart.destroy();
    equityCurveChart = new Chart(ctx, {
        type: "line",
        data: {
            datasets: [{
                label: "Cumulative P&L",
                data,
                borderColor: COLORS.teal,
                backgroundColor: COLORS.teal + "1a",
                fill: true,
                tension: 0.3,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: COLORS.teal,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: (items) => {
                            const d = new Date(items[0].raw.x);
                            return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: TZ });
                        },
                        label: (ctx) => `Cumulative P&L: $${ctx.raw.y.toFixed(2)}`
                    }
                }
            },
            scales: {
                x: {
                    type: "time",
                    time: { unit: "day", tooltipFormat: "MMM d" },
                    grid: { color: "rgba(255,255,255,0.06)" },
                    ticks: { maxTicksLimit: 12 }
                },
                y: {
                    ticks: { callback: v => "$" + v },
                    grid: { color: "rgba(255,255,255,0.06)" }
                }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Trade History Table (sortable)
// ---------------------------------------------------------------------------
function renderTradeTable(closed) {
    const tbody = document.getElementById("tradeTableBody");
    if (!tbody) return;

    setText("tradeCountBadge", `${closed.length} trades`);

    const sorted = sortTrades(closed);

    tbody.innerHTML = sorted.map(p => {
        const pnl = p.pnl || 0;
        const entry = p.entry_price || 0;
        const exit = p.exit_price || p.sell_price || 0;
        const holdH = holdHours(p.entry_time, p.exit_time);
        const reason = simplifyExitReason(p.exit_reason);

        return `<tr>
            <td>${fmt.datetime(p.exit_time)}</td>
            <td class="market-cell" title="${escHtml(p.market_question || '')}">${escHtml((p.market_question || "").slice(0, 50))}${(p.market_question || "").length > 50 ? "…" : ""}</td>
            <td><span class="badge badge-small">${escHtml(p.category || "other")}</span></td>
            <td>${p.side || "YES"}</td>
            <td>${fmt.price(entry)}</td>
            <td>${fmt.price(exit)}</td>
            <td class="${pnlClass(pnl)}">${fmt.usd(pnl)}</td>
            <td>${holdH}</td>
            <td><span class="badge badge-small">${escHtml(reason)}</span></td>
        </tr>`;
    }).join("");
}

function sortTrades(closed) {
    const sorted = [...closed];
    sorted.sort((a, b) => {
        let va, vb;
        switch (tradeSortKey) {
            case "date": va = a.exit_time || ""; vb = b.exit_time || ""; break;
            case "entry": va = a.entry_price || 0; vb = b.entry_price || 0; break;
            case "exit": va = a.exit_price || a.sell_price || 0; vb = b.exit_price || b.sell_price || 0; break;
            case "pnl": va = a.pnl || 0; vb = b.pnl || 0; break;
            case "hold":
                va = holdMs(a.entry_time, a.exit_time);
                vb = holdMs(b.entry_time, b.exit_time);
                break;
            default: va = a.exit_time || ""; vb = b.exit_time || "";
        }
        if (va < vb) return tradeSortDir === "asc" ? -1 : 1;
        if (va > vb) return tradeSortDir === "asc" ? 1 : -1;
        return 0;
    });
    return sorted;
}

function holdMs(entry, exit) {
    if (!entry || !exit) return 0;
    return new Date(exit) - new Date(entry);
}

function holdHours(entry, exit) {
    if (!entry || !exit) return "--";
    const ms = new Date(exit) - new Date(entry);
    const hours = Math.floor(ms / 3600000);
    const mins = Math.floor((ms % 3600000) / 60000);
    if (hours > 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
    return `${hours}h ${mins}m`;
}

function initTradeSort() {
    document.querySelectorAll(".sortable").forEach(th => {
        th.addEventListener("click", () => {
            const key = th.dataset.sort;
            if (tradeSortKey === key) {
                tradeSortDir = tradeSortDir === "asc" ? "desc" : "asc";
            } else {
                tradeSortKey = key;
                tradeSortDir = "desc";
            }
            // Update arrows
            document.querySelectorAll(".sortable .sort-arrow").forEach(a => a.textContent = "▲");
            th.querySelector(".sort-arrow").textContent = tradeSortDir === "asc" ? "▲" : "▼";
            renderTradeTable(closedPositionsCache);
        });
    });
}
