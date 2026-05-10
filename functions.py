import pandas as pd
import yfinance as yf
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from zoneinfo import ZoneInfo

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
def get_assets_data_bulk(tickers, max_retry=5, wait_sec=5):
    if not tickers:
        return {}, {}, {}, []

    yf_tickers = list(set([
        str(t).strip().upper() for t in tickers 
        if t != "CASH" and pd.notna(t)
    ]))
    
    if not yf_tickers:
        return {}, {}, {}, []

    for attempt in range(max_retry):

        price_dict = {}
        div_yield_dict = {}
        perf_dict = {}
        errors = []

        try:
            data = yf.download(
                yf_tickers,
                period="1y",
                actions=True,
                group_by='ticker',
                progress=False
            )
        except Exception:
            errors = yf_tickers.copy()
            data = None

        for t in yf_tickers:
            try:
                # --- データ取得 ---
                if data is None:
                    errors.append(t)
                    continue

                if len(yf_tickers) == 1:
                    df_t = data
                else:
                    if t in data.columns.levels[0]:
                        df_t = data[t]
                    else:
                        errors.append(t)
                        continue

                df_t = df_t.dropna(subset=["Close"])

                # --- fallback ---
                if df_t.empty:
                    fallback = yf.Ticker(t).history(period="5d")
                    if fallback.empty:
                        errors.append(t)
                        continue

                    current_price = float(fallback["Close"].iloc[-1])
                    annual_div_total = 0

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

                    annual_div_total = (
                        df_t["Dividends"].sum()
                        if "Dividends" in df_t.columns else 0
                    )

                # --- 格納 ---
                for key in [t.upper(), t.lower()]:
                    price_dict[key] = current_price
                    div_yield_dict[key] = (
                        annual_div_total / current_price
                        if current_price > 0 else 0.0
                    )
                    if t in perf_dict:
                        perf_dict[key] = perf_dict[t]

            except Exception:
                errors.append(t)
                continue

        # --- 成功判定 ---
        if not errors and all(
            (v is not None and v > 0) for v in price_dict.values()
        ):
            return price_dict, div_yield_dict, perf_dict, errors

        print(f"[RETRY] attempt {attempt+1} failed. errors={errors}")

        time.sleep(wait_sec)

    # 最終的にダメでも返す
    return price_dict, div_yield_dict, perf_dict, errors
    
# --- DataFrame整形 ---
def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = df.copy()

    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["cost_price"] = pd.to_numeric(df["cost_price"], errors="coerce")

    df["value"] = df["price"] * df["quantity"]

    def convert_to_jpy(row):
        if row["currency"] == "USD":
            return row["value"] * usd_jpy
        elif row["currency"] == "VND":
            return row["value"] * vnd_jpy
        return row["value"]

    df["value_jpy"] = df.apply(convert_to_jpy, axis=1)

    df["sector_group"] = df["sector"].fillna("未分類")

    return df

def clean_tickers(tickers):
    cleaned = []
    for t in tickers:
        if isinstance(t, str) and len(t) > 0:
            if not t.isdigit():  # "1","2"除外
                cleaned.append(t)
    return list(set(cleaned))

def save_daily_log_detail(df, creds_dict):

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

    today = datetime.now(
        ZoneInfo("Asia/Tokyo")
    ).strftime("%Y-%m-%d")

    rows = []
    for _, r in df.iterrows():
        rows.append([
            today,
            str(r["ticker"]) if pd.notna(r["ticker"]) else "",
            str(r["display_name"]) if pd.notna(r["display_name"]) else "",
            str(r["asset_class"]) if pd.notna(r["asset_class"]) else "",
            str(r["sector"]) if pd.notna(r["sector"]) else "",
            float(r["value_jpy"]) if pd.notna(r["value_jpy"]) else 0
        ])

    existing_data = sheet.get_all_values()

    filtered_data = [
        row for row in existing_data
        if len(row) == 0 or row[0] != today
    ]

    sheet.clear()
    sheet.update(filtered_data + rows)

    return f"{len(rows)} rows saved (overwrite mode)"

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
        d_merged
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
        d_merged
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
        d_merged
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
        d_merged
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

    ranking_df = d_merged[
        d_merged["diff_val"].abs() > 1
    ].copy()

    top_gainers = (
        ranking_df.sort_values(
            "growth_pct",
            ascending=False
        )
        .head(10)
    )

    top_losers = (
        ranking_df.sort_values(
            "growth_pct",
            ascending=True
        )
        .head(10)
    )

    return {
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
