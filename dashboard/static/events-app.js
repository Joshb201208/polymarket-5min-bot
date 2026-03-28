/* ===================================================================
   Events Agent Dashboard — events-app.js
   Fetches events API endpoints every 30s, updates DOM, draws charts.
   =================================================================== */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let categoryChart = null;
let exitChart = null;
let lastTickerPrices = {};
let tradeSortKey = "date";
let tradeSortDir = "desc";

const TICKER_INTERVAL = 15_000; // 15 seconds for ticker

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
    setInterval(() => { if (authToken) refreshTicker(); }, TICKER_INTERVAL);
    initTradeSort();
})();

// ---------------------------------------------------------------------------
// Main Refresh
// ---------------------------------------------------------------------------
async function refresh() {
    const [positions, trades, status, stats, lifecycle, regime, portfolio, unrealized, exitStatus, categories] = await Promise.all([
        fetchJSON("/api/events/positions"),
        fetchJSON("/api/events/trades"),
        fetchJSON("/api/events/status"),
        fetchJSON("/api/events/stats"),
        fetchJSON("/api/events/lifecycle"),
        fetchJSON("/api/events/regime"),
        fetchJSON("/api/events/portfolio_value"),
        fetchJSON("/api/events/unrealized"),
        fetchJSON("/api/events/exit_status"),
        fetchJSON("/api/events/categories"),
    ]);

    const ok = positions || trades || status;
    setConnected(!!ok);

    if (status) renderStatus(status);
    if (portfolio) renderPortfolioKPIs(portfolio);
    if (unrealized) renderPositions(unrealized, lifecycle, regime, exitStatus);
    if (stats) renderStats(stats, portfolio);
    if (trades) renderTrades(trades);
    if (categories) renderCategoryChartEnhanced(categories);

    const el = document.getElementById("lastUpdate");
    if (el) el.textContent = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
}

// ---------------------------------------------------------------------------
// Ticker Refresh (every 15s)
// ---------------------------------------------------------------------------
async function refreshTicker() {
    const tickerData = await fetchJSON("/api/events/ticker");
    if (tickerData) renderTicker(tickerData);
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

    const scanSub = document.getElementById("kpiLastScanSub");
    if (scanSub) scanSub.textContent = data.events_last_scan ? `Last scan: ${fmt.relative(data.events_last_scan)}` : "Last scan: --";
}

// ---------------------------------------------------------------------------
// Portfolio KPIs
// ---------------------------------------------------------------------------
function renderPortfolioKPIs(data) {
    setText("kpiPortfolioValue", fmt.usd(data.portfolio_value));
    setText("kpiPortfolioSub", `from ${fmt.usd(data.starting_bankroll)} start`);

    const pnlEl = document.getElementById("kpiUnrealizedPnl");
    if (pnlEl) {
        const pnl = data.unrealized_pnl || 0;
        const pct = data.unrealized_pnl_pct || 0;
        const sign = pnl >= 0 ? "+" : "";
        pnlEl.textContent = `${sign}${fmt.usd(pnl)}`;
        pnlEl.className = "kpi-value " + pnlClass(pnl);
        setText("kpiUnrealizedSub", `${sign}${pct.toFixed(1)}% | ${data.positions_up || 0} up / ${data.positions_down || 0} down`);
    }

    setText("kpiCash", fmt.usd(data.cash));
    setText("kpiCashSub", `${(data.cash_pct || 0).toFixed(0)}% of bankroll`);

    setText("kpiDeployed", fmt.usd(data.deployed));
    setText("kpiDeployedSub", `${(data.deployed_pct || 0).toFixed(0)}% of bankroll`);

    // Best/worst position
    if (data.best_position) {
        const bp = data.best_position;
        setText("kpiBestPos", `+${bp.pnl_pct}%`);
        setText("kpiBestPosSub", bp.question);
    } else {
        setText("kpiBestPos", "--");
        setText("kpiBestPosSub", "--");
    }

    if (data.worst_position) {
        const wp = data.worst_position;
        setText("kpiWorstPos", `${wp.pnl_pct}%`);
        setText("kpiWorstPosSub", wp.question);
    } else {
        setText("kpiWorstPos", "--");
        setText("kpiWorstPosSub", "--");
    }

    // Avg hold time
    const holdEl = document.getElementById("kpiAvgHold");
    if (holdEl) {
        const hours = data.avg_hold_hours;
        if (hours != null && hours > 0) {
            holdEl.textContent = hours > 24 ? `${(hours / 24).toFixed(1)}d` : `${hours.toFixed(0)}h`;
        } else {
            holdEl.textContent = "--";
        }
    }

    setText("kpiActiveCount", String(data.active_positions || 0));

    // Overview row
    setText("ovActive", String(data.active_positions || 0));
    const ovUnr = document.getElementById("ovUnrealized");
    if (ovUnr) {
        ovUnr.textContent = `${data.unrealized_pnl >= 0 ? "+" : ""}${fmt.usd(data.unrealized_pnl || 0)}`;
        ovUnr.className = "stat-mini-value " + pnlClass(data.unrealized_pnl || 0);
    }
    const ovReal = document.getElementById("ovRealized");
    if (ovReal) {
        ovReal.textContent = `${data.realized_pnl >= 0 ? "+" : ""}${fmt.usd(data.realized_pnl || 0)}`;
        ovReal.className = "stat-mini-value " + pnlClass(data.realized_pnl || 0);
    }
    setText("ovExposure", fmt.usd(data.deployed));
}

