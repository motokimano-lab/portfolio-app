import pandas as pd
import yfinance as yf
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from zoneinfo import ZoneInfo
from dividend_tax import normalize_account_type, calc_after_tax_dividend

# --- 為替 ---
import time

def get_fx(symbol, default, max_retry=5, wait_sec=5):
    for i in range(max_retry):
        try:
            fx = yf.Ticker(symbol)
            hist = fx.history(period="5d")

            if hist.empty:
                raise ValueError("empty data")

            price = float(hist["Close"].iloc[-1])

            if price > 0:
                return price, False

        except:
            pass

        time.sleep(wait_sec)

    return float(default), True

# --- 価格（これだけ使う） ---
def _dividends_on_current_share_basis(history):
    """Split candidates supported by price gaps or adjacent dividends.

    Try cumulative split ratios, including same-day and reverse splits.
    Adjacent dividends can support a correction or veto double adjustment.
    Keep ambiguous events unchanged; both signals are heuristics.
    Close/Open must be split-adjusted, not dividend-adjusted prices.
    """
    history = history.sort_index()
    dividends = pd.to_numeric(history['Dividends'], errors='coerce').fillna(0)
    dividends = dividends[dividends > 0].copy()
    if 'Stock Splits' not in history.columns:
        return dividends
    splits = pd.to_numeric(history['Stock Splits'], errors='coerce').fillna(0)
    splits = splits[(splits > 0) & (splits != 1)]
    original = dividends.copy()
    for ordinal, (date, dividend) in enumerate(original.items()):
        later = splits.loc[splits.index >= date]
        if later.empty:
            continue
        candidates = [float(dividend)]
        ratio = 1.0
        for split in reversed(later.tolist()):
            ratio *= float(split)
            candidates.append(float(dividend) / ratio)
        references = []
        if ordinal > 0:
            references.append(float(dividends.iloc[ordinal - 1]))
        if ordinal + 1 < len(original):
            references.append(float(original.iloc[ordinal + 1]))
        position = history.index.get_loc(date)
        drop = float('nan')
        if position > 0:
            drop = float(history['Close'].iloc[position - 1]) - float(
                history.get('Open', history['Close']).iloc[position])
        supported = []
        for candidate in candidates[1:]:
            price_support = (pd.notna(drop) and drop > 0
                and abs(candidate - drop)*5 < abs(dividend - drop)
                and abs(candidate - drop) <= candidate)
            continuity_support = any(
                abs(candidate - reference)/reference <= 0.25
                and abs(candidate - reference)*5 < abs(dividend - reference)
                for reference in references)
            # A neighboring dividend that already agrees with the raw value
            # is evidence against a second split adjustment.
            continuity_conflict = any(
                abs(dividend - reference)/reference <= 0.25
                and abs(candidate - reference)/reference > 0.25
                for reference in references)
            if (price_support or continuity_support) and not continuity_conflict:
                supported.append(candidate)
        if supported:
            def evidence_error(candidate):
                continuity_error = min((abs(candidate-r)/r for r in references),
                                       default=float('inf'))
                price_error = abs(candidate-drop)/candidate if pd.notna(drop) and drop > 0 else float('inf')
                return min(continuity_error, price_error)
            dividends.loc[date] = min(supported, key=evidence_error)
    return dividends


def get_assets_data_bulk(tickers, max_retry=5, wait_sec=5, include_dividend_details=False):
    """
    価格・過去365日配当・前日比を一括取得する。

    include_dividend_details=True の場合は、通常の4戻り値に加えて
    1株あたり過去365日配当、1株あたり予想配当（直近配当の年率換算）、
    予想値の算出根拠を返す。

    予想配当は会社予想ではなく、直近の支払実績から推計する Phase 1 用の
    forward run-rate。将来は Dividend_Data シートの手動補正を優先できる設計を想定。
    """
    def empty_result():
        base = ({}, {}, {}, [])
        if include_dividend_details:
            return (*base, {}, {}, {})
        return base

    if not tickers:
        return empty_result()

    yf_tickers = list(set([
        str(t).strip().upper() for t in tickers
        if t != "CASH" and pd.notna(t)
    ]))

    if not yf_tickers:
        return empty_result()

    for attempt in range(max_retry):
        price_dict = {}
        div_yield_dict = {}
        perf_dict = {}
        ttm_dividend_dict = {}
        forecast_dividend_dict = {}
        forecast_source_dict = {}
        errors = []

        try:
            data = yf.download(
                yf_tickers,
                period="1y",
                actions=True,
                group_by="ticker",
                progress=False
            )
        except Exception:
            errors = yf_tickers.copy()
            data = None

        for t in yf_tickers:
            try:
                if data is None:
                    errors.append(t)
                    continue

                if isinstance(data.columns, pd.MultiIndex):
                    if t in data.columns.get_level_values(0):
                        df_t = data[t]
                    else:
                        errors.append(t)
                        continue
                elif len(yf_tickers) == 1:
                    df_t = data
                else:
                    errors.append(t)
                    continue

                df_t = df_t.dropna(subset=["Close"])

                annual_div_total = 0.0
                forecast_div_total = 0.0
                forecast_source = "配当なし/未取得"

                # --- fallback ---
                if df_t.empty:
                    fallback = yf.Ticker(t).history(period="5d")
                    if fallback.empty:
                        errors.append(t)
                        continue

                    current_price = float(fallback["Close"].iloc[-1])

                    if len(fallback) >= 2:
                        prev = float(fallback["Close"].iloc[-2])
                        perf_dict[t] = (
                            current_price - prev,
                            (current_price - prev) / prev * 100
                        )
                    else:
                        perf_dict[t] = (0.0, 0.0)

                else:
                    current_price = float(df_t["Close"].iloc[-1])

                    if len(df_t) >= 2:
                        prev_price = float(df_t["Close"].iloc[-2])
                        diff_val = current_price - prev_price
                        diff_pct = (diff_val / prev_price) * 100
                        perf_dict[t] = (diff_val, diff_pct)
                    else:
                        perf_dict[t] = (0.0, 0.0)

                    if "Dividends" in df_t.columns:
                        dividends_paid = (
                            pd.to_numeric(df_t["Dividends"], errors="coerce")
                            .fillna(0)
                        )
                        dividends_paid = dividends_paid[dividends_paid > 0]
                        split_events = (
                            pd.to_numeric(df_t["Stock Splits"], errors="coerce")
                            .fillna(0)
                            if "Stock Splits" in df_t.columns
                            else pd.Series(dtype=float)
                        )
                        if ((split_events > 0) & (split_events != 1)).any():
                            # Separate unadjusted OHLC for the basis check only.
                            # Keep the original download's prices/performance.
                            event_history = yf.Ticker(t).history(
                                start=df_t.index.min().date(),
                                end=(df_t.index.max() + pd.Timedelta(days=1)).date(),
                                auto_adjust=False,
                                actions=True,
                            )
                            if event_history.empty:
                                raise ValueError("split/dividend verification data unavailable")
                            normalized = _dividends_on_current_share_basis(event_history)
                            # Match local trading dates across timezone-aware
                            # history and timezone-naive bulk-download indexes.
                            by_date = {d.date(): v for d, v in normalized.items()}
                            dividends_paid = pd.Series(
                                [by_date.get(d.date(), v) for d, v in dividends_paid.items()],
                                index=dividends_paid.index,
                                dtype=float,
                            )
                    else:
                        dividends_paid = pd.Series(dtype=float)

                    annual_div_total = float(dividends_paid.sum())

                    # Phase 1 の予想値:
                    # 直近1回の配当 × 推定年間支払回数。
                    # 支払回数は直近の支払間隔から 1/2/4/6/12 回へ丸める。
                    if not dividends_paid.empty:
                        latest_dividend = float(dividends_paid.iloc[-1])

                        if len(dividends_paid) >= 2:
                            intervals = (
                                dividends_paid.index.to_series()
                                .diff()
                                .dt.days
                                .dropna()
                            )
                            median_days = float(intervals.median()) if not intervals.empty else 365.0

                            if median_days <= 45:
                                payments_per_year = 12
                            elif median_days <= 75:
                                payments_per_year = 6
                            elif median_days <= 120:
                                payments_per_year = 4
                            elif median_days <= 220:
                                payments_per_year = 2
                            else:
                                payments_per_year = 1
                        else:
                            payments_per_year = 1

                        forecast_div_total = latest_dividend * payments_per_year
                        forecast_source = f"直近配当×{payments_per_year}回/年（推計）"

                for key in [t.upper(), t.lower()]:
                    price_dict[key] = current_price
                    ttm_dividend_dict[key] = annual_div_total
                    forecast_dividend_dict[key] = forecast_div_total
                    forecast_source_dict[key] = forecast_source
                    div_yield_dict[key] = (
                        annual_div_total / current_price
                        if current_price > 0 else 0.0
                    )
                    if t in perf_dict:
                        perf_dict[key] = perf_dict[t]

            except Exception:
                errors.append(t)
                continue

        errors = list(dict.fromkeys(errors))

        if not errors and all(
            (v is not None and v > 0) for v in price_dict.values()
        ):
            base = (price_dict, div_yield_dict, perf_dict, errors)
            if include_dividend_details:
                return (
                    *base,
                    ttm_dividend_dict,
                    forecast_dividend_dict,
                    forecast_source_dict
                )
            return base

        print(f"[RETRY] attempt {attempt+1} failed. errors={errors}")
        time.sleep(wait_sec)

    base = (price_dict, div_yield_dict, perf_dict, errors)
    if include_dividend_details:
        return (
            *base,
            ttm_dividend_dict,
            forecast_dividend_dict,
            forecast_source_dict
        )
    return base

