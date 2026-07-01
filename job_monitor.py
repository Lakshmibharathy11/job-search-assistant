"""
Bay Area Startup Job Monitor
============================
Monitors YC Work at a Startup and Greenhouse (multi-board)
for Data Scientist / Data Analyst / AI Engineer / ML Engineer roles.

Filters aggressively for entry-level / new-grad / unspecified experience.
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
import yaml
from bs4 import BeautifulSoup
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# Config — all personal preferences live in config.yaml.
# Edit that file to tune roles/keywords/locations/boards; no code changes needed.
# ─────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def load_config(path: str = CONFIG_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


CONFIG = load_config()

# ─────────────────────────────────────────────
# Logging — UTF-8 safe (fixes Windows cp1252 crash)
# ─────────────────────────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("job_monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Profile & preferences — loaded from config.yaml (see load_config() above).
# Edit config.yaml to tune skills/roles/locations/boards without touching code.
# ─────────────────────────────────────────────
_profile_cfg = CONFIG.get("profile", {})
_skills_cfg  = _profile_cfg.get("skills", {})

MY_PROFILE = {
    "skills":          sum(_skills_cfg.values(), []) if _skills_cfg else [],
    "skills_tier1":    _skills_cfg.get("tier1", []),
    "skills_tier2":    _skills_cfg.get("tier2", []),
    "skills_tier3":    _skills_cfg.get("tier3", []),
    "education":       _profile_cfg.get("education", "master"),
    "min_score":       _profile_cfg.get("min_score", 25),
}

# Roles targeted — used in email subject, WhatsApp, and keyword matching.
# Kept tight to entry-level/new-grad AI Engineer, Data Scientist, Data Analyst
# per user's stated focus. Add more in config.yaml's target_roles to widen it.
TARGET_KEYWORDS = [r.lower() for r in CONFIG.get("target_roles", [
    "ai engineer", "data scientist", "data analyst",
])]

# ─────────────────────────────────────────────
# Constants & Config
# ─────────────────────────────────────────────
PST = ZoneInfo("America/Los_Angeles")
DB_PATH = "job_monitor.db"

# Recency window: show jobs posted between 30 minutes and 1 week ago
# - Min 30 min: avoids jobs still being indexed / not fully published
# - Max 7 days: catches everything recent without flooding old listings
# - SQLite deduplication ensures you NEVER get notified twice about the same job
# - Greenhouse uses full 7-day window since updated_at reflects edits not post date
LOOKBACK_MIN_MINUTES = 0              # 0 = no minimum (include very new jobs too)
LOOKBACK_MAX_DAYS    = 7              # 1 week maximum age
LOOKBACK_MAX_MINUTES = 7 * 24 * 60   # = 10,080 minutes

MAX_EXPERIENCE_YEARS = CONFIG.get("experience", {}).get("max_years", 1)

_location_cfg = CONFIG.get("location", {})
BAY_AREA_CITIES = [c.lower() for c in _location_cfg.get("bay_area_cities", [])]

# Location ALLOW list for curated sources (Greenhouse/Lever/Ashby boards are
# already restricted to Bay Area-headquartered companies, so remote/US-general
# postings from them are still Bay Area-company roles).
LOCATION_ALLOW = BAY_AREA_CITIES + [c.lower() for c in _location_cfg.get("us_general", [])]

# Location BLOCK list — explicitly non-US locations to reject
LOCATION_BLOCK = [c.lower() for c in _location_cfg.get("block", [])]

# ── Experience level filters ──────────────────────────────────────
# Jobs MUST match at least one ENTRY signal OR have NO experience signal at all
ENTRY_SIGNALS = re.compile(
    r"\b("
    r"entry[\s\-]?level|entry|junior|jr\.?|new[\s\-]?grad|new[\s\-]?graduate|"
    r"associate|early[\s\-]?career|graduate|intern|internship|"
    r"0[\s\-]?[\-–][\s\-]?[12][\s\-]?year|"   # 0-1, 0-2 year
    r"1[\s\-]?[\-–][\s\-]?[23][\s\-]?year|"   # 1-2, 1-3 year
    r"recent[\s\-]?grad|fresh[\s\-]?grad|"
    r"no[\s\-]?experience[\s\-]?required|"
    r"i+[\s]?level|level[\s]?i+\b"             # Level I, Level II
    r")",
    re.IGNORECASE,
)

# Jobs with these are HARD BLOCKED regardless of anything else.
# Threshold is MAX_EXPERIENCE_YEARS + 1 and up (config.yaml experience.max_years,
# default 1) — kept tight to the 0-1 yr entry-level/new-grad focus.
SENIOR_BLOCK = re.compile(
    r"\b("
    r"senior|sr\.?|lead|principal|staff|director|vp|"
    r"vice[\s\-]?president|head[\s\-]?of|manager|"
    rf"[{MAX_EXPERIENCE_YEARS + 1}-9]\+?[\s]?year|10\+?[\s]?year|"   # (max+1)+ years and above
    rf"[{MAX_EXPERIENCE_YEARS + 1}-9][\s\-][\d]+[\s]?year"           # (max+1)-X years range
    r")\b",
    re.IGNORECASE,
)

# Experience year patterns — used to detect "2+ years", "5 years" etc.
EXP_YEAR_PATTERN = re.compile(
    r"(\d+)\+?\s*(?:to|-|–)\s*(\d+)\s*(?:years?|yrs?)|"  # 2-5 years
    r"(\d+)\+\s*(?:years?|yrs?)|"                          # 3+ years
    r"(\d+)\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)",  # 5 years experience
    re.IGNORECASE,
)

# ── Seniority / experience check ─────────────────────────────────

def _extract_max_experience(text: str) -> Optional[int]:
    """Return the maximum years of experience mentioned, or None."""
    matches = EXP_YEAR_PATTERN.findall(text)
    years = []
    for m in matches:
        for val in m:
            if val:
                try:
                    years.append(int(val))
                except ValueError:
                    pass
    return max(years) if years else None


def passes_experience_filter(title: str, description: str = "") -> tuple[bool, str]:
    """
    Returns (passes: bool, reason: str)

    Logic (bound is config.yaml experience.max_years, default 1):
    1. Hard block if title contains senior/lead/etc.
    2. Hard block if description explicitly requires more than max_years.
    3. Pass if title/description has entry-level signals.
    4. Pass if NO experience requirement is mentioned at all.
    5. Block otherwise.
    """
    combined = f"{title} {description}"

    # Rule 1 — hard block on senior title
    if SENIOR_BLOCK.search(title):
        return False, f"Senior title blocked: '{title}'"

    # Rule 2 — check explicit year requirements in description
    max_exp = _extract_max_experience(combined)
    if max_exp is not None and max_exp > MAX_EXPERIENCE_YEARS:
        return False, f"Requires {max_exp}+ years experience"

    # Rule 3 — explicit entry-level signal → always pass
    if ENTRY_SIGNALS.search(combined):
        return True, "Entry-level signal found"

    # Rule 4 — no experience requirement mentioned → pass (unspecified = open)
    if max_exp is None:
        return True, "No experience requirement specified"

    # Rule 5 — within the allowed years → pass
    if max_exp <= MAX_EXPERIENCE_YEARS:
        return True, f"Only {max_exp} year(s) required"

    return False, f"Requires {max_exp} years — too senior"


# ── Profile match scoring ─────────────────────────────────────────

def _title_base_score(title: str) -> int:
    """
    Gives a base score when no description is available (e.g. YC jobs).
    Based purely on job title matching our target roles.
    Ensures relevant titles like "Data Scientist" always pass even without desc.
    """
    t = title.lower()
    # Direct role matches — these are exactly what we want
    role_scores = {
        "data scientist":       70,
        "data science":         70,
        "data analyst":         70,
        "ai engineer":          70,
        "applied ai engineer":  70,
        "genai engineer":       70,
        "gen ai engineer":      70,
    }
    for role, base in role_scores.items():
        if role in t:
            return base
    return 30   # unknown title but passed keyword filter — give benefit of doubt


def profile_match_score(title: str, description: str = "") -> int:
    """
    Returns 0-100 score of how well the job matches Lakshmi's profile.
    Weighted by skill category importance.
    """
    combined = f"{title} {description}".lower()
    score = 0

    # ── Tier 1: Core AI/ML skills (5 pts each, up to 40) — from config.yaml
    t1_matches = sum(1 for s in MY_PROFILE["skills_tier1"] if s in combined)
    score += min(40, t1_matches * 5)

    # ── Tier 2: Data & Engineering skills (3 pts each, up to 25) — from config.yaml
    t2_matches = sum(1 for s in MY_PROFILE["skills_tier2"] if s in combined)
    score += min(25, t2_matches * 3)

    # ── Tier 3: BI & Visualization (2 pts each, up to 15) — from config.yaml
    t3_matches = sum(1 for s in MY_PROFILE["skills_tier3"] if s in combined)
    score += min(15, t3_matches * 2)

    # ── Bonus: Education match (+10)
    edu_keywords = ["master", "ms ", "m.s.", "m.s", "graduate", "phd"]
    if any(kw in combined for kw in edu_keywords):
        score += 10

    # ── Bonus: Entry-level / new grad (+5)
    if ENTRY_SIGNALS.search(combined):
        score += 5

    # ── Bonus: No sponsorship required (+5)
    if any(kw in combined for kw in ["no sponsorship", "authorized to work", "us citizen"]):
        score += 5

    return min(100, score)


# ─────────────────────────────────────────────
# User-agent pool
# ─────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ─────────────────────────────────────────────
# Job boards — tokens loaded from config.yaml (boards.greenhouse/lever/ashby).
# Wrong/outdated tokens 404 silently and just yield 0 jobs for that board.
# ─────────────────────────────────────────────
_boards_cfg = CONFIG.get("boards", {})
GREENHOUSE_BOARDS = _boards_cfg.get("greenhouse", [])
LEVER_BOARDS       = _boards_cfg.get("lever", [])
ASHBY_BOARDS       = _boards_cfg.get("ashby", [])

# ─────────────────────────────────────────────
# Spam / fake-job detection
# ─────────────────────────────────────────────
# Hard-block patterns — unambiguous scam signals. A match drops the job
# entirely, same as any other filter failure.
SPAM_HARD_BLOCK = re.compile(
    r"("
    r"processing[\s\-]?fee|training[\s\-]?fee|starter[\s\-]?kit|"
    r"purchase[\s\-](?:your[\s\-]?own[\s\-]?)?equipment|"
    r"wire[\s\-]?transfer|send[\s\-](?:us[\s\-])?(?:your[\s\-])?bank|"
    r"routing[\s\-]?number|social[\s\-]?security[\s\-]?number|\bssn\b|"
    r"no[\s\-]?interview[\s\-]?(?:necessary|required|needed)|"
    r"(?:immediate|same[\s\-]?day)[\s\-]?hire"
    r")",
    re.IGNORECASE,
)

# Free webmail domains — a posting whose *only* contact method is one of
# these (no company domain anywhere in the text) is a red flag.
_FREE_EMAIL_DOMAINS = r"(?:gmail|yahoo|hotmail|outlook|aol|protonmail)\.com"
SPAM_FREE_EMAIL_ONLY = re.compile(
    r"[\w.+-]+@" + _FREE_EMAIL_DOMAINS, re.IGNORECASE
)
SPAM_ANY_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)

# Soft-flag patterns — ambiguous, still shown but badged + score penalty.
SPAM_PAY_CLAIM = re.compile(
    r"\$\s?\d{2,3}\s?(?:/|per)\s?(?:hour|hr)|"      # "$100/hour" for a junior role
    r"\$\s?\d{1,3}[,.]?\d{3}\s?(?:/|per)\s?week",    # "$10,000/week"
    re.IGNORECASE,
)

# ─────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────
TWILIO_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_TO     = os.environ.get("TWILIO_TO_NUMBER", "")

SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO      = os.environ.get("EMAIL_TO", SMTP_USER)


def spam_check(
    title: str, description: str, location: str = "", source: str = ""
) -> tuple[bool, bool, list[str]]:
    """
    Returns (hard_block, suspicious, reasons).

    hard_block  — unambiguous scam signal, drop the job entirely.
    suspicious  — ambiguous signal, keep the job but badge it + penalize score.
    """
    combined = f"{title} {description}"
    reasons: list[str] = []

    if SPAM_HARD_BLOCK.search(combined):
        return True, False, ["Contains known scam phrasing (fee/payment/urgency request)"]

    emails = SPAM_ANY_EMAIL.findall(combined)
    if emails and all(SPAM_FREE_EMAIL_ONLY.search(e) for e in emails):
        return True, False, ["Only contact method is a personal webmail address"]

    # Only the YC marketplace source is un-curated enough to warrant a
    # short-description penalty — Greenhouse/Lever/Ashby always return full
    # ATS-authored descriptions.
    if source == "YC Work at a Startup" and len(description.strip()) < 30:
        reasons.append("Very short/vague description from an open marketplace listing")

    if location:
        loc = location.lower()
        if any(b in loc for b in LOCATION_BLOCK) and any(a in loc for a in LOCATION_ALLOW):
            reasons.append("Location text contradicts itself (mixes Bay Area/US and blocked region)")

    if SPAM_PAY_CLAIM.search(combined) and ENTRY_SIGNALS.search(combined):
        reasons.append("Unrealistic pay claim for an entry-level role")

    return False, bool(reasons), reasons


# ═════════════════════════════════════════════
# DATABASE
# ═════════════════════════════════════════════

def init_database() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            id           TEXT PRIMARY KEY,
            title        TEXT,
            company      TEXT,
            url          TEXT,
            source       TEXT,
            location     TEXT,
            posted_at    TEXT,
            notified_at  TEXT,
            match_score  INTEGER DEFAULT 0,
            exp_level    TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            found_at  TEXT,
            hour_pst  INTEGER,
            weekday   INTEGER
        )
    """)
    # Add new columns to existing db if upgrading
    try:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN match_score INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN exp_level TEXT DEFAULT ''")
    except Exception:
        pass   # columns already exist
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
           (id, title, company, url, source, location,
            posted_at, notified_at, match_score, exp_level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["id"], job["title"], job["company"], job["url"],
            job["source"], job.get("location", ""),
            job.get("posted_at", ""),
            datetime.now(timezone.utc).isoformat(),
            job.get("match_score", 0),
            job.get("exp_level", ""),
        ),
    )
    conn.execute(
        "INSERT INTO job_events (found_at, hour_pst, weekday) VALUES (?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            datetime.now(PST).hour,
            datetime.now(PST).weekday(),
        ),
    )
    conn.commit()


