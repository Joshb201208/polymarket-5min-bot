/* ===================================================================
   NBA Agent Dashboard — app.js
   Fetches all API endpoints every 30s, updates DOM, draws charts.
   =================================================================== */

// ---------------------------------------------------------------------------
// API Base URL
// ---------------------------------------------------------------------------
const API = "";
const REFRESH_INTERVAL = 30_000; // 30 seconds

// ---------------------------------------------------------------------------
// Auth State
// ---------------------------------------------------------------------------
let authToken = sessionStorage.getItem("nba_agent_token") || null;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let equityChart = null;
let dailyPnlChart = null;
let winRateTypeChart = null;
let sortColumn = "date";
let sortDirection = "desc";
let expandedTradeId = null;
let cachedPositions = null;
let cachedStats = null;

// ---------------------------------------------------------------------------
// Chart.js Defaults
// ---------------------------------------------------------------------------
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.color = "#71717a";
Chart.defaults.borderColor = "rgba(255,255,255,0.05)";

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------
// All dates displayed in Singapore time
const TZ = "Asia/Singapore";

const fmt = {
    usd: (v) => {
        if (v == null) return "--";
        const sign = v >= 0 ? "" : "-";
        return `${sign}$${Math.abs(v).toFixed(2)}`;
    },
    pct: (v) => (v == null ? "--" : `${v.toFixed(1)}%`),
    price: (v) => (v == null ? "--" : `¢${(v * 100).toFixed(1)}`),
    edge: (v) => (v == null ? "--" : `${(v * 100).toFixed(1)}%`),
    date: (ts) => {
        if (!ts) return "--";
        const d = new Date(ts);
        return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: TZ });
    },
    time: (ts) => {
        if (!ts) return "--";
        const d = new Date(ts);
        return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
    },
    datetime: (ts) => {
        if (!ts) return "--";
        const d = new Date(ts);
        return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: TZ })} ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: TZ })}`;
    },
    relative: (ts) => {
        if (!ts) return "--";
        const diff = Date.now() - new Date(ts).getTime();
        const hours = Math.floor(diff / 3600000);
        const mins = Math.floor((diff % 3600000) / 60000);
        if (hours > 24) return `${Math.floor(hours / 24)}d ago`;
        if (hours > 0) return `${hours}h ${mins}m`;
        return `${mins}m ago`;
    },
};

// ---------------------------------------------------------------------------
// Login Particle Canvas — Interactive Star Field
// ---------------------------------------------------------------------------
(function initLoginParticles() {
    const canvas = document.getElementById('loginCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    let w, h, particles, mouse, animId;
    let burstQueue = [];  // {cx, cy, strength} — queued shockwaves
    mouse = { x: -1000, y: -1000 };

    function resize() {
        w = canvas.width = window.innerWidth;
        h = canvas.height = window.innerHeight;
    }

    function createParticles() {
        const count = Math.min(Math.floor((w * h) / 6000), 200);
        particles = [];
        for (let i = 0; i < count; i++) {
            particles.push({
                x: Math.random() * w,
                y: Math.random() * h,
                vx: (Math.random() - 0.5) * 0.3,
                vy: (Math.random() - 0.5) * 0.3,
                r: Math.random() * 1.5 + 0.5,
                // Color: mix of cyan, purple, green
                color: [
                    'rgba(0, 240, 255,',
                    'rgba(139, 92, 246,',
                    'rgba(0, 255, 136,',
                ][Math.floor(Math.random() * 3)],
                baseAlpha: Math.random() * 0.5 + 0.2,
            });
        }
    }

    function draw() {
        ctx.clearRect(0, 0, w, h);

        const connectDist = 120;
        const mouseDist = 180;

        for (let i = 0; i < particles.length; i++) {
            const p = particles[i];

            // Mouse repulsion
            const dxM = p.x - mouse.x;
            const dyM = p.y - mouse.y;
            const distM = Math.sqrt(dxM * dxM + dyM * dyM);
            if (distM < mouseDist && distM > 0) {
                const force = (mouseDist - distM) / mouseDist * 0.8;
                p.vx += (dxM / distM) * force;
                p.vy += (dyM / distM) * force;
            }

            // Burst shockwaves (from typing)
            for (const burst of burstQueue) {
                const bx = p.x - burst.cx;
                const by = p.y - burst.cy;
                const bDist = Math.sqrt(bx * bx + by * by);
                if (bDist < burst.radius && bDist > 0) {
                    const bForce = (burst.radius - bDist) / burst.radius * burst.strength;
                    p.vx += (bx / bDist) * bForce;
                    p.vy += (by / bDist) * bForce;
                }
            }

            // Velocity damping
            p.vx *= 0.98;
            p.vy *= 0.98;

            // Move
            p.x += p.vx;
            p.y += p.vy;

            // Wrap edges
            if (p.x < -10) p.x = w + 10;
            if (p.x > w + 10) p.x = -10;
            if (p.y < -10) p.y = h + 10;
            if (p.y > h + 10) p.y = -10;

            // Draw particle with glow
            const alpha = p.baseAlpha + (distM < mouseDist ? (mouseDist - distM) / mouseDist * 0.4 : 0);
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
            ctx.fillStyle = p.color + Math.min(alpha, 0.9) + ')';
            ctx.fill();

            // Connect nearby particles with lines
            for (let j = i + 1; j < particles.length; j++) {
                const p2 = particles[j];
                const dx = p.x - p2.x;
                const dy = p.y - p2.y;
                const dist = Math.sqrt(dx * dx + dy * dy);
                if (dist < connectDist) {
                    const lineAlpha = (1 - dist / connectDist) * 0.15;
                    ctx.beginPath();
                    ctx.moveTo(p.x, p.y);
                    ctx.lineTo(p2.x, p2.y);
                    ctx.strokeStyle = 'rgba(0, 240, 255,' + lineAlpha + ')';
                    ctx.lineWidth = 0.5;
                    ctx.stroke();
                }
            }
        }

        // Clear processed bursts
        burstQueue = [];

        animId = requestAnimationFrame(draw);
    }

    // Keystroke burst — ripple outward from the input field
    function triggerKeystrokeBurst() {
        const input = document.getElementById('passkeyInput');
        if (!input) return;
        const rect = input.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        burstQueue.push({
            cx,
            cy,
            radius: 280 + Math.random() * 60,
            strength: 1.8 + Math.random() * 0.8,
        });
    }

    // Event listeners
    window.addEventListener('resize', () => { resize(); createParticles(); });

    document.getElementById('loginOverlay').addEventListener('mousemove', (e) => {
        mouse.x = e.clientX;
        mouse.y = e.clientY;
    });

    document.getElementById('loginOverlay').addEventListener('mouseleave', () => {
        mouse.x = -1000;
        mouse.y = -1000;
    });

    // Typing triggers burst
    document.getElementById('passkeyInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === 'Tab') return;
        triggerKeystrokeBurst();
    });

    // Expose stop function for after login
    window._stopLoginParticles = function() {
        if (animId) cancelAnimationFrame(animId);
    };

    resize();
    createParticles();
    draw();
})();

// ---------------------------------------------------------------------------
// Login Logic
// ---------------------------------------------------------------------------
function showLogin() {
    document.getElementById("loginOverlay").classList.remove("hidden");
    document.getElementById("dashboardContent").className = "dashboard-hidden";
}

function showDashboard() {
    document.getElementById("loginOverlay").classList.add("hidden");
    document.getElementById("dashboardContent").className = "dashboard-visible";
    // Stop particle animation to save CPU
    if (window._stopLoginParticles) window._stopLoginParticles();
}

async function handleLogin(passkey) {
    const errorEl = document.getElementById("loginError");
    const btn = document.getElementById("loginBtn");
    const input = document.getElementById("passkeyInput");

    btn.disabled = true;
    btn.querySelector(".login-btn-text").textContent = "VERIFYING...";
    errorEl.textContent = "";

    try {
        const res = await fetch(`${API}/api/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ passkey }),
        });

        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || "Wrong passkey");
        }

        const data = await res.json();
        authToken = data.token;
        sessionStorage.setItem("nba_agent_token", authToken);

        showDashboard();
        refresh();
    } catch (err) {
        errorEl.textContent = err.message || "Authentication failed";
        input.classList.add("shake");
        setTimeout(() => input.classList.remove("shake"), 500);
        input.value = "";
        input.focus();
    } finally {
        btn.disabled = false;
        btn.querySelector(".login-btn-text").textContent = "UNLOCK";
    }
}

