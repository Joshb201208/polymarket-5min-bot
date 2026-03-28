/* ===================================================================
   Overview Page — overview-app.js
   Command Center: KPIs, equity curve, sport cards, heatmap,
   activity feed, odds table, system health.
   =================================================================== */

// Chart instances
let equityCurveChart = null;
let nbaSparklineChart = null;
let nhlSparklineChart = null;

// -----------------------------------------------------------------------
// Equity Curve (3 lines: NBA, NHL, Combined)
// -----------------------------------------------------------------------
function drawEquityCurve(data) {
    if (!data) return;
    const ctx = document.getElementById("equityCurveChart").getContext("2d");
    const labels = (data.dates || []).map(d => {
        const dt = new Date(d + "T12:00:00Z");
        return dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    });

    const datasets = [
        {
            label: "Combined", data: data.combined || [],
            borderColor: "#ffffff", borderWidth: 2.5,
            backgroundColor: "rgba(255,255,255,0.03)", fill: false,
            tension: 0.4, pointRadius: 0, pointHoverRadius: 5,
            pointHoverBackgroundColor: "#fff",
        },
        {
            label: "NBA", data: data.nba || [],
            borderColor: "#00f0ff", borderWidth: 2,
            backgroundColor: "transparent", fill: false,
            tension: 0.4, pointRadius: 0, pointHoverRadius: 4,
            pointHoverBackgroundColor: "#00f0ff",
        },
        {
            label: "NHL", data: data.nhl || [],
            borderColor: "#f59e0b", borderWidth: 2,
            backgroundColor: "transparent", fill: false,
            tension: 0.4, pointRadius: 0, pointHoverRadius: 4,
            pointHoverBackgroundColor: "#f59e0b",
        },
    ];

    if (equityCurveChart) {
        equityCurveChart.data.labels = labels;
        equityCurveChart.data.datasets.forEach((ds, i) => { ds.data = datasets[i].data; });
        equityCurveChart.update("none");
        return;
    }

    equityCurveChart = new Chart(ctx, {
        type: "line",
        data: { labels, datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            animation: { duration: 1000, easing: "easeOutQuart" },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    display: true, position: "top",
                    labels: { usePointStyle: true, pointStyle: "line", padding: 16, font: { size: 11 } },
                },
                tooltip: {
                    backgroundColor: "rgba(18,19,26,0.95)", borderColor: "rgba(255,255,255,0.1)",
                    borderWidth: 1, cornerRadius: 8, padding: 12,
                    titleFont: { family: "'Inter'", size: 12, weight: "600" },
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    titleColor: "#a1a1aa", bodyColor: "#e4e4e7",
                    callbacks: { label: (c) => `${c.dataset.label}: $${c.parsed.y.toFixed(2)}` },
                },
                annotation: data.starting_bankroll ? {
                    annotations: {
                        startLine: {
                            type: "line", yMin: data.starting_bankroll, yMax: data.starting_bankroll,
                            borderColor: "rgba(255,255,255,0.2)", borderWidth: 1, borderDash: [6, 4],
                            label: { display: true, content: `Start: $${data.starting_bankroll}`, position: "end",
                                     font: { size: 10 }, color: "#71717a", backgroundColor: "transparent" },
                        },
                    },
                } : undefined,
            },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0, maxTicksLimit: 12 } },
                y: { grid: { color: "rgba(255,255,255,0.06)" }, ticks: { font: { family: "'JetBrains Mono'", size: 11 }, callback: (v) => `$${v}` } },
            },
        },
    });
}

