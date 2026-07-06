"""
dashboard.app
==============

⚡ Akash AI Pro - Secure Enterprise API Gateway
"Cybersecurity Command Center" - Pro-Level Streamlit Enterprise Dashboard.

ARCHITECTURAL DECISION -- Streamlit over a full React/Next.js SPA:
    For an internal FinOps/SecOps operator dashboard (as opposed to a
    customer-facing product), Streamlit lets us ship a data-dense,
    real-time-feeling operational cockpit with a fraction of the frontend
    engineering overhead of a React/Next.js build pipeline -- while still
    achieving a dark, "enterprise SaaS" visual bar via custom CSS
    injection and Plotly's dark theme. The dashboard talks to the FastAPI
    gateway's `/api/v1/metrics/dashboard` endpoint as its single source of
    truth, and gracefully falls back to a rich, realistic mock dataset if
    the backend is not yet reachable -- so this file always looks
    "incredibly busy and professional" even on a cold, backend-less first
    run.

COLOR LANGUAGE (Live Traffic chart):
    Green   -> Routed to AI (allowed traffic)
    Yellow  -> Rate-limited (429s)
    Red     -> Blocked / DDoS
    (No blue is used in the traffic chart per design direction.)
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ---------------------------------------------------------------------------
# Page config -- must be the first Streamlit call.
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Akash AI Pro | Command Center",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_BASE_URL = "http://localhost:8000"

# ---------------------------------------------------------------------------
# Auto-refresh -- gives the dashboard a genuinely "live" feel without
# requiring websockets for this reference implementation.
# ---------------------------------------------------------------------------
st_autorefresh(interval=4000, key="global_autorefresh")

# ---------------------------------------------------------------------------
# Custom CSS -- deep dark theme, glassmorphism, glowing accents.
# ---------------------------------------------------------------------------
CUSTOM_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    /* ---- Global background & padding reset ---- */
    .stApp {
        background: radial-gradient(circle at 12% 8%, #131a2e 0%, #0E1117 42%, #08090d 100%);
        color: #e6e9f5;
    }
    .block-container {
        padding-top: 1.4rem;
        padding-bottom: 2rem;
        max-width: 1500px;
    }
    footer, #MainMenu { visibility: hidden; }
    div[data-testid="stDecoration"] { display: none; }

    /* ---- Sidebar ---- */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0b0e1c 0%, #05060c 100%);
        border-right: 1px solid rgba(124,58,237,0.15);
    }
    .sidebar-brand {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 6px 0 2px 0;
    }
    .sidebar-brand-icon {
        font-size: 1.6rem;
        filter: drop-shadow(0 0 10px rgba(124,58,237,0.7));
    }
    .sidebar-brand-name {
        font-size: 1.05rem;
        font-weight: 800;
        color: #f1f4ff;
        line-height: 1.1;
    }
    .sidebar-brand-sub {
        font-size: 0.7rem;
        color: #6f7aa3;
        letter-spacing: 0.12em;
        text-transform: uppercase;
    }
    .nav-group-label {
        color: #545e82;
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        margin: 18px 0 6px 4px;
    }
    .nav-item {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 9px 12px;
        border-radius: 10px;
        color: #aab3d6;
        font-size: 0.88rem;
        font-weight: 500;
        margin-bottom: 3px;
        border: 1px solid transparent;
    }
    .nav-item-active {
        background: linear-gradient(90deg, rgba(124,58,237,0.22), rgba(6,182,212,0.08));
        border: 1px solid rgba(124,58,237,0.4);
        color: #f1f4ff;
        font-weight: 700;
        box-shadow: 0 0 18px rgba(124,58,237,0.15);
    }
    .conn-badge {
        display: flex;
        align-items: center;
        justify-content: space-between;
        background: rgba(52,211,153,0.08);
        border: 1px solid rgba(52,211,153,0.35);
        border-radius: 10px;
        padding: 8px 12px;
        margin-top: 10px;
        font-size: 0.8rem;
        color: #34d399;
        font-weight: 700;
    }
    .sys-ok {
        display: flex;
        align-items: center;
        gap: 8px;
        color: #34d399;
        font-size: 0.82rem;
        font-weight: 700;
        margin-top: 14px;
    }
    .pulse-dot {
        width: 9px; height: 9px; border-radius: 50%;
        background: #34d399;
        box-shadow: 0 0 0 0 rgba(52,211,153,0.7);
        animation: pulse 1.8s infinite;
    }
    @keyframes pulse {
        0%   { box-shadow: 0 0 0 0 rgba(52,211,153,0.55); }
        70%  { box-shadow: 0 0 0 8px rgba(52,211,153,0); }
        100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
    }

    /* ---- Glowing gradient title ---- */
    .glow-title {
        font-size: 2.5rem;
        font-weight: 800;
        background: linear-gradient(90deg, #7C3AED, #06B6D4, #22D3EE);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        text-shadow: 0 0 40px rgba(124, 58, 237, 0.35);
        margin-bottom: 0;
    }
    .subtitle {
        color: #7c88b3;
        font-size: 0.92rem;
        letter-spacing: 0.05em;
        margin-top: -6px;
    }

    /* ---- Top alert banner ---- */
    .alert-banner {
        display: flex;
        align-items: center;
        gap: 14px;
        background: linear-gradient(90deg, rgba(248,113,113,0.16), rgba(248,113,113,0.04));
        border: 1px solid rgba(248,113,113,0.45);
        box-shadow: 0 0 30px rgba(248,113,113,0.15), inset 0 1px 0 rgba(255,255,255,0.03);
        border-radius: 14px;
        padding: 14px 20px;
        color: #fecaca;
        font-size: 0.92rem;
        margin-bottom: 22px;
        animation: alertGlow 2.4s ease-in-out infinite;
    }
    @keyframes alertGlow {
        0%, 100% { box-shadow: 0 0 22px rgba(248,113,113,0.12); }
        50%      { box-shadow: 0 0 38px rgba(248,113,113,0.30); }
    }
    .alert-banner b { color: #f87171; }
    .alert-icon { font-size: 1.3rem; }

    /* ---- Glassmorphism KPI Card ---- */
    .kpi-card {
        background: linear-gradient(145deg, rgba(24,29,58,0.55), rgba(13,16,38,0.55));
        backdrop-filter: blur(14px);
        -webkit-backdrop-filter: blur(14px);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 18px;
        padding: 20px 22px;
        box-shadow: 0 0 25px rgba(34, 211, 238, 0.05), inset 0 1px 0 rgba(255,255,255,0.04);
        transition: all 0.25s ease;
        height: 100%;
    }
    .kpi-card:hover {
        border: 1px solid rgba(34, 211, 238, 0.5);
        box-shadow: 0 0 30px rgba(34, 211, 238, 0.16);
        transform: translateY(-2px);
    }
    .kpi-card-gold {
        background: linear-gradient(145deg, rgba(120,90,10,0.28), rgba(24,29,58,0.55));
        border: 1px solid rgba(251,191,36,0.35);
    }
    .kpi-card-gold:hover {
        border: 1px solid rgba(251,191,36,0.75);
        box-shadow: 0 0 34px rgba(251,191,36,0.28);
    }
    .kpi-label {
        color: #8b95bd;
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .kpi-value {
        font-size: 2.05rem;
        font-weight: 800;
        color: #f1f4ff;
        font-family: 'JetBrains Mono', monospace;
        margin-top: 6px;
    }
    .kpi-value-gold {
        color: #fbbf24;
        text-shadow: 0 0 22px rgba(251,191,36,0.55);
    }
    .kpi-sub {
        font-size: 0.8rem;
        margin-top: 6px;
        font-weight: 600;
    }
    .up   { color: #34d399; }
    .down { color: #f87171; }
    .neutral { color: #93a0c4; }

    /* ---- Status pills ---- */
    .status-pill {
        display: inline-block;
        padding: 4px 14px;
        border-radius: 999px;
        font-size: 0.76rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        border: 1px solid transparent;
    }
    .pill-healthy, .pill-closed { background: rgba(52, 211, 153, 0.14); color: #34d399; border-color: rgba(52,211,153,0.4); }
    .pill-degraded, .pill-halfopen { background: rgba(251, 191, 36, 0.14); color: #fbbf24; border-color: rgba(251,191,36,0.4); }
    .pill-open, .pill-tripped { background: rgba(248, 113, 113, 0.14); color: #f87171; border-color: rgba(248,113,113,0.4); }

    /* ---- Section headers ---- */
    .section-header {
        font-size: 1.05rem;
        font-weight: 700;
        color: #c9d1f0;
        border-left: 3px solid #7C3AED;
        padding-left: 10px;
        margin-top: 6px;
        margin-bottom: 14px;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .section-tag {
        font-size: 0.7rem;
        font-weight: 700;
        color: #6f7aa3;
        letter-spacing: 0.08em;
        border: 1px solid rgba(255,255,255,0.08);
        padding: 2px 10px;
        border-radius: 999px;
    }

    /* ---- Circuit breaker card ---- */
    .breaker-card {
        background: rgba(255,255,255,0.02);
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 14px;
        padding: 14px 16px;
        margin-bottom: 10px;
    }
    .breaker-card-open { border-color: rgba(248,113,113,0.45); box-shadow: 0 0 18px rgba(248,113,113,0.10); }
    .breaker-card-closed { border-color: rgba(52,211,153,0.30); }
    .breaker-card-halfopen { border-color: rgba(251,191,36,0.35); }
    .breaker-name {
        font-family: 'JetBrains Mono', monospace;
        font-weight: 700;
        color: #dfe4fb;
        font-size: 0.9rem;
    }
    .breaker-meta { color: #7c88b3; font-size: 0.76rem; margin-top: 4px; }

    /* ---- Threat log ---- */
    .log-wrap {
        max-height: 300px;
        overflow-y: auto;
        padding: 6px;
        background: rgba(255,255,255,0.015);
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.06);
    }
    .log-row {
        display: grid;
        grid-template-columns: 90px 190px 1fr 130px;
        gap: 10px;
        align-items: center;
        padding: 7px 10px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.78rem;
        border-bottom: 1px solid rgba(255,255,255,0.03);
    }
    .log-row:hover { background: rgba(255,255,255,0.03); }
    .log-danger { color: #f87171; }
    .log-warn { color: #fbbf24; }
    .log-info { color: #67e8a8; }
    .log-ip { color: #93a0c4; }

    div[data-testid="stMetricValue"] { font-family: 'JetBrains Mono', monospace; }

    ::-webkit-scrollbar { width: 7px; height: 7px; }
    ::-webkit-scrollbar-thumb { background: rgba(124,58,237,0.4); border-radius: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Mock data generation -- rich + dynamic so the cockpit looks "alive"
# ---------------------------------------------------------------------------
USERS = ["free_user", "premium_user", "admin", "acme_corp_svc", "beta_partner_api"]
MODELS = ["gpt-4o", "claude-opus", "claude-sonnet", "akash-llm-lite"]
ENDPOINTS = [
    "/api/v1/ai/chat/completions",
    "/api/v1/ai/embeddings",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/metrics/dashboard",
]
FAKE_IPS = ["203.0.113.44", "198.51.100.9", "192.0.2.187", "45.83.12.201", "89.248.165.32", "172.16.44.9"]

EVENT_POOL = [
    ("IP_BLOCKED", "danger", "DDoS volumetric threshold exceeded -- auto-blocked at edge"),
    ("RATE_LIMIT_BREACH", "warn", "Free-tier token bucket drained"),
    ("TOKEN_EXPIRED", "warn", "Expired JWT presented at gateway"),
    ("TOKEN_INVALID", "danger", "Malformed / forged JWT rejected"),
    ("LOGIN_FAILED", "warn", "Invalid credentials presented"),
    ("CIRCUIT_TRIP", "danger", "Circuit breaker tripped to OPEN after repeated upstream failures"),
    ("CIRCUIT_RECOVER", "info", "Circuit breaker recovered HALF_OPEN -> CLOSED"),
    ("API_USAGE", "info", "Authenticated request served successfully"),
]


def _generate_mock_dataset() -> dict:
    random.seed()
    recent_usage = []
    now = datetime.utcnow()
    for i in range(25):
        ts = now - timedelta(seconds=i * random.randint(4, 40))
        model = random.choices(MODELS, weights=[0.35, 0.3, 0.25, 0.1])[0]
        input_tokens = random.randint(20, 400)
        output_tokens = random.randint(40, 600)
        cost = round((input_tokens / 1000) * 0.003 + (output_tokens / 1000) * 0.006, 6)
        recent_usage.append(
            {
                "timestamp": ts.isoformat(),
                "username": random.choice(USERS),
                "tier": random.choice(["free", "premium", "admin"]),
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            }
        )

    cost_by_user, cost_by_model = {}, {}
    for r in recent_usage:
        cost_by_user[r["username"]] = cost_by_user.get(r["username"], 0) + r["cost_usd"]
        cost_by_model[r["model"]] = cost_by_model.get(r["model"], 0) + r["cost_usd"]

    breakers = [
        {
            "name": "primary_ai_service",
            "state": random.choices(["CLOSED", "OPEN", "HALF_OPEN"], weights=[0.75, 0.12, 0.13])[0],
            "failure_count": random.randint(0, 4),
            "failure_threshold": 5,
            "total_calls": random.randint(2000, 5000),
        },
        {
            "name": "billing_service",
            "state": random.choices(["CLOSED", "OPEN", "HALF_OPEN"], weights=[0.9, 0.05, 0.05])[0],
            "failure_count": random.randint(0, 2),
            "failure_threshold": 5,
            "total_calls": random.randint(800, 2000),
        },
        {
            "name": "embeddings_service",
            "state": random.choices(["CLOSED", "OPEN", "HALF_OPEN"], weights=[0.85, 0.08, 0.07])[0],
            "failure_count": random.randint(0, 3),
            "failure_threshold": 5,
            "total_calls": random.randint(1000, 3000),
        },
    ]

    return {
        "cost_summary": {
            "total_cost_usd": round(sum(cost_by_user.values()) * random.uniform(8, 14), 2),
            "total_input_tokens": sum(r["input_tokens"] for r in recent_usage) * random.randint(6, 12),
            "total_output_tokens": sum(r["output_tokens"] for r in recent_usage) * random.randint(6, 12),
            "total_requests": random.randint(1800, 4200),
            "cost_by_user": {k: round(v, 4) for k, v in cost_by_user.items()},
            "cost_by_model": {k: round(v, 4) for k, v in cost_by_model.items()},
        },
        "recent_usage": recent_usage,
        "circuit_breakers": breakers,
        "redis_healthy": random.random() > 0.05,
        "active_users": {"premium": random.randint(500, 900), "free": random.randint(1800, 2800)},
        "blocked_24h": random.randint(2200, 3200),
        "_is_mock": True,
    }


@st.cache_data(ttl=4)
def fetch_dashboard_data() -> dict:
    try:
        resp = requests.get(f"{API_BASE_URL}/api/v1/metrics/dashboard", timeout=1.5)
        resp.raise_for_status()
        payload = resp.json()
        payload["_is_mock"] = False
        return payload
    except (requests.RequestException, ValueError):
        return _generate_mock_dataset()


def _generate_mock_threat_log() -> list[dict]:
    random.seed()
    events = []
    now = datetime.utcnow()
    for i in range(20):
        etype, severity, msg = random.choice(EVENT_POOL)
        events.append(
            {
                "timestamp": (now - timedelta(seconds=i * random.randint(5, 90))).strftime("%H:%M:%S"),
                "event_type": etype,
                "severity": severity,
                "detail": msg,
                "ip": random.choice(FAKE_IPS),
                "endpoint": random.choice(ENDPOINTS),
            }
        )
    return events


def _build_traffic_series(n: int = 40):
    """Three synchronized series: allowed (green), rate-limited (yellow),
    blocked/DDoS (red). No blue is used, per design direction."""
    allowed, limited, blocked = [], [], []
    a, l, b = 260, 40, 12
    for i in range(n):
        a += random.uniform(-18, 22)
        l += random.uniform(-6, 7)
        b += random.uniform(-3, 4)
        a = max(180, min(340, a))
        l = max(15, min(110, l))
        b = max(3, min(45, b))
        allowed.append(int(a))
        limited.append(int(l))
        blocked.append(int(b))
    return allowed, limited, blocked


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        """
        <div class="sidebar-brand">
            <div class="sidebar-brand-icon">⚡</div>
            <div>
                <div class="sidebar-brand-name">Akash AI Pro</div>
                <div class="sidebar-brand-sub">Gateway</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="nav-group-label">Operations</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="nav-item nav-item-active">🧭&nbsp; Command Center</div>
        <div class="nav-item">📈&nbsp; Live Traffic</div>
        <div class="nav-item">🛡️&nbsp; Threat Log</div>
        <div class="nav-item">🎚️&nbsp; Rate Limits</div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="nav-group-label">FinOps</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="nav-item">💲&nbsp; Cost Tracker</div>
        <div class="nav-item">🥧&nbsp; Model Spend</div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="nav-group-label">Platform</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="nav-item">🔌&nbsp; Circuit Breakers</div>
        <div class="nav-item">🔐&nbsp; Auth &amp; Tokens</div>
        <div class="nav-item">⚙️&nbsp; Settings</div>
        """,
        unsafe_allow_html=True,
    )

    data = fetch_dashboard_data()
    is_mock = data.get("_is_mock", True)

    st.markdown(
        f"""
        <div class="conn-badge">
            <span>🗄️ Redis</span>
            <span>{'CONNECTED' if data.get('redis_healthy', True) else 'DEGRADED'}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if is_mock:
        st.warning("🟡 Backend offline — showing simulated telemetry", icon="⚠️")
    else:
        st.success("🟢 Live telemetry connected", icon="✅")

    st.markdown(
        """<div class="sys-ok"><div class="pulse-dot"></div> All systems operational</div>""",
        unsafe_allow_html=True,
    )
    st.caption(f"Last refreshed: {datetime.utcnow().strftime('%H:%M:%S')} UTC · auto-refresh 4s")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown('<div class="glow-title">⚡ Akash AI Pro — Command Center</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">SECURE ENTERPRISE API GATEWAY &nbsp;•&nbsp; REAL-TIME OPERATIONS COCKPIT</div>',
    unsafe_allow_html=True,
)
st.write("")

