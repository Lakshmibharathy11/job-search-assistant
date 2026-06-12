# Bay Area Startup Job Monitor 🚀

Automatically monitors **Wellfound**, **Greenhouse**, and **YC Work at a Startup**
for entry-level / new-grad **Data Scientist, Data Analyst, AI Engineer, and ML Engineer**
roles in the Bay Area. Sends **WhatsApp + email alerts** for every new match.
Runs on GitHub Actions free tier.

---

## Table of Contents

1. [Repository Setup](#1-repository-setup)
2. [Twilio WhatsApp Credentials](#2-twilio-whatsapp-credentials)
3. [Gmail App Password](#3-gmail-app-password)
4. [Adding GitHub Secrets](#4-adding-github-secrets)
5. [Testing Locally](#5-testing-locally)
6. [How the Smart Scheduler Works](#6-how-the-smart-scheduler-works)
7. [Monthly Runtime Budget](#7-monthly-runtime-budget)
8. [Customising](#8-customising)
9. [Troubleshooting](#9-troubleshooting)

---

## 1 · Repository Setup

```bash
# Clone or create your repo
git clone https://github.com/<you>/<your-repo>.git
cd <your-repo>

# Copy these files in
cp job_monitor.py requirements.txt .
cp -r .github .

# Commit and push
git add .
git commit -m "Add job monitor"
git push
```

GitHub Actions will automatically pick up `.github/workflows/monitor.yml` and
start running on the cron schedule.

---

## 2 · Twilio WhatsApp Credentials

### Step-by-step: Twilio Sandbox setup

1. **Create a free Twilio account** at https://www.twilio.com/try-twilio

2. In the Twilio Console, navigate to:
   **Messaging → Try it out → Send a WhatsApp message**

3. You'll see a **Sandbox number** (e.g. `+1 415 523 8886`) and a
   **join code** (e.g. `join apple-mango`).

4. **From your personal WhatsApp**, send the join code to the sandbox number:
   ```
   join apple-mango
   ```
   You'll receive a confirmation reply. You must do this once — it activates
   your number for the sandbox.

5. Find your credentials in **Account → API keys & tokens**:
   - **Account SID** — starts with `AC…`
   - **Auth Token** — click the eye icon to reveal

6. Your environment variables will be:

   | Variable | Value |
   |---|---|
   | `TWILIO_ACCOUNT_SID` | `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
   | `TWILIO_AUTH_TOKEN`  | `your_auth_token` |
   | `TWILIO_FROM_NUMBER` | `whatsapp:+14155238886` |
   | `TWILIO_TO_NUMBER`   | `whatsapp:+1XXXXXXXXXX` (your number, with country code) |

> **Note:** The free Twilio sandbox requires you to re-join every 72 hours.
> For production, upgrade to a paid Twilio number (~$1/month).

---

## 3 · Gmail App Password

Standard Gmail passwords don't work with SMTP. Use an **App Password** instead.

1. Go to your Google Account → **Security**
2. Under "How you sign in to Google", enable **2-Step Verification** if not already on
3. Then go to: https://myaccount.google.com/apppasswords
4. Select app: **Mail** → Select device: **Other (custom name)** → type "Job Monitor"
5. Click **Generate** — copy the 16-character password

| Variable | Value |
|---|---|
| `SMTP_HOST`     | `smtp.gmail.com` |
| `SMTP_PORT`     | `587` |
| `SMTP_USER`     | `your.email@gmail.com` |
| `SMTP_PASSWORD` | the 16-char App Password (no spaces) |
| `EMAIL_TO`      | where to send alerts (can be the same address) |

---

## 4 · Adding GitHub Secrets

1. Go to your repo on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** for each variable:

```
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_FROM_NUMBER
TWILIO_TO_NUMBER
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
EMAIL_TO
```

Secrets are encrypted and never visible in logs.

---

## 5 · Testing Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set env vars in your shell (or create a .env file and source it)
export TWILIO_ACCOUNT_SID="ACxxx"
export TWILIO_AUTH_TOKEN="xxx"
export TWILIO_FROM_NUMBER="whatsapp:+14155238886"
export TWILIO_TO_NUMBER="whatsapp:+1XXXXXXXXXX"
export SMTP_USER="you@gmail.com"
export SMTP_PASSWORD="xxxx xxxx xxxx xxxx"
export EMAIL_TO="you@gmail.com"

# Run (will exit early outside active hours — that's normal)
python job_monitor.py

# To bypass the scheduler for a one-off test, temporarily edit the
# should_run_now() call in run() to always return True:
#   if not should_run_now(conn):   →   if False:
```

Logs appear in both stdout and `job_monitor.log`.
The SQLite database (`job_monitor.db`) stores all seen jobs.

```bash
# Inspect the database
sqlite3 job_monitor.db "SELECT title, company, source, notified_at FROM seen_jobs ORDER BY notified_at DESC LIMIT 20;"
```

---

## 6 · How the Smart Scheduler Works

The GitHub Actions cron fires every 20 minutes, but `should_run_now()` exits
early unless the current PST time falls in an active window:

| Day | Active Windows | Interval |
|---|---|---|
| Tuesday / Wednesday / Thursday | 8 AM – 12 PM | Every 20 min |
| Monday / Friday | 9 AM – 11 AM | Every 60 min |
| All weekdays | 12 PM – 5 PM | Every 180 min |
| All weekdays | 7 PM (one check) | Once |
| Saturday / Sunday | — | No runs |

### Learning mode

After **30 jobs have been found**, the script analyses which hours produced the
most results and assigns weights (0–1) to each (weekday, hour) slot.  Slots with
weight < 0.3 are suppressed — the script skips that window on future runs.
Weights are stored in `schedule_weights` in SQLite and updated every run.

---

## 7 · Monthly Runtime Budget

```
Tue/Wed/Thu: 4h × 3/20 runs/h × 3 days × 4.3 weeks = 31.0 runs
Mon/Fri    : 2h × 1/60 runs/h × 2 days × 4.3 weeks =  2.9 runs
Off-peak   : 5h × 1/180 runs/h × 5 days × 4.3 weeks = 3.0 runs
7 PM check : 1 run × 5 days × 4.3 weeks              = 21.5 runs
─────────────────────────────────────────────────────────────────
Total runs/month                                       ≈ 58 runs
Average run time                                       ≈ 2 min
Estimated monthly runtime                              ≈ 116 min
─────────────────────────────────────────────────────────────────
Free tier limit                                        2,000 min
Your budget target                                     1,000 min
Actual usage                                           ≈ 116 min ✓
```

Even with generous rounding (3 min/run), you land at ~174 min — under 9% of the
2,000-minute free limit.

---

## 8 · Customising

### Add more Greenhouse companies

Find a company's Greenhouse slug: visit their jobs page
(e.g. `boards.greenhouse.io/acme`) and grab `acme`.

```python
# In job_monitor.py, add to GREENHOUSE_BOARDS:
GREENHOUSE_BOARDS = [
    ...,
    "acme",
    "your-startup",
]
```

### Change role keywords

```python
TARGET_KEYWORDS = [
    "data scientist",
    "data analyst",
    "ai engineer",
    "machine learning engineer",
    "research scientist",   # add any keyword here
]
```

### Extend the lookback window

```python
LOOKBACK_MINUTES = 60   # widen to 1 hour if you want more coverage
```

### Add gitignore

```
# .gitignore
job_monitor.db
job_monitor.log
.env
__pycache__/
*.pyc
```

---

## 9 · Troubleshooting

| Symptom | Fix |
|---|---|
| Script exits immediately | You're outside active scheduling hours — expected behaviour |
| No WhatsApp messages | Check you joined the Twilio sandbox; verify `TWILIO_TO_NUMBER` includes country code |
| SMTP auth error | Regenerate App Password; make sure 2-Step Verification is on |
| Wellfound returns 0 jobs | Wellfound's page structure may have changed; check `__NEXT_DATA__` key paths |
| Greenhouse board 404 | The board slug is wrong or that company doesn't use Greenhouse — remove it |
| Database resets between runs | The `actions/cache` step failed; check the Actions log for cache errors |
| Too many notifications | Narrow `LOCATION_ALLOW` or add more terms to `SENIORITY_BLOCK` |

---

*Built for the GitHub Actions free tier. No Claude API required.*
