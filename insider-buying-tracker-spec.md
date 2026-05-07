# Insider Buying Tracker — Claude Code Spec

## Overview

A Python pipeline that runs twice weekly via GitHub Actions, pulls all open-market insider **purchases** from SEC EDGAR Form 4 filings, enriches each with company context and price charts from Polygon.io, and publishes a cumulative weekly HTML report to GitHub Pages. No frameworks. No databases. Just flat files, Python, and static HTML.

---

## Why Insider Buys Specifically

Insider *sales* are noisy — executives sell for tax planning, diversification, divorce, house purchases. Insider *buys* are signal-rich: an officer spending their own money on open-market shares is one of the strongest publicly available indicators of conviction. This tool captures only transaction code `P` (open-market purchase) on non-derivative securities, filtering out grants (`A`), option exercises (`M`, `X`, `F`), gifts (`G`), and other noise.

---

## Architecture

```
GitHub Actions (cron: Tue + Fri 6:00 AM MT)
    │
    ├── 1. fetch_form4s.py        → Pull recent Form 4 filings from EDGAR
    │       └── EFTS search API (efts.sec.gov) — no API key needed
    │       └── XML parsing of each filing
    │       └── Filter to transaction code "P" + acquired ("A")
    │       └── Output: data/raw/{YYYY-WW}.json
    │
    ├── 2. enrich.py              → Add ticker, sector, market cap context
    │       └── SEC company_tickers.json (CIK → ticker mapping)
    │       └── Polygon Ticker Details V3 (company description, SIC, market cap)
    │       └── Polygon Ticker News (recent headlines)
    │       └── Output: data/enriched/{YYYY-WW}.json
    │
    ├── 3. fetch_charts.py        → Generate price charts for each company
    │       └── Polygon Aggregates (daily bars, 6-month lookback)
    │       └── matplotlib → base64 PNG (inline in HTML)
    │       └── Output: data/charts/{YYYY-WW}/{TICKER}.png
    │
    ├── 4. score_and_rank.py      → Score and rank the buys by significance
    │       └── Output: data/scored/{YYYY-WW}.json
    │
    ├── 5. generate_report.py     → Render weekly HTML report
    │       └── Jinja2 template → docs/reports/{YYYY-WW}.html
    │       └── Update docs/index.html (landing page + report archive)
    │
    └── 6. git commit + push      → GitHub Pages auto-deploys from /docs
```

---

## Data Sources

### SEC EDGAR (Free, No API Key)

**EFTS Search:** `https://efts.sec.gov/LATEST/search-index` — query Form 4 filings by date range. No auth, just User-Agent header. 10 req/sec.

**Filing XML:** `https://www.sec.gov/Archives/edgar/data/{CIK}/{accession}/{doc}` — structured XML with all transaction details.

**CIK Mapping:** `https://www.sec.gov/files/company_tickers.json` — CIK → ticker + company name.

### Polygon.io (Free Tier)

Free tier: 5 API calls/minute. This is the binding constraint — the pipeline must be designed around it.

**Ticker Details V3:** `GET /v3/reference/tickers/{ticker}` — returns company `description`, `sic_description`, `market_cap`, `total_employees`, `homepage_url`, `branding.icon_url`.

**Ticker News:** `GET /v2/reference/news?ticker={ticker}&limit=5` — returns recent headlines with `title`, `article_url`, `publisher.name`, `published_utc`.

**Aggregates (Bars):** `GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}` — daily OHLCV bars for chart generation.

**Rate limit strategy at 5 calls/min:**
- Each ticker needs ~3 Polygon calls (details, news, aggregates)
- That's ~1.67 tickers per minute, or ~100 tickers per hour
- A typical week has 30-80 unique tickers with insider buys → 1-2.5 hours of Polygon calls
- Use aggressive caching: if a ticker was enriched earlier in the same week (Tuesday run), skip it on Friday
- Cache company details for 30 days (they rarely change)
- Priority: enrich highest-scored transactions first, stop if hitting time/rate limits

---

