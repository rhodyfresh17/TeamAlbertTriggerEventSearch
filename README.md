# Sales Territory Trigger Event Scraper

Monitor news sources for sales trigger events (CFO hires, M&A, acquisitions, funding) in your territory. Get alerts when potential opportunities arise.

## Features

- **Multi-source monitoring**: PR Newswire, Business Wire, Globe Newswire, SEC EDGAR, Google News
- **Territory filtering**: Filter by US states, Canadian provinces, and major cities
- **Industry targeting**: Focus on Healthcare, Nonprofit, Hospitality, Restaurant/Franchise, Construction, Field Services, Energy, Oil & Gas, Insurance
- **Company size filtering**: Target mid-market private companies (20-2000 employees, $20M-$500M revenue)
- **Public company exclusion**: Automatically filters out NYSE/NASDAQ listed companies
- **Multiple alert channels**: File, Email, Slack, Desktop notifications
- **Deduplication**: SQLite database tracks seen events to avoid duplicates

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

### Target Industries
```yaml
  industries:
    - "Healthcare"
    - "Hospital"
    - "Nonprofit"
    - "Restaurant"
    - "Franchise"
    - "Construction"
    - "Insurance"
    # ... customizable
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

## Output

Alerts are saved to the `alerts/` directory:
- `alert_batch_YYYYMMDD_HHMMSS.txt` - Human-readable summary
- `alert_batch_YYYYMMDD_HHMMSS.json` - Machine-readable JSON

Each alert includes:
- Event type and title
- Company name and location
- Source and publication date
- Relevance score
- Matched keywords and regions
- Direct link to source

## Usage Examples

```bash
# Run with custom config
python -m src.main --config my-territory.yaml

# Clean up old entries (older than 60 days)
python -m src.main --cleanup 60

# Check statistics
python -m src.main --stats
```

## Relevance Scoring

Events are scored 0-100 based on:
- Event type (CFO hire = 40pts, M&A = 35pts)
- Territory match (up to 30pts)
- Industry match (20pts)
- Target company match (50pts bonus)

## Data Sources

| Source | Content | Update Frequency |
|--------|---------|------------------|
| Business Wire | Press releases | Real-time |
| PR Newswire | Press releases | Real-time |
| Globe Newswire | Press releases | Real-time |
| SEC EDGAR | 8-K filings | Real-time |
| Google News | News aggregation | Varies |

## Troubleshooting

**No events found:**
- Check your territory regions match news content
- Verify industry keywords are relevant
- Some sources may have rate limits

**Email not sending:**
- For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833)
- Check SMTP settings and firewall

**Too many irrelevant results:**
- Add more specific industries to the exclusion list
- Tighten territory matching

## License

MIT License
