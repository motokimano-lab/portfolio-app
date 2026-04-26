import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import streamlit as st

# app.py にある関数を使う
def get_fx(symbol, default):
    try:
        fx = yf.Ticker(symbol)
        return fx.history(period="1d")["Close"].iloc[-1]
    except:
        return default


def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = df.copy()

    df["quantity"] = pd.to_numeric(
        df["quantity"],
        errors="coerce"
    )

    df["price"] = pd.to_numeric(
        df["price"],
        errors="coerce"
    )

    df["value"] = df["price"] * df["quantity"]

    df["value_jpy"] = df.apply(
        lambda r:
            r["value"] * usd_jpy
            if r["currency"] == "USD"
            else (
                r["value"] * vnd_jpy
                if r["currency"] == "VND"
                else r["value"]
            ),
        axis=1
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
        creds_dict,
        scope
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

# 元データ読み込み
url = "https://docs.google.com/spreadsheets/d/18PLN9uJHxVZCAvAw92piWCniLlQ2i8Z6dT8ok_jycBI/export?format=csv&gid=0"
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