## Pipeline Stage Details

### Stage 1: `fetch_form4s.py`

**Purpose:** Find and parse all Form 4 filings since the last run.

**Logic:**
1. Read `data/checkpoint.json` for last run date.
2. Query EFTS for Form 4 filings in window. Paginate (100 per page).
3. For each filing:
   - Fetch filing index page to locate the `.xml` primary document
   - Parse the XML
4. Extract from `<nonDerivativeTransaction>` where `transactionCode == "P"` and `transactionAcquiredDisposedCode == "A"`:
   - `transactionShares`, `transactionPricePerShare`, `transactionDate`
   - `sharesOwnedFollowingTransaction`
5. Extract reporting owner: `rptOwnerName`, `rptOwnerCik`, `officerTitle`, `isDirector`, `isOfficer`, `isTenPercentOwner`
6. Extract issuer: `issuerCik`, `issuerName`, `issuerTradingSymbol`
7. Compute: `totalValue = shares × price`, `percentageIncrease = shares / (sharesAfter - shares) × 100`

**Output:** `data/raw/{YYYY-WW}.json` — append/merge with existing file for current week.

```json
{
  "week": "2026-W18",
  "last_updated": "2026-05-01T12:00:00Z",
  "transactions": [
    {
      "filing_accession": "0001234567-26-000123",
      "filing_date": "2026-04-29",
      "filing_url": "https://www.sec.gov/Archives/edgar/data/...",
      "issuer_cik": "320193",
      "issuer_name": "Apple Inc.",
      "issuer_ticker": "AAPL",
      "insider_name": "JOHN DOE",
      "insider_cik": "9876543",
      "insider_title": "SVP, General Counsel",
      "is_director": false,
      "is_officer": true,
      "is_ten_pct_owner": false,
      "transaction_date": "2026-04-28",
      "shares": 5000,
      "price_per_share": 187.50,
      "total_value": 937500.00,
      "shares_owned_after": 25000,
      "percentage_increase": 25.0
    }
  ]
}
```

**Edge cases:**
- **Amended filings (4/A):** Replace original transactions from that accession number.
- **Multiple transactions per filing:** Each becomes a separate row.
- **Missing price (footnoted):** Set `price_per_share: null`, still include the transaction.
- **Missing ticker in XML:** Resolve via CIK mapping in Stage 2.

### Stage 2: `enrich.py`

**Purpose:** Resolve tickers, pull company context from Polygon.

**Logic:**
1. Load cached `company_tickers.json` (refresh if >7 days old).
2. Resolve any null tickers via CIK.
3. For each unique ticker this week:
   - Check cache (`data/cache/polygon/{TICKER}.json`). If <30 days old, reuse for company details.
   - Otherwise, call **Polygon Ticker Details V3** → store description, SIC, market cap, etc.
   - Call **Polygon Ticker News** (always fresh, no cache) → store top 3-5 headlines.
   - Respect rate limit: sleep to enforce 5 calls/min ceiling.

**Output:** `data/enriched/{YYYY-WW}.json` — transactions plus nested `company_info` and `recent_news` per ticker.

### Stage 3: `fetch_charts.py`

**Purpose:** Generate 6-month price charts for each company with insider buys this week.

**Logic:**
1. For each unique ticker in the enriched data:
   - Call **Polygon Aggregates** for daily bars, 6 months back from today.
   - Generate a matplotlib chart matching the momentum report style:
     - Price line (blue/teal)
     - Volume bars (subtle, bottom axis)
     - Vertical red marker line on each insider buy date
     - Annotation label: "Insider Buy: {name}, {shares} @ ${price}"
     - 50-day and 200-day SMA overlays
     - Clean, minimal styling (white background, subtle grid)
   - Save as PNG to `data/charts/{YYYY-WW}/{TICKER}.png`
   - Also encode as base64 for inline embedding in HTML
2. Rate limit: same 5 calls/min. Charts share the Polygon budget with enrichment — pipeline should batch all Polygon calls across stages 2 and 3 through a single rate-limited client.