# --- DataFrame整形 ---
def normalize_positions(df):
    """Optional native-fund columns; legacy rows retain unit=1 and all values."""
    from mutual_funds import FUND_SPECS

    df = df.copy()
    for column in ["instrument_type", "fund_code", "product_name"]:
        if column not in df.columns:
            df[column] = ""
        df[column] = df[column].fillna("").astype(str).str.strip()
    if "account_type" in df.columns:
        df["account_type"] = df["account_type"].fillna("").map(normalize_account_type)
    df["instrument_type"] = df["instrument_type"].str.lower()
    native = df["instrument_type"].eq("mutual_fund")
    if "price_unit" not in df.columns:
        df["price_unit"] = float("nan")
    df["price_unit"] = pd.to_numeric(df["price_unit"], errors="coerce")
    df.loc[~native, "price_unit"] = 1.0
    df.loc[native, "price_unit"] = df.loc[native, "price_unit"].fillna(10000)
    if (df.loc[native, "price_unit"] != 10000).any():
        raise ValueError("投資信託のprice_unitは10000で指定してください")
    if df.loc[native, "fund_code"].eq("").any():
        raise ValueError("投資信託のfund_codeが未入力です")
    if (~df.loc[native, "currency"].fillna("JPY").isin(["JPY", ""])).any():
        raise ValueError("投資信託のcurrencyはJPYで指定してください")
    df.loc[native, "currency"] = "JPY"
    df.loc[native, "ticker"] = "MF:" + df.loc[native, "fund_code"]
    # Model attributes come from the official product registry, never ticker guesses.
    df["foreign_tax_adjustment"] = False
    df["underlying_foreign_tax_rate"] = 0.0
    for code, spec in FUND_SPECS.items():
        selected = native & df["fund_code"].eq(code)
        df.loc[selected, "product_name"] = spec["product_name"]
        df.loc[selected, "display_name"] = spec["display_name"]
        df.loc[selected, "foreign_tax_adjustment"] = spec.get("foreign_tax_adjustment", False)
        df.loc[selected, "underlying_foreign_tax_rate"] = spec.get("underlying_foreign_tax_rate", 0.0)
    return df


def get_position_assets_data(df, include_dividend_details=False, now=None):
    """Keep the old tuple API plus native-fund status; no MF goes to Yahoo."""
    from mutual_funds import get_mutual_fund_data

    df = normalize_positions(df)
    native = df["instrument_type"].eq("mutual_fund")
    legacy = df.loc[~native, "ticker"].unique().tolist()
    data = get_assets_data_bulk(legacy, include_dividend_details=True)
    price, yields, perf, errors, ttm, forecast, sources = data
    status = {}
    for code in df.loc[native, "fund_code"].unique():
        record = get_mutual_fund_data(code, now=now)
        ticker = "MF:" + code
        status[ticker] = record
        if not record["success"]:
            errors.append(ticker)
            price[ticker] = float("nan")
            ttm[ticker] = forecast[ticker] = float("nan")
            continue
        price[ticker] = record["price"]
        ttm[ticker] = record["ttm_distribution"]
        forecast[ticker] = record["forecast_distribution"]
        yields[ticker] = ttm[ticker] / price[ticker]
        perf[ticker] = (record["previous_change"], record["previous_change_pct"])
        sources[ticker] = record["forecast_source"]
    result = (price, yields, perf, list(dict.fromkeys(errors)))
    if include_dividend_details:
        result = (*result, ttm, forecast, sources)
    return result, status


def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = normalize_positions(df)

    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["cost_price"] = pd.to_numeric(df["cost_price"], errors="coerce")

    df["value"] = df["price"] * df["quantity"] / df["price_unit"]

    def convert_to_jpy(row):
        if row["currency"] == "USD":
            return row["value"] * usd_jpy
        elif row["currency"] == "VND":
            return row["value"] * vnd_jpy
        return row["value"]

    df["value_jpy"] = df.apply(convert_to_jpy, axis=1)
    df["fx_to_jpy"] = df["currency"].map(
        lambda currency: usd_jpy if currency == "USD" else vnd_jpy if currency == "VND" else 1.0
    )
    df["cost_basis_jpy"] = df["quantity"] / df["price_unit"] * df["cost_price"] * df["fx_to_jpy"]
    cash_mask = df["ticker"].astype(str).str.strip().str.upper().eq("CASH")
    df.loc[cash_mask, "cost_basis_jpy"] = df.loc[cash_mask, "value_jpy"]

    df["sector_group"] = df["sector"].fillna("未分類")

    return df

def clean_tickers(tickers):
    cleaned = []
    for t in tickers:
        if isinstance(t, str) and len(t) > 0:
            if not t.isdigit():  # "1","2"除外
                cleaned.append(t)
    return list(set(cleaned))

def build_current_market_movers(df, top_n=10):
    """Rank current holdings by price/NAV day return, independent of cash flows/trades.

    Multiple accounts holding the same display_name are combined using current
    JPY value as weights. CASH rows are excluded. This means deposits,
    withdrawals, new purchases, partial sales, and buy-size changes do not turn
    into fake gainers/losers in the Discord ranking.
    """
    data = df.copy()
    if data.empty:
        empty = pd.DataFrame(columns=["display_name", "market_pct"])
        return empty, empty

    data["ticker"] = data["ticker"].astype(str).str.strip().str.upper()
    data["display_name"] = data["display_name"].fillna(data["ticker"]).astype(str)
    data["day_diff_pct"] = pd.to_numeric(data["day_diff_pct"], errors="coerce")
    data["value_jpy"] = pd.to_numeric(data["value_jpy"], errors="coerce").fillna(0)

    data = data[
        data["ticker"].ne("CASH")
        & data["day_diff_pct"].notna()
        & data["value_jpy"].gt(0)
    ].copy()

    if data.empty:
        empty = pd.DataFrame(columns=["display_name", "market_pct"])
        return empty, empty

    data["weighted_pct"] = data["day_diff_pct"] * data["value_jpy"]
    grouped = (
        data.groupby("display_name", as_index=False)
        .agg(value_jpy=("value_jpy", "sum"), weighted_pct=("weighted_pct", "sum"))
    )
    grouped["market_pct"] = grouped["weighted_pct"] / grouped["value_jpy"]

    gainers = (
        grouped.sort_values("market_pct", ascending=False)
        .head(top_n)[["display_name", "market_pct"]]
        .reset_index(drop=True)
    )
    losers = (
        grouped.sort_values("market_pct", ascending=True)
        .head(top_n)[["display_name", "market_pct"]]
        .reset_index(drop=True)
    )
    return gainers, losers


ATTRIBUTION_SCHEMA_VERSION = 5


DAILY_LOG_V2_HEADERS = [
    "date", "snapshot_at", "owner", "account_type", "ticker", "display_name",
    "asset_class", "sector", "currency", "instrument_type", "fund_code",
    "product_name", "price_unit", "quantity", "price", "fx_to_jpy",
    "value_jpy", "cost_basis_jpy", "purpose", "role", "day_diff_pct",
    "fund_base_date", "proxy_ticker", "dividend_forecast_per_unit",
    "annual_div_forecast_jpy", "after_tax_div_forecast_jpy",
    "dividend_forecast_source"
]


def _contiguous_row_ranges(row_numbers):
    """Convert sorted 1-based row numbers into inclusive contiguous ranges."""
    rows = sorted(set(int(r) for r in row_numbers))
    if not rows:
        return []
    ranges = []
    start = previous = rows[0]
    for row in rows[1:]:
        if row == previous + 1:
            previous = row
            continue
        ranges.append((start, previous))
        start = previous = row
    ranges.append((start, previous))
    return ranges


def _replace_today_rows_safely(sheet, today, rows):
    """Replace only today's rows without clearing historical data.

    New rows are appended first. Only after the append succeeds are old rows
    for the same date deleted. A mid-write failure can therefore create
    duplicates for today, but cannot erase prior dates. The next successful run
    cleans up any duplicate rows for today.
    """
    existing_dates = sheet.col_values(1)
    old_today_rows = [
        index
        for index, value in enumerate(existing_dates, start=1)
        if index > 1 and str(value).strip() == today
    ]

    sheet.append_rows(rows, value_input_option="RAW")

    # Delete bottom-up so earlier row numbers remain stable while rows shift.
    for start, end in reversed(_contiguous_row_ranges(old_today_rows)):
        sheet.delete_rows(start, end)

    return len(old_today_rows)


def _sheet_client(creds_dict):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)


def _as_text(row, column):
    value = row.get(column, "")
    return str(value) if pd.notna(value) else ""


def _as_float(row, column, default=0.0):
    value = pd.to_numeric(pd.Series([row.get(column, default)]), errors="coerce").iloc[0]
    return float(value) if pd.notna(value) else float(default)


def save_daily_log_detail(df, creds_dict):
    client = _sheet_client(creds_dict)
    sheet = client.open("portfolio_data").worksheet("Daily_Log")

    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    rows = []
    for _, r in df.iterrows():
        rows.append([
            today,
            _as_text(r, "ticker"),
            _as_text(r, "display_name"),
            _as_text(r, "asset_class"),
            _as_text(r, "sector"),
            _as_float(r, "value_jpy"),
        ])

    replaced = _replace_today_rows_safely(sheet, today, rows)
    return f"{len(rows)} rows saved; replaced {replaced} rows for {today}"


