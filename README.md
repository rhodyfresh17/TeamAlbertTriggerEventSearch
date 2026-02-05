# Sales Territory Trigger Event Scraper

[![Trigger Event Scraper](https://github.com/mwjacobs3/TriggerEventSearch/actions/workflows/scraper.yml/badge.svg)](https://github.com/mwjacobs3/TriggerEventSearch/actions/workflows/scraper.yml)

Monitor news sources for sales trigger events (CFO hires, M&A, acquisitions, funding) in your territory. Get alerts when potential opportunities arise.

## Features

- **22+ news sources**: Industry-specific publications, PR wires, funding news, and Google News
- **6 job boards**: Indeed, ZipRecruiter, SimplyHired, Google Jobs, Ladders ($100K+), CFO.com
- **Company verification**: Apollo.io validates company size and public/private status before alerting
- **Recency prioritized**: Most recent news first, shows "5 min ago" timestamps
- **Territory filtering**: Filter by US states, Canadian provinces, and major cities
- **Industry targeting**: Healthcare, Nonprofit, Hospitality, Restaurant/Franchise, Construction, Field Services, Energy, Oil & Gas, Insurance, Casino/Gaming, Transportation/Logistics, Travel/Hotels, Airlines/Aviation
- **Smart filtering**: Skips public companies, verifies 20-2000 employees, $20M-$500M revenue
- **Multiple alert channels**: Email, Slack, File, Desktop notifications
- **Automated runs**: GitHub Actions runs every 3 hours

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
```

## How It Works

1. **Scrapes** 22+ RSS feeds and 6 job boards for trigger events
2. **Filters** by date (last 7 days), territory, and industry
3. **Verifies** companies via Apollo.io API:
   - Skips public companies (NYSE/NASDAQ)
   - Skips companies with >2,000 employees
   - Skips companies with >$500M revenue
4. **Alerts** via email with most recent events first

```
Verifying companies via Apollo.io...
  PASS: Regional Healthcare - Meets criteria
  SKIP: Microsoft - Public company (MSFT)
  SKIP: Big Corp - Too large (50,000 employees, max 2,000)
```

## Output Example

```
EVENT SUMMARY (Most Recent First)
============================================================

1. [CFO_HIRE] Regional Healthcare Names New CFO...
   Published: 2026-02-05 14:30 (5 min ago)
   Company: Regional Healthcare Inc
   Employees: 450
   Revenue: $75M - $100M
   Industry: Healthcare
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
    min_employees: 20
    max_employees: 2000
    min_revenue_millions: 20
    max_revenue_millions: 500
```

### Job Board Settings
```yaml
job_search:
  enabled: true
  titles:
    - "CFO"
    - "Chief Financial Officer"
    - "Controller"
    - "Finance Director"
  boards:
    indeed: true
    ziprecruiter: true
    simplyhired: true
    google_jobs: true
    ladders: true        # Executive jobs ($100K+)
    cfo_com: true        # CFO-specific news
```

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

The scraper runs automatically every 3 hours. Set these secrets:

| Secret | Description |
|--------|-------------|
| `SENDER_EMAIL` | Gmail address for sending alerts |
| `EMAIL_PASSWORD` | Gmail app password |
| `APOLLO_API_KEY` | Apollo.io API key for company verification |

## Data Sources

### Job Boards (6 sources)
| Source | Description |
|--------|-------------|
| Indeed | General job board |
| ZipRecruiter | Job aggregator |
| SimplyHired | Job search engine |
| Google Jobs | Job announcements via Google News |
| Ladders | Executive jobs $100K+ |
| CFO.com | CFO-specific hiring news |

### News & PR (22+ sources)
| Category | Sources |
|----------|---------|
| PR Wires | Business Wire, PR Newswire, Globe Newswire |
| Funding/M&A | Crunchbase News, PEHub |
| Healthcare | Fierce Healthcare |
| Nonprofit | Nonprofit Times, Nonprofit Quarterly |
| Restaurant | QSR Magazine, Franchise Wire |
| Insurance | Insurance Journal |
| Construction | Construction Dive |
| Energy | Utility Dive, Solar Power World, WebWire Oil & Energy |
| Hospitality | Hotel Management, Hotel Dive |
| Casino/Gaming | CDC Gaming Reports, SBC Americas |
| Transport | Supply Chain Brain, FreightWaves |
| Travel | Skift |
| Aviation | Simple Flying |
| Search | Google News (aggregated) |

## Relevance Scoring

Events are scored 0-100 based on:
- Event type (CFO hire = 45pts, M&A = 35pts)
- Territory match (up to 20pts)
- Industry match (15pts)
- Target company match (50pts bonus)
- Ladders/CFO.com sources get +10 bonus

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
- Ensure Apollo API key is set for company verification
- Add companies to exclusion list
- Tighten territory matching

**Still seeing public companies:**
- Set `APOLLO_API_KEY` secret in GitHub Actions
- Apollo verifies public/private status before alerting

## License

MIT License
