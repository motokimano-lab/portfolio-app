import pandas as pd
import yfinance as yf
import gspread
import streamlit as st
import json
import os

from datetime import datetime
from zoneinfo import ZoneInfo
from oauth2client.service_account import ServiceAccountCredentials


def get_fx(symbol, default):
    try:
        fx = yf.Ticker(symbol)
        price = fx.history(period="1d")["Close"].iloc[-1]
        return price, False
    except:
        return default, True


import time

def get_prices_bulk(tickers):
    try:
        df = yf.download(
            tickers=tickers,
            period="1d",
            group_by="ticker",
            threads=True
        )

        price_dict = {}

        for t in tickers:
            try:
                price = df[t]["Close"].iloc[-1]
                price_dict[t] = price
            except:
                price_dict[t] = None

        return price_dict

    except Exception as e:
        print("bulk取得エラー:", e)
        return {t: None for t in tickers}

def clean_tickers(tickers):
    cleaned = []
    for t in tickers:
        if isinstance(t, str) and len(t) > 0:
            if not t.isdigit():  # "1","2"除外
                cleaned.append(t)
    return list(set(cleaned))

def get_dividends(tickers):
    dividend_dict = {}
    div_errors = []

    for ticker in tickers:

        if ticker in ["CASH", "VOO"]:
            dividend_dict[ticker] = 0.0
            continue

        max_retry = 3

        for attempt in range(max_retry):
            try:
                stock = yf.Ticker(ticker)
                div_yield = stock.info.get("dividendYield", 0)

                if div_yield is None:
                    div_yield = 0.0

                if div_yield > 0.2:
                    div_yield = div_yield / 100

                dividend_dict[ticker] = div_yield
                break

            except Exception as e:
                if attempt < max_retry - 1:
                    time.sleep(10)
                else:
                    dividend_dict[ticker] = 0.0
                    div_errors.append(ticker)

    return dividend_dict, div_errors
import time

def get_performance(tickers):
    performance_dict = {}
    perf_errors = []

    for ticker in tickers:

        if ticker == "CASH":
            performance_dict[ticker] = (0.0, 0.0)
            continue

        max_retry = 3

        for attempt in range(max_retry):
            try:
                stock = yf.Ticker(ticker)

                hist = stock.history(period="2d")

                if len(hist) >= 2:
                    daily_pct = (
                        (hist["Close"].iloc[-1] - hist["Close"].iloc[-2])
                        / hist["Close"].iloc[-2] * 100
                    )
                else:
                    daily_pct = 0.0

                ytd = stock.info.get("ytdReturn", 0)
                ytd_pct = ytd * 100 if ytd else 0.0

                performance_dict[ticker] = (daily_pct, ytd_pct)
                break

            except Exception as e:
                if attempt < max_retry - 1:
                    time.sleep(10)
                else:
                    performance_dict[ticker] = (0.0, 0.0)
                    perf_errors.append(ticker)

    return performance_dict, perf_errors


def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = df.copy()

    print("=== prepare_base_dataframe BEFORE ===")
    print(df[["ticker", "quantity", "price", "currency"]])

    # 数値変換
    numeric_cols = ["quantity", "price", "cost_price"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    print("=== AFTER to_numeric ===")
    print(df[["ticker", "quantity", "price", "currency"]])

    # 評価額
    df["value"] = df["price"] * df["quantity"]

    print("=== AFTER value calc ===")
    print(df[["ticker", "quantity", "price", "value"]])
    print("usd_jpy =", usd_jpy)
    print("vnd_jpy =", vnd_jpy)
    
    # 円換算
    def convert_to_jpy(row):
        if row["currency"] == "USD":
            return row["value"] * usd_jpy
        elif row["currency"] == "VND":
            return row["value"] * vnd_jpy
        return row["value"]

    df["value_jpy"] = df.apply(convert_to_jpy, axis=1)

    # セクター補完
    df["sector_group"] = df["sector"].apply(
        lambda x: x if pd.notna(x) and str(x).strip() != "" else "未分類"
    )

    return df

def save_daily_log_detail(df):

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    if "GCP_SERVICE_ACCOUNT" in os.environ:
        creds_dict = json.loads(
            os.environ["GCP_SERVICE_ACCOUNT"]
        )
    else:
        creds_dict = dict(
            st.secrets["gcp_service_account"]
        )

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
