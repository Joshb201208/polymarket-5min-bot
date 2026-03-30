/* ===================================================================
   Stocks Dashboard — stocks-app.js
   Stock trading agent dashboard logic.
   Depends on common.js (fetchJSON, fmt, pnlClass, authToken, etc.)
   =================================================================== */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let equityChart = null;
let positionsData = [];
let posSortCol = "pnl_pct";
let posSortAsc = false;
let refreshTimer = null;

// ---------------------------------------------------------------------------
// Boot — check auth, then load data
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
    if (authToken) {
        showDashboard();
        refresh();
    } else {
        showLogin();
    }

    // Auto-refresh every 30s
    refreshTimer = setInterval(() => {
        if (authToken) refresh();
    }, REFRESH_INTERVAL);

    // Wire up positions table sort headers
    document.querySelectorAll(".stocks-positions-table th[data-col]").forEach(th => {
        th.addEventListener("click", () => {
            const col = th.dataset.col;
            if (posSortCol === col) {
                posSortAsc = !posSortAsc;
            } else {
                posSortCol = col;
                posSortAsc = col === "symbol" || col === "name"; // strings ascending
            }
            // Update header classes
            document.querySelectorAll(".stocks-positions-table th").forEach(h => {
                h.classList.remove("sort-active");
                const arrow = h.querySelector(".sort-arrow");
                if (arrow) arrow.textContent = "\u2195";
            });
            th.classList.add("sort-active");
            const thisArrow = th.querySelector(".sort-arrow");
            if (thisArrow) thisArrow.textContent = posSortAsc ? "\u2191" : "\u2193";
            renderPositionsTable();
        });
    });
});

// ---------------------------------------------------------------------------
// Main refresh
// ---------------------------------------------------------------------------
async function refresh() {
    setConnected(true);
    updateLastUpdate();

    const [summary, positions, trades, signals, equity, theses] = await Promise.all([
        fetchJSON("/api/stocks/summary"),
        fetchJSON("/api/stocks/positions"),
        fetchJSON("/api/stocks/trades"),
        fetchJSON("/api/stocks/signals"),
        fetchJSON("/api/stocks/equity-curve"),
        fetchJSON("/api/stocks/theses"),
    ]);

    if (!summary && !positions && !trades && !signals && !equity && !theses) {
        setConnected(false);
    }

    if (summary) renderSummary(summary);
    if (positions) {
        positionsData = Array.isArray(positions) ? positions : (positions.positions ?? positions.data ?? []);
        renderPositionsTable();
        document.getElementById("positionsCount").textContent = `${positionsData.length} positions`;
    }
    if (trades) renderTrades(trades);
    if (signals) renderSignals(signals);
    if (equity) renderEquityCurve(equity);
    if (theses) renderTheses(theses);

    updateLastUpdate();
}

// ---------------------------------------------------------------------------
// Update last-update timestamp
// ---------------------------------------------------------------------------
function updateLastUpdate() {
    const el = document.getElementById("lastUpdate");
    if (el) el.textContent = fmt.time(Date.now());
}