**Chart spec (matching momentum report style):**
- Figure size: ~1000×400px (rendered at 150 DPI)
- Title: `{TICKER} — {Company Name} ${current_price}`
- Subtitle: Transaction details
- X-axis: dates. Y-axis: price.
- SMA lines: 50-day (orange, dashed), 200-day (blue, dashed)
- Insider buy markers: vertical red lines + triangle markers
- Volume: bar chart on secondary y-axis, low opacity
- Save as PNG, read back as base64 for embedding

### Stage 4: `score_and_rank.py`

**Purpose:** Rank insider buys by significance.

**Scoring (weights in `config.yaml`):**

| Signal | Points | Rationale |
|--------|--------|-----------|
| `total_value` >= $1M | 3 | Large dollar commitment |
| `total_value` >= $500K | 2 | Meaningful commitment |
| `total_value` >= $100K | 1 | Baseline notable |
| `percentage_increase` >= 50% | 2 | Dramatically increasing position |
| `percentage_increase` >= 25% | 1 | Meaningfully increasing |
| `is_officer` (C-suite title) | 2 | CEO/CFO/COO buys are highest signal |
| `is_director` | 1 | Board visibility |
| `is_ten_pct_owner` | 0 | Often fund rebalancing |
| Cluster buy (>=2 insiders, same company, same week) | 3 | Strongest signal |

**Cluster detection:** Group by `issuer_cik` within the week. >=2 distinct insiders buying = cluster.

**Output:** `data/scored/{YYYY-WW}.json` — adds `score`, `score_breakdown`, `cluster_id`.

### Stage 5: `generate_report.py`

**Purpose:** Render the weekly HTML report and update the index/landing page.

**Weekly report structure** (single self-contained HTML file, inline CSS/JS, base64 charts):

```
┌─────────────────────────────────────────────────────┐
│  ← Back to Archive                                   │
│                                                       │
│  INSIDER BUYING REPORT                                │
│  Week of May 4, 2026                                  │
│  Updated: Fri May 2, 2026 (Run 2 of 2)              │
│                                                       │
│  SUMMARY                                              │
│  47 open-market insider purchases this week           │
│  Total value: $23.4M across 31 companies              │
│  3 cluster buys detected                              │
│                                                       │
├─────────────────────────────────────────────────────┤
│                                                       │
│  CLUSTER BUYS                                         │
│  ┌────────────────────────────────────────────────┐  │
│  │ XYZ Corp (XYZ) — 3 insiders bought              │  │
│  │   CEO: 50K shares @ $84 ($4.2M)                 │  │
│  │   CFO: 10K shares @ $83.50 ($835K)              │  │
│  │   Dir. J. Smith: 5K @ $84 ($420K)               │  │
│  │   Combined: $5.45M | Score: 14                  │  │
│  │                                                  │  │
│  │   [Company description paragraph]                │  │
│  │                                                  │  │
│  │   Recent News:                                   │  │
│  │   - Headline 1 (source, date)                    │  │
│  │   - Headline 2 (source, date)                    │  │
│  │   - Headline 3 (source, date)                    │  │
│  │                                                  │  │
│  │   [=== 6-month price chart with buy markers ===] │  │
│  └────────────────────────────────────────────────┘  │
│                                                       │
├─────────────────────────────────────────────────────┤
│                                                       │
│  TOP INDIVIDUAL BUYS (by score)                       │
│  Each entry: same format as above — score, insider    │
│  details, company description, headlines, chart       │
│                                                       │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ALL TRANSACTIONS TABLE (sortable via vanilla JS)     │
│  Columns: Date | Ticker | Company | Insider | Title | │
│  Shares | Price | Value | % Incr | Score | Filing     │
│                                                       │
│  [Each row links to SEC filing]                       │
│                                                       │
└─────────────────────────────────────────────────────┘
```

**Index / Landing Page (`docs/index.html`):**