def total_jobs_found(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0]


# Cadence is now controlled entirely by the workflow cron (Tue & Wed,
# 10am & 2pm PST — see .github/workflows/monitor.yml), so no in-script
# time-window gating is needed. The previous gate was left disabled via a
# stray `if False:` in run(), which is what caused unrestricted 24/7 runs.


# ═════════════════════════════════════════════
# HTTP HELPERS
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
        log.warning("HTTP %s -- %s", e.response.status_code, url)
    except requests.exceptions.ConnectionError:
        log.warning("Connection error: %s", url)
    except requests.exceptions.Timeout:
        log.warning("Timeout: %s", url)
    except Exception as e:
        log.warning("GET failed (%s): %s", type(e).__name__, url)
    return None


def _make_id(source: str, native_id: str, company: str, url: str) -> str:
    """
    Stable dedup ID. Prefers the platform's own posting ID (which doesn't
    change if a company edits the title/description later) over a hash of
    mutable text — this is what fixes jobs being re-notified after an edit.
    """
    if native_id:
        raw = f"{source.lower()}|{native_id}"
    else:
        raw = f"{source.lower()}|{company.lower()}|{url.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _matches_keyword(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TARGET_KEYWORDS)


def _matches_location(location: str, strict_bay_area: bool = False) -> bool:
    """
    Two-pass location check:
    1. Hard block if an explicit non-US keyword is found
    2. Allow only if a recognised US/Bay Area/remote keyword is found

    Blank location is treated as unspecified/remote-friendly and included.
    A location string that IS present but matches neither an allow nor a
    block keyword (e.g. "Seoul, South Korea", a city not in either list) is
    now EXCLUDED by default — previously it defaulted to included, which let
    unrelated-geography postings slip through the Bay Area-only focus.

    strict_bay_area=True is used for un-curated sources (YC marketplace):
    only an explicit Bay Area city/CA match is accepted, not generic
    remote/US-wide, since we can't assume the company is Bay Area-based.
    """
    if not location:
        return not strict_bay_area   # unknown location: include unless strict

    loc = location.lower().strip()

    # Pass 1: block explicit non-US locations
    if any(block_kw in loc for block_kw in LOCATION_BLOCK):
        return False

    if strict_bay_area:
        return any(city in loc for city in BAY_AREA_CITIES)

    # Pass 2: allow only recognised US / Bay Area / remote locations
    return any(allow_kw in loc for allow_kw in LOCATION_ALLOW)


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_in_window(posted_at: Optional[datetime]) -> tuple[bool, str]:
    """
    Returns (passes, reason) for the 30-min to 1-week recency window.
    - Too new  (< 30 min): job may not be fully published yet — skip
    - In range (30 min to 7 days): include
    - Too old  (> 7 days): skip
    - Unknown timestamp: always include (can't filter what we can't measure)
    """
    if posted_at is None:
        return True, "unknown timestamp"

    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)

    now      = datetime.now(timezone.utc)
    age_mins = (now - posted_at).total_seconds() / 60

    if age_mins < LOOKBACK_MIN_MINUTES:
        return False, f"too new ({age_mins:.0f} min old)"
    if age_mins >= LOOKBACK_MAX_MINUTES:
        return False, f"too old ({age_mins/1440:.1f} days old)"
    return True, f"{age_mins/60:.1f}h old"