// ---------------------------------------------------------------------------
// Positions (Enhanced Cards)
// ---------------------------------------------------------------------------
function renderPositions(unrealizedData, lifecycleData, regimeData, exitStatusData) {
    const positions = unrealizedData.positions || [];
    const grid = document.getElementById("positionsGrid");
    const countEl = document.getElementById("openCount");
    const emptyEl = document.getElementById("emptyPositions");

    if (countEl) countEl.textContent = `${positions.length} open`;

    if (positions.length === 0) {
        if (emptyEl) emptyEl.style.display = "";
        return;
    }
    if (emptyEl) emptyEl.style.display = "none";

    const lifecycleAssessments = (lifecycleData && lifecycleData.assessments) || {};
    const regimeAssessments = (regimeData && regimeData.assessments) || {};
    const exitStatuses = {};
    if (exitStatusData && exitStatusData.positions) {
        for (const es of exitStatusData.positions) {
            exitStatuses[es.id] = es;
        }
    }

    const stageColors = {
        early: "#3b82f6", developing: "#00f0ff", mature: "#f59e0b",
        late: "#f97316", terminal: "#ef4444", unknown: "#71717a",
    };
    const regimeIcons = {
        trending: "TRENDING", volatile: "VOLATILE", stale: "STALE", converging: "CONVERGING",
    };

    let html = "";
    for (const p of positions) {
        const cat = p.category || "other";
        const side = (p.side || "YES").toUpperCase();
        const sideClass = side === "YES" ? "ev-side-yes" : "ev-side-no";
        const entryPrice = p.entry_price || 0;
        const currentPrice = p.current_price;
        const pnl = p.unrealized_pnl;
        const pnlPct = p.unrealized_pnl_pct;
        const peakPnlPct = p.peak_pnl_pct;
        const cost = p.cost || 0;
        const currentValue = p.current_value;

        // Price arrow
        let priceArrow = "\u2192";
        let priceClass = "pnl-neutral";
        if (currentPrice != null && entryPrice > 0) {
            if (currentPrice > entryPrice) { priceArrow = "\u2191"; priceClass = "pnl-positive"; }
            else if (currentPrice < entryPrice) { priceArrow = "\u2193"; priceClass = "pnl-negative"; }
        }

        // P&L display
        const pnlSign = pnl >= 0 ? "+" : "";
        const pnlColor = pnl >= 0 ? "var(--profit)" : "var(--loss)";
        const pnlCls = pnl >= 0 ? "pnl-positive" : (pnl < 0 ? "pnl-negative" : "pnl-neutral");

        // Lifecycle
        const lc = lifecycleAssessments[p.market_id] || {};
        const stage = lc.stage || "";
        const stageColor = stageColors[stage] || "#71717a";
        const daysLeft = lc.days_remaining != null ? `${Math.round(lc.days_remaining)}d left` : "";
        const stageBadge = stage ? `<span class="ev-stage-badge" style="background:${stageColor}20;color:${stageColor};border-color:${stageColor}40;">${stage.toUpperCase()}</span>` : "";

        // Regime
        const rg = regimeAssessments[p.market_id] || {};
        const regime = rg.regime || "";
        const regimeChip = regime ? `<span class="ev-regime-chip">${regimeIcons[regime] || regime.toUpperCase()}</span>` : "";

        // Hold duration
        let holdStr = "--";
        if (p.hold_hours != null) {
            if (p.hold_hours >= 24) {
                const days = Math.floor(p.hold_hours / 24);
                const hrs = Math.round(p.hold_hours % 24);
                holdStr = `${days}d ${hrs}h`;
            } else {
                holdStr = `${Math.round(p.hold_hours)}h`;
            }
        }

        // Trailing stop bar
        let trailingStopHtml = "";
        if (peakPnlPct != null && peakPnlPct > 0) {
            const stopLevel = peakPnlPct * 0.6; // 40% drawdown from peak
            const barPct = Math.min(100, Math.max(0, (pnlPct || 0) / peakPnlPct * 100));
            const stopPct = 60; // trailing stop at 60% of peak
            trailingStopHtml = `
                <div class="ev-trailing-stop">
                    <div class="ev-trailing-bar-bg">
                        <div class="ev-trailing-bar-fill" style="width:${barPct}%;background:${pnl >= 0 ? 'var(--profit)' : 'var(--loss)'}"></div>
                        <div class="ev-trailing-stop-marker" style="left:${stopPct}%"></div>
                    </div>
                    <span class="ev-trailing-label">Trailing Stop at +${stopLevel.toFixed(1)}%</span>
                </div>`;
        }

        // Entry signals
        let signalsHtml = "";
        if (p.entry_signals && p.entry_signals.length > 0) {
            const sigChips = p.entry_signals.map(s => {
                const src = typeof s === "string" ? s : (s.source || "");
                const str = typeof s === "object" && s.strength ? ` (${s.strength.toFixed(2)})` : "";
                return `<span class="ev-signal-chip">${escHtml(src)}${str}</span>`;
            }).join("");
            signalsHtml = `<div class="ev-signals-row"><span class="ev-detail-label">Entry Signals</span><div>${sigChips}</div></div>`;
        }

        // Composite scores
        let compositeHtml = "";
        if (p.entry_composite != null || p.last_composite != null) {
            const ec = p.entry_composite != null ? p.entry_composite.toFixed(2) : "--";
            const lc2 = p.last_composite != null ? p.last_composite.toFixed(2) : "--";
            compositeHtml = `<div class="ev-composite-row">Composite: ${lc2} <span style="color:var(--text-dim);">(was ${ec} at entry)</span></div>`;
        }

        // Exit proximity
        const es = exitStatuses[p.id] || {};
        let exitProxHtml = "";
        if (es.zone) {
            const zoneColors = { tp_zone: "var(--profit)", monitoring: "#f59e0b", sl_zone: "var(--loss)" };
            exitProxHtml = `<span class="ev-exit-prox" style="color:${zoneColors[es.zone] || 'var(--text-muted)'}">${es.zone_icon || ""} ${es.zone_label || ""}</span>`;
        }

        // Card profit/loss border class
        const cardBorder = pnl > 0 ? "ev-card-profit" : (pnl < 0 ? "ev-card-loss" : "");

        html += `
        <div class="position-card ev-position-card ${cardBorder}">
            <div class="ev-card-top">
                <div class="ev-card-title-row">
                    <span class="ev-side-badge ${sideClass}">${side}</span>
                    <span class="ev-card-question">${escHtml((p.market_question || "").slice(0, 65))}</span>
                </div>
                <div class="ev-card-badges">
                    ${stageBadge}
                    <span class="badge badge-purple">${escHtml(cat)}</span>
                </div>
            </div>

            <div class="ev-card-price-row">
                <span class="ev-detail-label">Direction</span>
                <span class="ev-price-flow">
                    ${side} at ${fmt.price(entryPrice)}
                    <span class="${priceClass}" style="font-weight:600;"> ${priceArrow} </span>
                    Now: ${currentPrice != null ? fmt.price(currentPrice) : "--"}
                </span>
                <span class="ev-pnl-pct ${pnlCls}" style="font-weight:700;">${pnlSign}${(pnlPct || 0).toFixed(1)}%</span>
            </div>

            <div class="ev-card-pnl-row">
                <div class="ev-pnl-main">
                    <span class="ev-detail-label">Unrealized</span>
                    <span class="ev-pnl-big ${pnlCls}">${pnlSign}${fmt.usd(pnl || 0)}</span>
                </div>
                <div class="ev-pnl-peak">
                    <span class="ev-detail-label">Peak</span>
                    <span>${peakPnlPct != null ? `+${peakPnlPct.toFixed(1)}%` : "--"}</span>
                </div>
                <div class="ev-pnl-invested">
                    <span class="ev-detail-label">Invested</span>
                    <span>${fmt.usd(cost)}</span>
                </div>
                <div class="ev-pnl-value">
                    <span class="ev-detail-label">Current Value</span>
                    <span>${currentValue != null ? fmt.usd(currentValue) : "--"}</span>
                </div>
            </div>

            ${trailingStopHtml}

            <div class="ev-card-meta-row">
                <div class="ev-meta-item">
                    ${stageBadge ? `<span>${daysLeft}</span>` : ""}
                    ${regimeChip}
                </div>
                <div class="ev-meta-item">
                    <span class="ev-detail-label">Hold</span>
                    <span>${holdStr}</span>
                </div>
                <div class="ev-meta-item">
                    ${exitProxHtml}
                </div>
            </div>

            ${signalsHtml}
            ${compositeHtml}

            <div class="ev-card-actions">
                <button class="ev-btn ev-btn-profit" onclick="closePosition('${p.id}', 'take_profit')">Take Profit</button>
                <button class="ev-btn ev-btn-close" onclick="closePosition('${p.id}', 'manual')">Close Position</button>
            </div>
        </div>`;
    }
    grid.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Close Position
// ---------------------------------------------------------------------------
async function closePosition(positionId, reason) {
    if (!confirm(`Close position ${positionId}? This will queue an exit order.`)) return;
    try {
        const headers = { "Content-Type": "application/json" };
        if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
        const res = await fetch(`${API}/api/events/close/${positionId}`, {
            method: "POST",
            headers,
        });
        const data = await res.json();
        if (data.ok) {
            alert(`Close request queued for position.`);
            refresh();
        } else {
            alert(`Error: ${data.detail || "Unknown error"}`);
        }
    } catch (err) {
        alert(`Failed to close position: ${err.message}`);
    }
}

// ---------------------------------------------------------------------------
// Stats + Overview Row
// ---------------------------------------------------------------------------
function renderStats(data, portfolioData) {
    // Win rate
    setText("ovWinRate", fmt.pct(data.win_rate || 0));

    // Avg hold time
    const holdEl = document.getElementById("ovAvgHold");
    if (holdEl) {
        const hours = data.avg_hold_hours;
        if (hours != null && hours > 0) {
            holdEl.textContent = hours > 24 ? `${(hours / 24).toFixed(1)}d` : `${hours.toFixed(0)}h`;
        } else {
            holdEl.textContent = "--";
        }
    }

    // Profit factor from closed trades
    const wins = data.wins || 0;
    const losses = data.losses || 0;
    const pnl = data.total_pnl || 0;

    // Best/worst trade (from stats — realized)
    // These come from the stats endpoint computed over closed positions
    const closedTrades = [];
    // We show realized pnl in overview for best/worst
    setText("ovBestTrade", pnl > 0 ? `+${fmt.usd(pnl)}` : fmt.usd(pnl));

    // Exit analysis chart
    if (data.exit_reasons) {
        renderExitChart(data.exit_reasons);
    }

    // Category chart (fallback if enhanced categories endpoint not available)
    if (data.category_breakdown && !categoryChart) {
        renderCategoryChart(data.category_breakdown);
    }
}

// ---------------------------------------------------------------------------
// Category Chart (Enhanced — $ amount + count)
// ---------------------------------------------------------------------------
function renderCategoryChartEnhanced(catData) {
    const ctx = document.getElementById("categoryChart");
    if (!ctx) return;

    const categories = catData.categories || {};
    const labels = Object.keys(categories);
    const amounts = labels.map(l => categories[l].amount || 0);
    const counts = labels.map(l => categories[l].count || 0);

    if (labels.length === 0) return;

    const colors = {
        politics: "#8b5cf6", geopolitics: "#ef4444", economics: "#f59e0b",
        crypto: "#00f0ff", culture: "#ec4899", science: "#22c55e",
        entertainment: "#f97316", technology: "#3b82f6",
        commodities: "#d97706", macro_economics: "#10b981",
        forex: "#6366f1", climate: "#14b8a6", tech_industry: "#f43f5e",
        futures: "#a855f7", other: "#71717a",
    };

    const bgColors = labels.map(l => colors[l.toLowerCase()] || "#71717a");

    if (categoryChart) categoryChart.destroy();
    categoryChart = new Chart(ctx, {
        type: "doughnut",
        data: {
            labels: labels.map((l, i) => `${l.charAt(0).toUpperCase() + l.slice(1)} ($${amounts[i].toFixed(0)}, ${counts[i]})`),
            datasets: [{
                data: amounts,
                backgroundColor: bgColors,
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "bottom", labels: { boxWidth: 12, padding: 8, font: { size: 11 } } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const idx = ctx.dataIndex;
                            return ` $${amounts[idx].toFixed(2)} | ${counts[idx]} positions`;
                        }
                    }
                }
            },
        },
    });
}