```
┌─────────────────────────────────────────────────────┐
│  INSIDER BUYING TRACKER                               │
│                                                       │
│  [Project description: 2-3 paragraphs explaining      │
│   what insider buying is, why it matters, how this    │
│   tool works, and what data sources it uses]          │
│                                                       │
│  DISCLAIMER                                           │
│  This tool is for informational and educational       │
│  purposes only. It is not investment advice. The      │
│  data is sourced from SEC EDGAR filings and may       │
│  contain errors. Always do your own research          │
│  before making investment decisions. Past insider     │
│  buying activity does not predict future returns.     │
│                                                       │
├─────────────────────────────────────────────────────┤
│                                                       │
│  WEEKLY REPORTS                                       │
│  ┌─────────┬────────┬──────────┬──────────────────┐  │
│  │ Week    │ Buys   │ Value    │ Top Buy          │  │
│  ├─────────┼────────┼──────────┼──────────────────┤  │
│  │ W19 →   │ 47     │ $23.4M   │ CEO @ XYZ $4.2M │  │
│  │ W18 →   │ 35     │ $18.1M   │ CFO @ ABC $2.1M │  │
│  │ ...                                             │  │
│  └─────────┴────────┴──────────┴──────────────────┘  │
│                                                       │
│  Data: SEC EDGAR | Prices: Polygon.io                 │
│  Updated Tue/Fri 6AM MT via GitHub Actions            │
│                                                       │
└─────────────────────────────────────────────────────┘
```

---

## File Structure

```
insider-buying-tracker/
├── .github/
│   └── workflows/
│       └── run-tracker.yml
│
├── config.yaml
├── requirements.txt                  # requests, jinja2, pyyaml, matplotlib
│
├── src/
│   ├── fetch_form4s.py
│   ├── enrich.py
│   ├── fetch_charts.py
│   ├── score_and_rank.py
│   ├── generate_report.py
│   └── utils.py                     # Rate limiter, EDGAR/Polygon helpers
│
├── templates/
│   ├── weekly_report.html            # Jinja2 — individual report
│   └── index.html                    # Jinja2 — landing page + archive
│
├── data/
│   ├── checkpoint.json
│   ├── company_tickers.json          # Cached CIK→ticker
│   ├── cache/
│   │   └── polygon/                  # Ticker detail cache (30-day TTL)
│   ├── raw/
│   ├── enriched/
│   ├── scored/
│   └── charts/
│       └── {YYYY-WW}/               # PNGs per week
│
└── docs/                             # GitHub Pages root
    ├── .nojekyll
    ├── index.html                    # Landing page
    └── reports/
        └── {YYYY-WW}.html
```

---

## GitHub Actions Workflow

```yaml
name: Insider Buying Tracker

on:
  schedule:
    # Tuesday and Friday at 6:00 AM Mountain Time (12:00 UTC)
    - cron: '0 12 * * 2,5'
  workflow_dispatch:

jobs:
  run-tracker:
    runs-on: ubuntu-latest
    timeout-minutes: 180            # Allow up to 3 hrs for Polygon rate limiting
    permissions:
      contents: write

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Fetch Form 4 filings
        run: python src/fetch_form4s.py
        env:
          EDGAR_USER_AGENT: ${{ secrets.EDGAR_USER_AGENT }}

      - name: Enrich with Polygon data
        run: python src/enrich.py
        env:
          POLYGON_API_KEY: ${{ secrets.POLYGON_API_KEY }}

      - name: Generate price charts
        run: python src/fetch_charts.py
        env:
          POLYGON_API_KEY: ${{ secrets.POLYGON_API_KEY }}

      - name: Score and rank
        run: python src/score_and_rank.py

      - name: Generate report
        run: python src/generate_report.py

      - name: Commit and push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/ docs/
          git diff --staged --quiet || git commit -m "Update insider buying report $(date +%Y-%m-%d)"
          git push
```

---

## config.yaml

