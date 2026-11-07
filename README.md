# insider-trade

Collects US insider **open-market purchases** (SEC Form 4, transaction code `P`) every day
and ranks stocks by insider buying: **cost**, **recurrence**, and **number/seniority of insiders**.

## How it works

| Piece | Source / mechanism |
|---|---|
| Daily filings | EDGAR daily form index → every Form 4 / 4/A submission (XML) |
| History | SEC [Insider Transactions Data Sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets) (quarterly) |
| Tickers | `company_tickers_exchange.json` (SEC) |
| Shares outstanding | SEC XBRL `companyconcept` API (dei → us-gaap fallbacks, refreshed monthly) |
| Prices | yfinance (falls back to the latest Form 4 purchase price) |
| Storage | SQLite (`data/insider.db`) |
| Schedule | GitHub Actions, Tue–Sat 06:30 UTC; DB published to the single-commit `data` branch |

Only `P` (open-market purchase) rows from the non-derivative table are stored. Grants, option
exercises, tax withholdings, gifts and sales are ignored.

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export SEC_USER_AGENT="Your Name your@email.com"   # required by SEC; requests are rejected (403) without an email
```

## Usage

```bash
insider sync                 # download the latest DB built by GitHub Actions (no SEC calls)
insider rank                 # top 25 stocks, 90-day window
insider rank --window 30 --top 50 --min-insiders 2 --details
insider show AAPL            # purchases behind a ticker's score
insider status
```

Building the data yourself instead of syncing:

```bash
insider backfill --days 365  # quarterly data sets + daily index for the current quarter
insider ingest               # new filings since the last ingested day (idempotent)
insider market               # tickers, shares outstanding, prices
```

## Ranking

For each stock, purchases with a trade date inside the window are aggregated:

* **Cost**: mean percentile of total $ bought, $ bought as % of market cap, and average ΔOwn
  (shares bought ÷ shares held before the first buy, capped at 100%).
* **Recurrence**: percentile of distinct buy days + insiders who bought on 2+ days.
* **Insiders**: percentile of distinct insiders weighted by role:
  CEO/CFO/President 3, other officers 2, directors 1.5, 10% owners/other 0.5.

`score = 100 × (w_cost·cost + w_recurrence·recurrence + w_insiders·insiders) / Σw`
(defaults 0.4 / 0.3 / 0.3; change with `--w-cost`, `--w-recurrence`, `--w-insiders`).

Data handling:

* **Joint filings** (e.g. a fund and its GP filing together) are attributed to the most senior
  reporting owner, so one trade is counted once. The same trade reported in *separate* filings by
  related owners (identical date, shares, price and holdings after) is also counted once.
* **Amendments** (4/A) replace the earlier rows for the same issuer, insider and trade date.
* **10% owners and "other" filers** (mostly funds) are excluded by default; add them back with `--include-funds`.
* Issuers without a ticker are excluded unless `--include-unlisted` is set.
* **Splits**: Yahoo quotes are split-adjusted, so the average buy price (and **Chg**, the current
  price vs that average) is restated in today's share terms using yfinance split events.
* **Price mismatches**: if Form 4 prices differ from the quote on the trade date by more than
  1.5× (foreign currency, ordinary shares vs ADR), the ticker is marked `*`: % of market cap and
  Chg are hidden, and the $ amount is left out of the cost score.

Known limitations: Form 4 also codes IPO and private-placement purchases as `P`, so newly listed
companies can rank high (check with `insider show`). Prices from yfinance are unofficial.

## GitHub Actions

`.github/workflows/daily-ingest.yml` restores the DB from the `data` branch, ingests new filings,
refreshes market data, and force-pushes the DB back as a single commit (keeping the repo small).

* Add a repository secret `SEC_USER_AGENT` (e.g. `Your Name your@email.com`).
* First run: trigger the workflow manually with `backfill_days = 365`.
* GitHub disables scheduled workflows in public repos after 60 days without repository activity;
  re-enable from the Actions tab if that happens.

## Tests

```bash
pytest -q
```