def save_daily_log_v2(df, creds_dict, fund_status=None):
    """Save a detailed daily snapshot for future attribution analysis.

    The legacy Daily_Log remains the canonical source for the existing
    two-date comparison UI. Daily_Log_V2 starts collecting richer snapshots
    without changing or reinterpreting historical Daily_Log rows.
    """
    client = _sheet_client(creds_dict)
    sheet = client.open("portfolio_data").worksheet("Daily_Log_V2")

    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    today = now.strftime("%Y-%m-%d")
    snapshot_at = now.isoformat()

    from mutual_funds import FUND_SPECS

    status_by_code = {}
    for ticker, record in (fund_status or {}).items():
        code = str(ticker).replace("MF:", "")
        status_by_code[code] = record

    rows = []
    for _, r in df.iterrows():
        fund_code = _as_text(r, "fund_code")
        fund_record = status_by_code.get(fund_code, {})
        spec = FUND_SPECS.get(fund_code, {})
        quantity = _as_float(r, "quantity")
        price_unit = _as_float(r, "price_unit", 1.0) or 1.0
        fx_to_jpy = _as_float(r, "fx_to_jpy", 1.0) or 1.0
        dividend_forecast_per_unit = _as_float(
            r, "dividend_forecast_per_share"
        )
        annual_div_forecast_jpy = (
            quantity / price_unit
            * dividend_forecast_per_unit
            * fx_to_jpy
        )
        tax_row = {
            "annual_div_jpy": annual_div_forecast_jpy,
            "ticker": _as_text(r, "ticker"),
            "currency": _as_text(r, "currency"),
            "account_type": _as_text(r, "account_type"),
            "instrument_type": _as_text(r, "instrument_type"),
            "foreign_tax_adjustment": bool(
                r.get("foreign_tax_adjustment", False)
            ),
            "underlying_foreign_tax_rate": _as_float(
                r, "underlying_foreign_tax_rate", 0.0
            ),
        }
        after_tax_div_forecast_jpy = calc_after_tax_dividend(tax_row)

        rows.append([
            today,
            snapshot_at,
            _as_text(r, "owner"),
            _as_text(r, "account_type"),
            _as_text(r, "ticker"),
            _as_text(r, "display_name"),
            _as_text(r, "asset_class"),
            _as_text(r, "sector"),
            _as_text(r, "currency"),
            _as_text(r, "instrument_type"),
            fund_code,
            _as_text(r, "product_name"),
            price_unit,
            quantity,
            _as_float(r, "price"),
            fx_to_jpy,
            _as_float(r, "value_jpy"),
            _as_float(r, "cost_basis_jpy"),
            _as_text(r, "purpose"),
            _as_text(r, "role"),
            _as_float(r, "day_diff_pct"),
            str(fund_record.get("base_date") or ""),
            str(spec.get("attribution_proxy") or ""),
            dividend_forecast_per_unit,
            annual_div_forecast_jpy,
            after_tax_div_forecast_jpy,
            _as_text(r, "dividend_forecast_source"),
        ])

    replaced = _replace_today_rows_safely(sheet, today, rows)
    return f"Daily_Log_V2: {len(rows)} rows saved; replaced {replaced} rows for {today}"


ATTRIBUTION_ID_COLUMNS = [
    "owner", "account_type", "ticker", "display_name", "asset_class", "sector",
    "currency", "instrument_type", "fund_code", "product_name", "price_unit",
    "purpose", "role",
]


def normalize_daily_log_v2(df):
    """Normalize detailed snapshots used by the attribution engine."""
    data = df.copy()
    if data.empty:
        return data

    # These columns were added after V2 started. Treat them as optional for
    # backwards compatibility with the first snapshots.
    text_optional = [
        "fund_base_date", "proxy_ticker", "dividend_forecast_source"
    ]
    numeric_optional = [
        "dividend_forecast_per_unit", "annual_div_forecast_jpy",
        "after_tax_div_forecast_jpy",
    ]
    for column in text_optional:
        if column not in data.columns:
            data[column] = ""
    for column in numeric_optional:
        if column not in data.columns:
            data[column] = float("nan")

    required = set(DAILY_LOG_V2_HEADERS) - set(text_optional) - set(numeric_optional)
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError("Daily_Log_V2 required columns missing: " + ", ".join(missing))

    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.date
    data = data[data["date"].notna()].copy()

    text_columns = [
        "owner", "account_type", "ticker", "display_name", "asset_class", "sector",
        "currency", "instrument_type", "fund_code", "product_name", "purpose", "role",
        "fund_base_date", "proxy_ticker", "dividend_forecast_source",
    ]
    for column in text_columns:
        data[column] = data[column].fillna("").astype(str).str.strip()

    numeric_columns = [
        "price_unit", "quantity", "price", "fx_to_jpy", "value_jpy",
        "cost_basis_jpy", "day_diff_pct", "dividend_forecast_per_unit",
        "annual_div_forecast_jpy", "after_tax_div_forecast_jpy",
    ]
    for column in numeric_columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    data["price_unit"] = data["price_unit"].fillna(1.0)
    data["fx_to_jpy"] = data["fx_to_jpy"].fillna(1.0)
    data["quantity"] = data["quantity"].fillna(0.0)
    data["price"] = data["price"].fillna(0.0)
    data["value_jpy"] = data["value_jpy"].fillna(0.0)

    return data


def _snapshot_positions(df_day):
    """Aggregate duplicate account rows into stable attribution positions."""
    if df_day.empty:
        return pd.DataFrame()

    group_columns = ATTRIBUTION_ID_COLUMNS
    aggregated = (
        df_day.groupby(group_columns, dropna=False, as_index=False)
        .agg(
            quantity=("quantity", "sum"),
            price=("price", "first"),
            fx_to_jpy=("fx_to_jpy", "first"),
            value_jpy=("value_jpy", "sum"),
            cost_basis_jpy=("cost_basis_jpy", "sum"),
            fund_base_date=("fund_base_date", "first"),
            proxy_ticker=("proxy_ticker", "first"),
        )
    )
    return aggregated


def _position_value(quantity, price, fx_to_jpy, price_unit):
    unit = float(price_unit) if price_unit else 1.0
    return float(quantity) / unit * float(price) * float(fx_to_jpy)


def _shapley_position_effects(q0, p0, f0, q1, p1, f1, price_unit):
    """Exact symmetric 3-factor decomposition for quantity, local price and FX."""
    from itertools import permutations

    old = {"quantity": float(q0), "market": float(p0), "fx": float(f0)}
    new = {"quantity": float(q1), "market": float(p1), "fx": float(f1)}
    effects = {"quantity": 0.0, "market": 0.0, "fx": 0.0}

    for order in permutations(("quantity", "market", "fx")):
        state = old.copy()
        before = _position_value(
            state["quantity"], state["market"], state["fx"], price_unit
        )
        for factor in order:
            state[factor] = new[factor]
            after = _position_value(
                state["quantity"], state["market"], state["fx"], price_unit
            )
            effects[factor] += after - before
            before = after

    divisor = 6.0
    return {key: value / divisor for key, value in effects.items()}


def _shapley_fund_effects(q0, q1, nav0, market_ratio, fx_ratio, residual_ratio, price_unit):
    """Exact 4-factor decomposition for a JPY-priced unhedged US equity fund.

    The observed NAV ratio is represented as:
      proxy US-market ratio × USDJPY ratio × residual ratio.
    Shapley allocation splits interaction terms symmetrically so quantity,
    market, FX, and residual effects add exactly to the JPY valuation change.
    """
    from itertools import permutations

    old = {"quantity": float(q0), "market": 1.0, "fx": 1.0, "residual": 1.0}
    new = {
        "quantity": float(q1),
        "market": float(market_ratio),
        "fx": float(fx_ratio),
        "residual": float(residual_ratio),
    }
    effects = {key: 0.0 for key in old}
    unit = float(price_unit) if price_unit else 1.0

    def value(state):
        return (
            state["quantity"] / unit
            * float(nav0)
            * state["market"]
            * state["fx"]
            * state["residual"]
        )

    factors = ("quantity", "market", "fx", "residual")
    orders = list(permutations(factors))
    for order in orders:
        state = old.copy()
        before = value(state)
        for factor in order:
            state[factor] = new[factor]
            after = value(state)
            effects[factor] += after - before
            before = after

    divisor = float(len(orders))
    return {key: value_ / divisor for key, value_ in effects.items()}


def _history_close_series(symbol, start_date, end_date):
    """Download a simple adjusted-close series for attribution references."""
    history = yf.Ticker(symbol).history(
        start=pd.Timestamp(start_date).date(),
        end=pd.Timestamp(end_date).date(),
        auto_adjust=True,
        actions=False,
    )
    if history.empty or "Close" not in history.columns:
        raise ValueError(f"reference history unavailable: {symbol}")
    close = pd.to_numeric(history["Close"], errors="coerce").dropna()
    if close.empty:
        raise ValueError(f"reference close unavailable: {symbol}")
    close.index = pd.to_datetime(close.index)
    return close


def build_fund_reference_data(df_v2):
    """Build proxy-price and USDJPY references aligned to Japanese fund NAV dates.

    For a Japanese NAV dated D, the US-equity proxy uses the latest US close
    strictly before D. USDJPY uses the latest daily close on or before D.
    This is an estimate because Japanese funds use their own valuation timing
    and reference FX rates; any mismatch is kept as a separate residual effect.
    """
    data = normalize_daily_log_v2(df_v2)
    if data.empty:
        return {}, []

    funds = data[
        data["instrument_type"].eq("mutual_fund")
        & data["fund_base_date"].ne("")
        & data["proxy_ticker"].ne("")
    ].copy()
    if funds.empty:
        return {}, ["投資信託の基準日/ベンチマーク情報が未記録です"]

    funds["fund_base_date_parsed"] = pd.to_datetime(
        funds["fund_base_date"], errors="coerce"
    ).dt.date
    funds = funds[funds["fund_base_date_parsed"].notna()].copy()
    if funds.empty:
        return {}, ["投資信託の基準日を解釈できません"]

    dates = sorted(funds["fund_base_date_parsed"].unique())
    start = pd.Timestamp(dates[0]) - pd.Timedelta(days=10)
    end = pd.Timestamp(dates[-1]) + pd.Timedelta(days=2)

    references = {}
    warnings = []

    try:
        fx_history = _history_close_series("JPY=X", start, end)
    except Exception as error:
        fx_history = pd.Series(dtype=float)
        warnings.append(f"USDJPY参照取得失敗: {type(error).__name__}")

    proxy_histories = {}
    for proxy in sorted(funds["proxy_ticker"].unique()):
        try:
            proxy_histories[proxy] = _history_close_series(proxy, start, end)
        except Exception as error:
            warnings.append(f"{proxy}参照取得失敗: {type(error).__name__}")

    for _, row in (
        funds[["proxy_ticker", "fund_base_date_parsed"]]
        .drop_duplicates()
        .iterrows()
    ):
        proxy = row["proxy_ticker"]
        base_date = row["fund_base_date_parsed"]
        proxy_history = proxy_histories.get(proxy)
        if proxy_history is None or proxy_history.empty or fx_history.empty:
            continue

        proxy_candidates = proxy_history[
            proxy_history.index.date < base_date
        ]
        fx_candidates = fx_history[
            fx_history.index.date <= base_date
        ]
        if proxy_candidates.empty or fx_candidates.empty:
            warnings.append(f"{proxy} {base_date}: 対応する市場/為替参照値なし")
            continue

        references[(proxy, base_date.isoformat())] = {
            "proxy_price": float(proxy_candidates.iloc[-1]),
            "proxy_market_date": proxy_candidates.index[-1].date().isoformat(),
            "usd_jpy": float(fx_candidates.iloc[-1]),
            "fx_date": fx_candidates.index[-1].date().isoformat(),
        }

    return references, list(dict.fromkeys(warnings))


