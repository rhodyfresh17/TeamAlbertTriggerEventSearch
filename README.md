# Sales Territory Trigger Event Scraper

[![Trigger Event Scraper](https://github.com/mwjacobs3/TriggerEventSearch/actions/workflows/scraper.yml/badge.svg)](https://github.com/mwjacobs3/TriggerEventSearch/actions/workflows/scraper.yml)

Monitor news sources for sales trigger events (CFO hires, M&A, acquisitions, funding) in your territory. Get alerts when potential opportunities arise.

## Features

- **25+ news sources**: Industry publications, PR wires, funding news, and Google News
- **Open finance seats**: Adzuna job-posting API (CFO / Controller / VP Finance postings with structured company names)
- **SEC filings**: EDGAR 8-K executive changes and Form D private raises
- **Interactive dashboard**: Streamlit dashboard for reviewing and managing leads
- **Lead tracking**: Track leads through stages (new → reviewing → contacted → closed)
- **PE-backed bypass**: Automatically includes PE-backed acquisitions regardless of territory
- **Recency prioritized**: Most recent news first, shows "5 min ago" timestamps
- **Territory filtering**: Filter by US states, Canadian provinces, and major cities
- **Industry targeting**: Healthcare, Nonprofit, Hospitality, Restaurant/Franchise, Construction, Field Services, Energy, Oil & Gas, Insurance, Casino/Gaming, Transportation/Logistics, Travel/Hotels, Airlines/Aviation, Child Services, Medical Labs, Business Services, and more
- **Smart filtering**: Skips public companies (mega-cap blocklist + public-company indicators) and excluded industries at scrape time
- **Multiple alert channels**: Email, Slack, File, Desktop notifications
- **Automated runs**: GitHub Actions runs every 4 hours

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run once
python -m src.main

# Run continuously (checks every 30 minutes)
python -m src.main --daemon

# View statistics
python -m src.main --stats

# Launch the dashboard
python3 -m streamlit run dashboard.py
```

## Interactive Dashboard

The Streamlit dashboard provides an interactive interface to manage and review trigger event alerts.

### Running the Dashboard

```bash
# From the project directory
python3 -m streamlit run dashboard.py

# Opens at http://localhost:8501
```

### Dashboard Features

- **Filtering**: Filter by event type, date range, lead status, and search terms
- **Lead Management**: Track leads through stages (new, reviewing, contacted, interested, closed)
- **Notes**: Add notes to each lead for tracking conversations
- **Multiple Views**:
  - **Card View**: Detailed view with full descriptions and status controls
  - **Table View**: Quick scanning of all events with sortable columns
  - **Analytics**: Charts showing events by type, status, over time, and top companies
- **Export**: Download filtered results as CSV for use in CRM or spreadsheets

### Lead Statuses

| Status | Icon | Description |
|--------|------|-------------|
| NEW | 🆕 | Unreviewed event |
| REVIEWED - ON REP TAL | 🟠 | On rep's target account list |
| REVIEWED - NetSuite Customer | 💼 | Already a NetSuite customer |
| REVIEWED - Out of Alignment | ❌ | Not a fit for territory/criteria |

## How It Works

1. **Scrapes** 25+ RSS feeds, Google News, SEC EDGAR (8-K / Form D) and the Adzuna job API for trigger events
2. **Filters** by date (last 7 days), territory, and industry
3. **Dedups** by URL, syndicated headline and job-posting key (SQLite, persistent across runs)
4. **Alerts** via email with most recent events first

## Output Example

```
EVENT SUMMARY (Most Recent First)
============================================================

1. [CFO_HIRE] Regional Healthcare Names New CFO...
   Published: 2026-02-05 14:30 (5 min ago)
   Company: Regional Healthcare Inc
   Source: Business Wire
   Relevance: 85%
```

## Configuration

Edit `config.yaml` to customize:

### Territory Settings
```yaml
territory:
  name: "East Coast & Eastern Canada"
  regions:
    - "New York"
    - "Massachusetts"
    # ... add your states/provinces
  cities:
    - "Boston"
    - "Toronto"
    # ... add your target cities
```

### Company Filters
```yaml
  company_filters:
    exclude_public_companies: true
    public_company_indicators: ["NYSE", "NASDAQ", "publicly traded"]   # ticker / listing markers
    excluded_public_companies: ["GS Finance Corp", "Amazon", "Microsoft"]  # mega-cap blocklist (whole-word)
    target_size_indicators: ["mid-market", "privately held"]
```

### Job Postings (Adzuna)
Open finance seats come from the Adzuna API — see the `adzuna:` section of
`config.yaml`. Credentials are the `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`
environment variables (GitHub Secrets in CI, `.env` locally).

### Scraper Settings
```yaml
scraper:
  max_age_hours: 168     # 7 days
  check_interval: 30     # Minutes between daemon checks
```

### Alert Configuration

**Email Alerts:**
```yaml
alerts:
  email:
    enabled: true
    smtp_server: "smtp.gmail.com"
    smtp_port: 587
    sender_email: "your-email@gmail.com"
    sender_password: "your-app-password"  # Use Gmail app password
    recipient_emails:
      - "you@example.com"
```

**Slack Alerts:**
```yaml
alerts:
  slack:
    enabled: true
    webhook_url: "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
```

## Event Types Monitored

1. **CFO Hires** - New Chief Financial Officer appointments
2. **Executive Hires** - VP Finance, Controller, Finance Director
3. **M&A Activity** - Mergers, acquisitions, buyouts
4. **Funding Events** - Series A/B/C, private equity investments
5. **Job Postings** - Companies hiring for finance leadership roles

## GitHub Actions Setup

The scraper runs automatically every 4 hours. Set these secrets:

| Secret | Description |
|--------|-------------|
| `SENDER_EMAIL` | Gmail address for sending alerts |
| `EMAIL_PASSWORD` | Gmail app password |
| `ALERT_RECIPIENT` | Alert recipient (injected at runtime — never committed) |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Adzuna job API credentials |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Sync-step target (sync is skipped when unset) |

## Data Sources

### Job Postings
| Source | Description |
|--------|-------------|
| Adzuna | Job-posting API — open CFO / Controller / VP Finance seats with structured company names |

### SEC Filings
| Source | Description |
|--------|-------------|
| EDGAR 8-K | Executive-change filings (Item 5.02) and M&A items |
| EDGAR Form D | Private capital raises, territory-filtered |

### News & PR (25+ sources)
| Category | Sources |
|----------|---------|
| PR Wires | Business Wire, PR Newswire, Globe Newswire, Newswire.com, 1888 Press Release |
| Business News | Reuters Business, Business Insider |
| Canadian | CBC Business, Financial Post |
| Funding/M&A | Crunchbase News, PEHub |
| Healthcare | Fierce Healthcare |
| Nonprofit | Nonprofit Times, Nonprofit Quarterly, ProPublica |
| Restaurant | QSR Magazine |
| Insurance | Insurance Journal |
| Construction | Construction Dive |
| Energy | Utility Dive, Solar Power World |
| Data Centers | Data Center Dynamics, Data Center Knowledge |
| Hospitality | Hotel Management, Hotel Dive |
| Casino/Gaming | CDC Gaming Reports, SBC Americas |
| Transport | Supply Chain Brain, FreightWaves |
| Travel | Skift |
| Search | Google News (aggregated) |

## Relevance Scoring

Events are scored 0-100 based on:
- Event type (CFO hire = 45pts, M&A = 35pts)
- Territory match (up to 20pts)
- Industry match (15pts)
- Target company match (50pts bonus)

## Usage Examples

```bash
# Run with custom config
python -m src.main --config my-territory.yaml

# Clean up old entries (older than 60 days)
python -m src.main --cleanup 60

# Check statistics
python -m src.main --stats
```

## Troubleshooting

**No events found:**
- Check your territory regions match news content
- Verify industry keywords are relevant
- Some sources may have rate limits

**Email not sending:**
- For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833)
- Check SMTP settings and firewall

**Too many irrelevant results:**
- Add companies to exclusion list
- Tighten territory matching

**Still seeing public companies:**
- Add the name to `territory.company_filters.excluded_public_companies` in `config.yaml`
- Check `public_company_indicators` covers the wording used in the release

## License

MIT License
