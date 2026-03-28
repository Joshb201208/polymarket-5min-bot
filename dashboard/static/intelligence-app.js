/* ===================================================================
   Intelligence Feed Dashboard — intelligence-app.js
   Fetches intelligence API endpoints every 15s, updates DOM, draws charts.
   =================================================================== */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let compositeDonutChart = null;
let priceChartInst = null;
let selectedMarketId = null;
let cachedSignals = [];
let cachedScores = {};

// ---------------------------------------------------------------------------
// Source Colors & Labels
// ---------------------------------------------------------------------------
const SOURCE_COLORS = {
    x_scanner: '#3B82F6',
    orderbook: '#F97316',
    metaculus: '#8B5CF6',
    google_trends: '#22C55E',
    congress: '#EF4444',
    whale_tracker: '#EAB308',
    cross_market: '#14B8A6',
};
const SOURCE_LABELS = {
    x_scanner: 'X Scanner',
    orderbook: 'Orderbook',
    metaculus: 'Metaculus',
    google_trends: 'Google Trends',
    congress: 'Congress',
    whale_tracker: 'Whale Tracker',
    cross_market: 'Cross-Market',
};

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
    setInterval(() => { if (authToken) refresh(); }, 15000);
})();

// ---------------------------------------------------------------------------
// Main Refresh
// ---------------------------------------------------------------------------
async function refresh() {
    const [signalsData, scoresData, healthData] = await Promise.all([
        fetchJSON("/api/intelligence/signals"),
        fetchJSON("/api/intelligence/scores"),
        fetchJSON("/api/intelligence/health"),
    ]);

    const ok = signalsData || scoresData || healthData;
    setConnected(!!ok);

    const signals = (signalsData && signalsData.signals) || [];
    const scores = (scoresData && scoresData.scores) || {};
    const health = (healthData && healthData.sources) || {};

    cachedSignals = signals;
    cachedScores = scores;

    updateKPIs(signals, scores, health);
    renderSignalFeed(signals);
    renderHealth(health);

    if (selectedMarketId) {
        loadSpotlight(selectedMarketId);
    }

    const el = document.getElementById("lastUpdate");
    if (el) el.textContent = new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------
function updateKPIs(signals, scores, health) {
    // Active Signals: count of non-expired signals
    const now = Date.now();
    const activeSignals = signals.filter(s => {
        if (s.expired) return false;
        if (s.expires_at && new Date(s.expires_at).getTime() < now) return false;
        return true;
    });
    const activeCountEl = document.getElementById("kpiActiveSignals");
    if (activeCountEl) activeCountEl.textContent = activeSignals.length;
    const activeSubEl = document.getElementById("kpiActiveSignalsSub");
    if (activeSubEl) activeSubEl.textContent = `${signals.length} total (24h)`;

    // Sources Online: count of sources with status "active" or "connected"
    const healthEntries = Object.values(health);
    const onlineCount = healthEntries.filter(s =>
        s && (s.status === "active" || s.status === "connected")
    ).length;
    const sourcesEl = document.getElementById("kpiSourcesOnline");
    if (sourcesEl) sourcesEl.textContent = `${onlineCount}/${healthEntries.length}`;
    const sourcesSubEl = document.getElementById("kpiSourcesOnlineSub");
    if (sourcesSubEl) sourcesSubEl.textContent = "sources reporting";

    // Avg Composite: average of all composite scores
    const scoreValues = Object.values(scores);
    const composites = scoreValues
        .map(s => s && s.composite != null ? s.composite : null)
        .filter(v => v != null);
    const avgComposite = composites.length > 0
        ? composites.reduce((sum, v) => sum + v, 0) / composites.length
        : 0;
    const avgEl = document.getElementById("kpiAvgComposite");
    if (avgEl) avgEl.textContent = avgComposite.toFixed(1);
    const avgSubEl = document.getElementById("kpiAvgCompositeSub");
    if (avgSubEl) avgSubEl.textContent = `${composites.length} markets scored`;

    // Top Edge: highest composite score
    const topEdge = composites.length > 0 ? Math.max(...composites) : 0;
    const topEl = document.getElementById("kpiTopEdge");
    if (topEl) topEl.textContent = topEdge.toFixed(1);
    const topSubEl = document.getElementById("kpiTopEdgeSub");
    if (topSubEl) topSubEl.textContent = "highest score";
}

// ---------------------------------------------------------------------------
// Signal Feed
// ---------------------------------------------------------------------------
function renderSignalFeed(signals) {
    const container = document.getElementById("signalFeed");
    if (!container) return;

    // Apply filters
    const filtered = applyFilters(signals);

    if (filtered.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">
                    <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
                        <circle cx="24" cy="24" r="20" stroke="#71717a" stroke-width="1.5" stroke-dasharray="4 4"/>
                        <path d="M18 24h12M24 18v12" stroke="#71717a" stroke-width="1.5" stroke-linecap="round"/>
                    </svg>
                </div>
                <div class="empty-text">Collecting signals...</div>
                <div class="empty-sub">Intelligence sources are being monitored</div>
            </div>`;
        return;
    }

    let html = "";
    for (const sig of filtered) {
        const source = sig.source || "unknown";
        const color = SOURCE_COLORS[source] || "#71717a";
        const label = SOURCE_LABELS[source] || source;
        const direction = (sig.direction || "").toUpperCase();
        const isYes = direction === "YES" || direction === "UP" || direction === "BUY";
        const arrowHtml = isYes
            ? '<span style="color:#22c55e;font-weight:700">&uarr;YES</span>'
            : '<span style="color:#ef4444;font-weight:700">&darr;NO</span>';
        const strength = sig.strength != null ? Math.min(Math.max(sig.strength, 0), 100) : 0;
        const question = sig.market_question || sig.question || "";
        const truncated = question.length > 80 ? question.slice(0, 77) + "..." : question;
        const marketId = sig.market_id || "";
        const ts = sig.timestamp || sig.created_at || "";

        html += `
        <div class="signal-card" style="border-left:3px solid ${color}" onclick="selectSignal('${escHtml(marketId)}')" data-market-id="${escHtml(marketId)}">
            <div class="signal-card-header">
                <span class="signal-source-label" style="color:${color}">${escHtml(label)}</span>
                <span class="signal-time">${fmt.relative(ts)}</span>
            </div>
            <div class="signal-card-body">
                <div class="signal-market">${escHtml(truncated)}</div>
                <div class="signal-direction">${arrowHtml}</div>
            </div>
            <div class="signal-strength-bar">
                <div class="signal-strength-fill" style="width:${strength}%;background:${color}"></div>
            </div>
            <div class="signal-strength-label">${strength.toFixed(0)}% strength</div>
        </div>`;
    }
    container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Filter Logic
// ---------------------------------------------------------------------------
function applyFilters(signals) {
    let filtered = signals.slice();

    // Source filter
    const sourceFilter = document.getElementById("filterSource");
    if (sourceFilter && sourceFilter.value && sourceFilter.value !== "all") {
        filtered = filtered.filter(s => s.source === sourceFilter.value);
    }

    // Direction filter
    const dirFilter = document.getElementById("filterDirection");
    if (dirFilter && dirFilter.value && dirFilter.value !== "all") {
        const val = dirFilter.value.toUpperCase();
        filtered = filtered.filter(s => {
            const d = (s.direction || "").toUpperCase();
            if (val === "YES") return d === "YES" || d === "UP" || d === "BUY";
            if (val === "NO") return d === "NO" || d === "DOWN" || d === "SELL";
            return true;
        });
    }

    // Sort
    const sortEl = document.getElementById("filterSort");
    const sortVal = sortEl ? sortEl.value : "time";
    if (sortVal === "strength") {
        filtered.sort((a, b) => (b.strength || 0) - (a.strength || 0));
    } else {
        // Default: sort by timestamp descending
        filtered.sort((a, b) => {
            const ta = new Date(b.timestamp || b.created_at || 0).getTime();
            const tb = new Date(a.timestamp || a.created_at || 0).getTime();
            return ta - tb;
        });
    }

    return filtered;
}

// Wire up filter change events
(function initFilters() {
    const ids = ["filterSource", "filterDirection", "filterSort"];
    for (const id of ids) {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener("change", () => {
                renderSignalFeed(cachedSignals);
            });
        }
    }
})();

// ---------------------------------------------------------------------------
// Signal Selection → Spotlight
// ---------------------------------------------------------------------------
window.selectSignal = function (marketId) {
    if (!marketId) return;
    selectedMarketId = marketId;

    // Highlight selected card
    document.querySelectorAll(".signal-card").forEach(card => {
        card.classList.toggle("signal-card-selected", card.dataset.marketId === marketId);
    });

    loadSpotlight(marketId);
};

// ---------------------------------------------------------------------------
// Spotlight Panel
// ---------------------------------------------------------------------------
async function loadSpotlight(marketId) {
    const panel = document.getElementById("spotlightPanel");
    if (!panel) return;

    const placeholder = panel.querySelector(".spotlight-placeholder");
    const content = document.getElementById("spotlightContent");
    if (!content) return;

    const data = await fetchJSON(`/api/intelligence/market/${marketId}`);
    if (!data) {
        // Show placeholder, hide content
        if (placeholder) placeholder.style.display = "";
        content.style.display = "none";
        return;
    }

    // Hide placeholder, show content
    if (placeholder) placeholder.style.display = "none";
    content.style.display = "";

    const title = data.market_question || data.market_id || marketId;
    const signals = data.signals || [];
    const composite = data.composite != null ? data.composite : null;
    const orderbookDepth = data.orderbook_depth || {};
    const priceHistory = data.price_history || [];

    // Build breakdown from signals for donut chart
    const breakdown = {};
    for (const sig of signals) {
        const src = sig.source || "unknown";
        breakdown[src] = (breakdown[src] || 0) + (sig.strength || 0);
    }

    // Market title
    const titleEl = document.getElementById("spotlightMarketTitle");
    if (titleEl) titleEl.textContent = title;

    // Build signal breakdown table
    const breakdownTable = document.getElementById("signalBreakdown");
    if (breakdownTable) {
        if (signals.length > 0) {
            let tbody = "<thead><tr><th>Source</th><th>Direction</th><th>Strength</th><th>Time</th></tr></thead><tbody>";
            for (const sig of signals) {
                const src = sig.source || "unknown";
                const label = SOURCE_LABELS[src] || src;
                const color = SOURCE_COLORS[src] || "#71717a";
                const dir = (sig.direction || "").toUpperCase();
                const isYes = dir === "YES" || dir === "UP" || dir === "BUY";
                const dirHtml = isYes
                    ? '<span style="color:#22c55e">&uarr;YES</span>'
                    : '<span style="color:#ef4444">&darr;NO</span>';
                tbody += `<tr>
                    <td><span style="color:${color}">${escHtml(label)}</span></td>
                    <td>${dirHtml}</td>
                    <td>${sig.strength != null ? sig.strength.toFixed(0) + '%' : '--'}</td>
                    <td>${fmt.relative(sig.timestamp || sig.created_at)}</td>
                </tr>`;
            }
            tbody += "</tbody>";
            breakdownTable.innerHTML = tbody;
            breakdownTable.style.display = "";
        } else {
            breakdownTable.style.display = "none";
        }
    }

    // Orderbook depth
    const orderbookEl = document.getElementById("orderbookDepth");
    if (orderbookEl) {
        const bidDepth = orderbookDepth.bid || orderbookDepth.bids || 0;
        const askDepth = orderbookDepth.ask || orderbookDepth.asks || 0;
        const maxDepth = Math.max(bidDepth, askDepth, 1);
        orderbookEl.innerHTML = `
            <div class="spotlight-section-title">Orderbook Depth</div>
            <div class="orderbook-bars">
                <div class="orderbook-bar-row">
                    <span class="orderbook-label">Bid</span>
                    <div class="orderbook-bar-track">
                        <div class="orderbook-bar-fill orderbook-bid" style="width:${(bidDepth / maxDepth * 100).toFixed(1)}%"></div>
                    </div>
                    <span class="orderbook-value">${typeof bidDepth === 'number' ? bidDepth.toLocaleString() : '--'}</span>
                </div>
                <div class="orderbook-bar-row">
                    <span class="orderbook-label">Ask</span>
                    <div class="orderbook-bar-track">
                        <div class="orderbook-bar-fill orderbook-ask" style="width:${(askDepth / maxDepth * 100).toFixed(1)}%"></div>
                    </div>
                    <span class="orderbook-value">${typeof askDepth === 'number' ? askDepth.toLocaleString() : '--'}</span>
                </div>
            </div>`;
    }

    // Render charts after DOM update
    if (Object.keys(breakdown).length > 0) {
        renderCompositeDonut(breakdown, composite);
    }
    if (priceHistory.length > 0) {
        renderPriceChart(priceHistory);
    }
}

// ---------------------------------------------------------------------------
// Composite Donut Chart
// ---------------------------------------------------------------------------
function renderCompositeDonut(breakdown, composite) {
    const ctx = document.getElementById("compositeDonut");
    if (!ctx) return;

    if (compositeDonutChart) {
        compositeDonutChart.destroy();
        compositeDonutChart = null;
    }

    const labels = Object.keys(breakdown).map(k => SOURCE_LABELS[k] || k);
    const values = Object.values(breakdown);
    const colors = Object.keys(breakdown).map(k => SOURCE_COLORS[k] || "#71717a");

    const centerText = composite != null ? composite.toFixed(1) : "--";

    compositeDonutChart = new Chart(ctx, {
        type: "doughnut",
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: colors,
                borderWidth: 0,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: "65%",
            plugins: {
                legend: {
                    position: "bottom",
                    labels: { boxWidth: 10, padding: 6, font: { size: 10 } },
                },
                tooltip: {
                    backgroundColor: "rgba(18,19,26,0.95)",
                    borderColor: "rgba(139,92,246,0.3)",
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 10,
                    bodyFont: { family: "'JetBrains Mono'", size: 12 },
                    callbacks: {
                        label: (c) => `${c.label}: ${c.parsed.toFixed(1)}`,
                    },
                },
            },
        },
        plugins: [{
            id: "centerText",
            afterDraw(chart) {
                const { width, height, ctx: c } = chart;
                c.save();
                c.font = "bold 22px 'JetBrains Mono', monospace";
                c.fillStyle = "#e4e4e7";
                c.textAlign = "center";
                c.textBaseline = "middle";
                const centerY = (chart.chartArea.top + chart.chartArea.bottom) / 2;
                c.fillText(centerText, width / 2, centerY);
                c.restore();
            },
        }],
    });
}

// ---------------------------------------------------------------------------
// Price Chart
// ---------------------------------------------------------------------------
function renderPriceChart(priceHistory) {
    const ctx = document.getElementById("priceChart");
    if (!ctx) return;

    if (priceChartInst) {
        priceChartInst.destroy();
        priceChartInst = null;
    }

    const labels = priceHistory.map(p => {
        if (p.timestamp || p.time || p.t) {
            return fmt.time(p.timestamp || p.time || p.t);
        }
        return "";
    });
    const values = priceHistory.map(p => p.price != null ? p.price : (p.value != null ? p.value : p.y));

    const context = ctx.getContext("2d");
    const gradient = context.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, "rgba(139, 92, 246, 0.25)");
    gradient.addColorStop(0.5, "rgba(139, 92, 246, 0.08)");
    gradient.addColorStop(1, "rgba(139, 92, 246, 0)");

    priceChartInst = new Chart(ctx, {
        type: "line",
        data: {
            labels,
            datasets: [{
                label: "Price",
                data: values,
                borderColor: "#8B5CF6",
                borderWidth: 2,
                backgroundColor: gradient,
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                pointHoverRadius: 4,
                pointHoverBackgroundColor: "#8B5CF6",
                pointHoverBorderColor: "#fff",
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 600, easing: "easeOutQuart" },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18,19,26,0.95)",
                    borderColor: "rgba(139,92,246,0.3)",
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 10,
                    bodyFont: { family: "'JetBrains Mono'", size: 12 },
                    callbacks: {
                        label: (c) => `${(c.parsed.y * 100).toFixed(1)}¢`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { font: { size: 9 }, maxRotation: 0, maxTicksLimit: 6 },
                },
                y: {
                    grid: { color: "rgba(255,255,255,0.04)" },
                    ticks: {
                        font: { family: "'JetBrains Mono'", size: 10 },
                        callback: (v) => `${(v * 100).toFixed(0)}¢`,
                    },
                },
            },
        },
    });
}

// ---------------------------------------------------------------------------
// Source Health Grid
// ---------------------------------------------------------------------------
function renderHealth(healthData) {
    const grid = document.getElementById("sourceHealthGrid");
    if (!grid) return;

    const entries = Object.entries(healthData);
    if (entries.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-text">Collecting data...</div>
                <div class="empty-sub">Source health will appear here</div>
            </div>`;
        return;
    }

    const now = Date.now();
    // Assume 2 cycles = 2 * 15s = 30s for staleness check
    const staleCutoff = 30000;

    let html = "";
    for (const [name, info] of entries) {
        const label = SOURCE_LABELS[name] || name;
        const color = SOURCE_COLORS[name] || "#71717a";
        const status = info ? (info.status || "") : "";
        const lastUpdate = info ? info.last_update : null;
        const error = info ? info.error : null;

        let dotClass = "health-dot-gray";
        if (!info) {
            dotClass = "health-dot-gray";
        } else if (status === "error" || status === "disabled") {
            dotClass = "health-dot-red";
        } else if (status === "active" || status === "connected") {
            if (lastUpdate) {
                const age = now - new Date(lastUpdate).getTime();
                dotClass = age > staleCutoff ? "health-dot-yellow" : "health-dot-green";
            } else {
                dotClass = "health-dot-green";
            }
        } else {
            dotClass = "health-dot-gray";
        }

        const lastTime = lastUpdate ? fmt.relative(lastUpdate) : "no data";

        html += `
        <div class="health-badge" style="border-color:${color}33">
            <div class="health-badge-header">
                <span class="health-dot ${dotClass}"></span>
                <span class="health-name" style="color:${color}">${escHtml(label)}</span>
            </div>
            <div class="health-last-update">${escHtml(lastTime)}</div>
            ${error ? `<div class="health-error">${escHtml(error)}</div>` : ''}
        </div>`;
    }
    grid.innerHTML = html;
}
