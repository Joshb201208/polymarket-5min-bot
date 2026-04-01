/* ===================================================================
   Overview Command Center — overview-app.js
   Events agent dashboard logic.
   Depends on common.js (fetchJSON, fmt, pnlClass, authToken, etc.)
   =================================================================== */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let equityChart = null;
let eventsSparklineChart = null;
let oddsData = [];
let oddsSortCol = "edge";
let oddsSortAsc = false;
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

    // Wire up odds table sort headers
    document.querySelectorAll(".overview-odds-table th[data-col]").forEach(th => {
        th.addEventListener("click", () => {
            const col = th.dataset.col;
            if (oddsSortCol === col) {
                oddsSortAsc = !oddsSortAsc;
            } else {
                oddsSortCol = col;
                oddsSortAsc = col === "market"; // strings sort ascending by default
            }
            // Update header classes
            document.querySelectorAll(".overview-odds-table th").forEach(h => {
                h.classList.remove("sort-active");
                const arrow = h.querySelector(".sort-arrow");
                if (arrow) arrow.textContent = "↕";
            });
            th.classList.add("sort-active");
            const thisArrow = th.querySelector(".sort-arrow");
            if (thisArrow) thisArrow.textContent = oddsSortAsc ? "↑" : "↓";
            renderOddsTable();
        });
    });
});