// ---------------------------------------------------------------------------
// Render hero KPIs from summary data
// ---------------------------------------------------------------------------
function renderSummary(data) {
    // Portfolio value
    const portfolioValue = data.portfolio_value ?? data.total_value ?? 0;
    const cash = data.cash ?? data.cash_balance ?? 0;
    const invested = data.invested ?? data.positions_value ?? 0;
    setKpi("kpiPortfolioValue", fmt.usd(portfolioValue), "");
    document.getElementById("kpiPortfolioSub").textContent = `Cash: ${fmt.usd(cash)} \u00b7 Invested: ${fmt.usd(invested)}`;

    // Total P&L
    const totalPnl = data.total_pnl ?? data.realized_pnl ?? 0;
    const roi = data.roi ?? 0;
    setKpi("kpiTotalPnl", fmt.usd(totalPnl), pnlClass(totalPnl));
    document.getElementById("kpiTotalPnlSub").textContent = `ROI: ${fmt.pct(roi)}`;

    // Win rate
    const wins = data.wins ?? 0;
    const losses = data.losses ?? 0;
    const totalTrades = data.total_trades ?? (wins + losses);
    const winRate = data.win_rate ?? (totalTrades > 0 ? (wins / totalTrades * 100) : 0);
    setKpi("kpiWinRate", fmt.pct(winRate), "");
    document.getElementById("kpiWins").textContent = wins;
    document.getElementById("kpiLosses").textContent = losses;
    document.getElementById("kpiTotalTrades").textContent = totalTrades;

    // Open positions
    const openPos = data.open_positions ?? data.open_count ?? 0;
    const exposurePct = data.exposure_pct ?? 0;
    setKpi("kpiOpenPositions", openPos, "");
    document.getElementById("kpiExposureSub").textContent = `${fmt.pct(exposurePct)} exposure`;

    // Today's P&L
    const todayPnl = data.today_pnl ?? data.daily_pnl ?? 0;
    const todayTrades = data.today_trades ?? 0;
    setKpi("kpiTodayPnl", fmt.usd(todayPnl), pnlClass(todayPnl));
    document.getElementById("kpiTodaySub").textContent = `${todayTrades} trades today`;

    // Mode badge
    const mode = data.mode ?? "PAPER";
    renderModeBadge(mode);

    // Equity badges
    if (data.max_drawdown != null) {
        document.getElementById("drawdownBadge").textContent = `Max DD: ${fmt.usd(data.max_drawdown)}`;
    }
    if (data.sharpe_ratio != null) {
        document.getElementById("sharpeBadge").textContent = `Sharpe: ${(+data.sharpe_ratio).toFixed(2)}`;
    }
}

function setKpi(id, value, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.className = "kpi-value";
    if (id === "kpiPortfolioValue") el.classList.add("kpi-stock-accent");
    if (cls === "pnl-positive") el.classList.add("pnl-positive");
    else if (cls === "pnl-negative") el.classList.add("pnl-negative");
}

function renderModeBadge(mode) {
    const el = document.getElementById("modeBadge");
    if (!el) return;
    const isLive = (mode || "").toUpperCase() === "LIVE";
    el.className = `mode-badge ${isLive ? "mode-badge-live" : "mode-badge-paper"}`;
    const modeText = el.querySelector(".mode-text");
    const modeDot = el.querySelector(".mode-dot");
    if (modeText) modeText.textContent = mode.toUpperCase();
    if (!isLive && modeDot) {
        modeDot.style.background = "#eab308";
        modeDot.style.boxShadow = "0 0 6px #eab308";
    } else if (modeDot) {
        modeDot.style.background = "";
        modeDot.style.boxShadow = "";
    }
}