# Keep backward-compatible alias
def _is_recent(posted_at: Optional[datetime], lookback_minutes: int = LOOKBACK_MAX_MINUTES) -> bool:
    passes, _ = _is_in_window(posted_at)
    return passes


def _filter_and_score(raw: list[dict], strict_location: bool = False) -> list[dict]:
    """
    Apply ALL filters and add match_score + exp_level to each job.
    Returns only jobs that pass every filter.

    strict_location=True should be set for un-curated sources (YC) where we
    can't assume the company is Bay Area-based — see _matches_location().
    """
    out = []
    for job in raw:
        title       = job.get("title", "")
        description = job.get("description", "")
        location    = job.get("location", "")

        # ── Filter 1: must match a target role keyword
        if not _matches_keyword(title):
            log.debug("SKIP (keyword): %s", title)
            continue

        # ── Filter 2: location
        if not _matches_location(location, strict_bay_area=strict_location):
            log.debug("SKIP (location): %s @ %s", title, location)
            continue

        # ── Filter 3: recency window (30 min – 7 days)
        passes_time, time_reason = _is_in_window(_parse_iso(job.get("posted_at", "")))
        if not passes_time:
            log.debug("SKIP (time: %s): %s", time_reason, title)
            continue

        # ── Filter 4: experience level
        passes, reason = passes_experience_filter(title, description)
        if not passes:
            log.debug("SKIP (exp): %s — %s", title, reason)
            continue

        # ── Filter 5: spam / fake-job detection
        hard_block, suspicious, spam_reasons = spam_check(
            title, description, location, job.get("source", "")
        )
        if hard_block:
            log.info("SKIP (spam): %s @ %s — %s", title, job.get("company"), spam_reasons)
            continue

        # ── Score: profile match
        # Only apply min_score when we have a description to score against.
        # Many YC jobs return empty descriptions — penalising them would block all YC jobs.
        score = profile_match_score(title, description)
        has_description = len(description.strip()) > 50
        if has_description and score < MY_PROFILE["min_score"]:
            log.debug("SKIP (score %d%% with desc): %s", score, title)
            continue
        # If no description, give a base score from title keywords
        if not has_description:
            score = max(score, _title_base_score(title))

        if suspicious:
            score = max(0, score - 20)

        # Determine experience level label for display
        if ENTRY_SIGNALS.search(f"{title} {description}"):
            exp_level = "Entry Level"
        else:
            exp_level = "Unspecified (open)"

        job["match_score"] = score
        job["exp_level"]   = exp_level
        job["suspicious"]  = suspicious
        job["spam_reasons"] = spam_reasons
        out.append(job)

    return out