// Wire up the login form
document.getElementById("loginForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const passkey = document.getElementById("passkeyInput").value.trim();
    if (passkey) handleLogin(passkey);
});

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------
async function fetchJSON(endpoint) {
    try {
        const headers = {};
        if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
        const res = await fetch(`${API}${endpoint}`, { headers });
        if (res.status === 401) {
            // Token expired or invalid — show login
            authToken = null;
            sessionStorage.removeItem("nba_agent_token");
            showLogin();
            return null;
        }
        if (!res.ok) throw new Error(`${res.status}`);
        return await res.json();
    } catch (err) {
        console.error(`Fetch ${endpoint} failed:`, err);
        return null;
    }
}

function setConnected(ok) {
    const dot = document.querySelector("#connectionStatus .status-dot");
    const text = document.querySelector("#connectionStatus .status-text");
    if (ok) {
        dot.classList.add("connected");
        text.textContent = "Live";
    } else {
        dot.classList.remove("connected");
        text.textContent = "Offline";
    }
}

// ---------------------------------------------------------------------------
// Update Functions
// ---------------------------------------------------------------------------

function updateStatus(data) {
    if (!data) return;

    // Mode badge
    const badge = document.getElementById("modeBadge");
    const modeText = badge.querySelector(".mode-text");
    if (data.mode === "live") {
        badge.classList.add("live");
        modeText.textContent = "LIVE";
    } else {
        badge.classList.remove("live");
        modeText.textContent = "PAPER";
    }

    // Balance
    document.getElementById("kpiBalance").textContent = fmt.usd(data.bankroll);
    document.getElementById("kpiBalanceSub").textContent = `Starting: ${fmt.usd(data.starting_bankroll)}`;

    // Last update
    document.getElementById("lastUpdate").textContent = data.last_scan
        ? fmt.relative(data.last_scan)
        : "--";
}

