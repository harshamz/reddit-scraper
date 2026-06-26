import os
import re
import time
import json
from datetime import datetime, timezone
from typing import List, Dict, Tuple
from pathlib import Path

import requests
import pandas as pd
import streamlit as st
import plotly.express as px
from dotenv import load_dotenv

load_dotenv()

def _get_secret(key: str) -> str:
    val = os.getenv(key, "")
    if not val:
        try:
            val = st.secrets.get(key, "")
        except Exception:
            val = ""
    return val

SERPER_API_KEY = _get_secret("SERPER_API_KEY")
APP_USERNAME   = _get_secret("APP_USERNAME")
APP_PASSWORD   = _get_secret("APP_PASSWORD")
SERPER_URL     = "https://google.serper.dev/search"
LEADS_FILE     = Path("leads_tracker.json")
ALERTS_FILE    = Path("alerts.json")


# ──────────────────────────── Lead Tracker Storage ───────────────────────────
def load_leads() -> dict:
    # Session state is primary — survives widget reruns within a session
    if "leads_data" not in st.session_state:
        if LEADS_FILE.exists():
            try:
                st.session_state["leads_data"] = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
            except Exception:
                st.session_state["leads_data"] = {}
        else:
            st.session_state["leads_data"] = {}
    return st.session_state["leads_data"]

def save_leads(data: dict):
    st.session_state["leads_data"] = data
    try:
        LEADS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # Streamlit Cloud filesystem may be read-only; session state still holds data

def load_alerts() -> list:
    if "alerts_data" not in st.session_state:
        if ALERTS_FILE.exists():
            try:
                st.session_state["alerts_data"] = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                st.session_state["alerts_data"] = []
        else:
            st.session_state["alerts_data"] = []
    return st.session_state["alerts_data"]

def save_alerts(data: list):
    st.session_state["alerts_data"] = data
    try:
        ALERTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ──────────────────────────── Category System ────────────────────────────────
def get_category_keywords() -> Dict[str, List[str]]:
    return {
        "Pain Points": [
            "problem", "issue", "struggling", "frustrated", "annoying", "broken",
            "hate", "terrible", "awful", "worst", "failing", "difficult", "impossible",
            "bug", "error", "crash", "slow", "expensive", "overpriced", "waste",
            "disappointed", "regret", "mistake", "wrong", "bad", "horrible", "sucks",
            "fix", "solve", "help", "support", "trouble", "stuck", "confused"
        ],
        "Solution Requests": [
            "how to", "how do", "how can", "what's the best", "recommend", "suggestion",
            "advice", "help me", "looking for", "need", "want", "seeking",
            "alternative", "replacement", "instead of", "better than",
            "tutorial", "guide", "best way", "tips", "tricks"
        ],
        "Money Talk": [
            "price", "cost", "expensive", "cheap", "budget", "affordable", "money", "pay",
            "subscription", "fee", "charge", "billing", "worth it", "value", "roi",
            "save money", "deal", "discount", "free", "pricing", "quote", "salary",
            "$", "usd", "euro", "revenue", "profit"
        ],
        "Hot Discussions": [
            "trending", "viral", "popular", "everyone", "buzz", "hype", "news",
            "announcement", "update", "release", "launch", "breaking", "controversy",
            "debate", "discussion", "thoughts", "opinions", "hot take", "controversial"
        ],
        "Seeking Alternatives": [
            "alternative", "replacement", "instead of", "better than", "similar to",
            "competitor", "switch from", "migrate", "leave", "quit", "fed up",
            "tired of", "switching", "compare", "vs", "versus", "which is better"
        ],
        "Hiring / Looking for Service": [
            "hire", "hiring", "looking for", "need a", "want a", "seeking",
            "freelancer", "agency", "service", "help wanted", "job", "project",
            "work with", "collaborate", "outsource", "contractor", "budget is",
            "pay for", "willing to pay", "dm me", "contact me", "reach out"
        ]
    }

