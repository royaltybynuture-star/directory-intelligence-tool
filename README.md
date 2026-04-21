# Directory Intelligence Tool

Find non-obvious public directories where your ICP shows up, scrape them into Clay-ready CSVs, and get a campaign angle — all from the command line.

Three commands, each standalone:

- **`find`** — research non-obvious directories for your ICP: conference speaker lists, governing body pages, regulatory filings, permit databases. Not LinkedIn, not Apollo.
- **`extract`** — scrape a directory into a clean CSV. Plans the approach, handles HTML/JS/PDF/API pages, verifies the output. Honest about what it can't automate.
- **`strategize`** — assess whether a directory is a genuine fit for your offer, and propose the campaign angle.

You can run all three in sequence, or use any one by itself.

## Why This Exists

The best list for your next campaign is probably sitting on a page nobody thought to check — a governing body directory, a conference speaker list, a certification database. Real contacts, often senior, not being touched by anyone running a standard database search. The problem is finding those pages, pulling the data, and knowing what to do with it. That's what this is for.

Results vary by vertical. Highly organized industries (cybersecurity, finance, legal) tend to have richer public directory infrastructure than others.

---

## Setup

1. **Clone or copy this folder.**

2. **Install dependencies and register the `dit` command:**
   ```bash
   pip install -e .
   ```
   This puts `dit` on your PATH so you can run `dit find`, `dit extract 1`, etc. from any directory.

   If you'd rather not install globally, use `pip install -r requirements.txt` and replace `dit` with `python dit.py` in all commands below.

3. **Install the Playwright browser** (used by `extract` for JS-rendered pages):
   ```bash
   playwright install chromium
   ```

4. **Add your Anthropic API key.** Copy `.env.example` to `.env` and fill in:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   Get one at https://console.anthropic.com/. Web search must be enabled on your account for `find` to work.

