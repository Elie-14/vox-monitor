# VOX Egypt - Avengers: Doomsday booking early-warning system

Watches the official VOX Cinemas Egypt booking pages and alerts you (email + phone push + Windows sound) the moment
**Avengers: Doomsday** becomes bookable. It never logs in, never buys, never picks seats.

## 1. What was verified on 18 Oct 2026 (and what was not)

| Fact | Status |
|---|---|
| `avengers-doomsday` and `avengers-doomsday-infinity-vision` are on VOX Egypt's **Coming Soon** page, release 16 Dec 2026 | verified |
| Bookable movies appear on `/movies/whatson` with a **Showtimes** link and in the "Select Your Movie(s)" selector; Doomsday is in neither yet | verified |
| The movie page `<title>` says "Now Showing / Book Tickets" even for coming-soon films, so it is **not** used as evidence | verified |
| VOX Egypt currently lists 3 cinemas: City Centre Alexandria, City Centre Almaza, Mall of Egypt (new ones are detected automatically) | verified |
| The exact HTML of per-cinema session lists | **not verified** (nothing is on sale yet). The detector relies on the What's On card + selector + movie-page check instead. |
| Live runs against VOX from the build sandbox | **not possible** (network blocked). Tests use fixtures modelled on the verified structure. Do a real `check-sources` run yourself (step 14). |

## 2. Realistic free architecture

```
GitHub Actions (every 5 min, public repo, $0)
   -> python main.py loop --duration 270  (adaptive inside the window)
   -> reads VOX pages (+ optional feeds), state machine decides
   -> alerts: Email (Gmail SMTP)  +  ntfy.sh push  (+ Telegram optional)  (+ WhatsApp optional, paid)
Phone: ntfy app (loud "urgent" notification)       Laptop (Windows ON): windows_companion.py listens to the same ntfy topic
```
Honest limits: GitHub cron is best-effort (delays of 5-30+ min happen) and is auto-disabled after 60 days without repo
activity. For faster/more reliable running, an always-on VM is the (optional) upgrade - nothing was purchased.
Laptop sound works only while Windows is on and online.

### WhatsApp (important)
Official WhatsApp Business Cloud API needs a Meta Business account, a *separate* phone number, and an approved template.
Since **1 Oct 2026** Meta charges per delivered message even in-window, so it is **not free**. It is implemented
(`monitor/whatsapp_alerts.py`) but **disabled**. I did not use unofficial WhatsApp Web automation. Your free phone alert is
**ntfy** (or Telegram). Enable WhatsApp only after you approve the cost: set `notifications.whatsapp: true` and the `WHATSAPP_*` secrets.