function renderCategoryChart(catData) {
    const ctx = document.getElementById("categoryChart");
    if (!ctx) return;
    const labels = Object.keys(catData);
    const values = Object.values(catData);
    const colors = {
        politics: "#8b5cf6", geopolitics: "#ef4444", economics: "#f59e0b",
        crypto: "#00f0ff", culture: "#ec4899", science: "#22c55e",
        entertainment: "#f97316", technology: "#3b82f6", other: "#71717a",
    };
    const bgColors = labels.map(l => colors[l] || "#71717a");
    if (categoryChart) categoryChart.destroy();
    categoryChart = new Chart(ctx, {
        type: "doughnut",
        data: {
            labels: labels.map(l => l.charAt(0).toUpperCase() + l.slice(1)),
            datasets: [{ data: values, backgroundColor: bgColors, borderWidth: 0 }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 8, font: { size: 11 } } } },
        },
    });
}

function renderExitChart(exitData) {
    const ctx = document.getElementById("exitChart");
    if (!ctx) return;
    const labels = Object.keys(exitData);
    const values = Object.values(exitData);
    const colors = {
        "Take profit": "#22c55e", "Stop loss": "#ef4444",
        "Market resolved: WIN": "#00f0ff", "Market resolved: LOSS": "#f59e0b",
        "Low liquidity exit": "#71717a", "Edge reversal": "#f97316",
        "Trailing stop": "#a855f7", "Smart TP": "#10b981", "Time exit": "#6366f1",
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
            datasets: [{ data: values, backgroundColor: bgColors, borderWidth: 0, borderRadius: 4 }],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, ticks: { stepSize: 1 } },
                x: { ticks: { font: { size: 10 } } },
            },
        },
    });
}

