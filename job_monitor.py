"""
Bay Area Startup Job Monitor
============================
Monitors Wellfound, Greenhouse (multi-board), and YC Work at a Startup
for Data Scientist / Data Analyst / AI Engineer / ML Engineer roles.

Runs standalone — no Claude API required.
"""

import os
import json
import time
import sqlite3
import logging
import smtplib
import hashlib
import random
import sys
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo
from typing import Optional
import re

import requests
from bs4 import BeautifulSoup
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("job_monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants & Config
# ─────────────────────────────────────────────
PST = ZoneInfo("America/Los_Angeles")
DB_PATH = "job_monitor.db"
LOOKBACK_MINUTES = 30
LEARNING_THRESHOLD = 30     # jobs before schedule learning kicks in

TARGET_KEYWORDS = [
    "data scientist",
    "data analyst",
    "ai engineer",
    "machine learning engineer",
    "ml engineer",
]

LOCATION_ALLOW = [
    "san francisco",
    "bay area",
    "sf",
    "south bay",
    "east bay",
    "mountain view",
    "palo alto",
    "san jose",
    "remote",
    "remote us",
    "remote (us)",
    "united states",
]

# Seniority blacklist — skip explicit senior roles
SENIORITY_BLOCK = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|director|vp|head of|manager)\b",
    re.IGNORECASE,
)

# User-agent pool for rotation
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ─────────────────────────────────────────────
# Greenhouse boards — Bay Area startups
# ─────────────────────────────────────────────
# Slug = the part after boards.greenhouse.io/
GREENHOUSE_BOARDS = [
    "brex", "rippling", "scale", "cohere", "adept", "notion",
    "retool", "vercel", "linear", "loom", "benchling", "gusto",
    "lattice", "persona", "anyscale", "together", "perplexity",
    "mistral", "runway", "weights", "elevenlabs", "cognition",
    "lambda", "modal", "baseten", "clarifai", "huggingface",
    "replit", "codeium", "cursor", "hex", "preset", "deepgram",
    "assemblyai", "openai", "anthropic", "databricks", "confluent",
    "dbt", "fivetran", "hightouch", "census", "rudderstack",
]

# ─────────────────────────────────────────────
# Credentials (from env)
# ─────────────────────────────────────────────
TWILIO_SID      = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM     = os.environ.get("TWILIO_FROM_NUMBER", "")   # whatsapp:+14155238886
TWILIO_TO       = os.environ.get("TWILIO_TO_NUMBER", "")     # whatsapp:+1XXXXXXXXXX

SMTP_HOST       = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER       = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD   = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO        = os.environ.get("EMAIL_TO", SMTP_USER)


# ═════════════════════════════════════════════
# DATABASE
# ═════════════════════════════════════════════

def init_database() -> sqlite3.Connection:
    """
    Create (or open) the SQLite database with two tables:
      - seen_jobs : deduplication + job history
      - job_events: timestamps of found jobs for schedule learning
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            company     TEXT,
            url         TEXT,
            source      TEXT,
            location    TEXT,
            posted_at   TEXT,
            notified_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            found_at    TEXT,          -- ISO timestamp UTC
            hour_pst    INTEGER,       -- 0–23
            weekday     INTEGER        -- 0=Mon … 6=Sun
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule_weights (
            weekday     INTEGER,
            hour_pst    INTEGER,
            weight      REAL DEFAULT 1.0,
            PRIMARY KEY (weekday, hour_pst)
        )
    """)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)
    return conn


