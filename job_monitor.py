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
from bs4 import BeautifulSoup
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

load_dotenv()

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
# YOUR PROFILE — Lakshmi Bharathy Kumar
# M.S. Applied Data Intelligence, SJSU May 2026
# ─────────────────────────────────────────────
MY_PROFILE = {
    "skills": [
        # Languages
        "python", "sql", "javascript",

        # AI / LLM / Agents
        "langchain", "langgraph", "rag", "llm", "large language model",
        "openai", "gemini", "groq", "prompt engineering",
        "function calling", "agentic", "agent",
        "llm-as-judge", "generative ai", "gen ai",

        # ML / Deep Learning
        "pytorch", "scikit-learn", "xgboost", "transformers",
        "hugging face", "huggingface", "deep learning",
        "machine learning", "neural network", "nlp",
        "natural language processing", "computer vision",
        "classification", "regression", "feature engineering",
        "model evaluation", "fine-tuning", "lora", "qlora",
        "quantization", "onnx", "diffusion", "stable diffusion",
        "gans", "lstm", "cnn", "bert", "embeddings",

        # Data & Analytics
        "pandas", "numpy", "statistics", "eda",
        "data analysis", "data science", "analytics",
        "a/b testing", "experimentation", "hypothesis testing",

        # Data Engineering
        "airflow", "dbt", "kafka", "etl", "elt",
        "snowflake", "data pipeline", "data modeling",
        "spark",

        # Cloud & MLOps
        "aws", "docker", "fastapi", "github actions",
        "ci/cd", "mlops", "rest api",

        # Databases
        "postgresql", "mongodb", "mysql", "redis",
        "chromadb", "faiss", "vector database",
        "dynamodb", "s3",

        # BI & Visualization
        "power bi", "tableau", "streamlit", "matplotlib",
        "seaborn", "data visualization", "dashboard",
        "apache superset",
    ],

    "education": "master",    # boosts jobs mentioning MS/Master's/Graduate
    "experience_years": 2,    # 2 years professional experience
    "min_score": 25,          # lower = see more jobs; raise to 50 for stricter filtering
}

# Roles Lakshmi is targeting — used in email subject and WhatsApp
MY_TARGET_ROLES = [
    "Data Scientist", "Data Analyst", "AI Engineer",
    "ML Engineer", "Analytics Engineer", "Applied Scientist",
    "Business Intelligence Engineer", "Data Engineer",
]

# ─────────────────────────────────────────────
# Constants & Config
# ─────────────────────────────────────────────
PST = ZoneInfo("America/Los_Angeles")
DB_PATH = "job_monitor.db"
LEARNING_THRESHOLD = 30

# Recency window: show jobs posted between 30 minutes and 1 week ago
# - Min 30 min: avoids jobs still being indexed / not fully published
# - Max 7 days: catches everything recent without flooding old listings
# - SQLite deduplication ensures you NEVER get notified twice about the same job
# - Greenhouse uses full 7-day window since updated_at reflects edits not post date
LOOKBACK_MIN_MINUTES = 0              # 0 = no minimum (include very new jobs too)
LOOKBACK_MAX_DAYS    = 7              # 1 week maximum age
LOOKBACK_MAX_MINUTES = 7 * 24 * 60   # = 10,080 minutes

TARGET_KEYWORDS = [
    "data scientist",
    "data analyst",
    "ai engineer",
    "machine learning engineer",
    "ml engineer",
    "analytics engineer",
    "business intelligence",
    "research scientist",
    "applied scientist",
    "data engineer",
]