function updateStats(data) {
    if (!data) return;
    cachedStats = data;

    // Daily P&L (today)
    // Get today's date in Singapore time
    const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ }); // en-CA gives YYYY-MM-DD format
    const todayEntry = (data.daily_pnl || []).find((d) => d.date === today);
    const dailyPnl = todayEntry ? todayEntry.pnl : 0;
    const dailyEl = document.getElementById("kpiDailyPnl");
    dailyEl.textContent = (dailyPnl >= 0 ? "+" : "") + fmt.usd(dailyPnl);
    dailyEl.className = `kpi-value ${dailyPnl >= 0 ? "pnl-positive" : "pnl-negative"}`;

    // Count today's trades
    const todayTrades = (data.daily_pnl || []).filter((d) => d.date === today).length;
    document.getElementById("kpiDailyPnlSub").textContent = `${todayTrades} trade${todayTrades !== 1 ? "s" : ""} today`;

    // Win rate
    document.getElementById("kpiWinRate").textContent = fmt.pct(data.win_rate);
    document.getElementById("kpiWinBar").style.width = `${data.win_rate || 0}%`;

    // Total trades
    document.getElementById("kpiTotalTrades").textContent = data.total_trades || 0;
    document.getElementById("kpiWins").textContent = data.wins || 0;
    document.getElementById("kpiLosses").textContent = data.losses || 0;

    // ROI
    const roiEl = document.getElementById("kpiRoi");
    roiEl.textContent = (data.roi >= 0 ? "+" : "") + fmt.pct(data.roi);
    roiEl.className = `kpi-value ${data.roi >= 0 ? "pnl-positive" : "pnl-negative"}`;
    document.getElementById("kpiPnlTotal").textContent = `P&L: ${fmt.usd(data.total_pnl)}`;

    // Badges
    document.getElementById("drawdownBadge").textContent = `Max DD: ${fmt.pct(data.max_drawdown)}`;
    document.getElementById("profitFactorBadge").textContent = `PF: ${data.profit_factor === Infinity ? "∞" : data.profit_factor}`;

    // Streak + stats
    const streak = data.streak || {};
    const csEl = document.getElementById("currentStreak");
    csEl.textContent = `${streak.current || 0}${streak.type || ""}`;
    csEl.className = `stat-mini-value ${streak.type === "W" ? "streak-win" : "streak-loss"}`;
    document.getElementById("bestStreak").textContent = `${streak.best || 0}W`;
    document.getElementById("worstStreak").textContent = `${streak.worst || 0}L`;

    const awEl = document.getElementById("avgWin");
    awEl.textContent = fmt.usd(data.avg_win);
    const alEl = document.getElementById("avgLoss");
    alEl.textContent = fmt.usd(data.avg_loss);
    document.getElementById("avgEdge").textContent = `${data.avg_edge || 0}%`;

    document.getElementById("tradeCountBadge").textContent = `${data.total_trades || 0} closed`;

    // Draw charts
    drawEquityChart(data.daily_pnl || []);
    drawDailyPnlChart(data.daily_pnl || []);
    drawWinRateTypeChart(data.win_rate_by_type || {});
}

