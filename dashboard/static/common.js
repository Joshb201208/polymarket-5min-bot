/* ===================================================================
   Shared Dashboard Utilities — common.js
   Auth, fetch helpers, formatters, login particle canvas, HTML escape.
   Included by all pages.
   =================================================================== */

// ---------------------------------------------------------------------------
// API Base URL
// ---------------------------------------------------------------------------
const API = "";
const REFRESH_INTERVAL = 30_000; // 30 seconds

// ---------------------------------------------------------------------------
// Auth State — persisted in localStorage so it survives page navigation
// ---------------------------------------------------------------------------
// Migrate from old sessionStorage key if present
(function migrateAuthToken() {
    const old = sessionStorage.getItem("nba_agent_token");
    if (old && !localStorage.getItem("nba_agent_token")) {
        localStorage.setItem("nba_agent_token", old);
        sessionStorage.removeItem("nba_agent_token");
    }
})();

let authToken = localStorage.getItem("nba_agent_token") || null;

// ---------------------------------------------------------------------------
// Chart.js Defaults
// ---------------------------------------------------------------------------
if (typeof Chart !== "undefined") {
    Chart.defaults.font.family = "'Inter', sans-serif";
    Chart.defaults.color = "#a1a1aa";
    Chart.defaults.borderColor = "rgba(255,255,255,0.06)";
}

// ---------------------------------------------------------------------------
// Formatters  (Singapore time — UTC+8)
// ---------------------------------------------------------------------------
const TZ = "Asia/Singapore";

const fmt = {
    usd: (v) => {
        if (v == null) return "--";
        const sign = v >= 0 ? "" : "-";
        return `${sign}$${Math.abs(v).toFixed(2)}`;
    },
    pct: (v) => (v == null ? "--" : `${v.toFixed(1)}%`),
    price: (v) => (v == null ? "--" : `\u00a2${(v * 100).toFixed(1)}`),
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
// HTML Escape
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
// Login / Auth helpers
// ---------------------------------------------------------------------------
function showLogin() {
    const overlay = document.getElementById("loginOverlay");
    const content = document.getElementById("dashboardContent");
    if (overlay) overlay.classList.remove("hidden");
    if (content) content.className = "dashboard-hidden";
}

function showDashboard() {
    const overlay = document.getElementById("loginOverlay");
    const content = document.getElementById("dashboardContent");
    if (overlay) overlay.classList.add("hidden");
    if (content) content.className = "dashboard-visible";
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
        localStorage.setItem("nba_agent_token", authToken);

        showDashboard();
        if (typeof refresh === "function") refresh();
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

// ---------------------------------------------------------------------------
// Fetch helper
// ---------------------------------------------------------------------------
async function fetchJSON(endpoint) {
    try {
        const headers = {};
        if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
        const res = await fetch(`${API}${endpoint}`, { headers });
        if (res.status === 401) {
            authToken = null;
            localStorage.removeItem("nba_agent_token");
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
    if (!dot || !text) return;
    if (ok) {
        dot.classList.add("connected");
        text.textContent = "Live";
    } else {
        dot.classList.remove("connected");
        text.textContent = "Offline";
    }
}

// ---------------------------------------------------------------------------
// P&L color helper
// ---------------------------------------------------------------------------
function pnlClass(v) {
    if (v > 0) return "pnl-positive";
    if (v < 0) return "pnl-negative";
    return "pnl-neutral";
}

// ---------------------------------------------------------------------------
// Login Particle Canvas — Interactive Star Field
// ---------------------------------------------------------------------------
(function initLoginParticles() {
    const canvas = document.getElementById('loginCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    let w, h, particles, mouse, animId;
    let burstQueue = [];
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

            const dxM = p.x - mouse.x;
            const dyM = p.y - mouse.y;
            const distM = Math.sqrt(dxM * dxM + dyM * dyM);
            if (distM < mouseDist && distM > 0) {
                const force = (mouseDist - distM) / mouseDist * 0.8;
                p.vx += (dxM / distM) * force;
                p.vy += (dyM / distM) * force;
            }

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

            p.vx *= 0.98;
            p.vy *= 0.98;
            p.x += p.vx;
            p.y += p.vy;

            if (p.x < -10) p.x = w + 10;
            if (p.x > w + 10) p.x = -10;
            if (p.y < -10) p.y = h + 10;
            if (p.y > h + 10) p.y = -10;

            const alpha = p.baseAlpha + (distM < mouseDist ? (mouseDist - distM) / mouseDist * 0.4 : 0);
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
            ctx.fillStyle = p.color + Math.min(alpha, 0.9) + ')';
            ctx.fill();

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

        burstQueue = [];
        animId = requestAnimationFrame(draw);
    }

    function triggerKeystrokeBurst() {
        const input = document.getElementById('passkeyInput');
        if (!input) return;
        const rect = input.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        burstQueue.push({
            cx, cy,
            radius: 280 + Math.random() * 60,
            strength: 1.8 + Math.random() * 0.8,
        });
    }

    window.addEventListener('resize', () => { resize(); createParticles(); });

    const overlay = document.getElementById('loginOverlay');
    if (overlay) {
        overlay.addEventListener('mousemove', (e) => { mouse.x = e.clientX; mouse.y = e.clientY; });
        overlay.addEventListener('mouseleave', () => { mouse.x = -1000; mouse.y = -1000; });
    }

    const passInput = document.getElementById('passkeyInput');
    if (passInput) {
        passInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === 'Tab') return;
            triggerKeystrokeBurst();
        });
    }

    window._stopLoginParticles = function () {
        if (animId) cancelAnimationFrame(animId);
    };

    resize();
    createParticles();
    draw();
})();

// ---------------------------------------------------------------------------
// Wire up login form (safe to call even if form not on page)
// ---------------------------------------------------------------------------
(function () {
    const form = document.getElementById("loginForm");
    if (form) {
        form.addEventListener("submit", (e) => {
            e.preventDefault();
            const passkey = document.getElementById("passkeyInput").value.trim();
            if (passkey) handleLogin(passkey);
        });
    }
})();
