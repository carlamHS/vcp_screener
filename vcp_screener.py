#!/usr/bin/env python3
"""VCP Screener powered by yahooquery with local history caching.

This script screens a US equity universe for Minervini-style trend template
conditions and exports a dated report folder with:
- candidates.csv
- market_summary.json
- market_history.csv (rolling history of summary metrics)
- chart PNG files
- optional PDF report

Key design goal:
- Avoid downloading the full universe on every run.
- Bootstrap cache in bulk once, then refresh only stale/insufficient symbols.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import quote_plus

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from yahooquery import Ticker

INDEX_STYLES = {
    "SPY": "charles",
    "QQQ": "blueskies",
    "DIA": "classic",
    "IWM": "mike",
    "VOO": "classic",
    "FFTY": "yahoo",
}

INDEX_SYMBOLS = tuple(INDEX_STYLES.keys())


@dataclass
class ScreenResult:
    symbol: str
    rs_rating: float
    close: float
    turnover: float
    sma_20: float
    sma_50: float
    sma_150: float
    sma_200: float
    low_52w: float
    high_52w: float
    true_range_10d: float


@dataclass
class MarketStats:
    total: int = 0
    adv: int = 0
    decl: int = 0
    new_high: int = 0
    new_low: int = 0
    gauge: float = 0.0
    above_20: int = 0
    above_50: int = 0
    sma20_gt_50: int = 0
    sma50_gt_200: int = 0
    sma50_gt_150_gt_200: int = 0
    sma200_rising: int = 0

    def pct(self, value: int) -> float:
        if self.total == 0:
            return 0.0
        return round(value / self.total * 100.0, 3)


@dataclass
class FetchMetrics:
    requested_symbols: int = 0
    total_batches: int = 0
    completed_batches: int = 0
    failed_batches: int = 0
    retries: int = 0
    symbols_returned: int = 0
    recovered_symbols: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class CacheMetrics:
    requested_symbols: int = 0
    ready_symbols: int = 0
    bootstrap_symbols: int = 0
    incremental_symbols: int = 0
    refreshed_symbols: int = 0
    available_symbols: int = 0
    failed_symbols: int = 0
    no_data_skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VCP Screener (yahooquery edition)")
    parser.add_argument("--symbols-csv", type=Path, default=Path("companylist.csv"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("reports"),
        help="Root output folder. A date folder is created inside it.",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="Screen date (YYYY-MM-DD). Defaults to latest weekday in UTC.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=420,
        help="Calendar days of price history to request.",
    )
    parser.add_argument("--batch-size", type=int, default=120)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=6,
        help="Parallel workers for batch fetches.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Retries per batch when a request fails.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=1.25,
        help="Base delay for exponential backoff between retries.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print fetch progress every N completed batches.",
    )
    parser.add_argument(
        "--fallback-singletons",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recover failed batches by retrying each symbol individually.",
    )
    parser.add_argument(
        "--fallback-max-retries",
        type=int,
        default=1,
        help="Retries for per-symbol fallback recovery.",
    )
    parser.add_argument(
        "--fallback-workers",
        type=int,
        default=4,
        help="Parallel workers used during fallback single-symbol recovery.",
    )
    parser.add_argument(
        "--cache-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable local cache-first workflow.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache") / "vcp_screener",
        help="Directory for cached historical data.",
    )
    parser.add_argument(
        "--cache-refresh-padding-days",
        type=int,
        default=7,
        help="When refreshing stale symbols, re-fetch this many days before cached max date.",
    )
    parser.add_argument(
        "--max-cache-rows",
        type=int,
        default=1200,
        help="Maximum rows kept per symbol in cache.",
    )
    parser.add_argument(
        "--cache-no-data-retry-days",
        type=int,
        default=7,
        help="Days to wait before retrying symbols previously marked as no-data.",
    )
    parser.add_argument(
        "--cache-stale-after-days",
        type=int,
        default=3,
        help="Refresh only if cached max date is older than this many days before as-of date.",
    )
    parser.add_argument("--chart-limit", type=int, default=50)
    parser.add_argument(
        "--min-rs-rank",
        type=float,
        default=0.80,
        help="Only stocks with rs_rank >= this threshold get charts.",
    )
    parser.add_argument(
        "--index-charts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include index charts (SPY/QQQ/DIA/IWM/VOO/FFTY) in report.",
    )
    parser.add_argument(
        "--pdf-report",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Generate a combined PDF report.",
    )
    return parser.parse_args()


def default_as_of() -> date:
    as_of = datetime.utcnow().date()
    while as_of.weekday() >= 5:
        as_of -= timedelta(days=1)
    return as_of


def parse_as_of(value: str | None) -> date:
    if not value:
        return default_as_of()
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    while parsed.weekday() >= 5:
        parsed -= timedelta(days=1)
    return parsed


def chunked(values: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def unique_ordered(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def utc_now_text() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_symbols(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Ticker list not found: {csv_path}")

    df = pd.read_csv(csv_path)
    symbol_col = None
    for candidate in ("Symbol", "symbol", "Ticker", "ticker"):
        if candidate in df.columns:
            symbol_col = candidate
            break
    if symbol_col is None:
        symbol_col = df.columns[0]

    symbols = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
        .replace("", np.nan)
        .dropna()
        .tolist()
    )

    unique_symbols = unique_ordered(symbols)
    if not unique_symbols:
        raise ValueError(f"No symbols were found in {csv_path}")
    return unique_symbols


def _normalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()

    if "date" in df.columns:
        df = df.set_index("date")

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[~df.index.isna()]

    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    rename_map = {}
    for col in df.columns:
        lower = col.lower()
        if lower in {"open", "high", "low", "close", "volume", "adjclose"}:
            rename_map[col] = lower
    df = df.rename(columns=rename_map)

    if "adjclose" not in df.columns and "close" in df.columns:
        df["adjclose"] = df["close"]

    required = ["open", "high", "low", "close", "adjclose"]
    if any(col not in df.columns for col in required):
        return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 0

    for col in ["open", "high", "low", "close", "adjclose", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_index()
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=["adjclose", "close"])
    df["volume"] = df["volume"].fillna(0)

    return df[["open", "high", "low", "close", "adjclose", "volume"]]


def _extract_histories(raw: Any, requested_symbols: list[str]) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}

    if isinstance(raw, pd.DataFrame):
        frame = raw.copy()

        if isinstance(frame.index, pd.MultiIndex):
            names = [name or "" for name in frame.index.names]
            level = names.index("symbol") if "symbol" in names else 0

            for symbol in requested_symbols:
                try:
                    sub = frame.xs(symbol, level=level, drop_level=True)
                except KeyError:
                    continue
                cleaned = _normalize_ohlcv(sub)
                if not cleaned.empty:
                    histories[symbol] = cleaned

        elif "symbol" in frame.columns:
            for symbol, sub in frame.groupby("symbol"):
                normalized_symbol = str(symbol).upper()
                if normalized_symbol not in requested_symbols:
                    continue
                cleaned = _normalize_ohlcv(sub.drop(columns=["symbol"]))
                if not cleaned.empty:
                    histories[normalized_symbol] = cleaned

        elif len(requested_symbols) == 1:
            cleaned = _normalize_ohlcv(frame)
            if not cleaned.empty:
                histories[requested_symbols[0]] = cleaned

    elif isinstance(raw, dict):
        for symbol in requested_symbols:
            value = raw.get(symbol)
            if value is None:
                value = raw.get(symbol.lower())
            if value is None:
                value = raw.get(symbol.upper())

            if isinstance(value, pd.DataFrame):
                cleaned = _normalize_ohlcv(value)
                if not cleaned.empty:
                    histories[symbol] = cleaned
            elif isinstance(value, list) and value:
                cleaned = _normalize_ohlcv(pd.DataFrame(value))
                if not cleaned.empty:
                    histories[symbol] = cleaned

    return histories


def _fetch_batch_with_retry(
    batch: list[str],
    start: str,
    end: str,
    max_retries: int,
    retry_backoff_seconds: float,
) -> tuple[dict[str, pd.DataFrame], int, bool]:
    retries_used = 0

    for attempt in range(max_retries + 1):
        try:
            query = Ticker(batch, asynchronous=False, progress=False, validate=False)
            raw = query.history(start=start, end=end, interval="1d")
            histories = _extract_histories(raw, batch)
            return histories, retries_used, True
        except Exception:
            if attempt >= max_retries:
                break
            retries_used += 1
            delay = retry_backoff_seconds * (2**attempt)
            delay += random.uniform(0.0, retry_backoff_seconds * 0.25)
            time.sleep(max(0.0, delay))

    return {}, retries_used, False


def _recover_batch_as_singletons(
    batch: list[str],
    start: str,
    end: str,
    max_retries: int,
    retry_backoff_seconds: float,
    fallback_workers: int,
) -> tuple[dict[str, pd.DataFrame], int]:
    recovered: dict[str, pd.DataFrame] = {}
    retries_used = 0

    worker_count = max(1, min(fallback_workers, len(batch)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_symbol: dict[Future[tuple[dict[str, pd.DataFrame], int, bool]], str] = {}
        for symbol in batch:
            future = executor.submit(
                _fetch_batch_with_retry,
                [symbol],
                start,
                end,
                max_retries,
                retry_backoff_seconds,
            )
            future_to_symbol[future] = symbol

        for future in as_completed(future_to_symbol):
            try:
                histories, symbol_retries, _ok = future.result()
            except Exception:
                histories, symbol_retries = {}, max_retries
            retries_used += symbol_retries
            recovered.update(histories)

    return recovered, retries_used


def _print_fetch_progress(label: str, metrics: FetchMetrics) -> None:
    print(
        f"{label}: batches {metrics.completed_batches}/{metrics.total_batches} | "
        f"retried {metrics.retries} | failed {metrics.failed_batches} | "
        f"recovered {metrics.recovered_symbols} | "
        f"symbols with data {metrics.symbols_returned}/{metrics.requested_symbols}"
    )


def merge_fetch_metrics(target: FetchMetrics, delta: FetchMetrics) -> None:
    target.requested_symbols += delta.requested_symbols
    target.total_batches += delta.total_batches
    target.completed_batches += delta.completed_batches
    target.failed_batches += delta.failed_batches
    target.retries += delta.retries
    target.symbols_returned += delta.symbols_returned
    target.recovered_symbols += delta.recovered_symbols


def run_batch_fetch(
    symbols: list[str],
    start_date: date,
    end_date: date,
    batch_size: int,
    max_workers: int,
    max_retries: int,
    retry_backoff_seconds: float,
    progress_every: int,
    fallback_singletons: bool,
    fallback_max_retries: int,
    fallback_workers: int,
    label: str,
    on_batch: Callable[[list[str], dict[str, pd.DataFrame]], None] | None = None,
) -> tuple[dict[str, pd.DataFrame], FetchMetrics]:
    symbols = unique_ordered([symbol for symbol in symbols if symbol])
    batches = list(chunked(symbols, max(1, batch_size)))

    metrics = FetchMetrics(
        requested_symbols=len(symbols),
        total_batches=len(batches),
    )

    start = start_date.isoformat()
    end = (end_date + timedelta(days=1)).isoformat()

    stored_histories: dict[str, pd.DataFrame] = {}

    if not batches:
        return stored_histories, metrics

    def process_batch_result(
        batch: list[str],
        histories: dict[str, pd.DataFrame],
        retries_used: int,
        ok: bool,
    ) -> None:
        nonlocal stored_histories

        if not ok:
            metrics.failed_batches += 1
            if fallback_singletons and batch:
                recovered, fallback_retries = _recover_batch_as_singletons(
                    batch=batch,
                    start=start,
                    end=end,
                    max_retries=fallback_max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                    fallback_workers=fallback_workers,
                )
                retries_used += fallback_retries
                metrics.recovered_symbols += len(recovered)
                histories = recovered

        metrics.completed_batches += 1
        metrics.retries += retries_used
        metrics.symbols_returned += len(histories)

        if on_batch is None:
            stored_histories.update(histories)
        else:
            on_batch(batch, histories)

        if progress_every > 0 and (
            metrics.completed_batches % progress_every == 0
            or metrics.completed_batches == metrics.total_batches
        ):
            _print_fetch_progress(label, metrics)

    if max_workers <= 1 or len(batches) == 1:
        for batch in batches:
            histories, retries_used, ok = _fetch_batch_with_retry(
                batch=batch,
                start=start,
                end=end,
                max_retries=max_retries,
                retry_backoff_seconds=retry_backoff_seconds,
            )
            process_batch_result(batch, histories, retries_used, ok)

        return stored_histories, metrics

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch: dict[Future[tuple[dict[str, pd.DataFrame], int, bool]], list[str]] = {}
        for batch in batches:
            future = executor.submit(
                _fetch_batch_with_retry,
                batch,
                start,
                end,
                max_retries,
                retry_backoff_seconds,
            )
            future_to_batch[future] = batch

        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                histories, retries_used, ok = future.result()
            except Exception:
                histories, retries_used, ok = {}, max_retries, False
            process_batch_result(batch, histories, retries_used, ok)

    return stored_histories, metrics


def cache_prices_dir(cache_dir: Path) -> Path:
    return cache_dir / "prices"


def cache_index_path(cache_dir: Path) -> Path:
    return cache_dir / "cache_index.csv"


def cache_path_for_symbol(cache_dir: Path, symbol: str) -> Path:
    filename = f"{quote_plus(symbol)}.pkl.gz"
    return cache_prices_dir(cache_dir) / filename


def load_cache_index(cache_dir: Path) -> dict[str, dict[str, Any]]:
    path = cache_index_path(cache_dir)
    if not path.exists():
        return {}

    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        return {}

    records: dict[str, dict[str, Any]] = {}
    for row in df.to_dict(orient="records"):
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        records[symbol] = {
            "symbol": symbol,
            "filename": row.get("filename", ""),
            "min_date": row.get("min_date", ""),
            "max_date": row.get("max_date", ""),
            "rows": parse_int(row.get("rows", 0), 0),
            "updated_utc": row.get("updated_utc", ""),
            "status": row.get("status", "ok"),
        }

    return records


def save_cache_index(cache_dir: Path, records: dict[str, dict[str, Any]]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(records.values(), key=lambda r: str(r.get("symbol", "")))

    if rows:
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame(
            columns=["symbol", "filename", "min_date", "max_date", "rows", "updated_utc", "status"]
        )

    df.to_csv(cache_index_path(cache_dir), index=False)


def load_history_from_cache(cache_dir: Path, symbol: str) -> pd.DataFrame | None:
    path = cache_path_for_symbol(cache_dir, symbol)
    if not path.exists():
        return None

    try:
        data = pd.read_pickle(path, compression="gzip")
    except Exception:
        return None

    if not isinstance(data, pd.DataFrame):
        return None

    cleaned = _normalize_ohlcv(data)
    if cleaned.empty:
        return None

    return cleaned


def store_history_in_cache(
    cache_dir: Path,
    symbol: str,
    history: pd.DataFrame,
    max_cache_rows: int,
    index_records: dict[str, dict[str, Any]],
) -> bool:
    cleaned = _normalize_ohlcv(history)
    if cleaned.empty:
        return False

    cleaned = cleaned.sort_index()
    if max_cache_rows > 0 and len(cleaned) > max_cache_rows:
        cleaned = cleaned.iloc[-max_cache_rows:]

    path = cache_path_for_symbol(cache_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        cleaned.to_pickle(path, compression="gzip")
    except Exception:
        return False

    index_records[symbol] = {
        "symbol": symbol,
        "filename": path.name,
        "min_date": cleaned.index.min().date().isoformat(),
        "max_date": cleaned.index.max().date().isoformat(),
        "rows": int(len(cleaned)),
        "updated_utc": utc_now_text(),
        "status": "ok",
    }
    return True


def mark_no_data_in_cache(index_records: dict[str, dict[str, Any]], symbol: str) -> None:
    index_records[symbol] = {
        "symbol": symbol,
        "filename": "",
        "min_date": "",
        "max_date": "",
        "rows": 0,
        "updated_utc": utc_now_text(),
        "status": "no_data",
    }


def merge_histories(
    existing: pd.DataFrame | None,
    incoming: pd.DataFrame,
    max_cache_rows: int,
) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    if existing is not None and not existing.empty:
        pieces.append(existing)
    if incoming is not None and not incoming.empty:
        pieces.append(incoming)

    if not pieces:
        return pd.DataFrame()

    merged = pd.concat(pieces, axis=0)
    merged = merged.sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]

    if max_cache_rows > 0 and len(merged) > max_cache_rows:
        merged = merged.iloc[-max_cache_rows:]

    return merged


def plan_cache_refresh(
    symbols: list[str],
    index_records: dict[str, dict[str, Any]],
    cache_dir: Path,
    required_start_date: date,
    as_of: date,
    min_required_rows: int,
    refresh_padding_days: int,
    no_data_retry_days: int,
    stale_after_days: int,
) -> tuple[int, list[str], dict[date, list[str]], int]:
    ready_symbols = 0
    bootstrap_symbols: list[str] = []
    incremental_groups: dict[date, list[str]] = defaultdict(list)
    no_data_skipped = 0
    fresh_enough_threshold = as_of - timedelta(days=7)
    stale_threshold = as_of - timedelta(days=max(0, stale_after_days))

    for symbol in symbols:
        record = index_records.get(symbol)
        path = cache_path_for_symbol(cache_dir, symbol)

        status = str(record.get("status", "")).lower() if record is not None else ""
        if status in {"no_data", "stale_no_update"}:
            updated = parse_iso_date(record.get("updated_utc"))
            if updated is not None and updated >= as_of - timedelta(days=max(0, no_data_retry_days)):
                no_data_skipped += 1
                continue

        if record is None or not path.exists():
            bootstrap_symbols.append(symbol)
            continue

        rows = parse_int(record.get("rows", 0), 0)
        min_date = parse_iso_date(record.get("min_date"))
        max_date = parse_iso_date(record.get("max_date"))

        if rows < min_required_rows:
            # Common for recent IPOs: data is current but not long enough yet.
            if max_date is not None and max_date >= fresh_enough_threshold:
                no_data_skipped += 1
                continue
            bootstrap_symbols.append(symbol)
            continue

        if min_date is None or min_date > required_start_date:
            bootstrap_symbols.append(symbol)
            continue

        if max_date is None or max_date < stale_threshold:
            refresh_start = required_start_date
            if max_date is not None:
                refresh_start = max(required_start_date, max_date - timedelta(days=refresh_padding_days))
            incremental_groups[refresh_start].append(symbol)
            continue

        ready_symbols += 1

    return ready_symbols, bootstrap_symbols, incremental_groups, no_data_skipped


def refresh_cache_for_symbols(
    symbols: list[str],
    cache_dir: Path,
    required_start_date: date,
    as_of: date,
    min_required_rows: int,
    max_cache_rows: int,
    refresh_padding_days: int,
    no_data_retry_days: int,
    stale_after_days: int,
    batch_size: int,
    max_workers: int,
    max_retries: int,
    retry_backoff_seconds: float,
    progress_every: int,
    fallback_singletons: bool,
    fallback_max_retries: int,
    fallback_workers: int,
) -> tuple[FetchMetrics, CacheMetrics]:
    symbols = unique_ordered([symbol for symbol in symbols if symbol])
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_prices_dir(cache_dir).mkdir(parents=True, exist_ok=True)

    index_records = load_cache_index(cache_dir)

    ready_symbols, bootstrap_symbols, incremental_groups, no_data_skipped = plan_cache_refresh(
        symbols=symbols,
        index_records=index_records,
        cache_dir=cache_dir,
        required_start_date=required_start_date,
        as_of=as_of,
        min_required_rows=min_required_rows,
        refresh_padding_days=refresh_padding_days,
        no_data_retry_days=no_data_retry_days,
        stale_after_days=stale_after_days,
    )

    incremental_symbols = sum(len(v) for v in incremental_groups.values())

    cache_metrics = CacheMetrics(
        requested_symbols=len(symbols),
        ready_symbols=ready_symbols,
        bootstrap_symbols=len(bootstrap_symbols),
        incremental_symbols=incremental_symbols,
        no_data_skipped=no_data_skipped,
    )

    print(
        f"Cache plan: ready {ready_symbols} | "
        f"bootstrap {len(bootstrap_symbols)} | incremental {incremental_symbols}"
    )

    total_fetch_metrics = FetchMetrics()
    refreshed_symbols: set[str] = set()

    bootstrap_returned: set[str] = set()
    if bootstrap_symbols:
        print(f"Cache bootstrap fetch: {len(bootstrap_symbols)} symbols from {required_start_date}")

        def on_bootstrap_batch(_batch: list[str], histories: dict[str, pd.DataFrame]) -> None:
            for symbol, hist in histories.items():
                bootstrap_returned.add(symbol)
                stored = store_history_in_cache(
                    cache_dir=cache_dir,
                    symbol=symbol,
                    history=hist,
                    max_cache_rows=max_cache_rows,
                    index_records=index_records,
                )
                if stored:
                    refreshed_symbols.add(symbol)
                    updated_record = index_records.get(symbol, {})
                    rows = parse_int(updated_record.get("rows", 0), 0)
                    max_date = parse_iso_date(updated_record.get("max_date"))
                    if (
                        rows < min_required_rows
                        and max_date is not None
                        and max_date < as_of - timedelta(days=7)
                    ):
                        updated_record["status"] = "stale_no_update"
                        updated_record["updated_utc"] = utc_now_text()

        _, fetch_metrics = run_batch_fetch(
            symbols=bootstrap_symbols,
            start_date=required_start_date,
            end_date=as_of,
            batch_size=batch_size,
            max_workers=max_workers,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            progress_every=progress_every,
            fallback_singletons=fallback_singletons,
            fallback_max_retries=fallback_max_retries,
            fallback_workers=fallback_workers,
            label="Cache bootstrap",
            on_batch=on_bootstrap_batch,
        )
        merge_fetch_metrics(total_fetch_metrics, fetch_metrics)

        for symbol in set(bootstrap_symbols) - bootstrap_returned:
            mark_no_data_in_cache(index_records, symbol)

    for group_start in sorted(incremental_groups.keys()):
        group_symbols = incremental_groups[group_start]
        if not group_symbols:
            continue

        print(f"Cache incremental fetch: {len(group_symbols)} symbols from {group_start}")
        incremental_returned: set[str] = set()

        def on_incremental_batch(_batch: list[str], histories: dict[str, pd.DataFrame]) -> None:
            for symbol, incoming in histories.items():
                incremental_returned.add(symbol)
                existing_record = index_records.get(symbol, {})
                previous_max = parse_iso_date(existing_record.get("max_date"))

                existing = load_history_from_cache(cache_dir, symbol)
                merged = merge_histories(
                    existing=existing,
                    incoming=incoming,
                    max_cache_rows=max_cache_rows,
                )
                stored = store_history_in_cache(
                    cache_dir=cache_dir,
                    symbol=symbol,
                    history=merged,
                    max_cache_rows=max_cache_rows,
                    index_records=index_records,
                )
                if stored:
                    refreshed_symbols.add(symbol)
                    updated_record = index_records.get(symbol, {})
                    new_max = parse_iso_date(updated_record.get("max_date"))
                    stale_threshold = as_of - timedelta(days=max(0, stale_after_days))
                    if (
                        previous_max is not None
                        and new_max is not None
                        and new_max <= previous_max
                        and new_max < stale_threshold
                    ):
                        updated_record["status"] = "stale_no_update"
                        updated_record["updated_utc"] = utc_now_text()

        _, fetch_metrics = run_batch_fetch(
            symbols=group_symbols,
            start_date=group_start,
            end_date=as_of,
            batch_size=batch_size,
            max_workers=max_workers,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            progress_every=progress_every,
            fallback_singletons=fallback_singletons,
            fallback_max_retries=fallback_max_retries,
            fallback_workers=fallback_workers,
            label="Cache incremental",
            on_batch=on_incremental_batch,
        )
        merge_fetch_metrics(total_fetch_metrics, fetch_metrics)

        for symbol in set(group_symbols) - incremental_returned:
            record = index_records.get(symbol)
            if record is None:
                continue
            record["status"] = "stale_no_update"
            record["updated_utc"] = utc_now_text()

    save_cache_index(cache_dir, index_records)

    available_symbols = 0
    stale_threshold = as_of - timedelta(days=7)
    for symbol in symbols:
        record = index_records.get(symbol)
        if record is None:
            continue

        rows = parse_int(record.get("rows", 0), 0)
        min_date = parse_iso_date(record.get("min_date"))
        max_date = parse_iso_date(record.get("max_date"))

        if rows < min_required_rows:
            continue
        if min_date is None or min_date > required_start_date:
            continue
        if max_date is None or max_date < stale_threshold:
            continue
        if not cache_path_for_symbol(cache_dir, symbol).exists():
            continue

        available_symbols += 1

    cache_metrics.refreshed_symbols = len(refreshed_symbols)
    cache_metrics.available_symbols = available_symbols
    cache_metrics.failed_symbols = max(0, len(symbols) - available_symbols)

    print(
        f"Cache status: available {cache_metrics.available_symbols}/{cache_metrics.requested_symbols} | "
        f"refreshed {cache_metrics.refreshed_symbols} | "
        f"no-data skipped {cache_metrics.no_data_skipped}"
    )

    return total_fetch_metrics, cache_metrics


def stream_cached_batches(
    symbols: list[str],
    cache_dir: Path,
    batch_size: int,
    progress_every: int,
    label: str,
    on_batch: Callable[[list[str], dict[str, pd.DataFrame]], None],
) -> int:
    symbols = unique_ordered([symbol for symbol in symbols if symbol])
    batches = list(chunked(symbols, max(1, batch_size)))

    symbols_with_data = 0

    if not batches:
        return symbols_with_data

    for i, batch in enumerate(batches, start=1):
        histories: dict[str, pd.DataFrame] = {}
        for symbol in batch:
            hist = load_history_from_cache(cache_dir, symbol)
            if hist is None or hist.empty:
                continue
            histories[symbol] = hist

        symbols_with_data += len(histories)
        on_batch(batch, histories)

        if progress_every > 0 and (i % progress_every == 0 or i == len(batches)):
            print(
                f"{label}: batches {i}/{len(batches)} | "
                f"symbols with data {symbols_with_data}/{len(symbols)}"
            )

    return symbols_with_data


def load_histories_from_cache(symbols: list[str], cache_dir: Path) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    for symbol in unique_ordered(symbols):
        hist = load_history_from_cache(cache_dir, symbol)
        if hist is None or hist.empty:
            continue
        histories[symbol] = hist
    return histories


def safe_ratio(current: float, base: float) -> float:
    if base == 0:
        return 0.0
    return (current - base) / base


def screen_symbol(symbol: str, hist: pd.DataFrame, stats: MarketStats) -> ScreenResult | None:
    closes = hist["adjclose"]
    volumes = hist["volume"]

    if len(closes) < 2:
        return None

    current_close = float(closes.iloc[-1])
    yesterday_close = float(closes.iloc[-2])
    turnover = float(volumes.iloc[-1] * current_close)

    stats.total += 1
    if current_close > yesterday_close:
        stats.adv += 1
        stats.gauge += turnover
    else:
        stats.decl += 1
        stats.gauge -= turnover

    if current_close >= float(closes.max()) * 0.97:
        stats.new_high += 1
    elif current_close <= float(closes.min()) * 1.03:
        stats.new_low += 1

    sma_20_series = closes.rolling(20).mean()
    sma_50_series = closes.rolling(50).mean()
    sma_150_series = closes.rolling(150).mean()
    sma_200_series = closes.rolling(200).mean()

    sma_20 = float(sma_20_series.iloc[-1]) if len(closes) >= 20 else float("nan")
    sma_50 = float(sma_50_series.iloc[-1]) if len(closes) >= 50 else float("nan")
    sma_150 = float(sma_150_series.iloc[-1]) if len(closes) >= 150 else float("nan")
    sma_200 = float(sma_200_series.iloc[-1]) if len(closes) >= 200 else float("nan")
    sma_200_20 = float(sma_200_series.iloc[-21]) if len(closes) >= 220 else float("nan")

    if not np.isnan(sma_20) and current_close > sma_20:
        stats.above_20 += 1
    if not np.isnan(sma_50) and current_close > sma_50:
        stats.above_50 += 1
    if not np.isnan(sma_20) and not np.isnan(sma_50) and sma_20 > sma_50:
        stats.sma20_gt_50 += 1
    if not np.isnan(sma_50) and not np.isnan(sma_200) and sma_50 > sma_200:
        stats.sma50_gt_200 += 1
    if (
        not np.isnan(sma_50)
        and not np.isnan(sma_150)
        and not np.isnan(sma_200)
        and sma_50 > sma_150 > sma_200
    ):
        stats.sma50_gt_150_gt_200 += 1
    if not np.isnan(sma_200) and not np.isnan(sma_200_20) and sma_200 > sma_200_20:
        stats.sma200_rising += 1

    if len(closes) < 260:
        return None

    close_3m = float(closes.iloc[-63])
    close_6m = float(closes.iloc[-126])
    close_9m = float(closes.iloc[-189])
    close_12m = float(closes.iloc[-250])

    rs_rating = (
        safe_ratio(current_close, close_3m) * 40
        + safe_ratio(current_close, close_6m) * 20
        + safe_ratio(current_close, close_9m) * 20
        + safe_ratio(current_close, close_12m) * 20
    )

    low_52w = float(closes.iloc[-260:].min())
    high_52w = float(closes.iloc[-260:].max())
    true_range_10d = float(closes.iloc[-10:].max() - closes.iloc[-10:].min())

    conditions = [
        current_close > sma_150 > sma_200,
        sma_50 > sma_200,
        sma_200 > sma_200_20,
        sma_50 > sma_150 > sma_200,
        current_close > sma_50,
        current_close >= 1.4 * low_52w,
        current_close >= 0.75 * high_52w,
        turnover >= 2_000_000,
        true_range_10d < current_close * 0.08,
        current_close > 10,
    ]

    if any(np.isnan(value) for value in (sma_50, sma_150, sma_200, sma_200_20)):
        return None

    if all(conditions):
        return ScreenResult(
            symbol=symbol,
            rs_rating=float(rs_rating),
            close=current_close,
            turnover=turnover,
            sma_20=sma_20,
            sma_50=sma_50,
            sma_150=sma_150,
            sma_200=sma_200,
            low_52w=low_52w,
            high_52w=high_52w,
            true_range_10d=true_range_10d,
        )

    return None


def select_chart_symbols(candidates: pd.DataFrame, chart_limit: int, min_rs_rank: float) -> list[str]:
    if candidates.empty or chart_limit <= 0:
        return []

    eligible = candidates[candidates["rs_rank"] >= min_rs_rank]
    if eligible.empty:
        return []

    return eligible.head(chart_limit)["symbol"].astype(str).tolist()


def to_plot_frame(hist: pd.DataFrame) -> pd.DataFrame:
    return hist.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )[["Open", "High", "Low", "Close", "Volume"]]


def create_candlestick_chart(
    hist: pd.DataFrame,
    title: str,
    output_path: Path,
    style: str = "starsandstripes",
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plot_frame = to_plot_frame(hist)

    mpf.plot(
        plot_frame,
        type="candle",
        mav=(20, 50, 200),
        volume=True,
        figratio=(16, 9),
        figscale=1.1,
        style=style,
        title=title,
        savefig=dict(fname=str(output_path), dpi=120, pad_inches=0.12),
    )
    plt.close("all")


def generate_charts(
    candidates: pd.DataFrame,
    chart_symbols: list[str],
    histories: dict[str, pd.DataFrame],
    output_dir: Path,
    include_indexes: bool,
) -> list[Path]:
    chart_paths: list[Path] = []
    chart_dir = output_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    if include_indexes:
        for symbol in INDEX_SYMBOLS:
            hist = histories.get(symbol)
            if hist is None or len(hist) < 50:
                continue
            path = chart_dir / f"index_{symbol}.png"
            create_candlestick_chart(
                hist=hist,
                title=f"{symbol} Index",
                output_path=path,
                style=INDEX_STYLES.get(symbol, "classic"),
            )
            chart_paths.append(path)

    if not chart_symbols or candidates.empty:
        return chart_paths

    by_symbol = candidates.set_index("symbol")

    for symbol in chart_symbols:
        if symbol not in by_symbol.index:
            continue

        hist = histories.get(symbol)
        if hist is None or len(hist) < 50:
            continue

        row = by_symbol.loc[symbol]
        path = chart_dir / f"{row.rs_rank:.4f}_{symbol}.png"
        create_candlestick_chart(
            hist=hist,
            title=f"{symbol} | RS Rank {row.rs_rank:.1%}",
            output_path=path,
        )
        chart_paths.append(path)

    return chart_paths


def build_market_summary(
    stats: MarketStats,
    as_of: date,
    symbols_scanned: int,
    symbols_with_data: int,
    candidates: int,
    runtime_seconds: float,
    screen_fetch_metrics: FetchMetrics,
    chart_fetch_metrics: FetchMetrics,
    cache_metrics: CacheMetrics | None,
) -> dict[str, Any]:
    summary = {
        "as_of": as_of.isoformat(),
        "symbols_scanned": symbols_scanned,
        "symbols_with_data": symbols_with_data,
        "advanced": stats.adv,
        "declined": stats.decl,
        "new_high": stats.new_high,
        "new_low": stats.new_low,
        "gauge": round(stats.gauge, 2),
        "pct_above_20dma": stats.pct(stats.above_20),
        "pct_above_50dma": stats.pct(stats.above_50),
        "pct_20dma_gt_50dma": stats.pct(stats.sma20_gt_50),
        "pct_50dma_gt_200dma": stats.pct(stats.sma50_gt_200),
        "pct_50_gt_150_gt_200": stats.pct(stats.sma50_gt_150_gt_200),
        "pct_200dma_rising": stats.pct(stats.sma200_rising),
        "candidates": candidates,
        "candidate_rate_pct": round(candidates / stats.total * 100.0, 3) if stats.total else 0.0,
        "runtime_seconds": round(runtime_seconds, 2),
        "screen_fetch": screen_fetch_metrics.to_dict(),
        "chart_fetch": chart_fetch_metrics.to_dict(),
    }

    if cache_metrics is not None:
        summary["cache"] = cache_metrics.to_dict()

    return summary


def flatten_summary_for_history(summary: dict[str, Any]) -> dict[str, Any]:
    flat = dict(summary)
    for key in ("screen_fetch", "chart_fetch", "cache"):
        value = flat.pop(key, {})
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[f"{key}_{sub_key}"] = sub_value
    return flat


def append_market_history(history_path: Path, summary: dict[str, Any]) -> None:
    row = pd.DataFrame([flatten_summary_for_history(summary)])
    if history_path.exists():
        old = pd.read_csv(history_path)
        merged = pd.concat([old, row], ignore_index=True)
        merged = merged.drop_duplicates(subset=["as_of"], keep="last")
        merged = merged.sort_values("as_of")
    else:
        merged = row
    merged.to_csv(history_path, index=False)


def create_pdf_report(
    output_path: Path,
    summary: dict[str, Any],
    candidates: pd.DataFrame,
    chart_paths: Iterable[Path],
) -> None:
    with PdfPages(output_path) as pdf:
        fig = plt.figure(figsize=(11, 8.5))
        fig.patch.set_facecolor("#f7f9fb")
        fig.text(0.05, 0.95, "VCP Screener Report", fontsize=24, fontweight="bold", va="top")
        fig.text(0.05, 0.90, f"As of {summary['as_of']}", fontsize=13)

        summary_lines = [
            f"Symbols scanned: {summary['symbols_scanned']} (with data: {summary['symbols_with_data']})",
            f"Advance/Decline: {summary['advanced']} / {summary['declined']}",
            f"New High/New Low: {summary['new_high']} / {summary['new_low']}",
            f"Gauge: {summary['gauge']:,}",
            f"Above 20-DMA: {summary['pct_above_20dma']}%",
            f"Above 50-DMA: {summary['pct_above_50dma']}%",
            f"20-DMA > 50-DMA: {summary['pct_20dma_gt_50dma']}%",
            f"50-DMA > 200-DMA: {summary['pct_50dma_gt_200dma']}%",
            f"50 > 150 > 200-DMA: {summary['pct_50_gt_150_gt_200']}%",
            f"Rising 200-DMA: {summary['pct_200dma_rising']}%",
            f"Candidates: {summary['candidates']} ({summary['candidate_rate_pct']}%)",
            f"Runtime: {summary['runtime_seconds']} sec",
        ]

        if isinstance(summary.get("cache"), dict):
            cache = summary["cache"]
            summary_lines.append(
                f"Cache: {cache.get('available_symbols', 0)}/{cache.get('requested_symbols', 0)} "
                f"available, refreshed {cache.get('refreshed_symbols', 0)}"
            )

        y = 0.84
        for line in summary_lines:
            fig.text(0.05, y, line, fontsize=12)
            y -= 0.042

        if not candidates.empty:
            top = candidates[["symbol", "rs_rank", "rs_rating", "close"]].head(25).copy()
            top["rs_rank"] = top["rs_rank"].map(lambda x: f"{x:.1%}")
            top["rs_rating"] = top["rs_rating"].map(lambda x: f"{x:.4f}")
            top["close"] = top["close"].map(lambda x: f"{x:.2f}")

            ax = fig.add_axes([0.53, 0.13, 0.42, 0.74])
            ax.axis("off")
            table = ax.table(
                cellText=top.values,
                colLabels=["Symbol", "RS Rank", "RS Rating", "Close"],
                loc="upper left",
                cellLoc="left",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(9)
            table.scale(1.05, 1.2)

        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        for chart_path in chart_paths:
            image = plt.imread(chart_path)
            fig, ax = plt.subplots(figsize=(11, 8.5))
            ax.imshow(image)
            ax.axis("off")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)


def main() -> int:
    args = parse_args()
    run_started = time.perf_counter()

    as_of = parse_as_of(args.as_of)
    start_date = as_of - timedelta(days=max(300, args.lookback_days))

    output_dir = args.output_root / as_of.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols = load_symbols(args.symbols_csv)

    stats = MarketStats()
    results: list[ScreenResult] = []
    symbols_with_data = 0

    def on_screen_batch(batch: list[str], histories: dict[str, pd.DataFrame]) -> None:
        nonlocal symbols_with_data
        for symbol in batch:
            hist = histories.get(symbol)
            if hist is None or hist.empty:
                continue
            symbols_with_data += 1
            result = screen_symbol(symbol=symbol, hist=hist, stats=stats)
            if result:
                results.append(result)

    screen_fetch_metrics = FetchMetrics()
    chart_fetch_metrics = FetchMetrics()
    cache_metrics: CacheMetrics | None = None

    if args.cache_enabled:
        cache_symbols = list(symbols)
        if args.index_charts:
            cache_symbols.extend(INDEX_SYMBOLS)
        cache_symbols = unique_ordered(cache_symbols)

        print(f"Preparing cache for {len(cache_symbols)} symbols ({start_date} -> {as_of})...")
        screen_fetch_metrics, cache_metrics = refresh_cache_for_symbols(
            symbols=cache_symbols,
            cache_dir=args.cache_dir,
            required_start_date=start_date,
            as_of=as_of,
            min_required_rows=260,
            max_cache_rows=max(300, args.max_cache_rows),
            refresh_padding_days=max(0, args.cache_refresh_padding_days),
            no_data_retry_days=max(0, args.cache_no_data_retry_days),
            stale_after_days=max(0, args.cache_stale_after_days),
            batch_size=max(1, args.batch_size),
            max_workers=max(1, args.max_workers),
            max_retries=max(0, args.max_retries),
            retry_backoff_seconds=max(0.0, args.retry_backoff_seconds),
            progress_every=max(0, args.progress_every),
            fallback_singletons=args.fallback_singletons,
            fallback_max_retries=max(0, args.fallback_max_retries),
            fallback_workers=max(1, args.fallback_workers),
        )

        print(f"Screening from cache ({len(symbols)} symbols)...")
        symbols_with_data = stream_cached_batches(
            symbols=symbols,
            cache_dir=args.cache_dir,
            batch_size=max(1, args.batch_size),
            progress_every=max(0, args.progress_every),
            label="Cache screen",
            on_batch=on_screen_batch,
        )

    else:
        print(f"Screening {len(symbols)} symbols ({start_date} -> {as_of})...")
        _, screen_fetch_metrics = run_batch_fetch(
            symbols=symbols,
            start_date=start_date,
            end_date=as_of,
            batch_size=max(1, args.batch_size),
            max_workers=max(1, args.max_workers),
            max_retries=max(0, args.max_retries),
            retry_backoff_seconds=max(0.0, args.retry_backoff_seconds),
            progress_every=max(0, args.progress_every),
            fallback_singletons=args.fallback_singletons,
            fallback_max_retries=max(0, args.fallback_max_retries),
            fallback_workers=max(1, args.fallback_workers),
            label="Screen fetch",
            on_batch=on_screen_batch,
        )

    if results:
        candidates = pd.DataFrame(asdict(row) for row in results)
        candidates["rs_rank"] = candidates["rs_rating"].rank(pct=True, method="min")
        candidates = candidates.sort_values(["rs_rank", "rs_rating"], ascending=[False, False])
    else:
        candidates = pd.DataFrame(
            columns=[
                "symbol",
                "rs_rating",
                "close",
                "turnover",
                "sma_20",
                "sma_50",
                "sma_150",
                "sma_200",
                "low_52w",
                "high_52w",
                "true_range_10d",
                "rs_rank",
            ]
        )

    candidates_path = output_dir / "candidates.csv"
    candidates.to_csv(candidates_path, index=False)

    chart_symbols = select_chart_symbols(
        candidates=candidates,
        chart_limit=max(0, args.chart_limit),
        min_rs_rank=max(0.0, min(1.0, args.min_rs_rank)),
    )

    chart_fetch_symbols: list[str] = list(chart_symbols)
    if args.index_charts:
        chart_fetch_symbols.extend(INDEX_SYMBOLS)
    chart_fetch_symbols = unique_ordered(chart_fetch_symbols)

    chart_histories: dict[str, pd.DataFrame] = {}

    if chart_fetch_symbols:
        if args.cache_enabled:
            chart_histories = load_histories_from_cache(chart_fetch_symbols, args.cache_dir)
            chart_fetch_metrics = FetchMetrics(
                requested_symbols=len(chart_fetch_symbols),
                total_batches=0,
                completed_batches=0,
                failed_batches=0,
                retries=0,
                symbols_returned=len(chart_histories),
                recovered_symbols=0,
            )
            print(
                f"Cache chart load: symbols with data {len(chart_histories)}/{len(chart_fetch_symbols)}"
            )
        else:
            print(f"Fetching chart histories for {len(chart_fetch_symbols)} symbols...")
            chart_histories, chart_fetch_metrics = run_batch_fetch(
                symbols=chart_fetch_symbols,
                start_date=start_date,
                end_date=as_of,
                batch_size=min(max(1, args.batch_size), 60),
                max_workers=max(1, min(args.max_workers, 4)),
                max_retries=max(0, args.max_retries),
                retry_backoff_seconds=max(0.0, args.retry_backoff_seconds),
                progress_every=max(0, args.progress_every),
                fallback_singletons=args.fallback_singletons,
                fallback_max_retries=max(0, args.fallback_max_retries),
                fallback_workers=max(1, args.fallback_workers),
                label="Chart fetch",
                on_batch=None,
            )

    chart_paths = generate_charts(
        candidates=candidates,
        chart_symbols=chart_symbols,
        histories=chart_histories,
        output_dir=output_dir,
        include_indexes=args.index_charts,
    )

    runtime_seconds = time.perf_counter() - run_started

    summary = build_market_summary(
        stats=stats,
        as_of=as_of,
        symbols_scanned=len(symbols),
        symbols_with_data=symbols_with_data,
        candidates=len(candidates),
        runtime_seconds=runtime_seconds,
        screen_fetch_metrics=screen_fetch_metrics,
        chart_fetch_metrics=chart_fetch_metrics,
        cache_metrics=cache_metrics,
    )

    summary_path = output_dir / "market_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    history_path = args.output_root / "market_history.csv"
    append_market_history(history_path, summary)

    if args.pdf_report:
        pdf_path = output_dir / "report.pdf"
        create_pdf_report(
            output_path=pdf_path,
            summary=summary,
            candidates=candidates,
            chart_paths=chart_paths,
        )

    print("Run complete")
    print(f"- Candidates: {len(candidates)}")
    print(f"- Summary:    {summary_path}")
    print(f"- CSV:        {candidates_path}")
    print(f"- Charts:     {len(chart_paths)}")
    print(f"- Runtime:    {runtime_seconds:.2f} sec")
    if args.pdf_report:
        print(f"- PDF:        {output_dir / 'report.pdf'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