// ---------------------------------------------------------------------------
// Equity Curve
// ---------------------------------------------------------------------------
function renderEquityCurve(data) {
    const canvas = document.getElementById("equityChart");
    if (!canvas) return;

    const labels = data.labels ?? data.dates ?? [];
    const portfolioSeries = normalizeSeries(data.portfolio ?? data.equity ?? data.values ?? []);
    const benchmarkSeries = normalizeSeries(data.benchmark ?? []);

    const finalLabels = labels.length > 0 ? labels :
        portfolioSeries.map((_, i) => {
            const d = new Date();
            d.setDate(d.getDate() - (portfolioSeries.length - 1 - i));
            return fmt.date(d);
        });

    if (equityChart) equityChart.destroy();

    const ctx = canvas.getContext("2d");

    const gradPortfolio = ctx.createLinearGradient(0, 0, 0, 280);
    gradPortfolio.addColorStop(0, "rgba(16,185,129,0.18)");
    gradPortfolio.addColorStop(1, "rgba(16,185,129,0)");

    const gradBenchmark = ctx.createLinearGradient(0, 0, 0, 280);
    gradBenchmark.addColorStop(0, "rgba(255,255,255,0.05)");
    gradBenchmark.addColorStop(1, "rgba(255,255,255,0)");

    const datasets = [
        {
            label: "Portfolio",
            data: portfolioSeries.length > 0 ? portfolioSeries : null,
            borderColor: "#10b981",
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.4,
            fill: true,
            backgroundColor: gradPortfolio,
            order: 1,
        },
    ];

    if (benchmarkSeries.length > 0) {
        datasets.push({
            label: "Benchmark",
            data: benchmarkSeries,
            borderColor: "rgba(255,255,255,0.3)",
            borderWidth: 1.5,
            borderDash: [5, 3],
            pointRadius: 0,
            pointHoverRadius: 3,
            tension: 0.4,
            fill: true,
            backgroundColor: gradBenchmark,
            order: 2,
        });
    }

    equityChart = new Chart(canvas, {
        type: "line",
        data: {
            labels: finalLabels,
            datasets: datasets.filter(d => d.data !== null),
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 600 },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(17,24,39,0.95)",
                    borderColor: "rgba(255,255,255,0.08)",
                    borderWidth: 1,
                    titleColor: "#a1a1aa",
                    bodyColor: "#e4e4e7",
                    padding: 12,
                    callbacks: {
                        label: (ctx) => ` ${ctx.dataset.label}: ${fmt.usd(ctx.parsed.y)}`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { color: "rgba(255,255,255,0.04)" },
                    ticks: {
                        color: "#52525b",
                        font: { size: 11 },
                        maxTicksLimit: 10,
                        maxRotation: 0,
                    },
                },
                y: {
                    grid: { color: "rgba(255,255,255,0.04)" },
                    ticks: {
                        color: "#52525b",
                        font: { size: 11 },
                        callback: (v) => fmt.usd(v),
                    },
                },
            },
        },
    });
}

function normalizeSeries(data) {
    if (!Array.isArray(data) || data.length === 0) return [];
    if (typeof data[0] === "number") return data;
    return data.map(d => d.value ?? d.cumulative_pnl ?? d.y ?? d.pnl ?? d.portfolio_value ?? 0);
}