def _fund_reference_for(row, fund_reference):
    proxy = str(row.get("proxy_ticker", "") or "").strip()
    base_date = str(row.get("fund_base_date", "") or "").strip()
    if not proxy or not base_date:
        return None
    return (fund_reference or {}).get((proxy, base_date))


def attribution_between_snapshots(
    start_df, end_df, start_date, end_date, fund_reference=None
):
    """Return exact per-position JPY attribution between two snapshots."""
    start = _snapshot_positions(start_df)
    end = _snapshot_positions(end_df)

    start_map = {}
    end_map = {}

    def key_for(row):
        return tuple(row[column] for column in ATTRIBUTION_ID_COLUMNS)

    for _, row in start.iterrows():
        start_map[key_for(row)] = row
    for _, row in end.iterrows():
        end_map[key_for(row)] = row

    records = []
    for key in sorted(set(start_map) | set(end_map), key=lambda x: tuple(map(str, x))):
        before = start_map.get(key)
        after = end_map.get(key)
        meta_source = after if after is not None else before

        unit = float(meta_source["price_unit"] or 1.0)
        if before is not None and after is not None:
            before_unit = float(before["price_unit"] or 1.0)
            after_unit = float(after["price_unit"] or 1.0)
            if abs(before_unit - after_unit) > 1e-12:
                raise ValueError(
                    f"price_unit changed for {meta_source['ticker']}: "
                    f"{before_unit} -> {after_unit}"
                )
            unit = before_unit

        if before is None:
            q0 = 0.0
            p0 = float(after["price"])
            f0 = float(after["fx_to_jpy"])
            v0 = 0.0
        else:
            q0 = float(before["quantity"])
            p0 = float(before["price"])
            f0 = float(before["fx_to_jpy"])
            v0 = float(before["value_jpy"])

        if after is None:
            q1 = 0.0
            p1 = float(before["price"])
            f1 = float(before["fx_to_jpy"])
            v1 = 0.0
        else:
            q1 = float(after["quantity"])
            p1 = float(after["price"])
            f1 = float(after["fx_to_jpy"])
            v1 = float(after["value_jpy"])

        total_change = v1 - v0
        residual_effect = 0.0
        fund_fx_estimated = False
        attribution_method = "direct"

        is_fund = str(meta_source.get("instrument_type", "")).strip() == "mutual_fund"
        before_ref = _fund_reference_for(before, fund_reference) if before is not None else None
        after_ref = _fund_reference_for(after, fund_reference) if after is not None else None

        if (
            is_fund
            and before is not None
            and after is not None
            and before_ref
            and after_ref
            and p0 > 0
            and before_ref["proxy_price"] > 0
            and before_ref["usd_jpy"] > 0
        ):
            market_ratio = after_ref["proxy_price"] / before_ref["proxy_price"]
            fx_ratio = after_ref["usd_jpy"] / before_ref["usd_jpy"]
            nav_ratio = p1 / p0
            residual_ratio = nav_ratio / (market_ratio * fx_ratio)

            effects = _shapley_fund_effects(
                q0, q1, p0, market_ratio, fx_ratio, residual_ratio, unit
            )
            residual = total_change - sum(effects.values())
            effects["residual"] += residual
            residual_effect = effects["residual"]
            fund_fx_estimated = True
            attribution_method = "fund_proxy_estimate"
        else:
            effects = _shapley_position_effects(
                q0, p0, f0, q1, p1, f1, unit
            )
            residual = total_change - sum(effects.values())
            effects["market"] += residual

        ticker_text = str(meta_source.get("ticker", "") or "").strip().upper()
        sector_text = str(meta_source.get("sector", "") or "").strip()
        asset_class_text = str(meta_source.get("asset_class", "") or "").strip()
        currency_text = str(meta_source.get("currency", "") or "").strip().upper()

        if ticker_text == "CASH":
            market_group = "現金"
        elif is_fund:
            market_group = "米国株投信"
        elif sector_text:
            market_group = sector_text
        elif asset_class_text:
            market_group = asset_class_text
        else:
            market_group = "未分類"

        fx_driver_currency = "USD" if fund_fx_estimated else (currency_text or "JPY")

        record = {
            "start_date": start_date,
            "end_date": end_date,
            **{column: meta_source[column] for column in ATTRIBUTION_ID_COLUMNS},
            "value_start": v0,
            "value_end": v1,
            "quantity_start": q0,
            "quantity_end": q1,
            "price_start": p0,
            "price_end": p1,
            "fx_start": f0,
            "fx_end": f1,
            "total_change": total_change,
            "flow_effect": effects["quantity"],
            "market_effect": effects["market"],
            "fx_effect": effects["fx"],
            "residual_effect": residual_effect,
            "fund_fx_estimated": fund_fx_estimated,
            "attribution_method": attribution_method,
            "market_group": market_group,
            "fx_driver_currency": fx_driver_currency,
        }
        record["market_fx_effect"] = record["market_effect"] + record["fx_effect"]
        record["investment_effect"] = (
            record["market_effect"] + record["fx_effect"] + record["residual_effect"]
        )
        records.append(record)

    return pd.DataFrame(records)


def analyze_attribution_period(
    df_v2, start_date=None, end_date=None, fund_reference=None
):
    """Aggregate daily attribution over consecutive V2 snapshots."""
    data = normalize_daily_log_v2(df_v2)
    empty_summary = {
        "available_dates": [],
        "actual_start": None,
        "actual_end": None,
        "total_change": 0.0,
        "flow_effect": 0.0,
        "market_effect": 0.0,
        "fx_effect": 0.0,
        "residual_effect": 0.0,
        "investment_effect": 0.0,
        "fund_fx_estimated": False,
    }
    if data.empty:
        return pd.DataFrame(), pd.DataFrame(), empty_summary

    dates = sorted(data["date"].unique())
    if start_date is not None:
        start_date = pd.Timestamp(start_date).date()
        dates = [d for d in dates if d >= start_date]
    if end_date is not None:
        end_date = pd.Timestamp(end_date).date()
        dates = [d for d in dates if d <= end_date]

    empty_summary["available_dates"] = dates
    if len(dates) < 2:
        return pd.DataFrame(), pd.DataFrame(), empty_summary

    pieces = []
    for previous_date, current_date in zip(dates, dates[1:]):
        before = data[data["date"].eq(previous_date)]
        after = data[data["date"].eq(current_date)]
        pieces.append(
            attribution_between_snapshots(
                before, after, previous_date, current_date, fund_reference
            )
        )

    detail = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    daily = (
        detail.groupby("end_date", as_index=False)[
            ["total_change", "flow_effect", "market_effect", "fx_effect", "residual_effect", "investment_effect"]
        ]
        .sum()
        .rename(columns={"end_date": "date"})
    )

    summary = {
        "available_dates": dates,
        "actual_start": dates[0],
        "actual_end": dates[-1],
        "total_change": float(detail["total_change"].sum()),
        "flow_effect": float(detail["flow_effect"].sum()),
        "market_effect": float(detail["market_effect"].sum()),
        "fx_effect": float(detail["fx_effect"].sum()),
        "residual_effect": float(detail["residual_effect"].sum()),
        "investment_effect": float(detail["investment_effect"].sum()),
        "fund_fx_estimated": bool(detail["fund_fx_estimated"].any()),
    }
    return detail, daily, summary


def summarize_attribution(detail, group_by):
    """Aggregate attribution detail by one or more descriptive columns."""
    if detail.empty:
        return pd.DataFrame()

    columns = [group_by] if isinstance(group_by, str) else list(group_by)
    result = (
        detail.groupby(columns, dropna=False, as_index=False)[
            ["value_start", "value_end", "total_change", "flow_effect",
             "market_effect", "fx_effect", "residual_effect",
             "market_fx_effect", "investment_effect"]
        ]
        .sum()
    )
    return result.sort_values("investment_effect", ascending=False).reset_index(drop=True)


HISTORICAL_RECONSTRUCTION_START = pd.Timestamp("2026-04-12").date()
HISTORICAL_RECONSTRUCTION_END = pd.Timestamp("2026-10-13").date()


def _historical_currency(ticker, display_name):
    ticker = str(ticker or "").strip().upper()
    display = str(display_name or "").strip().upper()
    if ticker == "CASH":
        return display if display in {"JPY", "USD", "VND"} else "JPY"
    if ticker.endswith(".T") or ticker.endswith("-JPY"):
        return "JPY"
    return "USD"


def _split_normalized_close(history):
    """Raw close normalized for stock splits but not dividends."""
    if history is None or history.empty or "Close" not in history.columns:
        return pd.Series(dtype=float)
    close = pd.to_numeric(history["Close"], errors="coerce")
    splits = pd.to_numeric(
        history.get("Stock Splits", pd.Series(0.0, index=history.index)),
        errors="coerce",
    ).fillna(0.0)
    split_multiplier = splits.where((splits > 0) & (splits != 1), 1.0)
    # A split-date close is already post-split, so only later splits adjust it.
    future_splits = split_multiplier.iloc[::-1].cumprod().iloc[::-1]
    future_splits = future_splits.shift(-1, fill_value=1.0)
    normalized = close / future_splits
    normalized = normalized.dropna()
    normalized.index = pd.to_datetime(normalized.index)
    return normalized


