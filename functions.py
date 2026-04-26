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
        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")

        if not hist.empty:
            return hist["Close"].dropna().iloc[-1], False
    except:
        pass

    if fallback_price is not None:
        return fallback_price, True

    return cost_price, True

def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = df.copy()

    # 数値変換
    numeric_cols = ["quantity", "price", "cost_price"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 評価額
    df["value"] = df["price"] * df["quantity"]

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