// ---------------------------------------------------------------------------
// Trades (Enhanced)
// ---------------------------------------------------------------------------
function renderTrades(data) {
    const closedPositions = data.closed_positions || [];
    const tbody = document.getElementById("tradeTableBody");
    const badge = document.getElementById("tradeCountBadge");

    if (badge) badge.textContent = `${closedPositions.length} closed`;

    // Overview row — best/worst/profit factor from closed
    if (closedPositions.length > 0) {
        const pnls = closedPositions.map(p => p.pnl || 0);
        const best = Math.max(...pnls);
        const worst = Math.min(...pnls);
        const grossProfit = pnls.filter(p => p > 0).reduce((a, b) => a + b, 0);
        const grossLoss = Math.abs(pnls.filter(p => p < 0).reduce((a, b) => a + b, 0));
        const pf = grossLoss > 0 ? (grossProfit / grossLoss).toFixed(2) : "--";

        setText("ovBestTrade", `+${fmt.usd(best)}`);
        setText("ovWorstTrade", fmt.usd(worst));
        setText("ovProfitFactor", pf);
    }

    if (closedPositions.length === 0) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="empty-table-cell">No closed events positions yet</td></tr>';
        return;
    }

    // Sort
    let sorted = [...closedPositions];
    if (tradeSortKey === "date") {
        sorted.sort((a, b) => {
            const cmp = (b.exit_time || "").localeCompare(a.exit_time || "");
            return tradeSortDir === "desc" ? cmp : -cmp;
        });
    } else if (tradeSortKey === "pnl") {
        sorted.sort((a, b) => {
            const cmp = (b.pnl || 0) - (a.pnl || 0);
            return tradeSortDir === "desc" ? cmp : -cmp;
        });
    } else if (tradeSortKey === "hold") {
        sorted.sort((a, b) => {
            const aHold = holdHours(a);
            const bHold = holdHours(b);
            const cmp = bHold - aHold;
            return tradeSortDir === "desc" ? cmp : -cmp;
        });
    }

    let tableHtml = "";
    for (const p of sorted) {
        const pnl = p.pnl || 0;
        const pnlCls = pnlClass(pnl);
        const sign = pnl >= 0 ? "+" : "";

        // Hold duration
        const hh = holdHours(p);
        let holdStr = "--";
        if (hh > 0) {
            holdStr = hh >= 24 ? `${(hh / 24).toFixed(1)}d` : `${hh.toFixed(0)}h`;
        }

        // Entry signals
        const signals = p.entry_signals || [];
        let sigHtml = "";
        if (signals.length > 0) {
            sigHtml = signals.map(s => {
                const src = typeof s === "string" ? s : (s.source || "");
                return `<span class="ev-signal-chip-sm">${escHtml(src)}</span>`;
            }).join("");
        } else {
            sigHtml = '<span style="color:var(--text-dim);">--</span>';
        }

        // Signal accuracy
        const correct = pnl > 0;
        const accBadge = correct
            ? '<span class="ev-acc-badge ev-acc-correct" title="Signals were correct">&#x2713;</span>'
            : '<span class="ev-acc-badge ev-acc-wrong" title="Signals were wrong">&#x2717;</span>';

        // Exit reason
        const exitReason = p.exit_reason || "";

        tableHtml += `<tr>
            <td>${fmt.datetime(p.exit_time)}</td>
            <td class="trade-market">${escHtml((p.market_question || "").slice(0, 50))}</td>
            <td><span class="badge badge-purple">${escHtml(p.category || "other")}</span></td>
            <td>${sigHtml}</td>
            <td>${fmt.price(p.entry_price)}</td>
            <td>${fmt.price(p.exit_price)}</td>
            <td class="${pnlCls}">${sign}${fmt.usd(pnl)}</td>
            <td>${holdStr}</td>
            <td class="trade-exit-reason">${escHtml(exitReason)}</td>
            <td>${accBadge}</td>
        </tr>`;
    }
    if (tbody) tbody.innerHTML = tableHtml;
}