# ═════════════════════════════════════════════
# SCRAPERS
# ═════════════════════════════════════════════

# ── 1. Y Combinator Work at a Startup ────────────────────────────

YC_SEARCH_URL = "https://www.workatastartup.com/jobs/search"

def fetch_ycombinator() -> list[dict]:
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    # YC is an open marketplace (not curated to Bay Area startups), so we
    # search using exactly the configured target roles and apply strict
    # Bay Area location matching below.
    search_terms = TARGET_KEYWORDS

    for kw in search_terms:
        params = {
            "q":        kw,
            "job_type": "fulltime",
            # Removed: remote=only_remote_ok (was blocking Bay Area on-site jobs)
            # Removed: experience=entry_level (was blocking untagged entry roles)
            # We filter experience ourselves via passes_experience_filter()
        }
        r = _get(YC_SEARCH_URL, params=params)
        if not r:
            continue
        try:
            data = r.json()
        except Exception as e:
            log.warning("YC JSON parse error (%s): %s", kw, e)
            continue

        raw_jobs = data.get("jobs", [])
        log.info("YC raw: %d jobs for keyword='%s'", len(raw_jobs), kw)

        for item in raw_jobs:
            jid     = item.get("id", "")
            url     = f"https://www.workatastartup.com/jobs/{jid}"
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title     = item.get("title", "")
            company   = (item.get("company") or {}).get("name", "Unknown")
            locs      = item.get("locations") or []
            remote    = item.get("remote", "")
            location  = ", ".join(locs) if locs else remote
            desc      = item.get("description", "") or ""
            posted_raw = item.get("created_at", "") or item.get("updated_at", "")
            # YC also provides min/max experience years — use them directly
            min_exp = item.get("min_exp_years")
            max_exp = item.get("max_exp_years")
            if min_exp is not None or max_exp is not None:
                exp_str = f"{min_exp or 0}-{max_exp or '?'} years"
                desc = f"{desc} Experience: {exp_str}".strip()

            jobs.append(dict(
                id          = _make_id("yc", str(jid), company, url),
                title       = title,
                company     = company,
                location    = location,
                url         = url,
                source      = "YC Work at a Startup",
                posted_at   = posted_raw,
                description = desc,
            ))

        time.sleep(random.uniform(0.5, 1.0))

    # strict_location=True: YC is an un-curated marketplace, so require an
    # explicit Bay Area match rather than accepting generic remote/US-wide.
    result = _filter_and_score(jobs, strict_location=True)
    log.info("YC: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ── 2. Greenhouse (Official Public API) ──────────────────────────
# Docs: https://developers.greenhouse.io/job-board.html
# Key facts from official docs:
#   - Fully public, no authentication required for GET endpoints
#   - ?content=true returns full job description in ONE call (no second request needed)
#   - board_token = the slug from boards.greenhouse.io/<board_token>
#   - updated_at field available for recency filtering

GH_BASE = "https://boards-api.greenhouse.io/v1/boards"


def fetch_greenhouse() -> list[dict]:
    """
    Official Greenhouse Job Board API (public, no auth).
    Uses ?content=true to get full description in a single API call —
    eliminating the need for per-job follow-up requests.

    Endpoint: GET /v1/boards/{board_token}/jobs?content=true
    """
    jobs: list[dict] = []

    for board in GREENHOUSE_BOARDS:
        # Single API call with content=true gets everything we need
        url = f"{GH_BASE}/{board}/jobs"
        r = _get(url, params={"content": "true"})

        if not r:
            # 404 means wrong slug — skip silently
            continue

        try:
            data = r.json()
        except Exception as e:
            log.warning("Greenhouse %s JSON error: %s", board, e)
            continue

        board_jobs = data.get("jobs", [])
        if not board_jobs:
            log.debug("Greenhouse %s: 0 jobs posted", board)
            continue

        log.info("Greenhouse %s: %d total jobs", board, len(board_jobs))

        for item in board_jobs:
            title      = item.get("title", "")
            job_url    = item.get("absolute_url", "")
            location   = (item.get("location") or {}).get("name", "")
            posted_raw = item.get("updated_at", "") or item.get("created_at", "")
            company    = board.replace("-", " ").title()

            # Description comes directly from ?content=true — no extra request needed
            raw_desc = item.get("content", "") or ""
            desc = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True) if raw_desc else ""

            # Also extract department name for better context
            departments = item.get("departments", []) or []
            dept_name   = departments[0].get("name", "") if departments else ""

            # Use office location if job location is blank
            if not location:
                offices  = item.get("offices", []) or []
                location = offices[0].get("name", "") if offices else ""

            jobs.append(dict(
                id          = _make_id("greenhouse", str(item.get("id", "")), company, job_url),
                title       = title,
                company     = company,
                location    = location,
                url         = job_url,
                source      = f"Greenhouse ({board})",
                posted_at   = posted_raw,
                description = desc,
                department  = dept_name,
            ))

        time.sleep(random.uniform(0.5, 1.0))

    result = _filter_and_score(jobs)
    log.info("Greenhouse: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ── 3. Lever (Official Public API) ───────────────────────────────
# Docs: https://github.com/lever/postings-api — fully public, no auth.
# Endpoint: GET /v0/postings/{company}?mode=json

LEVER_BASE = "https://api.lever.co/v0/postings"


def fetch_lever() -> list[dict]:
    jobs: list[dict] = []

    for board in LEVER_BOARDS:
        url = f"{LEVER_BASE}/{board}"
        r = _get(url, params={"mode": "json"})
        if not r:
            continue  # 404/blocked = wrong slug, skip silently

        try:
            postings = r.json()
        except Exception as e:
            log.warning("Lever %s JSON error: %s", board, e)
            continue

        if not isinstance(postings, list) or not postings:
            log.debug("Lever %s: 0 jobs posted", board)
            continue

        log.info("Lever %s: %d total jobs", board, len(postings))
        company = board.replace("-", " ").title()

        for item in postings:
            title      = item.get("text", "")
            job_url    = item.get("hostedUrl", "")
            categories = item.get("categories", {}) or {}
            location   = categories.get("location", "") or ""
            posted_raw = item.get("createdAt", "")
            if isinstance(posted_raw, (int, float)):
                posted_raw = datetime.fromtimestamp(posted_raw / 1000, tz=timezone.utc).isoformat()
            desc = item.get("descriptionPlain", "") or item.get("description", "") or ""

            jobs.append(dict(
                id          = _make_id("lever", str(item.get("id", "")), company, job_url),
                title       = title,
                company     = company,
                location    = location,
                url         = job_url,
                source      = f"Lever ({board})",
                posted_at   = posted_raw,
                description = desc,
            ))

        time.sleep(random.uniform(0.5, 1.0))

    result = _filter_and_score(jobs)
    log.info("Lever: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ── 4. Ashby (Official Public API) ───────────────────────────────
# Docs: https://developers.ashbyhq.com/docs/job-posting-api — fully public, no auth.
# Endpoint: GET /posting-api/job-board/{board}

ASHBY_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def fetch_ashby() -> list[dict]:
    jobs: list[dict] = []

    for board in ASHBY_BOARDS:
        url = f"{ASHBY_BASE}/{board}"
        r = _get(url, params={"includeCompensation": "false"})
        if not r:
            continue  # 404/blocked = wrong slug, skip silently

        try:
            data = r.json()
        except Exception as e:
            log.warning("Ashby %s JSON error: %s", board, e)
            continue

        postings = data.get("jobs", [])
        if not postings:
            log.debug("Ashby %s: 0 jobs posted", board)
            continue

        log.info("Ashby %s: %d total jobs", board, len(postings))
        company = board.replace("-", " ").title()

        for item in postings:
            title      = item.get("title", "")
            job_url    = item.get("jobUrl", "") or item.get("applyUrl", "")
            location   = item.get("location", "") or ""
            posted_raw = item.get("publishedDate", "") or item.get("updatedAt", "")
            raw_desc   = item.get("descriptionHtml", "") or ""
            desc       = BeautifulSoup(raw_desc, "lxml").get_text(" ", strip=True) if raw_desc else ""

            jobs.append(dict(
                id          = _make_id("ashby", str(item.get("id", "")), company, job_url),
                title       = title,
                company     = company,
                location    = location,
                url         = job_url,
                source      = f"Ashby ({board})",
                posted_at   = posted_raw,
                description = desc,
            ))

        time.sleep(random.uniform(0.5, 1.0))

    result = _filter_and_score(jobs)
    log.info("Ashby: %d matching jobs (from %d raw)", len(result), len(jobs))
    return result


# ═════════════════════════════════════════════
# NOTIFICATIONS
# ═════════════════════════════════════════════

def send_whatsapp(job: dict) -> bool:
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.warning("Twilio creds missing — skipping WhatsApp: %s", job["title"])
        return False

    score_bar = "█" * (job.get("match_score", 0) // 10) + "░" * (10 - job.get("match_score", 0) // 10)
    title = job["title"] if not job.get("suspicious") else f"⚠️ {job['title']} (verify carefully)"

    body = (
        f"🚀 *New Job Alert*\n"
        f"*{title}*\n"
        f"🏢 {job['company']}\n"
        f"📍 {job.get('location') or 'Not listed'}\n"
        f"🎯 Level: {job.get('exp_level', 'Unknown')}\n"
        f"📊 Match: {score_bar} {job.get('match_score', 0)}%\n"
        f"📋 {job['source']}\n"
        f"🔗 {job['url']}"
    )

    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(from_=TWILIO_FROM, to=TWILIO_TO, body=body)
        log.info("WhatsApp sent (SID: %s): %s @ %s", msg.sid, job["title"], job["company"])
        return True
    except Exception as e:
        log.error("WhatsApp failed for %s: %s", job["title"], e)
        return False


def send_combined_email(jobs: list[dict]) -> bool:
    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning("SMTP creds missing — skipping email")
        return False

    count   = len(jobs)
    subject = f"[Job Alert] {count} New Job{'s' if count > 1 else ''} Found — {datetime.now(PST).strftime('%b %d %I:%M %p')} PST"

    rows = ""
    for i, job in enumerate(sorted(jobs, key=lambda j: j.get("match_score", 0), reverse=True)):
        bg    = "#f9fafb" if i % 2 == 0 else "#ffffff"
        score = job.get("match_score", 0)
        score_color = "#16a34a" if score >= 60 else "#d97706" if score >= 30 else "#dc2626"
        level = job.get("exp_level", "")
        level_badge = (
            f'<span style="background:#dcfce7;color:#166534;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;">{level}</span>'
            if "Entry" in level else
            f'<span style="background:#fef9c3;color:#713f12;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;">{level}</span>'
        )
        spam_badge = (
            f'<br><span title="{"; ".join(job.get("spam_reasons", []))}" '
            f'style="background:#fee2e2;color:#991b1b;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;">⚠️ Verify Carefully</span>'
            if job.get("suspicious") else ""
        )
        rows += f"""
        <tr style="background:{bg};">
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
            <strong style="font-size:14px;">{job['title']}</strong>{spam_badge}<br>
            <span style="color:#6b7280;font-size:12px;">{job['company']}</span>
          </td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-size:12px;">
            {job.get('location') or 'Not listed'}
          </td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-size:12px;">
            {level_badge}
          </td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
            <span style="font-weight:bold;color:{score_color};">{score}%</span>
          </td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-size:11px;color:#6b7280;">
            {job['source'].split('(')[0].strip()}
          </td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
            <a href="{job['url']}"
               style="background:#4f46e5;color:#fff;padding:5px 12px;
                      border-radius:4px;text-decoration:none;font-size:12px;">
              Apply →
            </a>
          </td>
        </tr>"""

    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;padding:20px;">
  <h2 style="color:#4f46e5;margin-bottom:4px;">🚀 {count} New Job Match{'es' if count > 1 else ''}!</h2>
  <p style="color:#6b7280;margin-top:0;">
    Scanned at {datetime.now(PST).strftime('%A, %b %d %Y • %I:%M %p')} PST
    • Sorted by profile match score
  </p>
  <table style="width:100%;border-collapse:collapse;margin-top:12px;">
    <thead>
      <tr style="background:#4f46e5;color:#fff;">
        <th style="padding:10px;text-align:left;">Role & Company</th>
        <th style="padding:10px;text-align:left;">Location</th>
        <th style="padding:10px;text-align:left;">Level</th>
        <th style="padding:10px;text-align:left;">Match</th>
        <th style="padding:10px;text-align:left;">Source</th>
        <th style="padding:10px;text-align:left;">Link</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#9ca3af;font-size:11px;margin-top:20px;">
    Bay Area Job Monitor • Only entry-level &amp; unspecified experience roles
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
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP auth failed — check credentials")
    except Exception as e:
        log.error("Email failed: %s", e)
    return False


# ═════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════

def run() -> None:
    conn = init_database()

    log.info("Starting scrape at %s PST", datetime.now(PST).strftime("%A %Y-%m-%d %H:%M"))

    all_jobs: list[dict] = []
    all_jobs.extend(fetch_ycombinator())
    all_jobs.extend(fetch_greenhouse())
    all_jobs.extend(fetch_lever())
    all_jobs.extend(fetch_ashby())

    log.info("Total matching jobs before dedup: %d", len(all_jobs))

    new_jobs = []
    for job in all_jobs:
        if is_seen(conn, job["id"]):
            log.debug("Already seen: %s @ %s", job["title"], job["company"])
            continue
        log.info(
            "NEW [%d%% match | %s%s]: %s @ %s [%s]",
            job.get("match_score", 0),
            job.get("exp_level", "?"),
            " | SUSPICIOUS" if job.get("suspicious") else "",
            job["title"], job["company"], job["source"],
        )
        new_jobs.append(job)

    if new_jobs:
        send_combined_email(new_jobs)
        for job in new_jobs:
            send_whatsapp(job)
            mark_seen(conn, job)
            time.sleep(1)
    else:
        log.info("No new jobs found this run.")

    log.info("Run complete — %d new job(s) notified.", len(new_jobs))
    conn.close()


if __name__ == "__main__":
    run()