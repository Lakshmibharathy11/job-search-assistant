# Bay Area Job Monitor

An automated pipeline that watches startup job boards for **entry-level / new-grad AI Engineer, Data Scientist, and Data Analyst** roles (0–1 yr experience) in the Bay Area, and only pings me when something genuinely new and relevant shows up — no logins, no paid APIs, no inbox flooding.

It runs on GitHub Actions, notifies via email + WhatsApp, and tracks everything it's already seen in a small SQLite DB that's committed straight back to the repo.

---

## Why this exists

The first version of this project worked, but it had a flaw that's easy to miss until you actually look at the data: **it kept re-notifying me about jobs I'd already seen**, and it ran far more often than it needed to. Digging into the logs and the SQLite DB directly (not just re-reading the code) turned up two concrete bugs:

1. **The scheduling guard was silently disabled.** Somewhere along the way, the code that was supposed to restrict runs to business-hours windows got short-circuited with a stray `if False:`. Combined with a `*/20 * * * *` cron, the workflow was actually running every 20 minutes, 24/7 — including Sundays, which the schedule explicitly said should have zero runs. That's ~500 Action runs/week when a handful would do.
2. **Deduplication was keyed on the job title**, which is mutable. The DB showed the same Databricks posting getting re-notified 19 minutes apart because the company had lightly edited the title (added "— U.S. Federal Sector"). A cosmetic edit to a live posting made it look like a brand-new job.

Once you see those two bugs, "duplicate notifications + wasted Action credits" stops being a mystery and becomes two specific fixes — which is exactly what this rewrite does, plus a few other upgrades described below.

---

## Job boards used — and why only free, public APIs

| Board | Auth required? | Why it's here |
|---|---|---|
| **Greenhouse** | No | Fully public JSON API (`boards-api.greenhouse.io`). `?content=true` returns the whole job description in one request — no per-job follow-up call needed. Used by a large slice of well-funded Bay Area AI/data startups (Databricks, Anthropic, Scale AI, Together AI, etc.). |
| **Lever** | No | Public JSON API (`api.lever.co/v0/postings/<company>`). Common among fintech/data companies (Plaid, Wealthfront, Clari). |
| **Ashby** | No | Public JSON API (`api.ashbyhq.com/posting-api/job-board/<company>`), increasingly the default ATS for newer, fast-growing YC-style startups (Ramp, Notion, OpenAI). |
| **YC "Work at a Startup"** | No | The one open *marketplace* in the mix — not curated to any single geography, so it's filtered more strictly (see below). |

**Why not scrape Wellfound/AngelList, LinkedIn, or Indeed?** They don't offer a free public API — the only way in is either a paid tier or scripting a login with real credentials against an undocumented internal API. That's fragile (breaks on any UI change), against most of these platforms' Terms of Service, and risks the account behind it. Greenhouse, Lever, and Ashby are the opposite: documented, public, no-auth, and stable — they're built to be embedded in a company's own careers page, so they're not going anywhere. That reliability is worth more than the marginal reach of one extra scraped source.

**The company-board-list trick:** Greenhouse/Lever/Ashby require picking specific companies (`board tokens`) rather than searching globally. Instead of treating that as a limitation, it's used as an implicit Bay Area filter — the board list (`config.yaml`) is curated to companies actually headquartered in the Bay Area. That means even a "Remote" posting from one of those companies is still a Bay Area company's role, which is what justifies accepting remote/US-wide locations from these three sources while being much stricter about location for YC (see next section), where the company pool isn't geographically curated at all.

---

## Filtering pipeline

Every raw posting runs through the same gauntlet before it's allowed to reach an email or WhatsApp message:

```
raw posting
   │
   ├─► 1. Role/keyword match      (must be AI Engineer / Data Scientist / Data Analyst)
   ├─► 2. Location match           (Bay Area, or remote from a curated Bay Area company;
   │                                 YC listings require an explicit Bay Area match)
   ├─► 3. Recency window           (30 min – 7 days old)
   ├─► 4. Experience filter        (0–1 yr required, hard-block on senior/lead/2+ yrs)
   ├─► 5. Spam / fake-job check    (see below)
   ├─► 6. Profile match scoring    (0–100%, skills-weighted)
   └─► 7. Dedup against DB         (stable platform ID, not title hash)
   │
   ▼
 notified (email + WhatsApp)
```

Everything from roles to skills to location lists to board tokens lives in **`config.yaml`**, not in the code — tuning what counts as a match is a config edit, not a redeploy.

---

## Fake job / spam detection

Open job marketplaces (YC's in particular) occasionally surface scam postings — "processing fee" schemes, requests for bank details up front, fake urgency ("no interview necessary, hire today"). Rather than a single yes/no filter, detection is split into two tiers so it doesn't accidentally throw away real jobs on a hunch:

- **Hard block** (dropped entirely, same as any other failed filter) — unambiguous scam signals: requests for fees/payment/bank or SSN details, "no interview necessary" + "immediate hire" combos, or a posting whose *only* contact method is a personal Gmail/Yahoo/Hotmail address with no company domain anywhere.
- **Soft flag** (still shown, but badged **⚠️ Verify Carefully** in the email and prefixed with ⚠️ in WhatsApp, plus a score penalty) — ambiguous signals that could just mean a low-effort posting rather than a scam: a very short/vague description from the un-curated YC marketplace, location text that contradicts itself (e.g. mentions a blocked country alongside "Bay Area"), or an unrealistic pay claim ("$150/hr") attached to an entry-level title.

The split matters: a hard rule that's too aggressive silently deletes real opportunities, and a soft rule that's too permissive floods the inbox again — the two-tier design is the same "quality over quantity" principle applied to trust, not just relevance.

---

## Scheduling

GitHub Actions now runs the monitor **twice a week — Tuesday and Wednesday, 10:00 AM and 2:00 PM PST**. That's not an arbitrary choice: Tuesday/Wednesday are consistently the highest-volume days for new job postings (companies plan hiring during Monday meetings, then publish reqs once decisions land), and running once in the late morning and again in the early afternoon catches both the morning batch and same-day edits — without re-scanning the same static listings dozens of times a day. Four runs a week instead of ~500.

## Persistence

The "seen jobs" SQLite DB (`job_monitor.db`) is committed straight back into the repo at the end of every run, instead of relying on `actions/cache`. The cache approach used a run-unique key, which meant restoring it depended on prefix-matching a cache from a previous run — something that can silently miss or race if two runs ever overlapped. A git commit is unambiguous: it's either in the repo or it isn't, and it's fully inspectable/diffable in the commit history.

---

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy your credentials into `.env` (Twilio for WhatsApp, SMTP for email — see the variable names already referenced in `job_monitor.py`).
3. Tune your preferences in `config.yaml` — roles, skills, locations, experience cap, and board tokens.
4. Run it:
   ```bash
   python job_monitor.py
   ```

In production, this runs via `.github/workflows/monitor.yml` on the schedule described above, with credentials pulled from repository secrets.

## Dashboard

`Dashboard.py` is a Streamlit app that reads directly from `job_monitor.db` to show trends: most in-demand roles, top hiring companies, jobs by source/weekday, and a searchable table of everything found.

```bash
streamlit run Dashboard.py
```

## Tech stack

Python · `requests` + `BeautifulSoup` (HTML→text extraction for Greenhouse/Ashby descriptions) · SQLite · Twilio (WhatsApp) · SMTP (email) · Streamlit + Plotly (dashboard) · GitHub Actions (scheduling/CI) · PyYAML (config)
