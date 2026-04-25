import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import streamlit as st

# app.py にある関数を使う
from app import (
    prepare_base_dataframe,
    get_fx,
    get_price,
    save_daily_log_detail
)

# 元データ読み込み
url = "あなたのCSV URL"
df = pd.read_csv(url)

# 為替取得
usd_jpy = get_fx("JPY=X", 150)
vnd_jpy = get_fx("VNDJPY=X", 0.006)

# 前処理
df = prepare_base_dataframe(df, usd_jpy, vnd_jpy)

# fallbackチェック
warning_tickers = []

for ticker in df["ticker"].unique():
    if ticker == "CASH":
        continue

    fallback_price = df.loc[
        df["ticker"] == ticker,
        "cost_price"
    ].iloc[0]

    price, used_fallback = get_price(
        ticker,
        fallback_price
    )

    if used_fallback:
        warning_tickers.append(ticker)

# 保存判定
if warning_tickers:
    print(
        f"保存中止：価格取得失敗 → {', '.join(warning_tickers)}"
    )
else:
    result = save_daily_log_detail(df)
    print(result)