def is_seen(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE id = ?", (job_id,)
    ).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO seen_jobs
           (id, title, company, url, source, location, posted_at, notified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["id"], job["title"], job["company"], job["url"],
            job["source"], job.get("location", ""), job.get("posted_at", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.execute(
        """INSERT INTO job_events (found_at, hour_pst, weekday) VALUES (?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            datetime.now(PST).hour,
            datetime.now(PST).weekday(),
        ),
    )
    conn.commit()


def total_jobs_found(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0]


# ═════════════════════════════════════════════
# SMART SCHEDULING
# ═════════════════════════════════════════════

# Base schedule (weekday → list of (start_hour, end_hour, interval_min))
# GitHub Actions runs cron every 20 min; should_run_now() gates execution.
BASE_SCHEDULE = {
    0: [(9, 11, 60), (12, 17, 180), (19, 20, 999)],   # Monday
    1: [(8, 12, 20), (12, 17, 180), (19, 20, 999)],   # Tuesday
    2: [(8, 12, 20), (12, 17, 180), (19, 20, 999)],   # Wednesday
    3: [(8, 12, 20), (12, 17, 180), (19, 20, 999)],   # Thursday
    4: [(9, 11, 60), (12, 17, 180), (19, 20, 999)],   # Friday
    5: [],                                              # Saturday
    6: [],                                              # Sunday
}


def should_run_now(conn: Optional[sqlite3.Connection] = None) -> bool:
    """
    Returns True only when the current PST time falls inside an active
    scheduling window.  If learning is enabled and we have enough data,
    learned weights can suppress a window by marking it low-priority.
    """
    now = datetime.now(PST)
    weekday = now.weekday()   # 0 = Monday
    hour    = now.hour
    minute  = now.minute

    windows = BASE_SCHEDULE.get(weekday, [])
    if not windows:
        log.info("Weekend — no runs scheduled today.")
        return False

    for (start, end, interval) in windows:
        if start <= hour < end:
            # Check if this is the right minute within the interval
            # (GitHub cron fires every 20 min; we gate finer intervals here)
            if interval <= 20:
                return True   # every GH Actions run counts
            # For intervals > 20 min: only run at the start of each interval
            mins_since_start = (hour - start) * 60 + minute
            if mins_since_start % interval < 20:
                # Learning override: suppress if weight < 0.3
                if conn and _learned_weight(conn, weekday, hour) < 0.3:
                    log.info(
                        "Learning suppressed run at weekday=%d hour=%d (low weight)",
                        weekday, hour,
                    )
                    return False
                return True

    log.info("Outside all active windows for weekday=%d hour=%d — skipping.", weekday, hour)
    return False


def _learned_weight(conn: sqlite3.Connection, weekday: int, hour: int) -> float:
    row = conn.execute(
        "SELECT weight FROM schedule_weights WHERE weekday=? AND hour_pst=?",
        (weekday, hour),
    ).fetchone()
    return row[0] if row else 1.0


def learn_and_adjust_schedule(conn: sqlite3.Connection) -> None:
    """
    After LEARNING_THRESHOLD jobs have been found, compute which
    (weekday, hour) slots produced the most jobs and boost/suppress weights.
    Called once per run; only updates weights when ≥ LEARNING_THRESHOLD jobs exist.
    """
    total = total_jobs_found(conn)
    if total < LEARNING_THRESHOLD:
        log.info("Learning: only %d jobs found so far (need %d) — skipping.", total, LEARNING_THRESHOLD)
        return

    rows = conn.execute(
        "SELECT weekday, hour_pst, COUNT(*) as cnt FROM job_events GROUP BY weekday, hour_pst"
    ).fetchall()

    if not rows:
        return

    max_cnt = max(r[2] for r in rows)
    for weekday, hour, cnt in rows:
        weight = round(cnt / max_cnt, 3)   # normalise 0..1
        conn.execute(
            """INSERT INTO schedule_weights (weekday, hour_pst, weight)
               VALUES (?, ?, ?)
               ON CONFLICT(weekday, hour_pst) DO UPDATE SET weight=excluded.weight""",
            (weekday, hour, weight),
        )
    conn.commit()
    log.info("Learning: schedule weights updated from %d data points.", total)


# Monthly budget estimate (printed at startup for reference)
def _print_budget_estimate() -> None:
    """
    Tue/Wed/Thu: 8am–12pm every 20 min = 12 runs × 3 days = 36/week
    Mon/Fri    : 9am–11am every 60 min =  2 runs × 2 days =  4/week
    Weekdays   : 12pm–5pm every 180 min= ~2 runs × 5 days = 10/week
    Weekdays   : 7pm check once        =  1 run  × 5 days =  5/week
    Total/week : ≈ 55 runs  × ~1.5 min/run ≈ 83 min/week
    Monthly    : ≈ 83 × 4.3 ≈ 357 minutes  ← well under 1,000
    """
    log.info(
        "Estimated monthly GitHub Actions runtime: ~357 min "
        "(budget: 1,000 min, limit: 2,000 min)"
    )


# ═════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════

def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=_headers(), timeout=20, **kwargs)
        r.raise_for_status()
        return r
    except requests.exceptions.HTTPError as e:
        log.warning("HTTP %s for %s", e.response.status_code, url)
    except requests.exceptions.ConnectionError:
        log.warning("Connection error: %s", url)
    except requests.exceptions.Timeout:
        log.warning("Timeout: %s", url)
    except Exception as e:
        log.warning("GET failed (%s): %s", type(e).__name__, url)
    return None


def _make_id(company: str, title: str, url: str) -> str:
    raw = f"{company.lower()}|{title.lower()}|{url.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _matches_keyword(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TARGET_KEYWORDS)


def _matches_level(title: str) -> bool:
    """Return False for explicit senior/lead/staff roles."""
    return not SENIORITY_BLOCK.search(title)


def _matches_location(location: str) -> bool:
    if not location:
        return True   # unknown → include
    loc = location.lower()
    return any(kw in loc for kw in LOCATION_ALLOW)


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    ts = ts.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _is_recent(posted_at: Optional[datetime]) -> bool:
    if posted_at is None:
        return True   # can't verify — include
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return posted_at >= cutoff


def _filter_jobs(raw: list[dict]) -> list[dict]:
    out = []
    for job in raw:
        if not _matches_keyword(job.get("title", "")):
            continue
        if not _matches_level(job.get("title", "")):
            continue
        if not _matches_location(job.get("location", "")):
            continue
        if not _is_recent(_parse_iso(job.get("posted_at", ""))):
            continue
        out.append(job)
    return out


# ═════════════════════════════════════════════
# SCRAPERS
# ═════════════════════════════════════════════

# ── 1. Y Combinator Work at a Startup ────────────────────────────────────────

YC_SEARCH_URL = "https://www.workatastartup.com/jobs/search"

def fetch_ycombinator() -> list[dict]:
    """
    Uses the JSON search endpoint exposed by workatastartup.com.
    Queries each role keyword separately; deduplicates by URL.
    """
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for kw in ["data scientist", "data analyst", "AI engineer", "machine learning engineer"]:
        params = {"q": kw, "remote": "only_remote_ok"}
        r = _get(YC_SEARCH_URL, params=params)
        if not r:
            continue
        try:
            data = r.json()
        except Exception as e:
            log.warning("YC JSON parse error (%s): %s", kw, e)
            continue

        for item in data.get("jobs", []):
            jid    = item.get("id", "")
            url    = f"https://www.workatastartup.com/jobs/{jid}"
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title   = item.get("title", "")
            company = (item.get("company") or {}).get("name", "Unknown")
            locs    = item.get("locations") or []
            remote  = item.get("remote", "")
            location = ", ".join(locs) if locs else remote
            posted_raw = item.get("created_at", "") or item.get("updated_at", "")

            jobs.append(dict(
                id        = _make_id(company, title, url),
                title     = title,
                company   = company,
                location  = location,
                url       = url,
                source    = "YC Work at a Startup",
                posted_at = posted_raw,
            ))

        time.sleep(random.uniform(0.5, 1.2))   # polite delay between keyword calls

    result = _filter_jobs(jobs)
    log.info("YC: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ── 2. Wellfound (AngelList Talent) ──────────────────────────────────────────

WELLFOUND_BASE = "https://wellfound.com"

def fetch_wellfound() -> list[dict]:
    """
    Wellfound is server-side rendered via Next.js.
    We parse the __NEXT_DATA__ JSON blob embedded in the HTML.
    NOTE: Wellfound occasionally changes their internal data shape;
    adjust the key path below if jobs stop appearing.
    """
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    search_terms = [
        ("data-scientist",          "san-francisco-ca"),
        ("data-analyst",            "san-francisco-ca"),
        ("artificial-intelligence", "san-francisco-ca"),
        ("machine-learning",        "san-francisco-ca"),
        ("data-scientist",          ""),   # remote
    ]

    for role, location_slug in search_terms:
        url = f"{WELLFOUND_BASE}/role/{role}"
        if location_slug:
            url += f"?location={location_slug}"

        r = _get(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "lxml")
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script_tag or not script_tag.string:
            log.warning("Wellfound: __NEXT_DATA__ missing for %s", role)
            continue

        try:
            nd = json.loads(script_tag.string)
        except Exception as e:
            log.warning("Wellfound JSON parse error: %s", e)
            continue

        # Navigate the Next.js page props — adjust if Wellfound restructures
        try:
            listings = (
                nd["props"]["pageProps"]
                   .get("searchResults", {})
                   .get("results", [])
            )
        except (KeyError, TypeError, AttributeError):
            listings = []

        # Fallback: look for jobListings anywhere in the tree
        if not listings:
            raw_str = json.dumps(nd)
            try:
                # Grab first occurrence of a "jobs" array
                match = re.search(r'"jobListings"\s*:\s*(\[.*?\])', raw_str, re.DOTALL)
                if match:
                    listings = json.loads(match.group(1))
            except Exception:
                pass

        for item in listings:
            slug      = item.get("slug", "") or item.get("jobUrl", "")
            job_url   = slug if slug.startswith("http") else f"{WELLFOUND_BASE}{slug}"
            if job_url in seen_urls or not slug:
                continue
            seen_urls.add(job_url)

            title    = item.get("title", "") or item.get("jobTitle", "")
            startup  = item.get("startup", {}) or item.get("company", {}) or {}
            company  = startup.get("name", "Unknown")
            loc_list = item.get("locationNames", []) or item.get("locations", [])
            location = ", ".join(loc_list) if isinstance(loc_list, list) else str(loc_list)
            posted_raw = item.get("liveStartAt", "") or item.get("createdAt", "")

            # liveStartAt is often a Unix timestamp int
            if isinstance(posted_raw, (int, float)):
                posted_raw = datetime.fromtimestamp(posted_raw, tz=timezone.utc).isoformat()

            jobs.append(dict(
                id        = _make_id(company, title, job_url),
                title     = title,
                company   = company,
                location  = location,
                url       = job_url,
                source    = "Wellfound",
                posted_at = str(posted_raw),
            ))

        time.sleep(random.uniform(1.0, 2.0))

    result = _filter_jobs(jobs)
    log.info("Wellfound: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ── 3. Greenhouse (public ATS API) ───────────────────────────────────────────

GH_API_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

def fetch_greenhouse() -> list[dict]:
    """
    Greenhouse exposes a fully public JSON API per company board.
    No authentication required.  We iterate over GREENHOUSE_BOARDS.
    """
    jobs: list[dict] = []

    for board in GREENHOUSE_BOARDS:
        r = _get(GH_API_TEMPLATE.format(board=board), params={"content": "true"})
        if not r:
            continue
        try:
            data = r.json()
        except Exception as e:
            log.warning("Greenhouse %s JSON error: %s", board, e)
            continue

        for item in data.get("jobs", []):
            title      = item.get("title", "")
            job_url    = item.get("absolute_url", "")
            location   = (item.get("location") or {}).get("name", "")
            posted_raw = item.get("updated_at") or item.get("created_at", "")
            company    = board.replace("-", " ").title()

            jobs.append(dict(
                id        = _make_id(company, title, job_url),
                title     = title,
                company   = company,
                location  = location,
                url       = job_url,
                source    = f"Greenhouse ({board})",
                posted_at = posted_raw,
            ))

        time.sleep(random.uniform(0.3, 0.8))

    result = _filter_jobs(jobs)
    log.info("Greenhouse: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ═════════════════════════════════════════════
# NOTIFICATIONS
# ═════════════════════════════════════════════

def send_whatsapp(job: dict) -> bool:
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.warning("Twilio creds missing — skipping WhatsApp: %s", job["title"])
        return False

    body = (
        f"*New Job Alert*\n"
        f"*{job['title']}*\n"
        f"Company: {job['company']}\n"
        f"Location: {job.get('location') or 'Location not listed'}\n"
        f"Source: {job['source']}\n"
        f"Link: {job['url']}"
    )

    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_FROM,
            to=TWILIO_TO,
            body=body,
        )
        log.info("WhatsApp sent (SID: %s): %s @ %s", msg.sid, job["title"], job["company"])
        return True
    except Exception as e:
        log.error("WhatsApp failed for %s @ %s: %s", job["title"], job["company"], e)
        return False


def send_email(job: dict) -> bool:
    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning("SMTP creds missing — skipping email: %s", job["title"])
        return False

    subject = f"[Job Alert] {job['title']} @ {job['company']}"
    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px;">
  <h2 style="color:#4f46e5;">New Job Match!</h2>
  <table style="border-collapse:collapse;width:100%;">
    <tr><td style="padding:8px;font-weight:bold;width:120px;">Role</td>
        <td style="padding:8px;">{job['title']}</td></tr>
    <tr style="background:#f9fafb;"><td style="padding:8px;font-weight:bold;">Company</td>
        <td style="padding:8px;">{job['company']}</td></tr>
    <tr><td style="padding:8px;font-weight:bold;">Location</td>
        <td style="padding:8px;">{job.get('location') or 'Not listed'}</td></tr>
    <tr style="background:#f9fafb;"><td style="padding:8px;font-weight:bold;">Source</td>
        <td style="padding:8px;">{job['source']}</td></tr>
    <tr><td style="padding:8px;font-weight:bold;">Posted</td>
        <td style="padding:8px;">{job.get('posted_at') or 'Unknown'}</td></tr>
  </table>
  <br>
  <a href="{job['url']}"
     style="display:inline-block;padding:12px 24px;background:#4f46e5;
            color:#fff;border-radius:8px;text-decoration:none;
            font-weight:bold;font-size:15px;">
    View &amp; Apply →
  </a>
  <p style="color:#6b7280;font-size:12px;margin-top:24px;">
    Sent by Bay Area Job Monitor • Unsubscribe by removing SMTP_USER from GitHub secrets.
  </p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASSWORD)
            srv.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("Email sent: %s @ %s", job["title"], job["company"])
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP auth failed — check SMTP_USER / SMTP_PASSWORD")
    except Exception as e:
        log.error("Email failed for %s: %s", job["title"], e)
    return False


# ═════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════
def send_combined_email(jobs: list[dict]) -> bool:
    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning("SMTP creds missing — skipping combined email")
        return False

    count = len(jobs)
    subject = f"[Job Alert] {count} New Job{'s' if count > 1 else ''} Found"

    # Build one row per job
    rows = ""
    for i, job in enumerate(jobs):
        bg = "#f9fafb" if i % 2 == 0 else "#ffffff"
        rows += f"""
        <tr style="background:{bg};">
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
                <strong>{job['title']}</strong><br>
                <span style="color:#6b7280;font-size:13px;">{job['company']}</span>
            </td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-size:13px;">
                {job.get('location') or 'Not listed'}
            </td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-size:13px;">
                {job['source']}
            </td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
                <a href="{job['url']}"
                   style="background:#4f46e5;color:#fff;padding:6px 12px;
                          border-radius:5px;text-decoration:none;font-size:13px;">
                    Apply
                </a>
            </td>
        </tr>"""

    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;">
  <h2 style="color:#4f46e5;">{count} New Job Match{'es' if count > 1 else ''}!</h2>
  <p style="color:#6b7280;">Found during the {datetime.now(PST).strftime('%A %b %d, %I:%M %p')} PST scan</p>
  <table style="width:100%;border-collapse:collapse;margin-top:16px;">
    <thead>
      <tr style="background:#4f46e5;color:#fff;">
        <th style="padding:10px;text-align:left;">Role & Company</th>
        <th style="padding:10px;text-align:left;">Location</th>
        <th style="padding:10px;text-align:left;">Source</th>
        <th style="padding:10px;text-align:left;">Link</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#6b7280;font-size:12px;margin-top:24px;">
    Bay Area Job Monitor • {datetime.now(PST).strftime('%Y-%m-%d %H:%M %Z')}
  </p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASSWORD)
            srv.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("Combined email sent: %d jobs", count)
        return True
    except Exception as e:
        log.error("Combined email failed: %s", e)
        return False
def run() -> None:
    _print_budget_estimate()

    conn = init_database()

    # Gate on schedule BEFORE doing any network requests
    if not should_run_now(conn):
        log.info("Not in an active scheduling window — exiting early.")
        conn.close()
        sys.exit(0)

    log.info("Active window confirmed — starting scrape at %s PST",
             datetime.now(PST).strftime("%A %Y-%m-%d %H:%M"))

    # Fetch from all three boards
    all_jobs: list[dict] = []
    all_jobs.extend(fetch_ycombinator())
    all_jobs.extend(fetch_wellfound())
    all_jobs.extend(fetch_greenhouse())

    log.info("Total matching jobs before dedup: %d", len(all_jobs))

    # Deduplicate, notify, persist
    new_count = 0
    new_jobs = []

    for job in all_jobs:
        if is_seen(conn, job["id"]):
            log.debug("Already seen: %s @ %s", job["title"], job["company"])
            continue
        log.info("NEW: %s @ %s [%s]", job["title"], job["company"], job["source"])
        new_jobs.append(job)

    if new_jobs:
        # Send ONE combined email for all new jobs
        email_ok = send_combined_email(new_jobs)

        # Send individual WhatsApp per job (short message, not spammy)
        for job in new_jobs:
            send_whatsapp(job)
            mark_seen(conn, job)
            new_count += 1
            time.sleep(1)
    else:
        log.info("No new jobs found this run.")
    
    
    
    

    
    
    

    
    
    
    

    

    
    
    


if __name__ == "__main__":
    run()