def classify_post(title: str, snippet: str) -> Tuple[str, float]:
    keywords = get_category_keywords()
    content  = f"{title.lower()} {(snippet or '').lower()}"
    scores   = {}
    for category, kws in keywords.items():
        score = 0
        for kw in kws:
            pattern = rf'\b{re.escape(kw.lower())}\b'
            score  += len(re.findall(pattern, content))
            score  += len(re.findall(pattern, title.lower())) * 2
        scores[category] = score
    if max(scores.values()) == 0:
        return "General Discussion", 0.0
    best = max(scores, key=scores.get)
    conf = min(scores[best] / max(len(content.split()) * 0.1, 1), 1.0)
    return best, round(conf, 2)

def category_color(cat: str) -> str:
    return {
        "Pain Points":               "#FF4B4B",
        "Solution Requests":         "#00D4FF",
        "Money Talk":                "#00FF88",
        "Hot Discussions":           "#FF8C00",
        "Seeking Alternatives":      "#9966FF",
        "Hiring / Looking for Service": "#FFD700",
        "General Discussion":        "#666666",
    }.get(cat, "#666666")

def category_icon(cat: str) -> str:
    return {
        "Pain Points":               "😣",
        "Solution Requests":         "❓",
        "Money Talk":                "💰",
        "Hot Discussions":           "🔥",
        "Seeking Alternatives":      "🔄",
        "Hiring / Looking for Service": "💼",
        "General Discussion":        "💬",
    }.get(cat, "💬")


# ──────────────────────────── Serper Search ───────────────────────────────────
def extract_subreddit(url: str) -> str:
    m = re.search(r'reddit\.com/r/([^/]+)', url)
    return f"r/{m.group(1)}" if m else "reddit"

def extract_username(url: str) -> str:
    # /r/sub/comments/id/title/ → no username in URL, return empty
    # We get username from snippet if possible
    return ""

TIME_FILTERS = {
    "This Month": "qdr:m",
    "This Week":  "qdr:w",
    "Today":      "qdr:d",
    "This Year":  "qdr:y",
    "All Time":   None,
}

# Platform configs — site filter + label
PLATFORMS = {
    "🟠 Reddit":         "site:reddit.com",
    "🐦 Twitter / X":    "site:x.com",
    "👥 Facebook Groups": "site:facebook.com/groups",
    "💼 LinkedIn":       "site:linkedin.com",
    "🌐 All Platforms":  "",   # no site filter
}

def detect_platform(url: str) -> str:
    if "reddit.com"   in url: return "Reddit"
    if "x.com"        in url: return "Twitter/X"
    if "twitter.com"  in url: return "Twitter/X"
    if "facebook.com" in url: return "Facebook"
    if "linkedin.com" in url: return "LinkedIn"
    return "Other"

def serper_search(query: str, num: int = 30, time_filter: str = "qdr:m",
                  platform: str = "🟠 Reddit") -> pd.DataFrame:
    if not SERPER_API_KEY:
        st.error("SERPER_API_KEY missing in .env file!")
        return pd.DataFrame()

    site_filter = PLATFORMS.get(platform, "site:reddit.com")
    full_query  = f"{site_filter} {query}".strip()
    rows, fetched, page = [], 0, 1

    while fetched < num:
        batch   = min(10, num - fetched)
        payload = {"q": full_query, "num": batch, "page": page}
        if time_filter:
            payload["tbs"] = time_filter
        try:
            r = requests.post(
                SERPER_URL,
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json=payload, timeout=15,
            )
            if r.status_code != 200:
                st.error(f"Serper API error: {r.status_code}")
                break
            data     = r.json()
            results  = data.get("organic", [])
            if not results:
                break
            for item in results:
                title   = item.get("title", "")
                link    = item.get("link", "")
                snippet = item.get("snippet", "")
                date    = item.get("date", "")
                cat, conf = classify_post(title, snippet)
                plat = detect_platform(link)
                # source label
                if plat == "Reddit":
                    source = extract_subreddit(link)
                elif plat == "Twitter/X":
                    source = "Twitter/X"
                elif plat == "Facebook":
                    source = "Facebook Group"
                elif plat == "LinkedIn":
                    source = "LinkedIn"
                else:
                    source = plat
                rows.append({
                    "Title":      title,
                    "Snippet":    snippet,
                    "Platform":   plat,
                    "Source":     source,
                    "Date":       date,
                    "Category":   cat,
                    "Confidence": conf,
                    "Link":       link,
                })
            fetched += len(results)
            if len(results) < batch:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            st.error(f"Request failed: {e}")
            break

    return pd.DataFrame(rows)