# Location ALLOW list — US only, all work modes welcome
# Covers: Bay Area in-person, Hybrid, Remote US, and unspecified US locations
LOCATION_ALLOW = [
    # ── Bay Area cities (in-person / hybrid) ─────────────────────
    "san francisco", "bay area", "sf", "south bay", "east bay",
    "mountain view", "palo alto", "san jose", "santa clara",
    "sunnyvale", "redwood city", "menlo park", "foster city",
    "burlingame", "san mateo", "oakland", "berkeley", "emeryville",
    "fremont", "milpitas", "cupertino", "campbell", "los gatos",
    "san carlos", "san ramon", "pleasanton", "walnut creek",
    "south san francisco", "daly city", "hayward", "union city",

    # ── State / country level ─────────────────────────────────────
    ", ca", ", ca,", "(ca)", "california",
    "united states", "usa", "u.s.", "u.s.a",
    "north america",

    # ── Remote (all formats companies use) ───────────────────────
    "remote",           # catches: remote, remote us, remote - us, etc.
    "work from home", "wfh", "distributed", "virtual",
    "anywhere in us", "anywhere",

    # ── Hybrid (all formats) ──────────────────────────────────────
    "hybrid",           # catches: hybrid, hybrid - sf, hybrid (us), etc.
    "flexible",         # "flexible location", "flexible work"
    "in-person or remote", "remote or in-person", "on-site or remote",

    # ── In-person / on-site ───────────────────────────────────────
    "on-site", "onsite", "on site", "in office", "in-office",
    "in person", "in-person",
]

# Location BLOCK list — explicitly non-US locations to reject
LOCATION_BLOCK = [
    "london", "uk", "united kingdom", "england",
    "canada", "toronto", "vancouver", "montreal",
    "india", "bangalore", "mumbai", "delhi", "hyderabad",
    "germany", "berlin", "munich", "frankfurt",
    "france", "paris",
    "singapore", "australia", "sydney", "melbourne",
    "brazil", "mexico", "latam", "latin america",
    "europe", "apac", "emea",
    "amsterdam", "dublin", "stockholm", "zurich",
]

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