function holdHours(p) {
    const entry = p.entry_time ? new Date(p.entry_time).getTime() : 0;
    const exit = p.exit_time ? new Date(p.exit_time).getTime() : 0;
    if (entry && exit) return (exit - entry) / 3600000;
    return 0;
}

// ---------------------------------------------------------------------------
// Trade Table Sorting
// ---------------------------------------------------------------------------
function initTradeSort() {
    document.querySelectorAll(".trade-table th.sortable").forEach(th => {
        th.addEventListener("click", () => {
            const key = th.dataset.sort;
            if (tradeSortKey === key) {
                tradeSortDir = tradeSortDir === "desc" ? "asc" : "desc";
            } else {
                tradeSortKey = key;
                tradeSortDir = "desc";
            }
            // Update UI
            document.querySelectorAll(".trade-table th.sortable").forEach(h => {
                h.classList.remove("sort-active");
                h.querySelector(".sort-arrow").textContent = "\u25B2";
            });
            th.classList.add("sort-active");
            th.querySelector(".sort-arrow").textContent = tradeSortDir === "desc" ? "\u25BC" : "\u25B2";
            // Re-render
            refresh();
        });
    });
}

// ---------------------------------------------------------------------------
// Live Price Ticker
// ---------------------------------------------------------------------------
function renderTicker(data) {
    const track = document.getElementById("tickerTrack");
    if (!track) return;

    const ticker = data.ticker || [];
    if (ticker.length === 0) {
        track.innerHTML = '<span class="ev-ticker-empty">No active positions to track</span>';
        return;
    }

    let html = "";
    for (const item of ticker) {
        const price = item.price != null ? (item.price * 100).toFixed(1) : "--";
        const changePct = item.change_pct || 0;
        const sign = changePct >= 0 ? "+" : "";
        const cls = changePct > 0 ? "ev-ticker-up" : (changePct < 0 ? "ev-ticker-down" : "ev-ticker-flat");
        const highlight = item.highlight ? "ev-ticker-highlight" : "";

        // Check if price changed since last tick
        const prevPrice = lastTickerPrices[item.id];
        const flash = (prevPrice != null && item.price != null && Math.abs(item.price - prevPrice) > 0.001) ? "ev-ticker-flash" : "";
        if (item.price != null) lastTickerPrices[item.id] = item.price;

        html += `<span class="ev-ticker-item ${cls} ${highlight} ${flash}">
            <span class="ev-ticker-name">${escHtml(item.question)}</span>
            <span class="ev-ticker-price">${price}&#162;</span>
            <span class="ev-ticker-change">${item.direction}${sign}${changePct.toFixed(1)}%</span>
        </span>`;

        // Separator
        html += '<span class="ev-ticker-sep">|</span>';
    }

    track.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}