def _bulk_history_frame(downloaded, ticker, ticker_count):
    if downloaded is None or downloaded.empty:
        return pd.DataFrame()
    if isinstance(downloaded.columns, pd.MultiIndex):
        level0 = downloaded.columns.get_level_values(0)
        if ticker in level0:
            return downloaded[ticker].copy()
        # Some yfinance versions return price field first and ticker second.
        level1 = downloaded.columns.get_level_values(1)
        if ticker in level1:
            return downloaded.xs(ticker, axis=1, level=1).copy()
        return pd.DataFrame()
    if ticker_count == 1:
        return downloaded.copy()
    return pd.DataFrame()


def build_historical_reference_data(
    df_log, start_date=HISTORICAL_RECONSTRUCTION_START,
    end_date=HISTORICAL_RECONSTRUCTION_END, min_rows=50,
):
    """Fetch price/FX references for high-confidence legacy Daily_Log dates."""
    log = df_log.copy()
    if log.empty:
        return {}, []
    log["date"] = pd.to_datetime(log["date"], errors="coerce").dt.date
    log["ticker"] = log["ticker"].fillna("").astype(str).str.strip().str.upper()
    log = log[log["date"].notna()].copy()
    start_date = pd.Timestamp(start_date).date()
    end_date = pd.Timestamp(end_date).date()
    log = log[log["date"].between(start_date, end_date)].copy()
    if log.empty:
        return {}, []

    row_counts = log.groupby("date").size()
    valid_dates = sorted(row_counts[row_counts >= int(min_rows)].index.tolist())
    if len(valid_dates) < 2:
        return {}, []

    excluded_labels = {"USSTOCK", "JPSTOCK", "EESTOCK", "CRYPTO"}
    tickers = sorted({
        ticker for ticker in log["ticker"].unique().tolist()
        if ticker and ticker != "CASH" and ticker not in excluded_labels
    })
    warnings = []
    references = {"prices": {}, "fx": {}, "valid_dates": valid_dates}

    fetch_start = pd.Timestamp(valid_dates[0]) - pd.Timedelta(days=10)
    fetch_end = pd.Timestamp(valid_dates[-1]) + pd.Timedelta(days=2)
    if tickers:
        try:
            downloaded = yf.download(
                tickers, start=fetch_start.date(), end=fetch_end.date(),
                actions=True, auto_adjust=False, group_by="ticker",
                progress=False, threads=True,
            )
        except Exception as error:
            downloaded = None
            warnings.append(f"過去価格一括取得失敗: {type(error).__name__}")

        for ticker in tickers:
            frame = _bulk_history_frame(downloaded, ticker, len(tickers))
            series = _split_normalized_close(frame)
            if series.empty:
                warnings.append(f"{ticker}: 過去価格なし")
                continue
            for log_date in valid_dates:
                candidates = series[series.index.date < log_date]
                if not candidates.empty:
                    references["prices"][(ticker, log_date)] = float(candidates.iloc[-1])

    fx_symbols = {"USD": "JPY=X", "VND": "VNDJPY=X"}
    try:
        fx_download = yf.download(
            list(fx_symbols.values()), start=fetch_start.date(), end=fetch_end.date(),
            actions=False, auto_adjust=False, group_by="ticker",
            progress=False, threads=True,
        )
    except Exception as error:
        fx_download = None
        warnings.append(f"過去為替一括取得失敗: {type(error).__name__}")

    for currency, symbol in fx_symbols.items():
        frame = _bulk_history_frame(fx_download, symbol, len(fx_symbols))
        if frame.empty or "Close" not in frame.columns:
            warnings.append(f"{currency}/JPY: 過去為替なし")
            continue
        series = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        series.index = pd.to_datetime(series.index)
        for log_date in valid_dates:
            candidates = series[series.index.date <= log_date]
            if not candidates.empty:
                references["fx"][(currency, log_date)] = float(candidates.iloc[-1])

    return references, list(dict.fromkeys(warnings))


def _legacy_market_fx_effects(value_start, market_ratio, fx_ratio):
    """Two-factor Shapley split of a held position value change."""
    v0 = float(value_start)
    market_delta = float(market_ratio) - 1.0
    fx_delta = float(fx_ratio) - 1.0
    market_effect = v0 * market_delta * (1.0 + 0.5 * fx_delta)
    fx_effect = v0 * fx_delta * (1.0 + 0.5 * market_delta)
    return market_effect, fx_effect


def reconstruct_historical_attribution(
    df_log, references, start_date=HISTORICAL_RECONSTRUCTION_START,
    end_date=HISTORICAL_RECONSTRUCTION_END, min_rows=50,
):
    """Reconstruct legacy attribution from saved JPY values plus price/FX history.

    Internal trades normally net out at portfolio level because stock purchases
    were recorded together with the corresponding cash reduction. The remaining
    net flow therefore approximates salary/bonus/dividend/other external cash.
    """
    log = df_log.copy()
    if log.empty:
        return pd.DataFrame(), {"valid_dates": [], "warnings": []}
    log["date"] = pd.to_datetime(log["date"], errors="coerce").dt.date
    log["ticker"] = log["ticker"].fillna("").astype(str).str.strip().str.upper()
    log["display_name"] = log["display_name"].fillna("").astype(str).str.strip()
    log["asset_class"] = log["asset_class"].fillna("").astype(str).str.strip()
    log["sector"] = log["sector"].fillna("").astype(str).str.strip()
    log["value_jpy"] = pd.to_numeric(
        log["value_jpy"].astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)
    start_date = pd.Timestamp(start_date).date()
    end_date = pd.Timestamp(end_date).date()
    log = log[log["date"].between(start_date, end_date)].copy()

    row_counts = log.groupby("date").size()
    valid_dates = sorted(row_counts[row_counts >= int(min_rows)].index.tolist())
    if references and references.get("valid_dates"):
        valid_reference_dates = set(references["valid_dates"])
        valid_dates = [d for d in valid_dates if d in valid_reference_dates]
    if len(valid_dates) < 2:
        return pd.DataFrame(), {"valid_dates": valid_dates, "warnings": []}

    grouped = (
        log[log["date"].isin(valid_dates)]
        .groupby(
            ["date", "ticker", "display_name", "asset_class", "sector"],
            dropna=False, as_index=False,
        )["value_jpy"].sum()
    )

    pieces = []
    warnings = []
    for d0, d1 in zip(valid_dates, valid_dates[1:]):
        before = grouped[grouped["date"].eq(d0)]
        after = grouped[grouped["date"].eq(d1)]
        key_cols = ["ticker", "display_name", "asset_class", "sector"]
        before_map = {tuple(row[col] for col in key_cols): row for _, row in before.iterrows()}
        after_map = {tuple(row[col] for col in key_cols): row for _, row in after.iterrows()}
        for key in sorted(set(before_map) | set(after_map), key=lambda x: tuple(map(str, x))):
            row0 = before_map.get(key)
            row1 = after_map.get(key)
            meta = row1 if row1 is not None else row0
            ticker = str(meta["ticker"]).upper()
            display = str(meta["display_name"])
            asset_class = str(meta["asset_class"])
            sector = str(meta["sector"])
            v0 = float(row0["value_jpy"]) if row0 is not None else 0.0
            v1 = float(row1["value_jpy"]) if row1 is not None else 0.0
            currency = _historical_currency(ticker, display)
            market_effect = 0.0
            fx_effect = 0.0
            flow_effect = 0.0
            residual_effect = 0.0
            method = "historical_reconstructed"

            if row0 is None:
                flow_effect = v1
            elif row1 is None:
                flow_effect = -v0
            else:
                if ticker == "CASH":
                    market_ratio = 1.0
                    market_known = True
                else:
                    p0 = references.get("prices", {}).get((ticker, d0))
                    p1 = references.get("prices", {}).get((ticker, d1))
                    market_known = p0 is not None and p1 is not None and p0 > 0
                    market_ratio = (p1 / p0) if market_known else 1.0

                if currency == "JPY":
                    fx_ratio = 1.0
                    fx_known = True
                else:
                    f0 = references.get("fx", {}).get((currency, d0))
                    f1 = references.get("fx", {}).get((currency, d1))
                    fx_known = f0 is not None and f1 is not None and f0 > 0
                    fx_ratio = (f1 / f0) if fx_known else 1.0

                if market_known and fx_known:
                    market_effect, fx_effect = _legacy_market_fx_effects(
                        v0, market_ratio, fx_ratio
                    )
                    flow_effect = v1 - v0 - market_effect - fx_effect
                else:
                    method = "historical_reconstructed_partial"
                    if fx_known:
                        fx_effect = v0 * (fx_ratio - 1.0)
                    residual_effect = v1 - v0 - fx_effect
                    warnings.append(f"{d0}→{d1} {ticker}: 市場参照不足")

            market_group = (
                "現金" if ticker == "CASH" else sector or asset_class or "未分類"
            )
            record = {
                "start_date": d0, "end_date": d1,
                "owner": "", "account_type": "", "ticker": ticker,
                "display_name": display, "asset_class": asset_class,
                "sector": sector, "currency": currency, "instrument_type": "",
                "fund_code": "", "product_name": "", "price_unit": 1.0,
                "purpose": "", "role": "",
                "value_start": v0, "value_end": v1, "total_change": v1 - v0,
                "flow_effect": flow_effect, "market_effect": market_effect,
                "fx_effect": fx_effect, "residual_effect": residual_effect,
                "fund_fx_estimated": False, "attribution_method": method,
                "market_group": market_group,
                "fx_driver_currency": currency,
            }
            record["market_fx_effect"] = market_effect + fx_effect
            record["investment_effect"] = market_effect + fx_effect + residual_effect
            pieces.append(record)

    detail = pd.DataFrame(pieces)
    return detail, {
        "valid_dates": valid_dates,
        "warnings": list(dict.fromkeys(warnings)),
        "start": valid_dates[0] if valid_dates else None,
        "end": valid_dates[-1] if valid_dates else None,
        "method": "historical_reconstructed",
    }