# Jobs with these are HARD BLOCKED regardless of anything else
SENIOR_BLOCK = re.compile(
    r"\b("
    r"senior|sr\.?|lead|principal|staff|director|vp|"
    r"vice[\s\-]?president|head[\s\-]?of|manager|"
    r"[3-9]\+?[\s]?year|10\+?[\s]?year|"       # 3+ years and above
    r"[3-9][\s\-][\d]+[\s]?year"               # 3-X years range
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

    Logic:
    1. Hard block if title contains senior/lead/etc.
    2. Hard block if description explicitly requires 3+ years.
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
    if max_exp is not None and max_exp >= 3:
        return False, f"Requires {max_exp}+ years experience"

    # Rule 3 — explicit entry-level signal → always pass
    if ENTRY_SIGNALS.search(combined):
        return True, "Entry-level signal found"

    # Rule 4 — no experience requirement mentioned → pass (unspecified = open)
    if max_exp is None:
        return True, "No experience requirement specified"

    # Rule 5 — 1-2 years mentioned → pass (close enough for new grad)
    if max_exp <= 2:
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
        "data scientist":              70,
        "data analyst":                70,
        "machine learning engineer":   70,
        "ml engineer":                 70,
        "ai engineer":                 70,
        "analytics engineer":          65,
        "data engineer":               65,
        "applied scientist":           65,
        "research scientist":          60,
        "business intelligence":       60,
        "quantitative analyst":        55,
        "applied ml":                  65,
        "nlp engineer":                65,
        "computer vision":             60,
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

    # ── Tier 1: Core AI/ML skills (5 pts each, up to 40)
    tier1 = [
        "python", "machine learning", "deep learning", "pytorch",
        "llm", "rag", "langchain", "langgraph", "generative ai",
        "nlp", "natural language processing", "transformer",
        "data science", "scikit-learn",
    ]
    t1_matches = sum(1 for s in tier1 if s in combined)
    score += min(40, t1_matches * 5)

    # ── Tier 2: Data & Engineering skills (3 pts each, up to 25)
    tier2 = [
        "sql", "pandas", "data analysis", "airflow", "dbt",
        "snowflake", "aws", "docker", "fastapi", "kafka",
        "postgresql", "mongodb", "etl", "data pipeline",
        "feature engineering", "a/b testing", "experimentation",
    ]
    t2_matches = sum(1 for s in tier2 if s in combined)
    score += min(25, t2_matches * 3)

    # ── Tier 3: BI & Visualization (2 pts each, up to 15)
    tier3 = [
        "power bi", "tableau", "streamlit", "dashboard",
        "data visualization", "matplotlib", "analytics",
    ]
    t3_matches = sum(1 for s in tier3 if s in combined)
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
# Greenhouse boards — verified slugs only
# ─────────────────────────────────────────────
# Greenhouse board tokens
# How to find: go to a company's job page -> look at URL: boards.greenhouse.io/<TOKEN>
# Official API docs: https://developers.greenhouse.io/job-board.html
# Authentication: NOT required for GET endpoints (fully public API)
# ── Greenhouse board tokens ────────────────────────────────────────
# Verified from log output June 14 2026:
#   WORKING:   assemblyai, brex, gusto, lattice, databricks, fivetran,
#              hightouch, anthropic, scaleai, togetherai, coinbase, figma,
#              asana, lyft, stripe, airtable, amplitude, mixpanel, vercel
#   404 (wrong slug): openai, cohere, rippling, notion, retool, benchling,
#              anyscale, mistral, perplexity, elevenlabs, baseten, deepgram,
#              replit, persona, wandb, huggingface, dbtlabs, census, airbyte,
#              doordash, linear, supabase, modal, replicate, groq, cerebras
#
# How to find correct slug: visit boards.greenhouse.io/<slug> in browser
# If it shows jobs -> slug is correct. If 404 -> try company name variations.
GREENHOUSE_BOARDS = [
    # ── Confirmed working from logs ──────────────────────────────
    "assemblyai",           # AssemblyAI
    "brex",                 # Brex
    "gusto",                # Gusto
    "lattice",              # Lattice
    "databricks",           # Databricks
    "fivetran",             # Fivetran
    "hightouch",            # Hightouch
    "anthropic",            # Anthropic
    "scaleai",              # Scale AI
    "togetherai",           # Together AI
    "coinbase",             # Coinbase
    "figma",                # Figma
    "asana",                # Asana
    "lyft",                 # Lyft
    "stripe",               # Stripe
    "airtable",             # Airtable
    "amplitude",            # Amplitude
    "mixpanel",             # Mixpanel
    "vercel",               # Vercel

    # ── Corrected slugs (fixed from 404s) ────────────────────────
    "openai-2",             # OpenAI (try common variants)
    "cohereai",             # Cohere
    "ripplingwork",         # Rippling
    "notionlabs",           # Notion
    "retoolhq",             # Retool
    "benchling",            # Benchling (retry — may be intermittent)
    "anyscaleinc",          # Anyscale
    "mistralai",            # Mistral
    "perplexityai",         # Perplexity
    "elevenlabsio",         # ElevenLabs
    "basetenhq",            # Baseten
    "deepgramai",           # Deepgram
    "replitapp",            # Replit
    "withpersona",          # Persona
    "weightsandbiases",     # Weights & Biases
    "huggingfaceinc",       # HuggingFace
    "getdbt",               # dbt Labs
    "getcensus",            # Census
    "airbyteinc",           # Airbyte
    "doordash-2",           # DoorDash
    "linearapp",            # Linear
    "supabaseinc",          # Supabase
    "modalapp",             # Modal
    "replicateai",          # Replicate
    "groqinc",              # Groq
    "cerebrasai",           # Cerebras
    "datastax",             # DataStax
    "pineconeio",           # Pinecone
    "weaviateinc",          # Weaviate
    "groqcloud",            # Groq (alt)
    "langchainai",          # LangChain
    "vellumhq",             # Vellum

    # ── Additional strong matches for your profile ────────────────
    "snowflake",            # Snowflake
    "dbt-labs",             # dbt Labs (alt slug)
    "mongodb",              # MongoDB
    "elastic",              # Elastic
    "confluent",            # Confluent
    "segment",              # Segment (Twilio)
    "datarobot",            # DataRobot
    "domino-data-lab",      # Domino Data Lab
    "weights-biases",       # W&B alt slug
    "modal-labs",           # Modal alt slug
]

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule_weights (
            weekday   INTEGER,
            hour_pst  INTEGER,
            weight    REAL DEFAULT 1.0,
            PRIMARY KEY (weekday, hour_pst)
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


# ═════════════════════════════════════════════
# SMART SCHEDULING
# ═════════════════════════════════════════════

BASE_SCHEDULE = {
    0: [(9, 11, 60),  (12, 17, 180), (19, 20, 999)],   # Monday
    1: [(8, 12, 20),  (12, 17, 180), (19, 20, 999)],   # Tuesday
    2: [(8, 12, 20),  (12, 17, 180), (19, 20, 999)],   # Wednesday
    3: [(8, 12, 20),  (12, 17, 180), (19, 20, 999)],   # Thursday
    4: [(9, 11, 60),  (12, 17, 180), (19, 20, 999)],   # Friday
    5: [],                                               # Saturday
    6: [],                                               # Sunday
}


def should_run_now(conn: Optional[sqlite3.Connection] = None) -> bool:
    now     = datetime.now(PST)
    weekday = now.weekday()
    hour    = now.hour
    minute  = now.minute

    windows = BASE_SCHEDULE.get(weekday, [])
    if not windows:
        log.info("Weekend — no runs scheduled today.")
        return False

    for (start, end, interval) in windows:
        if start <= hour < end:
            if interval <= 20:
                return True
            mins_since_start = (hour - start) * 60 + minute
            if mins_since_start % interval < 20:
                if conn and _learned_weight(conn, weekday, hour) < 0.3:
                    log.info("Learning suppressed run at weekday=%d hour=%d", weekday, hour)
                    return False
                return True

    log.info("Outside active windows for weekday=%d hour=%d — skipping.", weekday, hour)
    return False


def _learned_weight(conn: sqlite3.Connection, weekday: int, hour: int) -> float:
    row = conn.execute(
        "SELECT weight FROM schedule_weights WHERE weekday=? AND hour_pst=?",
        (weekday, hour),
    ).fetchone()
    return row[0] if row else 1.0


def learn_and_adjust_schedule(conn: sqlite3.Connection) -> None:
    total = total_jobs_found(conn)
    if total < LEARNING_THRESHOLD:
        log.info("Learning: %d/%d jobs — not enough data yet.", total, LEARNING_THRESHOLD)
        return
    rows = conn.execute(
        "SELECT weekday, hour_pst, COUNT(*) FROM job_events GROUP BY weekday, hour_pst"
    ).fetchall()
    if not rows:
        return
    max_cnt = max(r[2] for r in rows)
    for weekday, hour, cnt in rows:
        weight = round(cnt / max_cnt, 3)
        conn.execute(
            """INSERT INTO schedule_weights (weekday, hour_pst, weight) VALUES (?, ?, ?)
               ON CONFLICT(weekday, hour_pst) DO UPDATE SET weight=excluded.weight""",
            (weekday, hour, weight),
        )
    conn.commit()
    log.info("Learning: weights updated from %d data points.", total)


def _print_budget_estimate() -> None:
    log.info(
        "Estimated monthly GitHub Actions runtime: ~116 min "
        "(free tier limit: 2,000 min)"
    )


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


def _make_id(company: str, title: str, url: str) -> str:
    raw = f"{company.lower()}|{title.lower()}|{url.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _matches_keyword(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TARGET_KEYWORDS)


def _matches_location(location: str) -> bool:
    """
    Two-pass location check:
    1. Hard block if an explicit non-US keyword is found
    2. Allow if a US/Bay Area keyword is found OR location is empty/unknown
    """
    if not location:
        return True   # no location listed — include (many remote jobs skip it)

    loc = location.lower().strip()

    # Pass 1: block explicit non-US locations
    if any(block_kw in loc for block_kw in LOCATION_BLOCK):
        return False

    # Pass 2: allow US / Bay Area / remote
    if any(allow_kw in loc for allow_kw in LOCATION_ALLOW):
        return True

    # Unknown location not blocked and not recognised — include it
    # (better to see an irrelevant job than miss a good one)
    return True


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


def _filter_and_score(raw: list[dict]) -> list[dict]:
    """
    Apply ALL filters and add match_score + exp_level to each job.
    Returns only jobs that pass every filter.
    """
    out = []
    for job in raw:
        title       = job.get("title", "")
        description = job.get("description", "")

        # ── Filter 1: must match a target role keyword
        if not _matches_keyword(title):
            log.debug("SKIP (keyword): %s", title)
            continue

        # ── Filter 2: location
        if not _matches_location(job.get("location", "")):
            log.debug("SKIP (location): %s @ %s", title, job.get("location"))
            continue

        # ── Filter 3: recency window (30 min – 7 days)
        passes_time, time_reason = _is_in_window(_parse_iso(job.get("posted_at", "")))
        if not passes_time:
            log.debug("SKIP (time: %s): %s", time_reason, title)
            continue

        # ── Filter 4: experience level (the main new filter)
        passes, reason = passes_experience_filter(title, description)
        if not passes:
            log.debug("SKIP (exp): %s — %s", title, reason)
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

        # Determine experience level label for display
        if ENTRY_SIGNALS.search(f"{title} {description}"):
            exp_level = "Entry Level"
        else:
            exp_level = "Unspecified (open)"

        job["match_score"] = score
        job["exp_level"]   = exp_level
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

    search_terms = [
        "data scientist", "data analyst",
        "machine learning engineer", "AI engineer",
        "analytics engineer", "data engineer",
    ]

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
                id          = _make_id(company, title, url),
                title       = title,
                company     = company,
                location    = location,
                url         = url,
                source      = "YC Work at a Startup",
                posted_at   = posted_raw,
                description = desc,
            ))

        time.sleep(random.uniform(0.5, 1.0))

    result = _filter_and_score(jobs)
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
                id          = _make_id(company, title, job_url),
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