```yaml
edgar:
  user_agent: "InsiderBuyTracker your.email@example.com"
  rate_limit_delay_ms: 150
  max_retries: 3

polygon:
  rate_limit_calls_per_min: 5
  cache_ttl_days: 30              # Company details cache
  chart_lookback_days: 180        # 6 months of daily bars
  news_limit: 5                   # Headlines per ticker
  max_enrichment_tickers: 80      # Safety cap per run

schedule:
  runs_per_week: 2
  run_days: [tuesday, friday]
  timezone: "America/Denver"

scoring:
  value_thresholds:
    - { min: 1000000, points: 3 }
    - { min: 500000, points: 2 }
    - { min: 100000, points: 1 }
  pct_increase_thresholds:
    - { min: 50, points: 2 }
    - { min: 25, points: 1 }
  role_points:
    c_suite: 2
    officer: 1
    director: 1
    ten_pct_owner: 0
  cluster_bonus: 3

filters:
  min_transaction_value: 10000
  exclude_10pct_only: false
  transaction_codes: ["P"]

report:
  top_individual_detail_count: 15  # How many get the full card
  description_max_chars: 500       # Truncate company descriptions
```

---

## Form 4 XML Parsing Reference

Critical XML path for non-derivative purchases:

```xml
<ownershipDocument>
  <issuer>
    <issuerCik>320193</issuerCik>
    <issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>

  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>1234567</rptOwnerCik>
      <rptOwnerName>DOE JOHN</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>SVP, General Counsel</officerTitle>
      <isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>

  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>187.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionDate><value>2026-04-28</value></transactionDate>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>25000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
```

**Transaction codes:**

| Code | Meaning | Include? |
|------|---------|----------|
| `P` | Open-market purchase | **YES** |
| `S` | Open-market sale | No |
| `A` | Grant/award | No |
| `M` | Exercise of derivative | No |
| `X` | Exercise in-the-money derivative | No |
| `F` | Payment of exercise price/tax | No |
| `G` | Gift | No |
| `I` | Discretionary transaction | No |

---

## Polygon.io API Reference (Specific Endpoints Used)

### Ticker Details V3
```
GET https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={key}
```
Response fields we use: `results.description`, `results.sic_description`, `results.market_cap`, `results.total_employees`, `results.homepage_url`, `results.branding.icon_url`

### Ticker News
```
GET https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=5&apiKey={key}
```
Response fields we use: `results[].title`, `results[].article_url`, `results[].publisher.name`, `results[].published_utc`

### Aggregates (Daily Bars)
```
GET https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}?adjusted=true&sort=asc&apiKey={key}
```
Response fields we use: `results[].t` (timestamp ms), `results[].o`, `.h`, `.l`, `.c` (OHLC), `results[].v` (volume)

---

## Edge Cases and Defensive Design

**Duplicate handling:** Key = `{accession_number}:{transaction_index}`. Check before appending.

**Amended filings (4/A):** Replace original transactions from that accession.

**Missing prices:** Keep transaction with `null` price — a missing price on large share counts is itself interesting.

**Polygon failures:** If a ticker fails enrichment, still include it in the report with "Data unavailable" placeholder.

**Polygon free tier exhaustion:** Prioritize enrichment by score (highest first). If budget runs out, include remaining transactions without Polygon data.

**Chart generation failures:** Try/except per ticker. Failed charts = placeholder text in report.

**Large weeks (earnings season):** Could see 100+ unique tickers. The `max_enrichment_tickers` cap in config.yaml prevents runaway runs.

---

## Dependencies (requirements.txt)

```
requests>=2.31.0
jinja2>=3.1.0
pyyaml>=6.0
matplotlib>=3.8.0
```

---

## Implementation Order for Claude Code

1. `src/utils.py` — Rate limiter (shared EDGAR + Polygon), JSON I/O, logging, date helpers
2. `config.yaml` — All configuration with defaults
3. `src/fetch_form4s.py` — EFTS query + XML parsing + filter to code "P"
4. `src/enrich.py` — CIK→ticker resolution + Polygon details + news
5. `src/fetch_charts.py` — Polygon aggregates + matplotlib chart generation
6. `src/score_and_rank.py` — Scoring heuristic + cluster detection
7. `templates/weekly_report.html` — Jinja2 HTML (momentum-report style)
8. `templates/index.html` — Jinja2 landing page + archive + disclaimer
9. `src/generate_report.py` — Template rendering
10. `.github/workflows/run-tracker.yml` — Cron automation
11. `docs/` — Static scaffolding + `.nojekyll`

