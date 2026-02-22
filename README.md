# vcp_screener

Refined VCP/Minervini-style stock screener using `yahooquery.Ticker` with a **cache-first** architecture.

## What Changed

Original behavior downloaded broad historical data on every run.

New behavior:
- First run: bulk bootstrap download into local cache
- Next runs: only refresh stale/insufficient symbols
- Screening reads from local cache instead of re-downloading the full universe

This significantly reduces runtime for recurring runs.

## What It Does

- Loads a ticker universe from `companylist.csv`
- Pulls daily OHLCV via `yahooquery`
- Applies trend-template filters (50/150/200 MA alignment, 52-week positioning, turnover, range contraction)
- Computes RS score + RS rank
- Exports dated report folder:
  - `candidates.csv`
  - `market_summary.json`
  - `charts/*.png`
  - `report.pdf` (optional)
- Maintains rolling `reports/market_history.csv`
- Maintains local price cache in `.cache/vcp_screener` by default

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python vcp_screener.py
```

Tuned full-universe run:

```bash
python vcp_screener.py \
  --as-of 2026-02-20 \
  --symbols-csv companylist.csv \
  --output-root reports \
  --cache-enabled \
  --cache-dir .cache/vcp_screener \
  --cache-no-data-retry-days 7 \
  --cache-stale-after-days 3 \
  --batch-size 120 \
  --max-workers 6 \
  --max-retries 3 \
  --retry-backoff-seconds 1.25 \
  --fallback-singletons \
  --fallback-max-retries 1 \
  --fallback-workers 6 \
  --chart-limit 60 \
  --min-rs-rank 0.85
```

## Key Flags

- `--as-of YYYY-MM-DD`: run for a specific date (weekends auto-shift to previous Friday)
- `--symbols-csv PATH`: universe source CSV
- `--output-root PATH`: output root (default `reports`)
- `--lookback-days N`: lookback window for screening
- `--cache-enabled / --no-cache-enabled`: enable/disable cache-first mode
- `--cache-dir PATH`: cache location
- `--cache-refresh-padding-days N`: overlap window when refreshing stale symbols
- `--max-cache-rows N`: rows retained per symbol in cache
- `--cache-no-data-retry-days N`: cooldown before retrying symbols previously marked as no-data
- `--cache-stale-after-days N`: refresh only when cached data is older than N days
- `--batch-size N`: request batch size
- `--max-workers N`: parallel workers for batch fetches
- `--max-retries N`: retries per failed batch
- `--retry-backoff-seconds FLOAT`: exponential backoff base delay
- `--fallback-singletons / --no-fallback-singletons`: recover failed batch symbols individually
- `--fallback-max-retries N`: retries for each fallback single-symbol request
- `--fallback-workers N`: workers used during fallback recovery
- `--progress-every N`: print progress every N batches
- `--chart-limit N`: max candidate charts
- `--min-rs-rank FLOAT`: minimum RS rank for chart generation (`0.0` to `1.0`)
- `--index-charts / --no-index-charts`: include/skip SPY/QQQ/DIA/IWM/VOO/FFTY charts
- `--pdf-report / --no-pdf-report`: include/skip PDF output

## Performance Notes

- First run is the expensive run (cache bootstrap).
- Recurring runs are much faster because they mostly read local cache and do small incremental refreshes.
- Symbols with repeated empty Yahoo responses are put on a no-data cooldown to avoid re-downloading them every run.
- Keep `--fallback-singletons` enabled for maximum coverage; disable it for lower runtime when partial data loss is acceptable.

## Notes

- The screener runs once per command.
- Schedule with cron/Task Scheduler for daily automation.
- Yahoo data availability can vary by symbol.