# ═════════════════════════════════════════════
# NOTIFICATIONS
# ═════════════════════════════════════════════

def send_whatsapp(job: dict) -> bool:
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log.warning("Twilio creds missing — skipping WhatsApp: %s", job["title"])
        return False

    score_bar = "█" * (job.get("match_score", 0) // 10) + "░" * (10 - job.get("match_score", 0) // 10)

    body = (
        f"🚀 *New Job Alert*\n"
        f"*{job['title']}*\n"
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
        rows += f"""
        <tr style="background:{bg};">
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
            <strong style="font-size:14px;">{job['title']}</strong><br>
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
    _print_budget_estimate()
    conn = init_database()

    #if not should_run_now(conn):
    if False:
        log.info("Not in an active scheduling window — exiting early.")
        conn.close()
        sys.exit(0)

    log.info("Active window — starting scrape at %s PST",
             datetime.now(PST).strftime("%A %Y-%m-%d %H:%M"))

    all_jobs: list[dict] = []
    all_jobs.extend(fetch_ycombinator())
    all_jobs.extend(fetch_greenhouse())

    log.info("Total matching jobs before dedup: %d", len(all_jobs))

    new_jobs = []
    for job in all_jobs:
        if is_seen(conn, job["id"]):
            log.debug("Already seen: %s @ %s", job["title"], job["company"])
            continue
        log.info(
            "NEW [%d%% match | %s]: %s @ %s [%s]",
            job.get("match_score", 0),
            job.get("exp_level", "?"),
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
    learn_and_adjust_schedule(conn)
    conn.close()


if __name__ == "__main__":
    run()