// ---------------------------------------------------------------------------
// Main refresh
// ---------------------------------------------------------------------------
async function refresh() {
    setConnected(true);
    updateLastUpdate();

    // Fire all API calls in parallel
    const [overview, equity, heatmap, activity, odds] = await Promise.all([
        fetchJSON("/api/combined/overview"),
        fetchJSON("/api/combined/equity-curve"),
        fetchJSON("/api/combined/heatmap"),
        fetchJSON("/api/combined/activity-feed"),
        fetchJSON("/api/combined/odds-snapshot"),
    ]);

    if (!overview && !equity && !heatmap && !activity && !odds) {
        setConnected(false);
    }

    if (overview) renderOverview(overview);
    if (equity) renderEquityCurve(equity);
    if (heatmap) renderHeatmap(heatmap);
    if (activity) renderActivityFeed(activity);
    if (odds) {
        oddsData = Array.isArray(odds) ? odds : (odds.markets || odds.data || []);
        renderOddsTable();
        document.getElementById("oddsCount").textContent = `${oddsData.length} markets`;
    }

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
// Render hero KPIs + agent cards from overview data
// ---------------------------------------------------------------------------
function renderOverview(data) {
    // Extract top-level stats
    const combined = data.combined || data;
    const events = data.events || {};

    // ---- Hero KPIs ----
    // Total portfolio
    const totalPortfolio = combined.total_portfolio ?? combined.portfolio_value ?? 0;
    const cash = combined.cash ?? combined.total_cash ?? 0;
    const betsValue = combined.bets_value ?? combined.total_bets ?? 0;
    setKpi("kpiTotalPortfolio", fmt.usd(totalPortfolio), pnlClass(totalPortfolio - (combined.starting_bankroll ?? totalPortfolio)));
    document.getElementById("kpiPortfolioSub").textContent = `Cash: ${fmt.usd(cash)} · Bets: ${fmt.usd(betsValue)}`;

    // Combined P&L
    const combinedPnl = combined.realized_pnl ?? combined.total_pnl ?? combined.pnl ?? 0;
    const roi = combined.roi ?? (combined.starting_bankroll ? (combinedPnl / combined.starting_bankroll * 100) : 0);
    setKpi("kpiCombinedPnl", fmt.usd(combinedPnl), pnlClass(combinedPnl));
    document.getElementById("kpiCombinedPnlSub").textContent = `ROI: ${fmt.pct(roi)}`;

    // Win rate
    const evtWR = events.win_rate ?? 0;
    const evtTrades = events.total_trades ?? 0;
    const evtWins = Math.round(evtWR / 100 * evtTrades);
    const totalLosses = evtTrades - evtWins;
    setKpi("kpiWinRate", fmt.pct(evtWR), "");
    document.getElementById("kpiWins").textContent = evtWins;
    document.getElementById("kpiLosses").textContent = totalLosses;
    document.getElementById("kpiTrades").textContent = evtTrades;

    // Open positions
    const openPos = combined.total_open_positions ?? combined.open_positions ?? 0;
    const exposure = combined.total_exposure ?? combined.exposure ?? 0;
    setKpi("kpiOpenPositions", openPos, "");
    document.getElementById("kpiOpenSub").textContent = `${fmt.usd(exposure)} exposure`;

    // Today's P&L
    const todayPnl = combined.today_pnl ?? combined.daily_pnl ?? 0;
    const todayTrades = combined.today_trades ?? 0;
    setKpi("kpiTodayPnl", fmt.usd(todayPnl), pnlClass(todayPnl));
    document.getElementById("kpiTodaySub").textContent = `${todayTrades} trades today`;

    // ---- Mode badge ----
    renderModeBadge("modeBadgeEvents", events.mode ?? combined.events_mode ?? "PAPER");

    // ---- Events Agent Card ----
    renderAgentCard("events", events, combined);

    // ---- Drawdown / PF badges ----
    if (combined.max_drawdown != null) {
        document.getElementById("drawdownBadge").textContent = `Max DD: ${fmt.usd(combined.max_drawdown)}`;
    }
    if (combined.profit_factor != null) {
        document.getElementById("profitFactorBadge").textContent = `PF: ${(+combined.profit_factor).toFixed(2)}`;
    }
}

function setKpi(id, value, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.className = "kpi-value";
    if (cls === "pnl-positive") el.classList.add("pnl-positive");
    else if (cls === "pnl-negative") el.classList.add("pnl-negative");
}

function renderModeBadge(id, mode) {
    const el = document.getElementById(id);
    if (!el) return;
    const isLive = (mode || "").toUpperCase() === "LIVE";
    el.className = `mode-badge ${isLive ? "mode-badge-live" : "mode-badge-paper"}`;
    const modeDot = el.querySelector(".mode-dot");
    if (!isLive) {
        if (modeDot) { modeDot.style.background = "#eab308"; modeDot.style.boxShadow = "0 0 6px #eab308"; }
    } else {
        if (modeDot) { modeDot.style.background = ""; modeDot.style.boxShadow = ""; }
    }
}

function renderAgentCard(agent, data, combined) {
    const prefix = "evt";

    const todayPnl = data.today_pnl ?? data.daily_pnl ?? 0;
    const openPos = data.open_positions ?? data.open_count ?? 0;
    const wins = data.wins ?? 0;
    const losses = data.losses ?? 0;
    const trades = data.total_trades ?? (wins + losses);
    const wr = data.win_rate ?? (trades > 0 ? (wins / trades * 100) : 0);
    const totalPnl = data.realized_pnl ?? data.total_pnl ?? data.pnl ?? 0;
    const lastScan = data.last_scan ?? data.last_scan_at ?? data.last_updated ?? null;
    const mode = data.mode ?? "PAPER";

    // Update mode badge on card
    const cardModeBadge = document.getElementById("eventsModeCard");
    if (cardModeBadge) {
        const isLive = (mode || "").toUpperCase() === "LIVE";
        cardModeBadge.className = `mode-badge ${isLive ? "mode-badge-live" : "mode-badge-paper"}`;
        const dot = cardModeBadge.querySelector(".mode-dot");
        const txt = cardModeBadge.querySelector(".mode-text");
        if (txt) txt.textContent = mode.toUpperCase();
        if (dot && !isLive) { dot.style.background = "#eab308"; dot.style.boxShadow = "0 0 6px #eab308"; }
    }

    // Stats
    setStatValue(`${prefix}TodayPnl`, fmt.usd(todayPnl), pnlClass(todayPnl));
    setStatValue(`${prefix}OpenPos`, openPos, "");
    setStatValue(`${prefix}WinRate`, fmt.pct(wr), wr >= 55 ? "pnl-positive" : wr < 45 ? "pnl-negative" : "");
    setStatValue(`${prefix}Trades`, trades, "");
    setStatValue(`${prefix}TotalPnl`, fmt.usd(totalPnl), pnlClass(totalPnl));
    const scanEl = document.getElementById(`${prefix}LastScan`);
    if (scanEl) scanEl.textContent = lastScan ? fmt.relative(lastScan) : "--";

    // Sparkline
    const sparkData = data.equity_curve ?? data.sparkline ?? [];
    if (sparkData.length > 0) {
        renderSparkline("eventsSparkline", sparkData, "#8b5cf6");
    }
}

function setStatValue(id, value, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.className = "agent-stat-value";
    if (cls === "pnl-positive") el.classList.add("pnl-positive");
    else if (cls === "pnl-negative") el.classList.add("pnl-negative");
}

// ---------------------------------------------------------------------------
// Sparkline (mini line chart)
// ---------------------------------------------------------------------------
function renderSparkline(canvasId, data, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    // Extract values
    const values = data.map(d => (typeof d === "object" ? (d.cumulative_pnl ?? d.value ?? d.y ?? d) : d));

    if (eventsSparklineChart) eventsSparklineChart.destroy();

    const chart = new Chart(canvas, {
        type: "line",
        data: {
            labels: values.map((_, i) => i),
            datasets: [{
                data: values,
                borderColor: color,
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.4,
                fill: true,
                backgroundColor: (ctx) => {
                    const gradient = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height);
                    gradient.addColorStop(0, color.replace(")", ", 0.25)").replace("rgb", "rgba"));
                    gradient.addColorStop(1, "transparent");
                    return gradient;
                },
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 400 },
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: {
                x: { display: false },
                y: { display: false },
            },
        },
    });

    eventsSparklineChart = chart;
}

// ---------------------------------------------------------------------------
// Equity Curve
// ---------------------------------------------------------------------------
function renderEquityCurve(data) {
    const canvas = document.getElementById("equityChart");
    if (!canvas) return;

    const labels = data.labels ?? data.dates ?? [];
    const eventsSeries = normalizeEquitySeries(data.events_cumulative ?? data.events ?? data.events_series ?? []);

    const finalLabels = labels.length > 0 ? labels :
        eventsSeries.map((_, i) => {
            const d = new Date();
            d.setDate(d.getDate() - (eventsSeries.length - 1 - i));
            return fmt.date(d);
        });

    if (equityChart) equityChart.destroy();

    const ctx = canvas.getContext("2d");

    const gradEvents = ctx.createLinearGradient(0, 0, 0, 280);
    gradEvents.addColorStop(0, "rgba(139,92,246,0.18)");
    gradEvents.addColorStop(1, "rgba(139,92,246,0)");

    equityChart = new Chart(canvas, {
        type: "line",
        data: {
            labels: finalLabels,
            datasets: [
                {
                    label: "Events",
                    data: eventsSeries.length > 0 ? eventsSeries : null,
                    borderColor: "#8b5cf6",
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    tension: 0.4,
                    fill: true,
                    backgroundColor: gradEvents,
                },
            ].filter(d => d.data !== null),
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

function normalizeEquitySeries(data) {
    if (!Array.isArray(data) || data.length === 0) return [];
    if (typeof data[0] === "number") return data;
    return data.map(d => d.cumulative_pnl ?? d.value ?? d.y ?? d.pnl ?? 0);
}

// ---------------------------------------------------------------------------
// Calendar Heatmap — 90-day SVG
// ---------------------------------------------------------------------------
function renderHeatmap(data) {
    const svg = document.getElementById("heatmapSvg");
    if (!svg) return;

    // Normalize data — accept array of { date, pnl, trades } or object keyed by date
    let entries = {};
    if (Array.isArray(data)) {
        data.forEach(d => {
            const key = d.date ?? d.day;
            if (key) entries[key] = { pnl: d.pnl ?? d.value ?? 0, trades: d.trades ?? d.count ?? 0 };
        });
    } else if (data.days && Array.isArray(data.days)) {
        data.days.forEach(d => {
            const key = d.date ?? d.day;
            if (key) entries[key] = { pnl: d.pnl ?? 0, trades: d.trades ?? 0 };
        });
    } else if (typeof data === "object") {
        Object.entries(data).forEach(([k, v]) => {
            if (k === "days" || k === "summary") return;
            entries[k] = typeof v === "object" ? v : { pnl: v, trades: 0 };
        });
    }

    // Build last 90 days
    const days = [];
    for (let i = 89; i >= 0; i--) {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        d.setDate(d.getDate() - i);
        const key = d.toISOString().slice(0, 10);
        days.push({
            date: d,
            key,
            pnl: entries[key]?.pnl ?? null,
            trades: entries[key]?.trades ?? 0,
        });
    }

    // Layout constants
    const cellSize = 13;
    const cellGap = 3;
    const step = cellSize + cellGap;
    const dayLabels = ["", "Mon", "", "Wed", "", "Fri", ""];
    const leftPad = 32;
    const topPad = 20;

    const firstDay = days[0].date;
    const startDow = firstDay.getDay();

    const totalDaySlots = startDow + days.length;
    const numWeeks = Math.ceil(totalDaySlots / 7);

    const svgWidth = leftPad + numWeeks * step + 20;
    const svgHeight = topPad + 7 * step + 4;
    svg.setAttribute("width", svgWidth);
    svg.setAttribute("height", svgHeight);

    svg.innerHTML = "";

    // Day-of-week labels
    dayLabels.forEach((label, row) => {
        if (!label) return;
        const el = document.createElementNS("http://www.w3.org/2000/svg", "text");
        el.setAttribute("x", leftPad - 4);
        el.setAttribute("y", topPad + row * step + cellSize * 0.85);
        el.setAttribute("text-anchor", "end");
        el.setAttribute("font-size", "9");
        el.setAttribute("fill", "#52525b");
        el.setAttribute("font-family", "Inter, sans-serif");
        el.textContent = label;
        svg.appendChild(el);
    });

    // Month labels
    let lastMonth = -1;
    days.forEach((day, idx) => {
        const slot = startDow + idx;
        const week = Math.floor(slot / 7);
        const dow = slot % 7;
        if (dow === 0 || (idx === 0)) {
            const month = day.date.getMonth();
            if (month !== lastMonth) {
                lastMonth = month;
                const el = document.createElementNS("http://www.w3.org/2000/svg", "text");
                el.setAttribute("x", leftPad + week * step);
                el.setAttribute("y", topPad - 6);
                el.setAttribute("font-size", "9");
                el.setAttribute("fill", "#71717a");
                el.setAttribute("font-family", "Inter, sans-serif");
                el.textContent = day.date.toLocaleDateString("en-US", { month: "short" });
                svg.appendChild(el);
            }
        }
    });

    // Cells
    let profitDays = 0, lossDays = 0;

    days.forEach((day, idx) => {
        const slot = startDow + idx;
        const week = Math.floor(slot / 7);
        const dow = slot % 7;

        const x = leftPad + week * step;
        const y = topPad + dow * step;

        const fill = heatmapColor(day.pnl);

        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", x);
        rect.setAttribute("y", y);
        rect.setAttribute("width", cellSize);
        rect.setAttribute("height", cellSize);
        rect.setAttribute("rx", "3");
        rect.setAttribute("fill", fill);
        rect.setAttribute("data-date", day.key);
        rect.setAttribute("data-pnl", day.pnl ?? "null");
        rect.setAttribute("data-trades", day.trades);
        rect.style.cursor = "pointer";
        rect.style.transition = "opacity 0.15s";

        rect.addEventListener("mouseenter", (e) => showHeatmapTooltip(e, day));
        rect.addEventListener("mousemove", (e) => moveHeatmapTooltip(e));
        rect.addEventListener("mouseleave", hideHeatmapTooltip);

        svg.appendChild(rect);

        if (day.pnl !== null && day.pnl > 0) profitDays++;
        if (day.pnl !== null && day.pnl < 0) lossDays++;
    });

    const profEl = document.getElementById("heatmapProfitDays");
    const lossEl = document.getElementById("heatmapLossDays");
    if (profEl) profEl.textContent = `${profitDays} profit days`;
    if (lossEl) lossEl.textContent = `${lossDays} loss days`;
}

function heatmapColor(pnl) {
    if (pnl === null || pnl === undefined) return "#1a1b24";
    if (pnl === 0) return "#1f2937";
    if (pnl > 0) {
        if (pnl > 50) return "#22c55e";
        if (pnl > 20) return "#16a34a";
        if (pnl > 5)  return "#15803d";
        return "#14532d";
    } else {
        if (pnl < -50) return "#6b0000";
        if (pnl < -20) return "#991b1b";
        return "#7f1d1d";
    }
}

function showHeatmapTooltip(e, day) {
    const tip = document.getElementById("heatmapTooltip");
    if (!tip) return;
    document.getElementById("htDate").textContent = day.date.toLocaleDateString("en-US", {
        weekday: "short", month: "short", day: "numeric"
    });
    const pnlEl = document.getElementById("htPnl");
    if (day.pnl === null) {
        pnlEl.textContent = "No trades";
        pnlEl.className = "heatmap-tooltip-pnl";
    } else {
        pnlEl.textContent = fmt.usd(day.pnl);
        pnlEl.className = `heatmap-tooltip-pnl ${day.pnl >= 0 ? "pnl-positive" : "pnl-negative"}`;
    }
    document.getElementById("htTrades").textContent = `${day.trades} trades`;
    tip.classList.add("visible");
    moveHeatmapTooltip(e);
}
function moveHeatmapTooltip(e) {
    const tip = document.getElementById("heatmapTooltip");
    if (!tip) return;
    let x = e.clientX + 14;
    let y = e.clientY - 10;
    const rect = tip.getBoundingClientRect();
    if (x + rect.width + 10 > window.innerWidth) x = e.clientX - rect.width - 14;
    if (y + rect.height + 10 > window.innerHeight) y = e.clientY - rect.height - 14;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
}
function hideHeatmapTooltip() {
    const tip = document.getElementById("heatmapTooltip");
    if (tip) tip.classList.remove("visible");
}

// ---------------------------------------------------------------------------
// Live Activity Feed
// ---------------------------------------------------------------------------
function renderActivityFeed(data) {
    const feed = document.getElementById("activityFeed");
    const countEl = document.getElementById("activityCount");
    if (!feed) return;

    const items = Array.isArray(data) ? data : (data.activities ?? data.items ?? data.events ?? []);

    if (!items || items.length === 0) {
        feed.innerHTML = `<div class="activity-empty">No recent activity</div>`;
        if (countEl) countEl.textContent = "0 events";
        return;
    }

    const sorted = [...items].sort((a, b) => {
        const ta = new Date(a.timestamp ?? a.created_at ?? a.time ?? 0).getTime();
        const tb = new Date(b.timestamp ?? b.created_at ?? b.time ?? 0).getTime();
        return tb - ta;
    }).slice(0, 30);

    if (countEl) countEl.textContent = `${sorted.length} events`;

    feed.innerHTML = sorted.map(item => {
        const type = (item.type ?? item.action ?? "bet_placed").toLowerCase();
        const ts = item.timestamp ?? item.created_at ?? item.time ?? null;
        const amount = item.amount ?? item.pnl ?? item.stake ?? null;
        const desc = escHtml(item.description ?? item.market ?? item.detail ?? item.message ?? "Activity event");

        const { iconHtml, iconClass } = getActivityIcon(type);
        const agentBadge = `<span class="activity-badge activity-badge-events">Events</span>`;

        const amountHtml = amount != null
            ? `<div class="activity-amount ${pnlClass(amount)}">${fmt.usd(amount)}</div>`
            : "";

        return `
            <div class="activity-item">
                <div class="activity-icon ${iconClass}">${iconHtml}</div>
                ${agentBadge}
                <div class="activity-desc">${desc}</div>
                ${amountHtml}
                <div class="activity-time">${ts ? fmt.time(ts) : "--"}</div>
            </div>
        `;
    }).join("");
}

function getActivityIcon(type) {
    if (type.includes("win") || type.includes("profit")) {
        return { iconHtml: "✓", iconClass: "activity-icon-win" };
    }
    if (type.includes("loss") || type.includes("lose")) {
        return { iconHtml: "✗", iconClass: "activity-icon-loss" };
    }
    return { iconHtml: "→", iconClass: "activity-icon-bet" };
}

// ---------------------------------------------------------------------------
// Odds Comparison Table
// ---------------------------------------------------------------------------
function renderOddsTable() {
    const tbody = document.getElementById("oddsTableBody");
    if (!tbody || !oddsData.length) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="empty-table-cell">No markets available</td></tr>`;
        return;
    }

    const sorted = [...oddsData].sort((a, b) => {
        const av = getCellValue(a, oddsSortCol);
        const bv = getCellValue(b, oddsSortCol);
        if (typeof av === "string") return oddsSortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
        return oddsSortAsc ? av - bv : bv - av;
    });

    tbody.innerHTML = sorted.map(row => {
        const market = escHtml(row.market ?? row.title ?? row.name ?? "Unknown");
        const polyPrice = row.polymarket_price ?? row.poly_price ?? row.market_price ?? null;
        const fairValue = row.fair_value ?? row.model_price ?? row.theoretical ?? null;
        const edge = row.edge ?? row.edge_pct ?? null;
        const status = (row.status ?? row.signal ?? "NO_EDGE").toUpperCase().replace(/ /g, "_");

        const edgeVal = edge != null ? parseFloat(edge) : null;
        const edgePct = edgeVal != null ? edgeVal * 100 : null;
        const edgeClass = edgePct === null ? "" : edgePct > 4 ? "edge-strong" : edgePct < 0 ? "edge-negative" : "edge-positive";
        const edgeText = edgePct !== null ? `${edgePct.toFixed(1)}%` : "--";

        const { badgeClass, badgeText } = getStatusBadge(status, edgePct);

        return `
            <tr>
                <td style="max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${market}">${market}</td>
                <td class="mono">${polyPrice != null ? fmt.price(polyPrice) : "--"}</td>
                <td class="mono">${fairValue != null ? fmt.price(fairValue) : "--"}</td>
                <td class="mono ${edgeClass}">${edgeText}</td>
                <td><span class="status-badge ${badgeClass}">${badgeText}</span></td>
            </tr>
        `;
    }).join("");
}

function getCellValue(row, col) {
    switch (col) {
        case "market": return (row.market ?? row.title ?? row.name ?? "").toLowerCase();
        case "poly_price": return row.polymarket_price ?? row.poly_price ?? row.market_price ?? -Infinity;
        case "fair_value": return row.fair_value ?? row.model_price ?? -Infinity;
        case "edge": {
            const e = row.edge ?? row.edge_pct ?? null;
            return e != null ? parseFloat(e) : -Infinity;
        }
        default: return 0;
    }
}

function getStatusBadge(status, edgePct) {
    if (status === "BET_PLACED" || status === "BET") {
        return { badgeClass: "status-bet-placed", badgeText: "BET PLACED" };
    }
    if (status === "WATCHING" || status === "WATCH") {
        return { badgeClass: "status-watching", badgeText: "WATCHING" };
    }
    if (edgePct !== null && edgePct > 4) {
        return { badgeClass: "status-watching", badgeText: "WATCHING" };
    }
    return { badgeClass: "status-no-edge", badgeText: "NO EDGE" };
}

// ---------------------------------------------------------------------------
// Graceful degradation — show placeholders when APIs return null
// ---------------------------------------------------------------------------
function seedPlaceholders() {
    const placeholders = {
        kpiTotalPortfolio: "$--",
        kpiCombinedPnl: "$--",
        kpiWinRate: "--%",
        kpiOpenPositions: "--",
        kpiTodayPnl: "$--",
    };
    Object.entries(placeholders).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el && el.textContent === "$0.00" || el?.textContent === "0") el.textContent = val;
    });
}