---

## Open Questions to Resolve Before Building

### 1. Report detail depth — how many companies get the full treatment?

The momentum report gives every stock the "full card" (description + news + chart). With 30-80 insider buys per week, doing that for all of them makes the report very long and burns the Polygon budget. Options:

- **A) Full card for top 15 by score, summary table for the rest** (recommended — fast, budget-friendly, keeps focus on signal)
- **B) Full card for all** (long report, may hit Polygon limits on big weeks)
- **C) Full card for clusters + top 10 individuals, table for rest**

### 2. Chart style — how closely should we match the momentum report?

Your momentum report uses matplotlib with a specific style (price line, SMAs, clean white background). Should the insider chart:

- **A) Match momentum report style closely** (price + 50/200 SMA + volume + insider buy markers)
- **B) Simpler version** (just price line + insider buy date markers, no SMAs/volume)
- **C) More detailed** (add earnings dates, 52-week high/low bands, etc.)

### 3. GitHub Pages theme — should it share your blog's visual identity?

Your blog uses three photo themes (Canopy, Shoreline, Golden Hour). Your games use six themes (Midnight, Cream, Slate, Chalk, Mint, Paper). Should this project:

- **A) Stand alone with a clean, minimal financial-report look** (recommended — it's a different kind of project)
- **B) Use one of your blog themes for visual consistency**
- **C) Have its own themed look (dark mode financial dashboard aesthetic)**

### 4. Polygon budget allocation between stages

With 5 calls/min, we need to decide priority when budget is tight:

- **A) Charts first, then enrich** (visual impact is higher)
- **B) Enrich first, then charts** (context matters more than pictures)
- **C) Interleave: for each ticker, do all 3 calls together** (recommended — each ticker gets complete data before moving to the next)

### 5. How to handle the Tuesday to Friday merge for a weekly report?

- **A) Tuesday creates the report, Friday replaces it** with the full week's data (simpler, recommended)
- **B) Tuesday creates a partial report, Friday appends** — report shows "Run 1 of 2" / "Run 2 of 2"
- **C) Two separate reports per week** (Tuesday and Friday are independent)

### 6. Historical backfill — do we want to seed the archive?

SEC publishes quarterly bulk Insider Transactions Data Sets going back to 2006. We could backfill recent weeks/months to launch with a non-empty archive. Worth doing now, or defer?

- **A) Defer — start fresh, let it accumulate** (simpler)
- **B) Backfill last 4 weeks using EFTS** (small effort, nice to launch with data)
- **C) Backfill last quarter from SEC bulk dataset** (more work)

### 7. Repo naming and URL

This lives at `https://{username}.github.io/{repo-name}/`. Options:

- **A) `insider-buying-tracker`** (descriptive)
- **B) `insider-buys`** (shorter)
- **C) Something else?**

### 8. Should the report include current stock price context?

Beyond the chart, should we include a text line like "Current price: $187.50 | 52-wk range: $120-$200 | Market cap: $2.8T"? This is available from Polygon at no extra API cost (it's in the aggregates response). I'd recommend yes.

---

## Future Enhancements (Not in V1)

- **Email digest:** Summary email via Gmail SMTP + GitHub Actions (reuse Daily Sports Digest pattern)
- **RSS feed:** Generate RSS XML alongside HTML reports
- **Insider track records:** Score insiders by historical buy accuracy (needs backfill + price data)
- **Cross-reference with 13F:** Flag when Form 4 buys coincide with fund 13F additions
- **Buyback detection:** Flag companies doing both insider buys and share repurchases
- **Sector heatmap:** Visual showing which sectors are seeing the most insider buying