def _shapley_dividend_capacity_effects(
    q0, d0, f0, q1, d1, f1, price_unit
):
    """Exact symmetric decomposition of annual dividend capacity.

    Factors are quantity, per-unit annual dividend run-rate, and FX.
    Interaction terms are shared symmetrically so the three effects add exactly
    to the change in annual JPY dividend capacity.
    """
    from itertools import permutations

    old = {"quantity": float(q0), "dividend": float(d0), "fx": float(f0)}
    new = {"quantity": float(q1), "dividend": float(d1), "fx": float(f1)}
    effects = {"quantity": 0.0, "dividend": 0.0, "fx": 0.0}
    unit = float(price_unit) if price_unit else 1.0

    def value(state):
        return state["quantity"] / unit * state["dividend"] * state["fx"]

    orders = list(permutations(("quantity", "dividend", "fx")))
    for order in orders:
        state = old.copy()
        before = value(state)
        for factor in order:
            state[factor] = new[factor]
            after = value(state)
            effects[factor] += after - before
            before = after

    divisor = float(len(orders))
    return {key: value_ / divisor for key, value_ in effects.items()}


def analyze_dividend_capacity_period(
    df_v2, start_date=None, end_date=None, after_tax=True
):
    """Explain changes in annual dividend capacity between two V2 snapshots.

    Endpoint annualized forecast is decomposed into quantity, per-unit dividend
    run-rate, and FX effects. After-tax mode aligns with the take-home target.
    """
    data = normalize_daily_log_v2(df_v2)
    capacity_column = (
        "after_tax_div_forecast_jpy"
        if after_tax else "annual_div_forecast_jpy"
    )
    empty_summary = {
        "available_dates": [], "actual_start": None, "actual_end": None,
        "total_start": 0.0, "total_end": 0.0, "total_change": 0.0,
        "quantity_effect": 0.0, "dividend_effect": 0.0, "fx_effect": 0.0,
        "after_tax": bool(after_tax),
    }
    if data.empty:
        return pd.DataFrame(), empty_summary

    completeness = (
        data.groupby("date")[capacity_column]
        .apply(lambda series: series.notna().all())
    )
    dates = sorted(completeness[completeness].index.tolist())
    if start_date is not None:
        start_date = pd.Timestamp(start_date).date()
        dates = [d for d in dates if d >= start_date]
    if end_date is not None:
        end_date = pd.Timestamp(end_date).date()
        dates = [d for d in dates if d <= end_date]
    empty_summary["available_dates"] = dates
    if len(dates) < 2:
        return pd.DataFrame(), empty_summary

    actual_start = dates[0]
    actual_end = dates[-1]
    before = data[data["date"].eq(actual_start)].copy()
    after = data[data["date"].eq(actual_end)].copy()

    def snapshot(frame):
        records = []
        for _, row in frame.iterrows():
            quantity = float(row.get("quantity", 0) or 0)
            fx = float(row.get("fx_to_jpy", 1) or 1)
            unit = float(row.get("price_unit", 1) or 1)
            capacity = pd.to_numeric(
                pd.Series([row.get(capacity_column)]), errors="coerce"
            ).iloc[0]
            if pd.isna(capacity):
                continue
            if quantity > 0 and fx > 0:
                effective_per_unit = float(capacity) * unit / quantity / fx
            else:
                effective_per_unit = 0.0
            records.append({
                **{column: row.get(column, "") for column in ATTRIBUTION_ID_COLUMNS},
                "quantity": quantity, "fx_to_jpy": fx, "price_unit": unit,
                "capacity_jpy": float(capacity),
                "dividend_per_unit_effective": effective_per_unit,
            })
        if not records:
            return pd.DataFrame()
        frame_out = pd.DataFrame(records)
        return (
            frame_out.groupby(ATTRIBUTION_ID_COLUMNS, dropna=False, as_index=False)
            .agg(
                quantity=("quantity", "sum"),
                fx_to_jpy=("fx_to_jpy", "first"),
                price_unit=("price_unit", "first"),
                capacity_jpy=("capacity_jpy", "sum"),
                dividend_per_unit_effective=("dividend_per_unit_effective", "first"),
            )
        )

    start_snapshot = snapshot(before)
    end_snapshot = snapshot(after)
    start_map = {}
    end_map = {}

    def key_for(row):
        return tuple(row[column] for column in ATTRIBUTION_ID_COLUMNS)

    for _, row in start_snapshot.iterrows():
        start_map[key_for(row)] = row
    for _, row in end_snapshot.iterrows():
        end_map[key_for(row)] = row

    records = []
    all_keys = sorted(set(start_map) | set(end_map), key=lambda value: tuple(map(str, value)))
    for key in all_keys:
        row0 = start_map.get(key)
        row1 = end_map.get(key)
        meta = row1 if row1 is not None else row0
        unit = float(meta["price_unit"] or 1.0)
        if row0 is None:
            start_capacity = 0.0
            end_capacity = float(row1["capacity_jpy"])
            effects = {"quantity": end_capacity, "dividend": 0.0, "fx": 0.0}
        elif row1 is None:
            start_capacity = float(row0["capacity_jpy"])
            end_capacity = 0.0
            effects = {"quantity": -start_capacity, "dividend": 0.0, "fx": 0.0}
        else:
            start_capacity = float(row0["capacity_jpy"])
            end_capacity = float(row1["capacity_jpy"])
            effects = _shapley_dividend_capacity_effects(
                row0["quantity"], row0["dividend_per_unit_effective"], row0["fx_to_jpy"],
                row1["quantity"], row1["dividend_per_unit_effective"], row1["fx_to_jpy"],
                unit,
            )
            rounding_residual = end_capacity - start_capacity - sum(effects.values())
            effects["dividend"] += rounding_residual
        records.append({
            "start_date": actual_start, "end_date": actual_end,
            **{column: meta[column] for column in ATTRIBUTION_ID_COLUMNS},
            "capacity_start": start_capacity, "capacity_end": end_capacity,
            "total_change": end_capacity - start_capacity,
            "quantity_effect": effects["quantity"],
            "dividend_effect": effects["dividend"],
            "fx_effect": effects["fx"],
        })

    detail = pd.DataFrame(records)
    summary = {
        "available_dates": dates, "actual_start": actual_start, "actual_end": actual_end,
        "total_start": float(detail["capacity_start"].sum()),
        "total_end": float(detail["capacity_end"].sum()),
        "total_change": float(detail["total_change"].sum()),
        "quantity_effect": float(detail["quantity_effect"].sum()),
        "dividend_effect": float(detail["dividend_effect"].sum()),
        "fx_effect": float(detail["fx_effect"].sum()),
        "after_tax": bool(after_tax),
    }
    return detail, summary

def build_income_event_data(df_v2):
    """Fetch actual dividend/distribution events for securities in V2.

    Stock/ETF events come from Yahoo Finance corporate actions. Japanese mutual
    funds use their actual fund distribution histories; ETF proxy dividends are
    never substituted for fund distributions.

    Returned amounts are per share (stocks/ETFs) or per 10,000 units (funds),
    in the security's payout currency. The result is an attribution aid, not a
    brokerage cash-receipt ledger.
    """
    from mutual_funds import get_mutual_fund_distribution_history

    data = normalize_daily_log_v2(df_v2)
    columns = [
        "ticker", "event_date", "amount_per_unit", "currency",
        "price_unit", "source",
    ]
    if data.empty:
        return pd.DataFrame(columns=columns), []

    dates = sorted(data["date"].unique())
    if len(dates) < 2:
        return pd.DataFrame(columns=columns), []

    start_date = dates[0]
    end_date = dates[-1]
    records = []
    warnings = []

    direct = data[
        ~data["instrument_type"].eq("mutual_fund")
        & ~data["ticker"].str.upper().eq("CASH")
        & data["ticker"].ne("")
    ].copy()
    direct_tickers = sorted(direct["ticker"].unique().tolist())

    if direct_tickers:
        try:
            downloaded = yf.download(
                direct_tickers,
                start=(pd.Timestamp(start_date) - pd.Timedelta(days=3)).date(),
                end=(pd.Timestamp(end_date) + pd.Timedelta(days=2)).date(),
                actions=True,
                auto_adjust=False,
                group_by="ticker",
                progress=False,
            )
        except Exception as error:
            downloaded = None
            warnings.append(
                f"株式配当履歴の一括取得失敗: {type(error).__name__}"
            )

        currency_by_ticker = (
            direct.groupby("ticker")["currency"].first().to_dict()
            if not direct.empty else {}
        )

        for ticker in direct_tickers:
            try:
                if downloaded is None:
                    raise ValueError("bulk dividend data unavailable")

                if isinstance(downloaded.columns, pd.MultiIndex):
                    if ticker not in downloaded.columns.get_level_values(0):
                        raise ValueError("ticker missing from bulk dividend data")
                    history = downloaded[ticker].copy()
                elif len(direct_tickers) == 1:
                    history = downloaded.copy()
                else:
                    raise ValueError("unexpected dividend data shape")

                if "Dividends" not in history.columns:
                    continue

                dividends = pd.to_numeric(
                    history["Dividends"], errors="coerce"
                ).fillna(0)
                dividends = dividends[dividends > 0]

                if not dividends.empty and "Stock Splits" in history.columns:
                    splits = pd.to_numeric(
                        history["Stock Splits"], errors="coerce"
                    ).fillna(0)
                    if ((splits > 0) & (splits != 1)).any():
                        event_history = yf.Ticker(ticker).history(
                            start=(pd.Timestamp(start_date) - pd.Timedelta(days=30)).date(),
                            end=(pd.Timestamp(end_date) + pd.Timedelta(days=2)).date(),
                            auto_adjust=False,
                            actions=True,
                        )
                        if not event_history.empty:
                            normalized = _dividends_on_current_share_basis(event_history)
                            by_date = {idx.date(): value for idx, value in normalized.items()}
                            dividends = pd.Series(
                                [
                                    by_date.get(pd.Timestamp(idx).date(), value)
                                    for idx, value in dividends.items()
                                ],
                                index=dividends.index,
                                dtype=float,
                            )

                for event_index, amount in dividends.items():
                    event_date = pd.Timestamp(event_index).date()
                    if not (start_date <= event_date < end_date):
                        continue
                    records.append({
                        "ticker": ticker,
                        "event_date": event_date,
                        "amount_per_unit": float(amount),
                        "currency": str(currency_by_ticker.get(ticker, "") or ""),
                        "price_unit": 1.0,
                        "source": "yfinance",
                    })
            except Exception as error:
                warnings.append(
                    f"{ticker} 配当履歴取得失敗: {type(error).__name__}"
                )

    fund_rows = data[
        data["instrument_type"].eq("mutual_fund")
        & data["fund_code"].ne("")
    ][["ticker", "fund_code"]].drop_duplicates()

    for _, fund_row in fund_rows.iterrows():
        ticker = str(fund_row["ticker"])
        code = str(fund_row["fund_code"])
        try:
            distributions, source, source_warnings = (
                get_mutual_fund_distribution_history(code)
            )
            warnings.extend(
                [f"{ticker}: {warning}" for warning in source_warnings]
            )
            for event_index, amount in distributions.items():
                event_date = pd.Timestamp(event_index).date()
                if not (start_date <= event_date < end_date):
                    continue
                amount = float(amount)
                if amount <= 0:
                    continue
                records.append({
                    "ticker": ticker,
                    "event_date": event_date,
                    "amount_per_unit": amount,
                    "currency": "JPY",
                    "price_unit": 10000.0,
                    "source": source,
                })
        except Exception as error:
            warnings.append(
                f"{ticker} 分配金履歴取得失敗: {type(error).__name__}"
            )

    events = pd.DataFrame(records, columns=columns)
    if not events.empty:
        events["event_date"] = pd.to_datetime(events["event_date"]).dt.date
        events = events.sort_values(
            ["event_date", "ticker"]
        ).reset_index(drop=True)

    return events, list(dict.fromkeys(warnings))