// -----------------------------------------------------------------------
// Sparkline helper (mini chart for sport cards)
// -----------------------------------------------------------------------
function drawSparkline(canvasId, dataPoints, color, existingChart) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return existingChart;
    if (!dataPoints || dataPoints.length === 0) return existingChart;

    const values = dataPoints.map(d => d.pnl || d);
    const labels = dataPoints.map((_, i) => i);

    if (existingChart) {
        existingChart.data.labels = labels;
        existingChart.data.datasets[0].data = values;
        existingChart.update("none");
        return existingChart;
    }

    return new Chart(ctx.getContext("2d"), {
        type: "line",
        data: {
            labels,
            datasets: [{
                data: values, borderColor: color, borderWidth: 2,
                backgroundColor: color.replace(")", ",0.1)").replace("rgb", "rgba"),
                fill: true, tension: 0.4, pointRadius: 0,
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

// -----------------------------------------------------------------------
// Calendar Heatmap (SVG)
// -----------------------------------------------------------------------
function renderHeatmap(data) {
    const container = document.getElementById("heatmapSvg");
    const monthsEl = document.getElementById("heatmapMonths");
    if (!container || !data) return;

    const days = data.days || [];
    if (days.length === 0) {
        container.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#71717a" font-size="12">No trade data yet</text>';
        return;
    }

    // Build a map of date -> {pnl, trades}
    const dayMap = {};
    let minPnl = 0, maxPnl = 0;
    for (const d of days) {
        dayMap[d.date] = d;
        if (d.pnl < minPnl) minPnl = d.pnl;
        if (d.pnl > maxPnl) maxPnl = d.pnl;
    }

    // Generate last 90 days
    const now = new Date();
    const allDays = [];
    for (let i = 89; i >= 0; i--) {
        const dt = new Date(now);
        dt.setDate(dt.getDate() - i);
        allDays.push(dt.toISOString().split("T")[0]);
    }

    // Grid: columns = weeks, rows = day of week (0=Sun..6=Sat)
    const firstDate = new Date(allDays[0] + "T12:00:00Z");
    const firstDow = firstDate.getDay();

    const cellSize = 14;
    const cellGap = 3;
    const step = cellSize + cellGap;

    let cells = "";
    let currentMonth = -1;
    let monthLabels = "";

    for (let i = 0; i < allDays.length; i++) {
        const dateStr = allDays[i];
        const dt = new Date(dateStr + "T12:00:00Z");
        const dow = dt.getDay();
        const dayOffset = i + firstDow;
        const col = Math.floor(dayOffset / 7);
        const row = dow;

        // Month label
        if (dt.getMonth() !== currentMonth) {
            currentMonth = dt.getMonth();
            const monthName = dt.toLocaleDateString("en-US", { month: "short" });
            monthLabels += `<span style="left:${col * step}px">${monthName}</span>`;
        }

        const entry = dayMap[dateStr];
        let color = "#1f1f23"; // no trades
        if (entry && entry.trades > 0) {
            if (entry.pnl > 0) {
                const intensity = maxPnl > 0 ? Math.min(entry.pnl / maxPnl, 1) : 0;
                color = intensity > 0.5 ? "#15803d" : "#22c55e";
            } else if (entry.pnl < 0) {
                const intensity = minPnl < 0 ? Math.min(Math.abs(entry.pnl) / Math.abs(minPnl), 1) : 0;
                color = intensity > 0.5 ? "#991b1b" : "#dc2626";
            } else {
                color = "#3f3f46";
            }
        }

        const x = col * step;
        const y = row * step;
        const tooltip = entry && entry.trades > 0
            ? `${dateStr}: ${entry.pnl >= 0 ? "+" : ""}$${entry.pnl.toFixed(2)} (${entry.trades} trade${entry.trades !== 1 ? "s" : ""})`
            : `${dateStr}: No trades`;

        cells += `<rect x="${x}" y="${y}" width="${cellSize}" height="${cellSize}" rx="3" fill="${color}" data-tooltip="${escHtml(tooltip)}" class="heatmap-cell"/>`;
    }

    const totalCols = Math.ceil((allDays.length + firstDow) / 7);
    const svgWidth = totalCols * step;
    const svgHeight = 7 * step;
    container.setAttribute("width", svgWidth);
    container.setAttribute("height", svgHeight);
    container.setAttribute("viewBox", `0 0 ${svgWidth} ${svgHeight}`);
    container.innerHTML = cells;
    monthsEl.innerHTML = monthLabels;

    // Tooltip handlers
    const tooltipEl = document.getElementById("heatmapTooltip");
    container.querySelectorAll(".heatmap-cell").forEach(cell => {
        cell.addEventListener("mouseenter", (e) => {
            tooltipEl.textContent = cell.getAttribute("data-tooltip");
            tooltipEl.style.display = "block";
            const rect = cell.getBoundingClientRect();
            tooltipEl.style.left = rect.left + "px";
            tooltipEl.style.top = (rect.top - 36) + "px";
        });
        cell.addEventListener("mouseleave", () => {
            tooltipEl.style.display = "none";
        });
    });
}

// -----------------------------------------------------------------------
// Activity Feed
// -----------------------------------------------------------------------
function renderActivityFeed(data) {
    const container = document.getElementById("activityFeed");
    const items = (data && data.items) || [];
    document.getElementById("activityCount").textContent = `${items.length} events`;

    if (items.length === 0) {
        container.innerHTML = `<div class="empty-state"><div class="empty-text">No recent activity</div><div class="empty-sub">Activity will appear as bets are placed and resolved</div></div>`;
        return;
    }

    container.innerHTML = items.slice(0, 20).map(item => {
        const sportClass = (item.sport || "").toLowerCase() === "nhl" ? "badge-nhl" : "badge-nba";
        const sportLabel = (item.sport || "NBA").toUpperCase();
        const time = fmt.datetime(item.timestamp);
        const icons = {
            bet_placed: "&#9654;", market_resolved: "&#9632;", auto_hedge: "&#9670;",
            whale_alert: "&#9888;", line_movement: "&#8644;",
        };
        const icon = icons[item.type] || "&#8226;";
        const typeLabels = {
            bet_placed: "BET", market_resolved: "RESOLVED", auto_hedge: "HEDGE",
            whale_alert: "WHALE", line_movement: "LINE",
        };
        const typeLabel = typeLabels[item.type] || item.type;

        return `<div class="activity-item">
            <span class="activity-icon">${icon}</span>
            <span class="sport-badge ${sportClass}" style="font-size:9px;padding:2px 6px">${sportLabel}</span>
            <span class="activity-type-badge activity-type-${item.type || 'default'}">${typeLabel}</span>
            <span class="activity-desc">${escHtml(item.description || '')}</span>
            <span class="activity-time">${time}</span>
        </div>`;
    }).join("");
}

// -----------------------------------------------------------------------
// Odds Comparison Table
// -----------------------------------------------------------------------
function renderOddsTable(data) {
    const tbody = document.getElementById("oddsTableBody");
    const snapshots = (data && data.snapshots) || [];
    document.getElementById("oddsCount").textContent = `${snapshots.length} games`;

    if (snapshots.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-table-cell">No games currently being evaluated</td></tr>';
        return;
    }

    tbody.innerHTML = snapshots.map(s => {
        const edge = s.edge || 0;
        const edgePct = (edge * 100).toFixed(1);
        const rowClass = edge > 0.03 ? "odds-row-edge" : "";
        const sportClass = (s.sport || "").toLowerCase() === "nhl" ? "badge-nhl" : "badge-nba";

        let statusBadge = '<span class="odds-status odds-status-noedge">NO EDGE</span>';
        if (s.status === "bet_placed" || s.bet_placed) {
            statusBadge = '<span class="odds-status odds-status-bet">BET PLACED</span>';
        } else if (edge > 0.03 || s.status === "watching") {
            statusBadge = '<span class="odds-status odds-status-watching">WATCHING</span>';
        }

        return `<tr class="${rowClass}">
            <td class="td-market">${escHtml(s.game || s.matchup || '')}</td>
            <td><span class="sport-badge ${sportClass}" style="font-size:9px;padding:2px 6px">${(s.sport || "NBA").toUpperCase()}</span></td>
            <td class="mono">${s.polymarket_price != null ? (s.polymarket_price * 100).toFixed(1) + '¢' : '--'}</td>
            <td class="mono">${s.vegas_price != null ? (s.vegas_price * 100).toFixed(1) + '¢' : '--'}</td>
            <td class="mono">${s.fair_value != null ? (s.fair_value * 100).toFixed(1) + '¢' : '--'}</td>
            <td class="mono ${edge > 0.03 ? 'pnl-positive' : edge < -0.03 ? 'pnl-negative' : ''}">${edgePct}%</td>
            <td>${statusBadge}</td>
        </tr>`;
    }).join("");
}

// -----------------------------------------------------------------------
// System Health
// -----------------------------------------------------------------------
function updateSystemHealth(data) {
    if (!data) return;

    function setHealthDot(dotId, ok) {
        const el = document.getElementById(dotId);
        if (el) el.className = `health-dot ${ok ? "health-ok" : "health-error"}`;
    }

    if (data.nba_last_scan) {
        document.getElementById("healthNbaScan").textContent = fmt.relative(data.nba_last_scan);
        setHealthDot("healthDotNba", true);
    }
    if (data.nhl_last_scan) {
        document.getElementById("healthNhlScan").textContent = fmt.relative(data.nhl_last_scan);
        setHealthDot("healthDotNhl", true);
    }
    if (data.odds_api_credits != null) {
        document.getElementById("healthOddsCredits").textContent = data.odds_api_credits;
        setHealthDot("healthDotOdds", data.odds_api_credits > 10);
    }

    const apis = data.apis || {};
    if (apis.espn !== undefined) {
        document.getElementById("healthEspnStatus").textContent = apis.espn ? "OK" : "Down";
        setHealthDot("healthDotEspn", apis.espn);
    }
    if (apis.polymarket !== undefined) {
        document.getElementById("healthPolyStatus").textContent = apis.polymarket ? "OK" : "Down";
        setHealthDot("healthDotPoly", apis.polymarket);
    }
}

// -----------------------------------------------------------------------
// Main refresh
// -----------------------------------------------------------------------
async function refresh() {
    const [status, combinedStatus, stats, equityData, heatmapData, activityData, oddsData, healthData] = await Promise.all([
        fetchJSON("/api/status"),
        fetchJSON("/api/combined/status"),
        fetchJSON("/api/stats"),
        fetchJSON("/api/combined/equity-curve"),
        fetchJSON("/api/combined/calendar-heatmap"),
        fetchJSON("/api/combined/activity-feed"),
        fetchJSON("/api/odds-snapshots"),
        fetchJSON("/api/system-health"),
    ]);

    const anySuccess = status || combinedStatus || stats;
    setConnected(anySuccess);

    // Mode badge
    if (status) {
        const badge = document.getElementById("modeBadge");
        const modeText = badge.querySelector(".mode-text");
        if (status.mode === "live") {
            badge.classList.add("live");
            modeText.textContent = "LIVE";
        } else {
            badge.classList.remove("live");
            modeText.textContent = "PAPER";
        }
        document.getElementById("lastUpdate").textContent = status.last_scan
            ? fmt.relative(status.last_scan) : "--";
    }

    // Combined status — KPIs
    if (combinedStatus) {
        const cash = combinedStatus.bankroll || 0;
        const combined = combinedStatus.combined || {};
        const totalExposure = combinedStatus.total_exposure || 0;
        const portfolioValue = cash + totalExposure;

        document.getElementById("kpiPortfolio").textContent = fmt.usd(portfolioValue);
        document.getElementById("kpiPortfolioSub").textContent =
            `Cash: ${fmt.usd(cash)} \u00b7 Bets: ${fmt.usd(totalExposure)}`;

        const totalPnl = combined.total_pnl || 0;
        const pnlEl = document.getElementById("kpiTotalPnl");
        pnlEl.textContent = (totalPnl >= 0 ? "+" : "") + fmt.usd(totalPnl);
        pnlEl.className = `kpi-value ${pnlClass(totalPnl)}`;

        // Today's P&L
        const todayPnl = combined.today_pnl || 0;
        const todayPnlEl = document.getElementById("kpiTodayPnl");
        todayPnlEl.textContent = (todayPnl >= 0 ? "+" : "") + fmt.usd(todayPnl);
        todayPnlEl.className = `kpi-value ${pnlClass(todayPnl)}`;
        const todayTrades = combined.today_trades || 0;
        document.getElementById("kpiTodayPnlSub").textContent =
            `${todayTrades} trade${todayTrades !== 1 ? "s" : ""} today`;

        // Sport cards
        const nba = combinedStatus.nba || {};
        const nhl = combinedStatus.nhl || {};

        const nbaPnlEl = document.getElementById("nbaPnl");
        const nbaPnlVal = nba.total_pnl || 0;
        nbaPnlEl.textContent = (nbaPnlVal >= 0 ? "+" : "") + fmt.usd(nbaPnlVal);
        nbaPnlEl.className = `sport-card-pnl-value ${pnlClass(nbaPnlVal)}`;
        document.getElementById("nbaOpen").textContent = nba.open_positions || 0;
        document.getElementById("nbaExposure").textContent = fmt.usd(nba.exposure || 0);
        document.getElementById("nbaWinRate").textContent = fmt.pct(nba.win_rate);

        const nbaModeBadge = document.getElementById("nbaModeBadge");
        if (nba.mode === "live") {
            nbaModeBadge.textContent = "LIVE";
            nbaModeBadge.className = "mode-badge-inline mode-live";
        } else {
            nbaModeBadge.textContent = "PAPER";
            nbaModeBadge.className = "mode-badge-inline mode-paper";
        }

        const nhlPnlEl = document.getElementById("nhlPnl");
        const nhlPnlVal = nhl.total_pnl || 0;
        nhlPnlEl.textContent = (nhlPnlVal >= 0 ? "+" : "") + fmt.usd(nhlPnlVal);
        nhlPnlEl.className = `sport-card-pnl-value ${pnlClass(nhlPnlVal)}`;
        document.getElementById("nhlOpen").textContent = nhl.open_positions || 0;
        document.getElementById("nhlExposure").textContent = fmt.usd(nhl.exposure || 0);
        document.getElementById("nhlWinRate").textContent = fmt.pct(nhl.win_rate);

        const nhlModeBadge = document.getElementById("nhlModeBadge");
        if (nhl.mode === "live") {
            nhlModeBadge.textContent = "LIVE";
            nhlModeBadge.className = "mode-badge-inline mode-live";
        } else {
            nhlModeBadge.textContent = "PAPER";
            nhlModeBadge.className = "mode-badge-inline mode-paper";
        }
    }

    // Stats — win rate, total trades
    if (stats) {
        document.getElementById("kpiWinRate").textContent = fmt.pct(stats.win_rate);
        document.getElementById("kpiWins").textContent = stats.wins || 0;
        document.getElementById("kpiLosses").textContent = stats.losses || 0;
        document.getElementById("kpiTotalTrades").textContent = stats.total_trades || 0;
    }

    // Equity curve
    if (equityData) drawEquityCurve(equityData);

    // Sparklines
    if (equityData && equityData.nba_daily) {
        nbaSparklineChart = drawSparkline("nbaSparkline", equityData.nba_daily.slice(-7), "#00f0ff", nbaSparklineChart);
    }
    if (equityData && equityData.nhl_daily) {
        nhlSparklineChart = drawSparkline("nhlSparkline", equityData.nhl_daily.slice(-7), "#f59e0b", nhlSparklineChart);
    }

    // Heatmap
    if (heatmapData) renderHeatmap(heatmapData);

    // Activity feed
    if (activityData) renderActivityFeed(activityData);

    // Odds table
    if (oddsData) renderOddsTable(oddsData);

    // System health
    if (healthData) updateSystemHealth(healthData);
}

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------
if (authToken) {
    fetchJSON("/api/status").then((data) => {
        if (data) { showDashboard(); refresh(); }
        else { showLogin(); }
    });
} else {
    showLogin();
}

setInterval(() => { if (authToken) refresh(); }, REFRESH_INTERVAL);