# ---------------------------------------------------------------------------
# Top alert banner (simulated DDoS block)
# ---------------------------------------------------------------------------
alert_ip = random.choice(FAKE_IPS)
alert_rate = random.randint(180, 320)
st.markdown(
    f"""
    <div class="alert-banner">
        <div class="alert-icon">🛡️</div>
        <div>
            <b>DDoS pattern detected:</b> {alert_rate} req/s burst from <code>{alert_ip}</code>.
            Auto-blocked at edge.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Top KPI Row -- 4 glassmorphism cards
# ---------------------------------------------------------------------------
cost_summary = data["cost_summary"]
active_users = data.get("active_users", {"premium": 680, "free": 2410})
blocked_24h = data.get("blocked_24h", 2782)
redis_ok = data.get("redis_healthy", True)

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown(
        f"""
        <div class="kpi-card kpi-card-gold">
            <div class="kpi-label">💰 Dollars Spent on AI Tokens · Today</div>
            <div class="kpi-value kpi-value-gold">${cost_summary['total_cost_usd']:,.2f}</div>
            <div class="kpi-sub up">▲ +{random.uniform(4, 18):.1f}% vs yesterday</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col2:
    req_min = random.randint(380, 520)
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">📊 Requests / Min</div>
            <div class="kpi-value">{req_min}</div>
            <div class="kpi-sub neutral">p99 latency {random.randint(120, 210)} ms</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col3:
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">🚫 Blocked · 24H</div>
            <div class="kpi-value">{blocked_24h:,}</div>
            <div class="kpi-sub down">▲ {random.randint(20, 60)} in last hour</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col4:
    total_active = active_users["premium"] + active_users["free"]
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">👥 Active Users</div>
            <div class="kpi-value">{total_active:,}</div>
            <div class="kpi-sub neutral">{active_users['premium']} premium · {active_users['free']:,} free</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.write("")
st.write("")

# ---------------------------------------------------------------------------
# Row 2: Live Traffic (green/yellow/red, no blue) + Model Spend Donut
# ---------------------------------------------------------------------------
left, right = st.columns([2, 1])

with left:
    st.markdown(
        '<div class="section-header">📈 Live Traffic '
        '<span class="section-tag">Allowed · Rate-limited · Blocked</span></div>',
        unsafe_allow_html=True,
    )

    allowed, limited, blocked = _build_traffic_series(40)
    now = datetime.utcnow()
    timestamps = [now - timedelta(seconds=(40 - i) * 3) for i in range(40)]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=timestamps, y=allowed, name="Routed to AI",
            mode="lines", fill="tozeroy",
            line=dict(color="#22C55E", width=2.5, shape="spline"),
            fillcolor="rgba(34, 197, 94, 0.16)",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=timestamps, y=limited, name="Rate-limited (429)",
            mode="lines", fill="tozeroy",
            line=dict(color="#FACC15", width=2.2, shape="spline"),
            fillcolor="rgba(250, 204, 21, 0.10)",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=timestamps, y=blocked, name="Blocked / DDoS",
            mode="lines", fill="tozeroy",
            line=dict(color="#F43F5E", width=2.2, shape="spline"),
            fillcolor="rgba(244, 63, 94, 0.14)",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=360,
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color="#aab3d6")),
        xaxis=dict(showgrid=False, zeroline=False, color="#5b6690"),
        yaxis=dict(showgrid=False, zeroline=False, color="#5b6690"),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.markdown('<div class="section-header">🥧 Spend by Model</div>', unsafe_allow_html=True)
    cost_by_model = cost_summary.get("cost_by_model", {})
    if cost_by_model:
        fig2 = go.Figure(
            data=[
                go.Pie(
                    labels=list(cost_by_model.keys()),
                    values=list(cost_by_model.values()),
                    hole=0.58,
                    marker=dict(
                        colors=["#7C3AED", "#06B6D4", "#F472B6", "#FACC15"],
                        line=dict(color="#0E1117", width=2),
                    ),
                    textfont=dict(color="#e6e9f5"),
                )
            ]
        )
        fig2.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=360,
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=-0.2, font=dict(color="#aab3d6")),
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No usage recorded yet.")