function updatePositions(data) {
    if (!data) return;
    cachedPositions = data;

    const open = data.open || [];
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

    grid.innerHTML = open
        .map((p) => {
            const holdTime = fmt.relative(p.entry_time);
            const confClass =
                p.confidence === "HIGH" ? "conf-high" :
                p.confidence === "MEDIUM" ? "conf-medium" : "conf-low";
            const cardClass = "pos-neutral";

            // Extract game date from slug (nba-away-home-YYYY-MM-DD) or game_start_time
            let gameDate = "--";
            if (p.game_start_time) {
                const d = new Date(p.game_start_time);
                gameDate = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: TZ });
            } else if (p.market_slug) {
                const parts = p.market_slug.split("-");
                if (parts.length >= 5) {
                    const d = new Date(parts.slice(-3).join("-") + "T12:00:00Z");
                    gameDate = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: TZ });
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
                        <span class="pos-detail-label">Entry</span>
                        <span class="pos-detail-value">${fmt.price(p.entry_price)}</span>
                    </div>
                    <div class="pos-detail">
                        <span class="pos-detail-label">Cost</span>
                        <span class="pos-detail-value">${fmt.usd(p.cost)}</span>
                    </div>
                    <div class="pos-detail">
                        <span class="pos-detail-label">Edge</span>
                        <span class="pos-detail-value">${fmt.edge(p.edge_at_entry)}</span>
                    </div>
                    <div class="pos-detail">
                        <span class="pos-detail-label">Fair Price</span>
                        <span class="pos-detail-value">${fmt.price(p.our_fair_price)}</span>
                    </div>
                    <div class="pos-detail">
                        <span class="pos-detail-label">Hold Time</span>
                        <span class="pos-detail-value">${holdTime}</span>
                    </div>
                </div>
            </div>`;
        })
        .join("");
}

function updateTrades(positions) {
    if (!positions) return;

    const closed = (positions.closed || []).slice();

    // Sort
    closed.sort((a, b) => {
        let va, vb;
        switch (sortColumn) {
            case "date":
                va = a.exit_time || a.entry_time || "";
                vb = b.exit_time || b.entry_time || "";
                break;
            case "market":
                va = a.market_question || "";
                vb = b.market_question || "";
                break;
            case "entry":
                va = a.entry_price || 0;
                vb = b.entry_price || 0;
                break;
            case "exit":
                va = a.exit_price || 0;
                vb = b.exit_price || 0;
                break;
            case "pnl":
                va = a.pnl || 0;
                vb = b.pnl || 0;
                break;
            default:
                va = a.exit_time || "";
                vb = b.exit_time || "";
        }
        if (typeof va === "string") {
            const cmp = va.localeCompare(vb);
            return sortDirection === "asc" ? cmp : -cmp;
        }
        return sortDirection === "asc" ? va - vb : vb - va;
    });

    // Desktop table
    const tbody = document.getElementById("tradeTableBody");
    tbody.innerHTML = closed
        .map((p) => {
            const pnl = p.pnl || 0;
            const pnlClass = pnl > 0 ? "pnl-positive" : pnl < 0 ? "pnl-negative" : "pnl-neutral";
            const confClass =
                p.confidence === "HIGH" ? "conf-high" :
                p.confidence === "MEDIUM" ? "conf-medium" : "conf-low";
            const isExpanded = expandedTradeId === p.id;

            return `
            <tr onclick="toggleTradeDetail('${p.id}')">
                <td>${fmt.date(p.exit_time || p.entry_time)}</td>
                <td class="td-market" title="${escHtml(p.market_question)}">${escHtml(p.market_question)}</td>
                <td>${p.side}</td>
                <td>${fmt.price(p.entry_price)}</td>
                <td>${fmt.price(p.exit_price)}</td>
                <td class="${pnlClass}">${(pnl >= 0 ? "+" : "") + fmt.usd(pnl)}</td>
                <td>${fmt.edge(p.edge_at_entry)}</td>
                <td><span class="badge ${confClass}" style="font-size:10px">${p.confidence}</span></td>
            </tr>
            <tr class="trade-detail-row ${isExpanded ? "expanded" : ""}" id="detail-${p.id}">
                <td colspan="8" class="trade-detail-cell">
                    <div class="trade-detail-grid">
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Position ID</span>
                            <span class="trade-detail-value">${p.id}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Game Date</span>
                            <span class="trade-detail-value">${p.game_start_time ? new Date(p.game_start_time).toLocaleDateString("en-US", {weekday:"short",month:"short",day:"numeric",timeZone:TZ}) : "--"}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Entry Time</span>
                            <span class="trade-detail-value">${fmt.datetime(p.entry_time)}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Exit Time</span>
                            <span class="trade-detail-value">${fmt.datetime(p.exit_time)}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Cost</span>
                            <span class="trade-detail-value">${fmt.usd(p.cost)}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Shares</span>
                            <span class="trade-detail-value">${p.shares?.toFixed(2) || "--"}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Fair Price</span>
                            <span class="trade-detail-value">${fmt.price(p.our_fair_price)}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Exit Reason</span>
                            <span class="trade-detail-value">${p.exit_reason || "--"}</span>
                        </div>
                        <div class="trade-detail-item">
                            <span class="trade-detail-label">Mode</span>
                            <span class="trade-detail-value">${p.mode || "--"}</span>
                        </div>
                    </div>
                </td>
            </tr>`;
        })
        .join("");

    // Mobile cards
    const mobileContainer = document.getElementById("tradeCardsMobile");
    mobileContainer.innerHTML = closed
        .map((p) => {
            const pnl = p.pnl || 0;
            const pnlClass = pnl > 0 ? "pnl-positive" : pnl < 0 ? "pnl-negative" : "pnl-neutral";
            const confClass =
                p.confidence === "HIGH" ? "conf-high" :
                p.confidence === "MEDIUM" ? "conf-medium" : "conf-low";
            return `
            <div class="trade-card-m">
                <div class="trade-card-m-header">
                    <div class="trade-card-m-market">${escHtml(p.market_question)}</div>
                    <div class="trade-card-m-pnl ${pnlClass}">${(pnl >= 0 ? "+" : "") + fmt.usd(pnl)}</div>
                </div>
                <div class="trade-card-m-details">
                    <div class="trade-card-m-detail">
                        <span class="trade-card-m-label">Side</span>
                        <span class="trade-card-m-value">${p.side}</span>
                    </div>
                    <div class="trade-card-m-detail">
                        <span class="trade-card-m-label">Entry</span>
                        <span class="trade-card-m-value">${fmt.price(p.entry_price)}</span>
                    </div>
                    <div class="trade-card-m-detail">
                        <span class="trade-card-m-label">Exit</span>
                        <span class="trade-card-m-value">${fmt.price(p.exit_price)}</span>
                    </div>
                    <div class="trade-card-m-detail">
                        <span class="trade-card-m-label">Edge</span>
                        <span class="trade-card-m-value">${fmt.edge(p.edge_at_entry)}</span>
                    </div>
                    <div class="trade-card-m-detail">
                        <span class="trade-card-m-label">Date</span>
                        <span class="trade-card-m-value">${fmt.date(p.exit_time)}</span>
                    </div>
                    <div class="trade-card-m-detail">
                        <span class="trade-card-m-label">Conf</span>
                        <span class="trade-card-m-value"><span class="badge ${confClass}" style="font-size:9px;padding:2px 6px">${p.confidence}</span></span>
                    </div>
                </div>
            </div>`;
        })
        .join("");
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

    grid.innerHTML = research
        .map((r) => {
            const hasEdge = r.edge >= 0.04;
            const edgeClass = hasEdge ? "has-edge" : "no-edge";

            return `
            <div class="research-card ${edgeClass}">
                <div class="research-header">
                    <span class="research-game">${escHtml(r.game)}</span>
                    <span class="research-time">${fmt.datetime(r.game_time)}</span>
                </div>
                <div class="research-odds">
                    <div class="research-odds-item">
                        <div class="research-odds-label">Fair Price</div>
                        <div class="research-odds-value">${fmt.price(r.our_fair_price)}</div>
                    </div>
                    <div class="research-odds-item">
                        <div class="research-odds-label">Market</div>
                        <div class="research-odds-value">${fmt.price(r.market_price)}</div>
                    </div>
                    <div class="research-odds-item">
                        <div class="research-odds-label">Edge</div>
                        <div class="research-odds-value ${hasEdge ? "pnl-positive" : "pnl-neutral"}">${fmt.edge(r.edge)}</div>
                    </div>
                </div>
                <div class="research-analysis">${escHtml(r.analysis)}</div>
                <div class="research-footer">
                    <span class="badge ${r.confidence === "HIGH" ? "conf-high" : r.confidence === "MEDIUM" ? "conf-medium" : "conf-low"}" style="font-size:10px">${r.confidence}</span>
                    <span class="research-bet-status ${r.bet_placed ? "research-bet-placed" : "research-bet-passed"}">
                        ${r.bet_placed ? "✓ BET PLACED" : "— PASSED"}
                    </span>
                </div>
            </div>`;
        })
        .join("");
}

// Performance summary with period tabs
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
    } catch (e) {
        // silent
    }
}

function updatePerformanceGrid(data) {
    document.getElementById("perfBets").textContent = data.bets || 0;
    document.getElementById("perfWL").textContent = `${data.wins || 0} / ${data.losses || 0}`;
    document.getElementById("perfWinRate").textContent = fmt.pct(data.win_rate);

    const pnlEl = document.getElementById("perfPnl");
    const pnl = data.pnl || 0;
    pnlEl.textContent = (pnl >= 0 ? "+" : "") + fmt.usd(pnl);
    pnlEl.className = `summary-value ${pnl >= 0 ? "pnl-positive" : "pnl-negative"}`;

    const roiEl = document.getElementById("perfRoi");
    const roi = data.roi || 0;
    roiEl.textContent = (roi >= 0 ? "+" : "") + fmt.pct(roi);
    roiEl.className = `summary-value ${roi >= 0 ? "pnl-positive" : "pnl-negative"}`;

    const awEl = document.getElementById("perfAvgWin");
    awEl.textContent = fmt.usd(data.avg_win);
    awEl.className = "summary-value pnl-positive";

    const alEl = document.getElementById("perfAvgLoss");
    alEl.textContent = fmt.usd(data.avg_loss);
    alEl.className = "summary-value pnl-negative";

    document.getElementById("perfPF").textContent = data.profit_factor != null ? data.profit_factor.toFixed(2) + "x" : "--";
}

function updateDailySummary(stats, research) {
    // Fetch performance for current tab
    fetchPerformance(currentPeriod);
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

    // Gradient fill
    const gradient = ctx.createLinearGradient(0, 0, 0, 340);
    gradient.addColorStop(0, "rgba(0, 240, 255, 0.25)");
    gradient.addColorStop(0.5, "rgba(0, 240, 255, 0.08)");
    gradient.addColorStop(1, "rgba(0, 240, 255, 0)");

    equityChart = new Chart(ctx, {
        type: "line",
        data: {
            labels,
            datasets: [
                {
                    label: "Bankroll",
                    data: values,
                    borderColor: "#00f0ff",
                    borderWidth: 2.5,
                    backgroundColor: gradient,
                    fill: true,
                    tension: 0.4,
                    pointRadius: 3,
                    pointBackgroundColor: "#00f0ff",
                    pointBorderColor: "#0a0b0f",
                    pointBorderWidth: 2,
                    pointHoverRadius: 6,
                    pointHoverBackgroundColor: "#00f0ff",
                    pointHoverBorderColor: "#fff",
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {
                duration: 1000,
                easing: "easeOutQuart",
            },
            interaction: {
                mode: "index",
                intersect: false,
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18, 19, 26, 0.95)",
                    borderColor: "rgba(0, 240, 255, 0.3)",
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 12,
                    titleFont: { family: "'Inter'", size: 12, weight: "600" },
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    titleColor: "#71717a",
                    bodyColor: "#e4e4e7",
                    callbacks: {
                        label: (ctx) => `$${ctx.parsed.y.toFixed(2)}`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: {
                        font: { size: 10 },
                        maxRotation: 0,
                        maxTicksLimit: 10,
                    },
                },
                y: {
                    grid: { color: "rgba(255,255,255,0.04)" },
                    ticks: {
                        font: { family: "'JetBrains Mono'", size: 11 },
                        callback: (v) => `$${v}`,
                    },
                },
            },
        },
    });
}

function drawDailyPnlChart(dailyPnl) {
    const ctx = document.getElementById("dailyPnlChart").getContext("2d");

    const labels = dailyPnl.map((d) => d.date);
    const values = dailyPnl.map((d) => d.pnl);
    const colors = values.map((v) => (v >= 0 ? "#00ff88" : "#ff3366"));
    const bgColors = values.map((v) =>
        v >= 0 ? "rgba(0, 255, 136, 0.7)" : "rgba(255, 51, 102, 0.7)"
    );

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
        data: {
            labels,
            datasets: [
                {
                    label: "P&L",
                    data: values,
                    backgroundColor: bgColors,
                    borderColor: colors,
                    borderWidth: 1,
                    borderRadius: 4,
                    borderSkipped: false,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 800, easing: "easeOutQuart" },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18, 19, 26, 0.95)",
                    borderColor: "rgba(255,255,255,0.1)",
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 12,
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    callbacks: {
                        label: (ctx) => {
                            const v = ctx.parsed.y;
                            return `${v >= 0 ? "+" : ""}$${v.toFixed(2)}`;
                        },
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { font: { size: 10 }, maxRotation: 0, maxTicksLimit: 8 },
                },
                y: {
                    grid: { color: "rgba(255,255,255,0.04)" },
                    ticks: {
                        font: { family: "'JetBrains Mono'", size: 11 },
                        callback: (v) => `$${v}`,
                    },
                },
            },
        },
    });
}

function drawWinRateTypeChart(winRateByType) {
    const ctx = document.getElementById("winRateTypeChart").getContext("2d");

    const types = Object.keys(winRateByType);
    const values = Object.values(winRateByType);

    const colorMap = {
        moneyline: "#00f0ff",
        spread: "#8b5cf6",
        total: "#00ff88",
        futures: "#ff3366",
    };
    const bgMap = {
        moneyline: "rgba(0, 240, 255, 0.7)",
        spread: "rgba(139, 92, 246, 0.7)",
        total: "rgba(0, 255, 136, 0.7)",
        futures: "rgba(255, 51, 102, 0.7)",
    };

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
            datasets: [
                {
                    label: "Win Rate %",
                    data: values,
                    backgroundColor: bgColors,
                    borderColor: borderColors,
                    borderWidth: 1,
                    borderRadius: 6,
                    borderSkipped: false,
                    barThickness: 40,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 800, easing: "easeOutQuart" },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(18, 19, 26, 0.95)",
                    borderColor: "rgba(255,255,255,0.1)",
                    borderWidth: 1,
                    cornerRadius: 8,
                    padding: 12,
                    bodyFont: { family: "'JetBrains Mono'", size: 13 },
                    callbacks: {
                        label: (ctx) => `${ctx.parsed.y.toFixed(1)}%`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: {
                        font: { size: 11, weight: "500" },
                        color: "#e4e4e7",
                    },
                },
                y: {
                    min: 0,
                    max: 100,
                    grid: { color: "rgba(255,255,255,0.04)" },
                    ticks: {
                        font: { family: "'JetBrains Mono'", size: 11 },
                        callback: (v) => `${v}%`,
                    },
                },
            },
        },
    });
}

// ---------------------------------------------------------------------------
// Sort + Expand handlers
// ---------------------------------------------------------------------------

window.toggleTradeDetail = function (id) {
    if (expandedTradeId === id) {
        expandedTradeId = null;
    } else {
        expandedTradeId = id;
    }
    if (cachedPositions) updateTrades(cachedPositions);
};

document.addEventListener("click", (e) => {
    const th = e.target.closest("th.sortable");
    if (!th) return;

    const col = th.dataset.sort;
    if (sortColumn === col) {
        sortDirection = sortDirection === "asc" ? "desc" : "asc";
    } else {
        sortColumn = col;
        sortDirection = "desc";
    }

    // Update sort arrows
    document.querySelectorAll("th.sortable").forEach((el) => {
        el.classList.remove("sort-active");
        el.querySelector(".sort-arrow").textContent = "↕";
    });
    th.classList.add("sort-active");
    th.querySelector(".sort-arrow").textContent = sortDirection === "asc" ? "↑" : "↓";

    if (cachedPositions) updateTrades(cachedPositions);
});

// ---------------------------------------------------------------------------
// HTML escape
// ---------------------------------------------------------------------------
function escHtml(str) {
    if (!str) return "";
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Main refresh loop
// ---------------------------------------------------------------------------
async function refresh() {
    const [status, positions, stats, research] = await Promise.all([
        fetchJSON("/api/status"),
        fetchJSON("/api/positions"),
        fetchJSON("/api/stats"),
        fetchJSON("/api/research"),
    ]);

    const anySuccess = status || positions || stats || research;
    setConnected(anySuccess);

    if (status) updateStatus(status);
    if (stats) updateStats(stats);
    if (positions) {
        updatePositions(positions);
        updateTrades(positions);
    }
    if (research) updateResearch(research);
    updateDailySummary(stats, research);
}

// ---------------------------------------------------------------------------
// Init — check if already authenticated
// ---------------------------------------------------------------------------
initPeriodTabs();

if (authToken) {
    // Try a quick auth check against a protected endpoint
    fetchJSON("/api/status").then((data) => {
        if (data) {
            showDashboard();
            refresh();
        } else {
            showLogin();
        }
    });
} else {
    showLogin();
}

// Auto-refresh (only runs when dashboard is visible)
setInterval(() => {
    if (authToken) refresh();
}, REFRESH_INTERVAL);