def attach_income_effects(detail, income_events):
    """Attach estimated gross/after-tax cash-income effects to attribution rows.

    Events are assigned to intervals [start_date, end_date). Quantity at the
    start snapshot is used, because entitlement is determined before/at the
    corporate-action date. FX uses the midpoint of the two daily snapshot FX
    rates when payout currency is foreign.

    This estimates investment income; it does not assert the cash was actually
    credited to a tracked cash account on that date.
    """
    result = detail.copy()
    for column in [
        "income_effect_gross", "income_effect_after_tax",
        "income_event_count",
    ]:
        result[column] = 0.0

    if result.empty or income_events is None or income_events.empty:
        result["total_return_effect_gross"] = result.get(
            "investment_effect", pd.Series(index=result.index, dtype=float)
        )
        result["total_return_effect_after_tax"] = result.get(
            "investment_effect", pd.Series(index=result.index, dtype=float)
        )
        return result

    events = income_events.copy()
    events["event_date"] = pd.to_datetime(
        events["event_date"], errors="coerce"
    ).dt.date
    events["amount_per_unit"] = pd.to_numeric(
        events["amount_per_unit"], errors="coerce"
    ).fillna(0.0)

    from mutual_funds import FUND_SPECS

    for index, row in result.iterrows():
        ticker = str(row.get("ticker", "") or "")
        if not ticker or ticker.upper() == "CASH":
            continue

        start_date = pd.Timestamp(row["start_date"]).date()
        end_date = pd.Timestamp(row["end_date"]).date()
        matched = events[
            events["ticker"].eq(ticker)
            & events["event_date"].ge(start_date)
            & events["event_date"].lt(end_date)
        ]
        if matched.empty:
            continue

        q0 = float(row.get("quantity_start", 0) or 0)
        if q0 <= 0:
            continue

        price_unit = float(row.get("price_unit", 1) or 1)
        currency = str(row.get("currency", "") or "").upper()
        if currency == "JPY":
            fx = 1.0
        else:
            fx0 = float(row.get("fx_start", 1) or 1)
            fx1 = float(row.get("fx_end", fx0) or fx0)
            fx = (fx0 + fx1) / 2.0

        amount_per_unit = float(matched["amount_per_unit"].sum())
        gross_jpy = q0 / price_unit * amount_per_unit * fx

        tax_row = {
            "annual_div_jpy": gross_jpy,
            "ticker": ticker,
            "currency": currency,
            "account_type": row.get("account_type", ""),
            "instrument_type": row.get("instrument_type", ""),
            "foreign_tax_adjustment": False,
            "underlying_foreign_tax_rate": 0.0,
        }
        fund_code = str(row.get("fund_code", "") or "")
        if fund_code in FUND_SPECS:
            spec = FUND_SPECS[fund_code]
            tax_row["foreign_tax_adjustment"] = spec.get(
                "foreign_tax_adjustment", False
            )
            tax_row["underlying_foreign_tax_rate"] = spec.get(
                "underlying_foreign_tax_rate", 0.0
            )

        after_tax_jpy = calc_after_tax_dividend(tax_row)

        result.at[index, "income_effect_gross"] = gross_jpy
        result.at[index, "income_effect_after_tax"] = after_tax_jpy
        result.at[index, "income_event_count"] = int(len(matched))

    result["total_return_effect_gross"] = (
        result["investment_effect"] + result["income_effect_gross"]
    )
    result["total_return_effect_after_tax"] = (
        result["investment_effect"] + result["income_effect_after_tax"]
    )
    return result


def calculate_estimated_total_return_twr(
    detail, income_column="income_effect_gross"
):
    """Chain-link daily returns including dividend/distribution income."""
    if detail is None or detail.empty:
        return pd.DataFrame(), {
            "valid": False,
            "twr": None,
            "periods": 0,
            "income_effect": 0.0,
            "return_effect": 0.0,
            "invalid_periods": 0,
            "method": "daily_modified_dietz_midpoint_total_return",
        }
    if income_column not in detail.columns:
        raise ValueError(f"missing income column: {income_column}")

    work = detail.copy()
    work["return_effect"] = (
        pd.to_numeric(work["investment_effect"], errors="coerce").fillna(0.0)
        + pd.to_numeric(work[income_column], errors="coerce").fillna(0.0)
    )

    daily = (
        work.groupby(["start_date", "end_date"], as_index=False)[
            [
                "value_start", "value_end", "total_change",
                "flow_effect", "return_effect", income_column,
            ]
        ]
        .sum()
        .sort_values("end_date")
        .reset_index(drop=True)
    )
    daily["capital_base"] = daily["value_start"] + 0.5 * daily["flow_effect"]
    daily["period_return"] = daily["return_effect"] / daily["capital_base"]

    invalid = (
        ~pd.to_numeric(daily["capital_base"], errors="coerce").gt(0)
        | ~pd.to_numeric(daily["period_return"], errors="coerce").notna()
        | daily["period_return"].le(-1.0)
    )
    daily["valid_return"] = ~invalid
    daily.loc[invalid, "period_return"] = float("nan")

    summary = {
        "valid": not bool(invalid.any()),
        "twr": None,
        "periods": int(len(daily)),
        "income_effect": float(daily[income_column].sum()),
        "return_effect": float(daily["return_effect"].sum()),
        "invalid_periods": int(invalid.sum()),
        "method": "daily_modified_dietz_midpoint_total_return",
    }
    if invalid.any():
        return daily, summary

    daily["cumulative_twr"] = (1.0 + daily["period_return"]).cumprod() - 1.0
    summary["twr"] = float(daily.iloc[-1]["cumulative_twr"])
    return daily, summary


def calculate_estimated_twr(detail):
    """Chain daily flow-adjusted portfolio returns from attribution detail.

    Exact TWR needs a portfolio valuation immediately before every external
    cash flow. Daily_Log_V2 has daily snapshots but no intraday flow timestamps,
    so this uses a daily Modified-Dietz-style midpoint assumption:

        daily return = investment effect / (start value + 0.5 * flow effect)

    Daily returns are chain-linked. Internal buys/sells normally offset at the
    total-portfolio level; remaining quantity/flow effect acts as the cash-flow
    adjustment. This is therefore labelled estimated TWR in the UI.
    """
    empty_summary = {
        "valid": False,
        "twr": None,
        "periods": 0,
        "start_value": 0.0,
        "end_value": 0.0,
        "total_change": 0.0,
        "flow_effect": 0.0,
        "investment_effect": 0.0,
        "simple_asset_change": None,
        "invalid_periods": 0,
        "method": "daily_modified_dietz_midpoint",
    }
    if detail is None or detail.empty:
        return pd.DataFrame(), empty_summary

    required = {
        "start_date", "end_date", "value_start", "value_end",
        "total_change", "flow_effect", "investment_effect",
    }
    missing = sorted(required - set(detail.columns))
    if missing:
        raise ValueError(
            "attribution detail missing TWR columns: " + ", ".join(missing)
        )

    daily = (
        detail.groupby(["start_date", "end_date"], as_index=False)[
            [
                "value_start", "value_end", "total_change",
                "flow_effect", "investment_effect",
            ]
        ]
        .sum()
        .sort_values("end_date")
        .reset_index(drop=True)
    )

    daily["capital_base"] = (
        daily["value_start"] + 0.5 * daily["flow_effect"]
    )
    daily["period_return"] = daily["investment_effect"] / daily["capital_base"]

    invalid = (
        ~pd.to_numeric(daily["capital_base"], errors="coerce").gt(0)
        | ~pd.to_numeric(daily["period_return"], errors="coerce").notna()
        | daily["period_return"].le(-1.0)
    )
    daily["valid_return"] = ~invalid
    daily.loc[invalid, "period_return"] = float("nan")

    if invalid.any():
        summary = empty_summary.copy()
        summary.update({
            "periods": int(len(daily)),
            "start_value": float(daily.iloc[0]["value_start"]),
            "end_value": float(daily.iloc[-1]["value_end"]),
            "total_change": float(daily["total_change"].sum()),
            "flow_effect": float(daily["flow_effect"].sum()),
            "investment_effect": float(daily["investment_effect"].sum()),
            "invalid_periods": int(invalid.sum()),
        })
        return daily, summary

    daily["cumulative_twr"] = (1.0 + daily["period_return"]).cumprod() - 1.0

    start_value = float(daily.iloc[0]["value_start"])
    end_value = float(daily.iloc[-1]["value_end"])
    simple_asset_change = (
        end_value / start_value - 1.0
        if start_value > 0 else None
    )

    summary = {
        "valid": True,
        "twr": float(daily.iloc[-1]["cumulative_twr"]),
        "periods": int(len(daily)),
        "start_value": start_value,
        "end_value": end_value,
        "total_change": float(daily["total_change"].sum()),
        "flow_effect": float(daily["flow_effect"].sum()),
        "investment_effect": float(daily["investment_effect"].sum()),
        "simple_asset_change": simple_asset_change,
        "invalid_periods": 0,
        "method": "daily_modified_dietz_midpoint",
    }
    return daily, summary