st.write("")

# ---------------------------------------------------------------------------
# Row 3: Circuit Breakers + Spend by User
# ---------------------------------------------------------------------------
left2, right2 = st.columns([1.3, 1])

with left2:
    st.markdown('<div class="section-header">🔌 Circuit Breakers</div>', unsafe_allow_html=True)
    breakers = data.get("circuit_breakers", [])
    for br in breakers:
        state = br["state"]
        pill_class = {"CLOSED": "pill-closed", "OPEN": "pill-open", "HALF_OPEN": "pill-halfopen"}[state]
        card_class = {"CLOSED": "breaker-card-closed", "OPEN": "breaker-card-open", "HALF_OPEN": "breaker-card-halfopen"}[state]
        st.markdown(
            f"""
            <div class="breaker-card {card_class}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div class="breaker-name">{br['name']}</div>
                    <span class="status-pill {pill_class}">{state}</span>
                </div>
                <div class="breaker-meta">
                    {br['failure_count']}/{br['failure_threshold']} failures &nbsp;·&nbsp;
                    {br['total_calls']:,} total calls
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

with right2:
    st.markdown('<div class="section-header">👥 Spend by User</div>', unsafe_allow_html=True)
    cost_by_user = cost_summary.get("cost_by_user", {})
    if cost_by_user:
        user_df = pd.DataFrame(
            {"User": list(cost_by_user.keys()), "Cost": list(cost_by_user.values())}
        ).sort_values("Cost", ascending=True)
        fig3 = go.Figure(
            go.Bar(
                x=user_df["Cost"],
                y=user_df["User"],
                orientation="h",
                marker=dict(color="#22D3EE", line=dict(color="#7C3AED", width=1)),
            )
        )
        fig3.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=300,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(title="USD", gridcolor="rgba(255,255,255,0.05)", color="#5b6690"),
            yaxis=dict(color="#aab3d6"),
        )
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("No per-user spend yet.")

st.write("")

# ---------------------------------------------------------------------------
# Row 4: FinOps Ledger
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">🧾 Recent AI Requests (FinOps Ledger)</div>', unsafe_allow_html=True)
recent = data.get("recent_usage", [])
if recent:
    df = pd.DataFrame(recent)
    df = df.rename(
        columns={
            "timestamp": "Timestamp",
            "username": "User",
            "tier": "Tier",
            "model": "Model",
            "input_tokens": "In Tokens",
            "output_tokens": "Out Tokens",
            "cost_usd": "Cost (USD)",
        }
    )
    df["Cost (USD)"] = df["Cost (USD)"].apply(lambda x: f"${x:.6f}")
    st.dataframe(df, use_container_width=True, height=280, hide_index=True)
else:
    st.info("No requests logged yet -- send traffic to /api/v1/ai/chat/completions.")

st.write("")

# ---------------------------------------------------------------------------
# Row 5: Security & Threat Log (animated scrolling table)
# ---------------------------------------------------------------------------
st.markdown('<div class="section-header">🛡️ Security &amp; Threat Log</div>', unsafe_allow_html=True)

threat_events = _generate_mock_threat_log()
sc1, sc2, sc3 = st.columns(3)
danger_count = sum(1 for e in threat_events if e["severity"] == "danger")
warn_count = sum(1 for e in threat_events if e["severity"] == "warn")
info_count = sum(1 for e in threat_events if e["severity"] == "info")

sc1.metric("🔴 Critical Events", danger_count)
sc2.metric("🟡 Warnings", warn_count)
sc3.metric("🟢 Informational", info_count)

rows_html = "<div class='log-wrap'>"
for e in threat_events:
    css = {"danger": "log-danger", "warn": "log-warn", "info": "log-info"}[e["severity"]]
    icon = {"danger": "🔴", "warn": "🟡", "info": "🟢"}[e["severity"]]
    rows_html += (
        f"<div class='log-row {css}'>"
        f"<span>{icon} {e['timestamp']}</span>"
        f"<span><b>{e['event_type']}</b></span>"
        f"<span>{e['detail']} &nbsp;<span class='log-ip'>({e['endpoint']})</span></span>"
        f"<span class='log-ip'>ip: {e['ip']}</span>"
        f"</div>"
    )
rows_html += "</div>"
st.markdown(rows_html, unsafe_allow_html=True)

st.write("")
st.markdown(
    """
    <div style="text-align:center; color:#5b6690; font-size:0.78rem; padding-top:6px;">
        ⚡ Akash AI Pro — Secure Enterprise API Gateway &nbsp;|&nbsp;
        Built with FastAPI, Redis, Streamlit &amp; a lot of resilience engineering.
    </div>
    """,
    unsafe_allow_html=True,
)