// ---------------------------------------------------------------------------
// Active Positions Table
// ---------------------------------------------------------------------------
function renderPositionsTable() {
    const tbody = document.getElementById("positionsTableBody");
    if (!tbody || !positionsData.length) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="10" class="empty-table-cell">No active positions</td></tr>`;
        return;
    }

    const sorted = [...positionsData].sort((a, b) => {
        const av = getPosCellValue(a, posSortCol);
        const bv = getPosCellValue(b, posSortCol);
        if (typeof av === "string") return posSortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
        return posSortAsc ? av - bv : bv - av;
    });

    tbody.innerHTML = sorted.map(pos => {
        const symbol = escHtml(pos.symbol ?? pos.ticker ?? "???");
        const name = escHtml(pos.name ?? pos.company ?? "");
        const entryPrice = pos.entry_price ?? pos.avg_cost ?? null;
        const currentPrice = pos.current_price ?? pos.last_price ?? null;
        const pnl = pos.pnl ?? pos.unrealized_pnl ?? 0;
        const pnlPct = pos.pnl_pct ?? pos.return_pct ?? (entryPrice ? ((currentPrice - entryPrice) / entryPrice * 100) : 0);
        const conviction = pos.conviction ?? pos.confidence ?? null;
        const sector = escHtml(pos.sector ?? pos.industry ?? "");
        const thesis = escHtml(pos.thesis ?? pos.rationale ?? "");
        const daysHeld = pos.days_held ?? 0;

        const convBadge = conviction != null ? getConvictionBadge(conviction) : "--";
        const truncThesis = thesis.length > 60 ? thesis.substring(0, 57) + "..." : thesis;

        return `
            <tr onclick="openPositionModal(${escHtml(JSON.stringify(JSON.stringify(pos)))})" style="cursor:pointer;">
                <td class="mono" style="font-weight:600;color:#10b981;">${symbol}</td>
                <td style="max-width:140px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${name}">${name}</td>
                <td class="mono">${entryPrice != null ? "$" + (+entryPrice).toFixed(2) : "--"}</td>
                <td class="mono">${currentPrice != null ? "$" + (+currentPrice).toFixed(2) : "--"}</td>
                <td class="mono ${pnlClass(pnlPct)}" style="font-weight:600;">${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%</td>
                <td class="mono ${pnlClass(pnl)}">${fmt.usd(pnl)}</td>
                <td>${convBadge}</td>
                <td><span class="sector-badge">${sector || "--"}</span></td>
                <td class="thesis-truncated" title="${thesis}">${truncThesis || "--"}</td>
                <td class="mono">${daysHeld}d</td>
            </tr>
        `;
    }).join("");
}

function getPosCellValue(pos, col) {
    switch (col) {
        case "symbol": return (pos.symbol ?? pos.ticker ?? "").toLowerCase();
        case "name": return (pos.name ?? pos.company ?? "").toLowerCase();
        case "entry_price": return pos.entry_price ?? pos.avg_cost ?? -Infinity;
        case "current_price": return pos.current_price ?? pos.last_price ?? -Infinity;
        case "pnl": return pos.pnl ?? pos.unrealized_pnl ?? 0;
        case "pnl_pct": return pos.pnl_pct ?? pos.return_pct ?? 0;
        case "conviction": return pos.conviction ?? pos.confidence ?? 0;
        case "days_held": return pos.days_held ?? 0;
        default: return 0;
    }
}

function getConvictionBadge(conviction) {
    const c = Math.round(+conviction);
    let cls = "conviction-7";
    if (c >= 10) cls = "conviction-10";
    else if (c >= 9) cls = "conviction-9";
    else if (c >= 8) cls = "conviction-8";
    return `<span class="conviction-badge ${cls}">${c}/10</span>`;
}

// ---------------------------------------------------------------------------
// Position Detail Modal
// ---------------------------------------------------------------------------
function openPositionModal(posJson) {
    const pos = JSON.parse(posJson);
    const content = document.getElementById("posModalContent");
    if (!content) return;

    const symbol = escHtml(pos.symbol ?? pos.ticker ?? "???");
    const name = escHtml(pos.name ?? pos.company ?? "");

    content.innerHTML = `
        <div style="margin-bottom:16px;">
            <span style="font-family:var(--mono);font-size:20px;font-weight:700;color:#10b981;">${symbol}</span>
            <span style="font-size:14px;color:var(--text-muted);margin-left:8px;">${name}</span>
        </div>
        <div class="pos-modal-grid">
            <div class="pos-modal-item">
                <span class="pos-modal-label">Entry Price</span>
                <span class="pos-modal-value">$${(+(pos.entry_price ?? pos.avg_cost ?? 0)).toFixed(2)}</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">Current Price</span>
                <span class="pos-modal-value">$${(+(pos.current_price ?? pos.last_price ?? 0)).toFixed(2)}</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">P&L ($)</span>
                <span class="pos-modal-value ${pnlClass(pos.pnl ?? 0)}">${fmt.usd(pos.pnl ?? 0)}</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">P&L (%)</span>
                <span class="pos-modal-value ${pnlClass(pos.pnl_pct ?? 0)}">${(pos.pnl_pct ?? 0).toFixed(2)}%</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">Conviction</span>
                <span class="pos-modal-value">${pos.conviction ?? pos.confidence ?? "--"}/10</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">Sector</span>
                <span class="pos-modal-value">${escHtml(pos.sector ?? pos.industry ?? "--")}</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">Shares</span>
                <span class="pos-modal-value">${pos.shares ?? pos.quantity ?? "--"}</span>
            </div>
            <div class="pos-modal-item">
                <span class="pos-modal-label">Days Held</span>
                <span class="pos-modal-value">${pos.days_held ?? "--"}</span>
            </div>
        </div>
        ${pos.thesis || pos.rationale ? `
            <div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border);">
                <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px;">THESIS</div>
                <div style="font-size:13px;line-height:1.6;color:var(--text);">${escHtml(pos.thesis ?? pos.rationale ?? "")}</div>
            </div>
        ` : ""}
        ${pos.bull_case ? `
            <div style="margin-top:12px;">
                <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--profit);margin-bottom:4px;">BULL CASE</div>
                <div style="font-size:12px;color:var(--text);line-height:1.5;">${escHtml(pos.bull_case)}</div>
            </div>
        ` : ""}
        ${pos.bear_case ? `
            <div style="margin-top:12px;">
                <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--loss);margin-bottom:4px;">BEAR CASE</div>
                <div style="font-size:12px;color:var(--text);line-height:1.5;">${escHtml(pos.bear_case)}</div>
            </div>
        ` : ""}
    `;

    document.getElementById("positionModal").classList.add("open");
}

function closePositionModal(event) {
    if (event && event.target && event.target.id !== "positionModal") return;
    document.getElementById("positionModal").classList.remove("open");
}

// ---------------------------------------------------------------------------
// Recent Signals
// ---------------------------------------------------------------------------
function renderSignals(data) {
    const feed = document.getElementById("signalsFeed");
    const countEl = document.getElementById("signalsCount");
    if (!feed) return;

    const items = Array.isArray(data) ? data : (data.signals ?? data.items ?? []);

    if (!items || items.length === 0) {
        feed.innerHTML = `<div class="empty-state">No recent signals</div>`;
        if (countEl) countEl.textContent = "0 signals";
        return;
    }

    const sorted = [...items].sort((a, b) => {
        const ta = new Date(a.timestamp ?? a.created_at ?? a.time ?? 0).getTime();
        const tb = new Date(b.timestamp ?? b.created_at ?? b.time ?? 0).getTime();
        return tb - ta;
    }).slice(0, 20);

    if (countEl) countEl.textContent = `${sorted.length} signals`;

    feed.innerHTML = sorted.map(item => {
        const action = (item.action ?? item.signal ?? item.type ?? "hold").toLowerCase();
        const symbol = escHtml(item.symbol ?? item.ticker ?? "");
        const conviction = item.conviction ?? item.confidence ?? null;
        const ts = item.timestamp ?? item.created_at ?? item.time ?? null;
        const reason = escHtml(item.reason ?? item.rationale ?? item.description ?? "");

        const iconClass = action.includes("buy") ? "signal-icon-buy" :
                          action.includes("sell") ? "signal-icon-sell" : "signal-icon-hold";
        const iconChar = action.includes("buy") ? "\u2191" :
                         action.includes("sell") ? "\u2193" : "\u2022";

        return `
            <div class="signal-item">
                <div class="signal-icon ${iconClass}">${iconChar}</div>
                <div class="signal-desc">
                    <strong style="color:#10b981;">${symbol}</strong>
                    <span style="text-transform:uppercase;font-size:10px;font-weight:700;letter-spacing:0.06em;margin-left:6px;color:${action.includes("buy") ? "var(--profit)" : action.includes("sell") ? "var(--loss)" : "#eab308"};">${escHtml(action.toUpperCase())}</span>
                    ${reason ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px;">${reason}</div>` : ""}
                </div>
                ${conviction != null ? `<div class="signal-conviction" style="color:#10b981;">${conviction}/10</div>` : ""}
                <div class="signal-time">${ts ? fmt.relative(ts) : "--"}</div>
            </div>
        `;
    }).join("");
}

// ---------------------------------------------------------------------------
// Recent Trades
// ---------------------------------------------------------------------------
function renderTrades(data) {
    const feed = document.getElementById("tradesFeed");
    const countEl = document.getElementById("tradesCount");
    if (!feed) return;

    const items = Array.isArray(data) ? data : (data.trades ?? data.items ?? []);

    if (!items || items.length === 0) {
        feed.innerHTML = `<div class="empty-state">No recent trades</div>`;
        if (countEl) countEl.textContent = "0 trades";
        return;
    }

    const sorted = [...items].sort((a, b) => {
        const ta = new Date(a.timestamp ?? a.exit_time ?? a.closed_at ?? a.time ?? 0).getTime();
        const tb = new Date(b.timestamp ?? b.exit_time ?? b.closed_at ?? b.time ?? 0).getTime();
        return tb - ta;
    }).slice(0, 20);

    if (countEl) countEl.textContent = `${sorted.length} trades`;

    feed.innerHTML = sorted.map(item => {
        const action = (item.action ?? item.side ?? item.type ?? "trade").toLowerCase();
        const symbol = escHtml(item.symbol ?? item.ticker ?? "");
        const pnl = item.pnl ?? item.realized_pnl ?? null;
        const ts = item.timestamp ?? item.exit_time ?? item.closed_at ?? item.time ?? null;
        const reason = escHtml(item.exit_reason ?? item.reason ?? item.description ?? "");

        const isWin = pnl != null && pnl > 0;
        const isLoss = pnl != null && pnl < 0;
        const iconClass = isWin ? "signal-icon-buy" : isLoss ? "signal-icon-sell" : "signal-icon-hold";
        const iconChar = isWin ? "\u2713" : isLoss ? "\u2717" : "\u2022";

        return `
            <div class="signal-item">
                <div class="signal-icon ${iconClass}">${iconChar}</div>
                <div class="signal-desc">
                    <strong style="color:#10b981;">${symbol}</strong>
                    <span style="text-transform:uppercase;font-size:10px;font-weight:700;letter-spacing:0.06em;margin-left:6px;">${escHtml(action.toUpperCase())}</span>
                    ${reason ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px;">${reason}</div>` : ""}
                </div>
                ${pnl != null ? `<div class="signal-conviction ${pnlClass(pnl)}" style="font-weight:600;">${fmt.usd(pnl)}</div>` : ""}
                <div class="signal-time">${ts ? fmt.relative(ts) : "--"}</div>
            </div>
        `;
    }).join("");
}

// ---------------------------------------------------------------------------
// Thesis Board
// ---------------------------------------------------------------------------
function renderTheses(data) {
    const grid = document.getElementById("thesisGrid");
    const countEl = document.getElementById("thesesCount");
    if (!grid) return;

    const items = Array.isArray(data) ? data : (data.theses ?? data.items ?? []);

    if (!items || items.length === 0) {
        grid.innerHTML = `<div class="empty-state">No active theses</div>`;
        if (countEl) countEl.textContent = "0 theses";
        return;
    }

    if (countEl) countEl.textContent = `${items.length} theses`;

    grid.innerHTML = items.map(thesis => {
        const symbol = escHtml(thesis.symbol ?? thesis.ticker ?? "???");
        const name = escHtml(thesis.name ?? thesis.company ?? "");
        const conviction = thesis.conviction ?? thesis.confidence ?? null;
        const text = escHtml(thesis.thesis ?? thesis.summary ?? thesis.rationale ?? "");
        const bullCase = escHtml(thesis.bull_case ?? "");
        const bearCase = escHtml(thesis.bear_case ?? "");
        const catalysts = escHtml(thesis.catalysts ?? "");
        const risks = escHtml(thesis.risks ?? "");
        const citations = escHtml(thesis.citations ?? thesis.sources ?? "");

        const convBadge = conviction != null ? getConvictionBadge(conviction) : "";

        return `
            <div class="thesis-card">
                <div class="thesis-header">
                    <div>
                        <div class="thesis-symbol">${symbol}</div>
                        <div class="thesis-name">${name}</div>
                    </div>
                    ${convBadge}
                </div>
                ${text ? `<div class="thesis-text">${text}</div>` : ""}
                ${bullCase ? `
                    <div class="thesis-section">
                        <div class="thesis-section-title" style="color:var(--profit);">Bull Case</div>
                        <div class="thesis-section-body">${bullCase}</div>
                    </div>
                ` : ""}
                ${bearCase ? `
                    <div class="thesis-section">
                        <div class="thesis-section-title" style="color:var(--loss);">Bear Case</div>
                        <div class="thesis-section-body">${bearCase}</div>
                    </div>
                ` : ""}
                ${catalysts ? `
                    <div class="thesis-section">
                        <div class="thesis-section-title" style="color:#10b981;">Catalysts</div>
                        <div class="thesis-section-body">${catalysts}</div>
                    </div>
                ` : ""}
                ${risks ? `
                    <div class="thesis-section">
                        <div class="thesis-section-title" style="color:#eab308;">Risks</div>
                        <div class="thesis-section-body">${risks}</div>
                    </div>
                ` : ""}
                ${citations ? `<div class="thesis-citations">${citations}</div>` : ""}
            </div>
        `;
    }).join("");
}