def daily_log_migration_names(df_log, start_date, end_date):
    """Find ETF-proxy -> native-fund changes across successive logged dates."""
    log = df_log.copy()
    log["date"] = pd.to_datetime(log["date"]).dt.date
    start_date = pd.Timestamp(start_date).date()
    end_date = pd.Timestamp(end_date).date()
    dates = sorted(log["date"].unique())
    affected = set()
    for previous, current in zip(dates, dates[1:]):
        if not start_date < current <= end_date:
            continue
        before = log.loc[log["date"].eq(previous)]
        after = log.loc[log["date"].eq(current)]
        for proxy, name in [("VOO", "SP500"), ("SCHD", "SCHD")]:
            old = before.loc[before["display_name"].eq(name), "ticker"].astype(str)
            new = after.loc[after["display_name"].eq(name), "ticker"].astype(str)
            if old.eq(proxy).any() and new.str.startswith("MF:").sum() > old.str.startswith("MF:").sum():
                affected.add(name)
    return sorted(affected)


def compare_portfolio(df_log, start_date, end_date):

    df_comp = df_log.copy()

    df_comp["value_jpy"] = pd.to_numeric(
        df_comp["value_jpy"],
        errors="coerce"
    ).fillna(0)

    df_comp["date"] = pd.to_datetime(df_comp["date"]).dt.date

    # 期間抽出
    d_start_raw = df_comp[df_comp["date"] == start_date].copy()
    d_end_raw = df_comp[df_comp["date"] == end_date].copy()

    # 集計
    def summarize_assets(df_target):
        return (
            df_target
            .groupby(
                ["asset_class", "sector", "display_name"],
                dropna=False
            )["value_jpy"]
            .sum()
            .reset_index()
        )

    d_start = summarize_assets(d_start_raw)
    d_end = summarize_assets(d_end_raw)

    # マージ
    d_merged = pd.merge(
        d_end,
        d_start,
        on=["asset_class", "sector", "display_name"],
        how="outer",
        suffixes=("_end", "_start")
    ).fillna(0)

    # 差分計算
    d_merged["diff_val"] = (
        d_merged["value_jpy_end"]
        - d_merged["value_jpy_start"]
    )

    d_merged["growth_pct"] = d_merged.apply(
        lambda r:
        (
            r["diff_val"]
            / r["value_jpy_start"]
            * 100
        )
        if r["value_jpy_start"] != 0
        else 0,
        axis=1
    )

    d_merged["growth_pct"] = (
        d_merged["growth_pct"]
        .fillna(0)
        .round(2)
    )

    migration_names = daily_log_migration_names(df_log, start_date, end_date)
    d_merged["valuation_method_change"] = d_merged["display_name"].isin(migration_names)
    d_merged.loc[d_merged["valuation_method_change"], "growth_pct"] = float("nan")
    ranking = d_merged.loc[~d_merged["valuation_method_change"]]

    # summary
    total_end = d_merged["value_jpy_end"].sum()
    total_start = d_merged["value_jpy_start"].sum()

    total_diff = total_end - total_start

    total_growth = (
        (total_diff / total_start * 100)
        if total_start != 0
        else 0
    )

# --- 個別銘柄ランキング生成 ---

# 値上がり率TOP10
    top_gainers = (
        ranking
        .sort_values("growth_pct", ascending=False)
        .head(10)
        [[
            "display_name",
            "growth_pct",
            "diff_val"
        ]]
    )

# 値下がり率TOP10
    top_losers = (
        ranking
        .sort_values("growth_pct", ascending=True)
        .head(10)
        [[
            "display_name",
            "growth_pct",
            "diff_val"
        ]]
    )

# 金額増加TOP10
    top_diff_up = (
        ranking
        .sort_values("diff_val", ascending=False)
        .head(10)
        [[
            "display_name",
            "growth_pct",
            "diff_val"
        ]]
    )

# 金額減少TOP10
    top_diff_down = (
        ranking
        .sort_values("diff_val", ascending=True)
        .head(10)
        [[
            "display_name",
            "growth_pct",
            "diff_val"
        ]]
    )

# --- セクター別ランキング ---
    d_merged_sector = d_merged.copy()

    d_merged_sector["sector"] = (
        d_merged_sector["sector"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    d_merged_sector = d_merged_sector[
        d_merged_sector["sector"] != ""
    ]
    sector_summary = (
        d_merged_sector
        .groupby("sector", dropna=False)
        .agg({
            "value_jpy_start": "sum",
            "value_jpy_end": "sum",
            "diff_val": "sum"
        })
        .reset_index()
    )

    sector_summary["growth_pct"] = sector_summary.apply(
        lambda r: (
            r["diff_val"] / r["value_jpy_start"] * 100
            if r["value_jpy_start"] != 0 else 0
        ),
        axis=1
    )

    sector_summary = sector_summary.sort_values(
        "growth_pct",
        ascending=False
    )

# --- アセットクラス別ランキング ---
    asset_summary = (
        d_merged
        .groupby("asset_class", dropna=False)
        .agg({
            "value_jpy_start": "sum",
            "value_jpy_end": "sum",
            "diff_val": "sum"
        })
        .reset_index()
    )

    asset_summary["growth_pct"] = asset_summary.apply(
        lambda r: (
            r["diff_val"] / r["value_jpy_start"] * 100
            if r["value_jpy_start"] != 0 else 0
        ),
        axis=1
    )

    asset_summary = asset_summary.sort_values(
        "growth_pct",
        ascending=False
    )

# --- 各種ランキングまとめ ---    
    summary = {
        "migration_names": migration_names,
        "total_start": total_start,
        "total_end": total_end,
        "total_diff": total_diff,
        "total_growth": total_growth,

        "top_gainers": top_gainers,
        "top_losers": top_losers,

        "top_diff_up": top_diff_up,
        "top_diff_down": top_diff_down,
        
        "sector_summary": sector_summary,
        "asset_summary": asset_summary
    }

    return d_merged, summary

def compare_latest_logs(df_log):

    import pandas as pd

    df_log = df_log.copy()

    df_log["value_jpy"] = pd.to_numeric(
        df_log["value_jpy"],
        errors="coerce"
    ).fillna(0)

    df_log["date"] = pd.to_datetime(df_log["date"]).dt.date

    date_list = sorted(df_log["date"].unique())

    if len(date_list) < 2:
        return None

    start_date = date_list[-2]
    end_date = date_list[-1]

    d_start = df_log[df_log["date"] == start_date]
    d_end = df_log[df_log["date"] == end_date]

    def summarize_assets(df_target):
        return (
            df_target.groupby(
                ["asset_class", "sector", "display_name"],
                dropna=False
            )["value_jpy"]
            .sum()
            .reset_index()
        )

    d_start = summarize_assets(d_start)
    d_end = summarize_assets(d_end)

    d_merged = pd.merge(
        d_end,
        d_start,
        on=["asset_class", "sector", "display_name"],
        how="outer",
        suffixes=("_end", "_start")
    ).fillna(0)

    d_merged["diff_val"] = (
        d_merged["value_jpy_end"]
        - d_merged["value_jpy_start"]
    )

    d_merged["growth_pct"] = d_merged.apply(
        lambda r:
        (
            r["diff_val"]
            / r["value_jpy_start"]
            * 100
        )
        if r["value_jpy_start"] != 0
        else 0,
        axis=1
    )

    total_start = d_merged["value_jpy_start"].sum()
    total_end = d_merged["value_jpy_end"].sum()

    total_diff = total_end - total_start

    total_growth = (
        total_diff / total_start * 100
        if total_start != 0
        else 0
    )

    migration_names = daily_log_migration_names(df_log, start_date, end_date)
    d_merged["valuation_method_change"] = d_merged["display_name"].isin(migration_names)
    d_merged.loc[d_merged["valuation_method_change"], "growth_pct"] = float("nan")
    ranking_df = d_merged[
        (d_merged["diff_val"].abs() > 1) & ~d_merged["valuation_method_change"]
    ].copy()
    
    gainers_df = ranking_df[
        ranking_df["growth_pct"] > 0
    ]

    losers_df = ranking_df[
        ranking_df["growth_pct"] < 0
    ]

    top_gainers = (
        gainers_df.sort_values(
            "growth_pct",
            ascending=False
        )
        .head(10)
    )

    top_losers = (
        losers_df.sort_values(
            "growth_pct",
            ascending=True
        )
        .head(10)
    )

    return {
        "migration_names": migration_names,
        "start_date": start_date,
        "end_date": end_date,
        "total_start": total_start,
        "total_end": total_end,
        "total_diff": total_diff,
        "total_growth": total_growth,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "merged": d_merged
    }

def load_daily_log_detail(creds_dict):

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, scope
    )

    client = gspread.authorize(creds)

    spreadsheet = client.open("portfolio_data")
    sheet = spreadsheet.worksheet("Daily_Log")

    data = sheet.get_all_records()
    df_log = pd.DataFrame(data)

    return df_log

#一旦get_priceを復活

def get_price(ticker, fallback_price):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d")

        if not hist.empty:
            return hist["Close"].iloc[-1], False
        else:
            return fallback_price, True

    except:
        return fallback_price, True

print("functions loaded")
