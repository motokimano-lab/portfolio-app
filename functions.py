import pandas as pd
import yfinance as yf
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from zoneinfo import ZoneInfo
from dividend_tax import normalize_account_type

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


DAILY_LOG_V2_HEADERS = [
    "date", "snapshot_at", "owner", "account_type", "ticker", "display_name",
    "asset_class", "sector", "currency", "instrument_type", "fund_code",
    "product_name", "price_unit", "quantity", "price", "fx_to_jpy",
    "value_jpy", "cost_basis_jpy", "purpose", "role", "day_diff_pct",
    "fund_base_date", "proxy_ticker"
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
            _as_float(r, "price_unit", 1.0),
            _as_float(r, "quantity"),
            _as_float(r, "price"),
            _as_float(r, "fx_to_jpy", 1.0),
            _as_float(r, "value_jpy"),
            _as_float(r, "cost_basis_jpy"),
            _as_text(r, "purpose"),
            _as_text(r, "role"),
            _as_float(r, "day_diff_pct"),
            str(fund_record.get("base_date") or ""),
            str(spec.get("attribution_proxy") or ""),
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

    required = set(DAILY_LOG_V2_HEADERS)
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError("Daily_Log_V2 required columns missing: " + ", ".join(missing))

    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.date
    data = data[data["date"].notna()].copy()

    text_columns = [
        "owner", "account_type", "ticker", "display_name", "asset_class", "sector",
        "currency", "instrument_type", "fund_code", "product_name", "purpose", "role",
    ]
    for column in text_columns:
        data[column] = data[column].fillna("").astype(str).str.strip()

    numeric_columns = [
        "price_unit", "quantity", "price", "fx_to_jpy", "value_jpy",
        "cost_basis_jpy", "day_diff_pct",
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
        )
    )
    return aggregated


def _position_value(quantity, price, fx_to_jpy, price_unit):
    unit = float(price_unit) if price_unit else 1.0
    return float(quantity) / unit * float(price) * float(fx_to_jpy)


def _shapley_position_effects(q0, p0, f0, q1, p1, f1, price_unit):
    """Exact symmetric 3-factor decomposition for quantity, local price and FX.

    Interaction terms are shared fairly across the three factors, so the
    effects always add exactly to the JPY valuation change.
    """
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


def attribution_between_snapshots(start_df, end_df, start_date, end_date):
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

        effects = _shapley_position_effects(q0, p0, f0, q1, p1, f1, unit)
        total_change = v1 - v0
        residual = total_change - sum(effects.values())
        # Floating-point rounding only; assign residual to market so row totals
        # remain exact at currency precision.
        effects["market"] += residual

        record = {
            "start_date": start_date,
            "end_date": end_date,
            **{column: meta_source[column] for column in ATTRIBUTION_ID_COLUMNS},
            "value_start": v0,
            "value_end": v1,
            "total_change": total_change,
            "flow_effect": effects["quantity"],
            "market_effect": effects["market"],
            "fx_effect": effects["fx"],
        }
        record["market_fx_effect"] = record["market_effect"] + record["fx_effect"]
        records.append(record)

    return pd.DataFrame(records)


def analyze_attribution_period(df_v2, start_date=None, end_date=None):
    """Aggregate daily Shapley attribution over consecutive V2 snapshots.

    The result is suitable for 1D/1W/1M/3M/YTD/1Y/ALL/custom views.  It sums
    daily effects, so purchases and sales inside the period do not get mistaken
    for investment performance.
    """
    data = normalize_daily_log_v2(df_v2)
    empty_summary = {
        "available_dates": [],
        "actual_start": None,
        "actual_end": None,
        "total_change": 0.0,
        "flow_effect": 0.0,
        "market_effect": 0.0,
        "fx_effect": 0.0,
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
                before, after, previous_date, current_date
            )
        )

    detail = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    daily = (
        detail.groupby("end_date", as_index=False)[
            ["total_change", "flow_effect", "market_effect", "fx_effect"]
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
             "market_effect", "fx_effect", "market_fx_effect"]
        ]
        .sum()
    )
    return result.sort_values("market_fx_effect", ascending=False).reset_index(drop=True)


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