# ──────────────────────────── DM Template Generator ──────────────────────────
DM_TEMPLATES = {
    "🎯 SMM Clients": (
        "Hey! I saw your post about needing social media help. "
        "I'm an SMM specialist and I'd love to help grow your online presence. "
        "I handle content creation, scheduling, engagement, and analytics. "
        "Would you be open to a quick chat about your goals? "
        "Happy to share my portfolio and pricing. 😊"
    ),
    "🌐 Website Clients": (
        "Hi! I noticed you're looking for website help. "
        "I'm a web developer specializing in clean, fast, and affordable websites. "
        "I can build anything from landing pages to full business sites. "
        "Would love to discuss your project — happy to share examples of my work!"
    ),
    "🎬 Animation Clients": (
        "Hey! I came across your post about needing animation work. "
        "I'm a 2D/motion graphics animator and I'd love to bring your idea to life. "
        "I create explainer videos, logo animations, and more. "
        "Can I share my portfolio with you?"
    ),
    "Custom Search": (
        "Hi! I saw your post and I think I can help. "
        "I specialize in this area and have worked on similar projects. "
        "Would you be open to a quick chat? Happy to share more details!"
    ),
}

def generate_dm(title: str, snippet: str, service_type: str) -> str:
    return DM_TEMPLATES.get(service_type, DM_TEMPLATES["Custom Search"])

def dm_link(username: str) -> str:
    if username:
        return f"https://www.reddit.com/message/compose?to={username}"
    return ""


# ──────────────────────────── SMM Presets ────────────────────────────────────
SMM_PRESETS = {
    "🎯 SMM Clients": [
        "need social media manager",
        "hire social media manager",
        "looking for SMM",
        "manage my social media",
        "grow my instagram",
        "social media help needed",
        "content creator needed",
    ],
    "🌐 Website Clients": [
        "need a website built",
        "looking for web developer",
        "hire web designer",
        "build me a website",
        "website for my business",
        "affordable website developer",
        "need website help",
    ],
    "🎬 Animation Clients": [
        "need animator",
        "hire animator",
        "need explainer video",
        "logo animation needed",
        "motion graphics needed",
        "animated video for business",
        "2d animation needed",
    ],
}

STATUS_OPTIONS  = ["🆕 New", "📨 Contacted", "💬 Replied", "✅ Converted", "❌ Not Interested"]
STATUS_COLORS   = {
    "🆕 New":            "#666666",
    "📨 Contacted":      "#00D4FF",
    "💬 Replied":        "#FF8C00",
    "✅ Converted":      "#00FF88",
    "❌ Not Interested": "#FF4B4B",
}