5. **Create a profile** (required for `find` and `strategize`; `extract` doesn't use it):
   ```bash
   dit onboard
   ```
   This walks you through company, ICP, industries, geo, and exclusions. Takes 2 minutes.

   You can skip this step if you only plan to use `extract`.

---

## Quick start

After setup, the typical first run looks like this:

```bash
dit find                  # research 8 directories for your ICP
dit extract 1             # scrape the first one from that run
dit strategize 1          # assess fit + write a campaign angle
```

`dit extract 1` automatically pulls directory #1 from your most recent `find` run on the active profile. No workspace IDs needed. The same shorthand works for `strategize`.

---

## Commands reference

### `find` — research non-obvious directories

Uses your saved profile to surface 8 directories with direct URLs, record counts, page type, and scraping difficulty (1–5).

```bash
dit find                                        # use saved profile
dit find --geo "EMEA"                           # override geo for this run
dit find --icp "CISOs" --solution "cyber training" --exclude "RSA,Evanta"
dit find --more                                 # append new directories to your last run
dit find --profile cybersecurity                # use a specific profile without switching active
```

Output: `workspace/<profile>/<run_id>/find_report.md` and `directories.json`.

---

### `extract` — scrape a directory

Does not read your profile — it just scrapes what you point it at.

```bash
dit extract 1                                   # directory #1 from your latest find run
dit extract 3                                   # directory #3 from your latest find run
dit extract https://example.com/directory       # scrape a URL directly, no find needed
dit extract 2 --yes                             # skip the plan confirmation prompt
dit extract 2 --no-session                      # ignore any saved browser session for this run
```

`dit extract 1` is the standard path. You only need `--from-find <run_id>` if you're reaching back to an older run, not your most recent one.

The planner classifies the page (static HTML / JS-rendered / PDF / API / login-gated) and routes to the right scraper. If it can't be automated, it writes step-by-step manual instructions instead of pretending.

Output: `extracted_<N>.csv` (Clay-ready) and `extract_summary_<N>.md` in the run's workspace folder.

---

### `strategize` — assess fit + propose campaign angle

Reads your profile plus any `.md`/`.txt` files you've dropped in `context/` (campaign playbooks, frameworks, past-winner analyses).

```bash
dit strategize 1                                # directory #1 from your latest run (uses extract CSV if available, else find metadata)
dit strategize 3                                # directory #3, same logic
dit strategize https://example.com/directory    # assess a URL without running find or extract first
dit strategize "Directory of X" --sample-csv data.csv  # from a description + your own CSV
```

Output: `strategy_<N>.md` — relevance verdict, reasoning, campaign angle, and personalization opportunities. If the directory isn't a fit, it says so instead of inventing a reason.

---

### `onboard` — create or edit a profile

```bash
dit onboard                    # create or edit the default profile
dit onboard --name fintech     # create or edit a named profile
```

Re-running `onboard` on an existing profile shows current values as defaults. You can accept, edit, or overwrite each field.

---

### `profiles` — list or switch profiles

```bash
dit profiles                   # list all profiles (* marks the active one)
dit profiles --use fintech      # switch the active profile
```

---

## Profiles

You can keep multiple named profiles — one per vertical, persona, or go-to-market motion — and switch between them without re-onboarding.

```bash
dit onboard --name cybersecurity    # create or edit the cybersecurity profile
dit onboard --name landscaping      # create or edit the landscaping profile
dit profiles                        # list all; * marks active
dit profiles --use landscaping      # switch active
dit find --profile cybersecurity    # one-off: use a specific profile without switching active
```

- `find` and `strategize` use the active profile. `extract` doesn't use profiles at all.
- Each profile's runs are stored separately: `workspace/cybersecurity/`, `workspace/landscaping/`, etc.
- `dit extract 1` and `dit strategize 1` pull from the active profile's latest run.
- If you have a legacy `profile.json` from an older version, it's silently migrated to `profiles/default.json` on first run.

---

## Usage tips

**1. The standard pipeline**

```bash
dit find
dit extract 1
dit strategize 1
```

That's it. No run IDs, no flags. Three commands, end to end.

---

**2. Scraping a URL you found yourself**

You don't need to run `find` first. Point `extract` directly at any URL:

```bash
dit extract https://www.evanta.com/cio-los-angeles/governing-body
```

Then strategize with a description:

```bash
dit strategize "Evanta CIO Los Angeles governing body — 60 security leaders at F500 companies" --sample-csv workspace/standalone/20260420_093000/extracted_1.csv
```

---

**3. Working with multiple verticals**

Create one profile per vertical. Switch active before running:

```bash
dit onboard --name smb-retail
dit onboard --name enterprise-security
dit profiles --use smb-retail
dit find
```

Or use `--profile` for a one-off run without switching:

```bash
dit find --profile enterprise-security
```

---

**4. Sites that block scrapers**

`extract` tries HTTP first. If that returns a 403 or fails, it automatically falls back to a headless Chromium probe before classifying the page. No action needed on your end.

If you have a Firecrawl API key, set `FIRECRAWL_API_KEY` in `.env` and install the package:

```bash
pip install -e ".[firecrawl]"
```

When set, Firecrawl runs automatically as a last-resort fallback after the pre-built scrapers fail but before the tool writes manual instructions.

---

**5. Login-gated directories**

When the planner classifies a page as `custom` (login-gated or complex auth), it launches a browser you can interact with. After you log in and reach the directory listing, the scraper takes over. It will offer to save your session cookies so you don't have to log in again next time.

To force a fresh login (ignore saved session):

```bash
dit extract 2 --no-session
```

---

**6. Adding more directories to an existing find run**

If your first `find` run didn't surface enough options, continue it:

```bash
dit find --more
```

This appends new directories to the existing run, skipping URLs already surfaced.

---

**7. Strategize without running find or extract**

You can use `strategize` completely standalone — just describe the directory and optionally pass a CSV:

```bash
dit strategize "G2 Top Rated badge recipients for project management software, 2024"
dit strategize "Techstars alumni directory for the NYC 2023 cohort" --sample-csv alumni.csv
```

---

## Requirements

- **Python 3.11+**
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com/). Web search must be enabled on the account for `find` to work.
- **Playwright / Chromium** — `playwright install chromium` after pip install. Required for JS-rendered pages.
- **Optional: Firecrawl** — `pip install -e ".[firecrawl]"` + `FIRECRAWL_API_KEY` in `.env`. Adds a fallback scraper tier for stubborn pages.

---

## File layout

```
.
├── .env                          # API keys (gitignored)
├── profiles/                     # named profiles (gitignored)
│   ├── default.json
│   ├── cybersecurity.json
│   └── .active                   # tracks the active profile name
├── context/                      # drop strategy docs here for strategize (gitignored)
├── workspace/                    # per-run artifacts (gitignored)
│   └── <profile>/
│       └── <run_id>/
│           ├── find_report.md
│           ├── directories.json
│           ├── extracted_<N>.csv
│           ├── extract_summary_<N>.md
│           └── strategy_<N>.md
├── commands/                     # find / extract / strategize / onboard / profiles
├── core/                         # profile IO, workspace management, Claude client
├── skills/                       # html / js / pdf / api / custom / firecrawl scrapers + verify
└── dit.py                        # CLI entry point
```

## Who This Is For

GTM engineers, founders, and sales operators who want to build outbound lists from sources nobody else is pulling from. Useful whether you're running campaigns yourself or building lists for clients.

## Legal & Attribution

These techniques work on publicly accessible data. Read the ToS, know your jurisdiction, use judgment.

Built by [Royal Godwin](https://www.linkedin.com/in/royalgodwin) — GTM engineer.
