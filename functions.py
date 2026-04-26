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


@st.cache_data(ttl=3600)
def get_price(ticker, cost_price, fallback_price=None):
    if ticker == "CASH":
        return 1, False

    try:
        print(f"価格取得開始: {ticker}")

        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")

        print(f"{ticker} history:")
        print(hist.tail())

        if not hist.empty:
            price = hist["Close"].dropna().iloc[-1]
            print(f"{ticker} price取得成功: {price}")
            return price, False

        print(f"{ticker} history empty")

    except Exception as e:
        print(f"{ticker} エラー発生: {e}")

    if fallback_price is not None:
        print(f"{ticker} fallback_price使用: {fallback_price}")
        return fallback_price, True

    print(f"{ticker} cost_price使用: {cost_price}")
    return cost_price, True

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
    print(df[["ticker", "quantity", "price", "value", "value_jpy"]])
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