# ──────────────────────────── CSS ─────────────────────────────────────────────
def apply_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root {
        --primary:#FF4B4B; --bg:#0E1117; --surface:#1A1D23;
        --blue:#00D4FF; --green:#00FF88; --border:#333644; --text:#FAFAFA;
    }
    .stApp { background:linear-gradient(135deg,#0E1117 0%,#1a1d29 100%); font-family:'Inter',sans-serif; }
    .main-header { background:linear-gradient(90deg,#FF4B4B,#FF6B6B);
        -webkit-background-clip:text; -webkit-text-fill-color:transparent;
        font-size:2.2rem; font-weight:700; display:inline-block; }
    .sec-header { color:#FAFAFA; font-size:1.4rem; font-weight:600;
        border-bottom:2px solid #00D4FF; padding-bottom:0.4rem; margin:1rem 0 0.8rem 0; }
    .stButton>button { background:linear-gradient(45deg,#FF4B4B,#FF6B6B);
        border:none; border-radius:8px; color:white; font-weight:600;
        box-shadow:0 4px 15px rgba(255,75,75,.3); transition:all .3s; }
    .stButton>button:hover { transform:translateY(-2px); }
    .lead-card { background:#1A1D23; border:1px solid #333644; border-radius:10px;
        padding:1rem; margin:0.5rem 0; }
    a { color:#00D4FF !important; }
    </style>
    """, unsafe_allow_html=True)


# ──────────────────────────── UI Components ───────────────────────────────────
def show_stats(df: pd.DataFrame):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📊 Total Results", len(df))
    src_col = 'Source' if 'Source' in df.columns else ('Subreddit' if 'Subreddit' in df.columns else None)
    c2.metric("📂 Sources", df[src_col].nunique() if src_col else 0)
    hiring = len(df[df['Category'] == 'Hiring / Looking for Service']) if 'Category' in df.columns else 0
    c3.metric("💼 Hiring Posts", hiring)
    pain = len(df[df['Category'] == 'Pain Points']) if 'Category' in df.columns else 0
    c4.metric("😣 Pain Points", pain)

def show_categories(df: pd.DataFrame):
    if df.empty or 'Category' not in df.columns:
        return
    st.markdown('<div class="sec-header">🏷️ Content Categories</div>', unsafe_allow_html=True)
    counts = df['Category'].value_counts()
    cols   = st.columns(min(len(counts), 4))
    for i, (cat, count) in enumerate(counts.items()):
        color = category_color(cat)
        icon  = category_icon(cat)
        pct   = count / len(df) * 100
        with cols[i % min(len(counts), 4)]:
            st.markdown(f"""
            <div style="background:linear-gradient(135deg,{color}20,{color}10);
                border-left:4px solid {color};border-radius:8px;padding:0.8rem;
                margin:0.3rem 0;text-align:center;">
                <div style="font-size:1.6rem">{icon}</div>
                <div style="font-weight:600;color:{color};font-size:0.8rem">{cat}</div>
                <div style="font-size:1.2rem;font-weight:bold">{count}</div>
                <div style="font-size:0.75rem;opacity:0.8">{pct:.1f}%</div>
            </div>""", unsafe_allow_html=True)
    fig = px.pie(
        values=counts.values, names=counts.index, color=counts.index,
        color_discrete_map={c: category_color(c) for c in counts.index},
        title="Category Distribution",
    )
    fig.update_layout(plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)', font_color='white')
    st.plotly_chart(fig, use_container_width=True)

def show_results_with_dm(df: pd.DataFrame, service_type: str, name: str):
    st.markdown('<div class="sec-header">📋 Results + DM Actions</div>', unsafe_allow_html=True)

    leads_data = load_leads()

    for i, row in df.iterrows():
        link    = row.get("Link", "")
        title   = row.get("Title", "")
        snippet = row.get("Snippet", "")
        cat     = row.get("Category", "")
        sub     = row.get("Source", row.get("Subreddit", ""))
        plat    = row.get("Platform", "")
        date    = row.get("Date", "")
        color   = category_color(cat)
        icon    = category_icon(cat)

        # Lead status from tracker
        lead_id     = link
        lead_info   = leads_data.get(lead_id, {})
        lead_status = lead_info.get("status", "🆕 New")
        lead_note   = lead_info.get("note", "")

        with st.container():
            st.markdown(f"""
            <div class="lead-card">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                    <div style="flex:1">
                        <span style="background:{color}30;color:{color};padding:2px 8px;
                            border-radius:4px;font-size:0.75rem;font-weight:600;">
                            {icon} {cat}
                        </span>
                        <span style="color:#666;font-size:0.75rem;margin-left:8px;">{plat} • {sub} • {date}</span>
                        <h4 style="margin:0.4rem 0 0.3rem 0;color:#FAFAFA;">
                            <a href="{link}" target="_blank" style="text-decoration:none;color:#FAFAFA;">
                                {title}
                            </a>
                        </h4>
                        <p style="color:#A6A6A6;font-size:0.85rem;margin:0;">{snippet}</p>
                    </div>
                </div>
            </div>""", unsafe_allow_html=True)

            col1, col2, col3, col4 = st.columns([2, 2, 2, 2])

            # DM Template button
            with col1:
                dm_msg = generate_dm(title, snippet, service_type)
                st.code(dm_msg[:80] + "...", language=None)
                st.button("📋 Copy Message", key=f"copy_{i}",
                          help=dm_msg,
                          on_click=lambda m=dm_msg: st.session_state.update({"copied_msg": m}))

            # DM Link
            with col2:
                st.markdown(f"[🔗 Open Post]({link})", unsafe_allow_html=False)
                st.markdown(f"*Visit {plat} to contact them*", unsafe_allow_html=False)

            # Lead Status
            with col3:
                new_status = st.selectbox(
                    "Status", STATUS_OPTIONS,
                    index=STATUS_OPTIONS.index(lead_status) if lead_status in STATUS_OPTIONS else 0,
                    key=f"status_{i}",
                )

            # Note
            with col4:
                new_note = st.text_input("Note", value=lead_note, key=f"note_{i}",
                                         placeholder="e.g., replied, budget $200")

            # Save to tracker
            if new_status != lead_status or new_note != lead_note:
                leads_data[lead_id] = {
                    "title":   title,
                    "status":  new_status,
                    "note":    new_note,
                    "link":    link,
                    "saved_at": datetime.now().isoformat(),
                }
                save_leads(leads_data)

            # Add to tracker button
            if st.button("➕ Save Lead", key=f"save_{i}"):
                leads_data[lead_id] = {
                    "title":    title,
                    "status":   new_status,
                    "note":     new_note,
                    "link":     link,
                    "snippet":  snippet,
                    "saved_at": datetime.now().isoformat(),
                }
                save_leads(leads_data)
                st.success(f"✅ Lead saved!")

            st.divider()

    # Show copied message
    if "copied_msg" in st.session_state:
        st.markdown('<div class="sec-header">📋 DM Message (Copy Below)</div>', unsafe_allow_html=True)
        st.text_area("Message:", value=st.session_state["copied_msg"], height=150, key="msg_display")

    # Download
    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("📥 Download CSV", df.to_csv(index=False).encode(),
                           f"{name}.csv", "text/csv", use_container_width=True)
    with c2:
        st.download_button("📥 Download JSON", df.to_json(orient='records').encode(),
                           f"{name}.json", "application/json", use_container_width=True)


def show_lead_tracker():
    st.markdown('<div class="sec-header">📊 Lead Tracker</div>', unsafe_allow_html=True)
    leads_data = load_leads()

    if not leads_data:
        st.info("No leads saved yet — search first and click '➕ Save Lead' to add them here.")
        return

    # Stats
    statuses = [v.get("status", "🆕 New") for v in leads_data.values()]
    c1, c2, c3, c4, c5 = st.columns(5)
    for col, status in zip([c1, c2, c3, c4, c5], STATUS_OPTIONS):
        count = statuses.count(status)
        color = STATUS_COLORS[status]
        col.markdown(f"""
        <div style="background:{color}20;border-left:3px solid {color};
            border-radius:6px;padding:0.6rem;text-align:center;">
            <div style="font-weight:bold;font-size:1.3rem">{count}</div>
            <div style="font-size:0.75rem;color:{color}">{status}</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # Filter by status
    filter_status = st.selectbox("Filter by status:", ["All"] + STATUS_OPTIONS)

    for lead_id, info in leads_data.items():
        if filter_status != "All" and info.get("status") != filter_status:
            continue
        status = info.get("status", "🆕 New")
        color  = STATUS_COLORS.get(status, "#666666")
        st.markdown(f"""
        <div style="background:#1A1D23;border-left:4px solid {color};
            border-radius:8px;padding:0.8rem;margin:0.4rem 0;">
            <div style="display:flex;justify-content:space-between;">
                <span style="color:{color};font-weight:600;font-size:0.85rem">{status}</span>
                <span style="color:#666;font-size:0.75rem">{info.get('saved_at','')[:10]}</span>
            </div>
            <a href="{info.get('link','')}" target="_blank"
               style="color:#FAFAFA;font-weight:500;text-decoration:none;">
               {info.get('title','')[:80]}
            </a>
            <div style="color:#A6A6A6;font-size:0.8rem;margin-top:0.3rem">
                📝 {info.get('note','—')}
            </div>
        </div>""", unsafe_allow_html=True)

    # Export tracker
    st.markdown("---")
    tracker_df = pd.DataFrame([
        {"Title": v.get("title",""), "Status": v.get("status",""),
         "Note": v.get("note",""), "Link": v.get("link",""), "Date": v.get("saved_at","")}
        for v in leads_data.values()
    ])
    st.download_button("📥 Export Tracker CSV", tracker_df.to_csv(index=False).encode(),
                       "leads_tracker.csv", "text/csv", use_container_width=True)

    if st.button("🗑️ Clear All Leads", type="secondary"):
        save_leads({})
        st.rerun()


def show_daily_alerts():
    st.markdown('<div class="sec-header">🔔 Daily Auto Alerts</div>', unsafe_allow_html=True)
    alerts = load_alerts()

    st.markdown("**Save keywords here — new results will appear every time you open the app.**")

    with st.form("add_alert"):
        col1, col2, col3 = st.columns([3, 1, 1])
        with col1:
            new_kw = st.text_input("Keyword", placeholder="need social media manager")
        with col2:
            new_sub = st.text_input("Subreddit", placeholder="forhire (optional)")
        with col3:
            new_time = st.selectbox("Time", list(TIME_FILTERS.keys()), index=1)
        submitted = st.form_submit_button("➕ Add Alert", use_container_width=True)
        if submitted and new_kw.strip():
            alerts.append({
                "keyword":    new_kw.strip(),
                "subreddit":  new_sub.strip().lstrip("r/"),
                "time_label": new_time,
                "added_at":   datetime.now().isoformat(),
            })
            save_alerts(alerts)
            st.success(f"✅ Alert added for: '{new_kw}'")
            st.rerun()

    if not alerts:
        st.info("No alerts yet — add one above.")
        return

    st.markdown("---")
    st.markdown("**Saved Alerts:**")
    for i, alert in enumerate(alerts):
        col1, col2 = st.columns([5, 1])
        with col1:
            sub_txt = f" in r/{alert['subreddit']}" if alert.get("subreddit") else " (all platforms)"
            st.markdown(f"🔔 **{alert['keyword']}**{sub_txt} — _{alert['time_label']}_")
        with col2:
            if st.button("❌", key=f"del_alert_{i}"):
                alerts.pop(i)
                save_alerts(alerts)
                st.rerun()

    st.markdown("---")
    if st.button("🚀 Run All Alerts Now", use_container_width=True):
        all_dfs = []
        prog = st.progress(0, "Running alerts...")
        for i, alert in enumerate(alerts):
            prog.progress(int(i / len(alerts) * 100), f"Checking: {alert['keyword']}")
            q   = f"r/{alert['subreddit']} {alert['keyword']}" if alert.get("subreddit") else alert['keyword']
            tbs = TIME_FILTERS.get(alert["time_label"])
            df  = serper_search(q, num=10, time_filter=tbs)
            if not df.empty:
                df.insert(0, "Alert Keyword", alert['keyword'])
                all_dfs.append(df)
            time.sleep(0.5)
        prog.progress(100, "Done!")
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True).drop_duplicates(subset=["Link"])
            st.success(f"✅ {len(combined)} new posts found!")
            show_cols = [c for c in ['Alert Keyword','Title','Platform','Source','Date','Category','Link'] if c in combined.columns]
            st.dataframe(combined[show_cols],
                         use_container_width=True, height=400)
            st.download_button("📥 Download Results", combined.to_csv(index=False).encode(),
                               "alert_results.csv", "text/csv")
        else:
            st.warning("No new results found.")


# ──────────────────────────── Main ───────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="Reddit SMM Lead Finder",
        page_icon="reddit-logo.png",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_css()

    try:
        logo = __import__('base64').b64encode(open('reddit-logo.png', 'rb').read()).decode()
        st.markdown(f'''
        <div style="display:flex;align-items:center;justify-content:center;margin-bottom:1rem;">
            <img src="data:image/png;base64,{logo}" width="45" style="margin-right:12px;">
            <h1 class="main-header" style="margin:0;">Reddit SMM Lead Finder</h1>
        </div>''', unsafe_allow_html=True)
    except Exception:
        st.markdown('<h1 class="main-header">Reddit SMM Lead Finder</h1>', unsafe_allow_html=True)

    st.markdown(
        '<p style="text-align:center;color:#A6A6A6;margin-top:-0.8rem;">'
        'Find SMM • Website • Animation clients — DM Templates • Lead Tracker • Auto Alerts</p>',
        unsafe_allow_html=True,
    )

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs(["🔍 Search & Find Leads", "📊 Lead Tracker", "🔔 Daily Alerts"])

    # ── Tab 1: Search ─────────────────────────────────────────────────────────
    with tab1:
        with st.sidebar:
            st.markdown("### 🎯 Client Type")
            preset = st.selectbox("Service:", ["Custom Search"] + list(SMM_PRESETS.keys()))
            st.markdown("---")
            st.markdown("### ⚙️ Options")
            num_results = st.slider("Max Results", 10, 100, 20, 10)
            show_cats   = st.checkbox("Show Categories", value=True)
            st.markdown("---")
            st.markdown("### 📌 Best Subreddits")
            st.markdown("- `r/forhire`\n- `r/slavelabour`\n- `r/hiring`\n"
                        "- `r/smallbusiness`\n- `r/Entrepreneur`\n- `r/socialmedia`")

        st.markdown('<div class="sec-header">🔍 Search Reddit for Clients</div>', unsafe_allow_html=True)

        if preset != "Custom Search":
            st.info(f"Preset: **{preset}**")
            kw_cols = st.columns(min(len(SMM_PRESETS[preset]), 4))
            sel_kw  = st.session_state.get("preset_kw", SMM_PRESETS[preset][0])
            for i, kw in enumerate(SMM_PRESETS[preset]):
                if kw_cols[i % 4].button(kw, key=f"pkw_{i}"):
                    st.session_state["preset_kw"] = kw
                    sel_kw = kw
            default_kw = sel_kw
        else:
            default_kw = ""

        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            keywords_input = st.text_area(
                "**Keywords** (one per line — or separate with commas)",
                value=default_kw,
                placeholder="need social media manager\nhire animator\nbuild me a website",
                height=110,
            )
        with c2:
            time_label = st.selectbox("**Time Filter**", list(TIME_FILTERS.keys()), index=0)
        with c3:
            platform = st.selectbox("**Platform**", list(PLATFORMS.keys()), index=0)

        # Subreddit filter — only shown for Reddit platform
        sub_filter = ""
        if platform == "🟠 Reddit":
            sub_filter = st.text_input(
                "**Subreddit filter** (optional)",
                placeholder="forhire  (leave blank = all of Reddit)",
            )

        if st.button("🚀 Find Clients", use_container_width=True):
            raw           = keywords_input.replace(",", "\n")
            keywords_list = [k.strip() for k in raw.splitlines() if k.strip()]

            if not keywords_list:
                st.warning("Please enter at least one keyword.")
            else:
                tbs      = TIME_FILTERS[time_label]
                sub      = sub_filter.strip().lstrip("r/")
                all_dfs  = []
                progress = st.progress(0, "Searching...")

                for i, kw in enumerate(keywords_list):
                    # Apply subreddit filter for Reddit platform
                    q = f"r/{sub} {kw}" if (sub and platform == "🟠 Reddit") else kw
                    progress.progress(
                        int(i / len(keywords_list) * 100),
                        f"Searching {platform}: '{kw}' ({i+1}/{len(keywords_list)})",
                    )
                    df_kw = serper_search(q, num=num_results, time_filter=tbs, platform=platform)
                    if not df_kw.empty:
                        df_kw.insert(0, "Keyword", kw)
                        all_dfs.append(df_kw)
                    time.sleep(0.5)

                progress.progress(100, "Done!")

                if not all_dfs:
                    df = pd.DataFrame()
                else:
                    df = pd.concat(all_dfs, ignore_index=True).drop_duplicates(subset=["Link"])

                if df.empty:
                    st.error("No results found — try different keywords.")
                else:
                    kw_label = keywords_list[0] if len(keywords_list) == 1 else "multi_keyword"
                    # Store results in session state so widget interactions don't wipe them
                    st.session_state["search_df"]      = df
                    st.session_state["search_preset"]  = preset
                    st.session_state["search_kw_label"] = kw_label.replace(" ", "_")
                    st.session_state["show_cats"]      = show_cats

        # Render results from session state (survives widget clicks / reruns)
        if "search_df" in st.session_state and not st.session_state["search_df"].empty:
            df       = st.session_state["search_df"]
            _preset  = st.session_state.get("search_preset", preset)
            _label   = st.session_state.get("search_kw_label", "results")
            _cats    = st.session_state.get("show_cats", True)
            st.success(f"✅ **{len(df)}** posts found")
            show_stats(df)
            if _cats:
                show_categories(df)
            show_results_with_dm(df, _preset, _label)

    # ── Tab 2: Lead Tracker ───────────────────────────────────────────────────
    with tab2:
        show_lead_tracker()

    # ── Tab 3: Daily Alerts ───────────────────────────────────────────────────
    with tab3:
        show_daily_alerts()


# ──────────────────────────── Login Gate ─────────────────────────────────────
def check_login() -> bool:
    if st.session_state.get("authenticated"):
        return True

    # If no credentials configured, allow open access
    if not APP_USERNAME or not APP_PASSWORD:
        return True

    st.set_page_config(
        page_title="Login — Reddit SMM Lead Finder",
        page_icon="🔒",
        layout="centered",
    )
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
    .stApp { background:linear-gradient(135deg,#0E1117 0%,#1a1d29 100%); font-family:'Inter',sans-serif; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("<br><br>", unsafe_allow_html=True)
    col = st.columns([1, 2, 1])[1]
    with col:
        st.markdown("## 🔒 Login Required")
        st.markdown("Enter your credentials to access the app.")
        username = st.text_input("Username", placeholder="Enter username")
        password = st.text_input("Password", type="password", placeholder="Enter password")
        if st.button("Login", use_container_width=True):
            if username == APP_USERNAME and password == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect username or password.")
    return False


if __name__ == "__main__":
    if check_login():
        main()
