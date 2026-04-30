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
        return fx.history(period="1d")["Close"].iloc[-1]
    except:
        return default


import time

def get_price(ticker, cost_price, fallback_price=None):
    if ticker == "CASH":
        return 1, False

    max_retry = 3

    for attempt in range(max_retry):
        try:
            print(f"{ticker} 価格取得（{attempt+1}/{max_retry}回目）")

            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d")

            closes = hist["Close"].dropna()

            if not closes.empty:
                price = closes.iloc[-1]
                print(f"{ticker} 成功: {price}")
                return price, False

            print(f"{ticker} Closeが空")

        except Exception as e:
            print(f"{ticker} エラー: {e}")

        if attempt < max_retry - 1:
            time.sleep(5)

    print(f"{ticker} 全リトライ失敗")

    if fallback_price is not None:
        return fallback_price, True

    return cost_price, True

def get_dividend_data(ticker):

    if ticker in ["CASH", "VOO"]:
        return 0.0, False

    max_retry = 3

    for attempt in range(max_retry):
        try:
            stock = yf.Ticker(ticker)
            div_yield = stock.info.get('dividendYield', 0)

            if div_yield is None:
                div_yield = 0.0

            if div_yield > 0.2:
                div_yield = div_yield / 100

            return div_yield, False

        except Exception as e:
            print(f"{ticker} 配当エラー: {e}")

        if attempt < max_retry - 1:
            time.sleep(5)

    return 0.0, True

def get_performance(ticker):

    if ticker == "CASH":
        return (0.0, 0.0), False

    max_retry = 3

    for attempt in range(max_retry):
        try:
            stock = yf.Ticker(ticker)

            hist = stock.history(period="2d")

            if len(hist) >= 2:
                daily = (
                    (hist["Close"].iloc[-1] - hist["Close"].iloc[-2])
                    / hist["Close"].iloc[-2] * 100
                )
            else:
                daily = 0.0

            ytd = stock.info.get("ytdReturn", 0)
            ytd = ytd * 100 if ytd else 0.0

            return (daily, ytd), False

        except Exception as e:
            print(f"{ticker} パフォーマンスエラー: {e}")

        if attempt < max_retry - 1:
            time.sleep(5)

    return (0.0, 0.0), True

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