### Social sources: what is really monitored
| Source | Class | Why |
|---|---|---|
| VOX Egypt site (What's On + Coming Soon) | **Automated** | public pages, polite polling |
| Facebook `facebook.com/voxcinemasegypt` | **Manual** | no free feed without login; no scraping. Turn on "All posts" notifications in the app |
| Instagram `instagram.com/voxcinemasegypt` | **Manual** | login wall. Use the bell icon > Posts |
| TikTok (linked from linktr.ee/vox_cinemas) | **Manual** | handle not exposed in the page text; unverified |
| YouTube `/user/voxcinemas` (VOX footer) | **Automated after setup** | free RSS, but you must paste the channel-ID feed URL in `config.yaml`; may be a regional channel |
| Google News RSS (discovery) | **Automated, low trust** | can only raise polling speed; can never trigger an alert alone |
| X/Twitter, regional MAF accounts | **Not added** | no verified Egypt account found; add to `config.yaml` once verified |

## 3. Setup (Windows beginner, copy-paste)

Where to run each step is in brackets.

1. **[Windows PowerShell]** `mkdir $HOME\vox_monitor; cd $HOME\vox_monitor` then copy the project files here (unzip the zip).
2. **[PowerShell]** Install Python 3.12 from python.org (tick "Add python.exe to PATH"), then `python --version`.
3. **[PowerShell]** `python -m venv .venv`
4. **[PowerShell]** `.\.venv\Scripts\Activate.ps1` (if blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`).
5. **[PowerShell]** `pip install -r requirements-dev.txt`
6. **Files**: all files are already in the zip - nothing to type by hand.
7. **Secrets (local)**: `Copy-Item .env.example .env`, then edit `.env` in Notepad. Never commit `.env`.
8. **Email (free)**: Google Account > Security > turn on 2-Step Verification > App passwords > create one. Put it in `SMTP_PASSWORD`
   (not your normal password), `SMTP_USER` = your Gmail, `ALERT_EMAIL_TO` = the address that should receive alerts.
9. **Phone push (free)**: install the **ntfy** app, subscribe to your random topic (e.g. generate one with
   `python -c "import secrets;print('vox-'+secrets.token_urlsafe(24))"`), put it in `NTFY_TOPIC`. The topic acts like a password - keep it secret.
10. **WhatsApp**: skip (see above) unless you approve the cost.
11. **Windows sound**: **[PowerShell]** `pip install -r requirements-windows.txt`, set the topic: `$env:NTFY_TOPIC="your-topic"`,
    test: `python windows_companion.py --test`, run: `python windows_companion.py`. Auto-start: press Win+R, type `shell:startup`, create a
    shortcut there to `pythonw.exe C:\path\windows_companion.py` (set NTFY_TOPIC permanently via System Environment Variables).
    Missed alerts while the laptop was off are replayed once (ntfy keeps ~12 h).
12. **Tests**: `python -m pytest -q` (expect all passed).
13. **Dry run**: `python main.py run-once --dry-run` (checks, sends nothing).
14. **Check sources**: `python main.py check-sources`, then `python main.py status`.
15. **Test alerts**: `python main.py test-alert` (all configured channels) or `--channel email` / `--channel ntfy`.

### Cloud deployment (free) **[GitHub website]**
1. Create a free GitHub account, then **New repository** (name `vox-monitor`, must be **Public** so Actions minutes are free). Upload the project files (not `.env`).
2. Settings > Secrets and variables > Actions > **New repository secret** for each: `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`, `NTFY_TOPIC`
   (optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HEALTHCHECK_PING_URL`). Your email lives in a secret because the repo is public.
3. Actions tab > enable workflows > **vox-monitor** > **Run workflow** once.
4. **Verify it runs**: Actions tab shows green runs every ~5 min; the log prints `STATE ...` lines. Secrets are masked by GitHub and by the app's log filter.
5. **Dead-man's switch (recommended, free tier at healthchecks.io - verify current limits)**: create a check, put its ping URL in `HEALTHCHECK_PING_URL`; it emails you if pings stop (covers GitHub disabling the schedule).
6. **Keep it alive**: GitHub pauses schedules after 60 days of inactivity - make a small commit to the default branch every ~month or re-enable under Actions.
7. **Update**: edit files on GitHub (or push), runs pick them up automatically. **Stop**: Actions > vox-monitor > "..." > Disable workflow.
State (SQLite) is saved on the `monitor-state` branch so restarts never resend old alerts.

## 4. States and alerts
`UNKNOWN, NOT_LISTED, COMING_SOON, ANNOUNCED, SUSPECTED_OPEN, CONFIRMED_OPEN, TEMPORARILY_UNAVAILABLE, ERROR`
* **CONFIRMED_OPEN** (critical alert) only when VOX itself shows: What's On card with Showtimes **and** selector entry **and** a movie page verified as Avengers: Doomsday.
* **SUSPECTED_OPEN** (preliminary alert, clearly labelled): only one of those site signals, or an official post saying booking is open while the site cannot confirm.
* Errors, timeouts, CAPTCHAs, 403/429, empty or re-designed pages are never "open".
* Duplicate suppression is persistent; reopening after a real closure alerts again (`realert_on_reopen`).
* If `vox_whatson` is not read OK for 45 min you get a "coverage degraded" alert (and a "recovered" one).

## 5. Commands
`python main.py run-once|loop|status|check-sources|test-alert [--dry-run]` - logs are UTC, alerts show Africa/Cairo time.
Optional always-on host: run `python main.py loop` (set `PORT` for a `/health` endpoint).

## 6. Polling policy
normal 300 s, elevated 120 s (announcement/news hint), high 75 s (suspected open), 3600 s after confirmation; never below 60 s,
hard cap 150 requests/hour, robots.txt respected, ETag/Last-Modified used when supplied, exponential backoff with jitter, stops when blocked.
Inside GitHub Actions the effective floor between runs is ~5 min.
