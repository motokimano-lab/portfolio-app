import csv
import json
import os
from io import StringIO

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import requests

MAX_MUTUAL_FUND_BUSINESS_DAYS_OLD = 5

def notify_discord(message):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"content": message}, timeout=15)
    except requests.RequestException:
        pass

from functions import (
    get_fx,
    get_position_assets_data,
    normalize_positions,
    prepare_base_dataframe,
    save_daily_log_detail,
    load_daily_log_detail,
    compare_latest_logs
)

# --- ① 認証付きでPositionsを読み込み
creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT"])
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
spreadsheet = client.open("portfolio_data")
sheet = spreadsheet.worksheet("Positions")

# app.pyと同じ型推論・空欄処理を維持する。
csv_buffer = StringIO()
csv.writer(csv_buffer).writerows(sheet.get_all_values())
csv_buffer.seek(0)
df = normalize_positions(pd.read_csv(csv_buffer, dtype={"fund_code": "string"}))

# --- ② 為替
usd_jpy, usd_error = get_fx("JPY=X", 150)
vnd_jpy, vnd_error = get_fx("VNDJPY=X", 0.006)

# --- ③ ティッカー処理
df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
tickers = df["ticker"].unique()

asset_data, fund_status = get_position_assets_data(df)
price_dict, div_dict, perf_dict, errors = asset_data
stale_funds = []
for ticker, record in fund_status.items():
    if not record["success"]:
        print(f"ERROR {ticker}: {record['error']}")
    for warning in record["warnings"]:
        print(f"WARNING {ticker}: {warning}")
    age = record.get("business_days_old")
    if age is not None and age > MAX_MUTUAL_FUND_BUSINESS_DAYS_OLD:
        stale_funds.append(
            f"{ticker} {record.get('base_date')} ({age} business days old, source={record.get('source')})"
        )

price_dict["CASH"] = 1.0
div_dict["CASH"] = 0.0
perf_dict["CASH"] = (0.0, 0.0)

# --- ④ マッピング
df["price"] = df["ticker"].map(lambda x: price_dict.get(x))
df["div_yield"] = df["ticker"].map(lambda x: div_dict.get(x, 0))
df["day_diff_val"] = df["ticker"].map(lambda x: perf_dict.get(x, (0,0))[0])
df["day_diff_pct"] = df["ticker"].map(lambda x: perf_dict.get(x, (0,0))[1])

# --- ⑤ 計算
df = prepare_base_dataframe(df, usd_jpy, vnd_jpy)

# --- ⑥ 異常チェック（これ今のロジックそのまま使える）
if errors or usd_error or vnd_error or df["value_jpy"].isna().any():
    print("ERROR DETECTED → SKIP SAVE")
    notify_discord("⚠️ Portfolio Daily_Log保存をスキップしました：価格・為替データ取得エラー")
    raise SystemExit(1)

if stale_funds:
    details = "\n".join(stale_funds)
    print("STALE MUTUAL FUND DATA → SKIP SAVE\n" + details)
    notify_discord(
        "⚠️ Portfolio Daily_Log保存をスキップしました：投信基準価額が古すぎます\n"
        + details
    )
    raise SystemExit(1)

# --- ⑦ 保存
result = save_daily_log_detail(df, creds_dict)

print(result)

# --- Discord通知 ---
# ログ読み込み
df_log = load_daily_log_detail(creds_dict)

# 比較
compare_result = compare_latest_logs(df_log)

# Discord通知
webhook_url = os.environ["DISCORD_WEBHOOK_URL"]

# TOPランキング取得
top_gainers = compare_result["top_gainers"]
top_losers = compare_result["top_losers"]

# TOP3だけ使う
gain_text = ""
for _, row in top_gainers.head(3).iterrows():
    gain_text += (
        f"{row['display_name']} "
        f"{row['growth_pct']:+.2f}%\n"
    )

loss_text = ""
for _, row in top_losers.head(3).iterrows():
    loss_text += (
        f"{row['display_name']} "
        f"{row['growth_pct']:+.2f}%\n"
    )

migration_note = (
    "\n⚠️ 投信評価方法の移行差を含みます（" + "・".join(compare_result["migration_names"]) + "）\n"
    if compare_result["migration_names"] else ""
)
msg = f"""
📊 Portfolio Update{migration_note}

💰 総資産
{compare_result['total_end']:,.0f} 円

📈 前日比
{compare_result['total_diff']:+,.0f} 円
({compare_result['total_growth']:+.2f}%)

🟢 TOP Gainers
{gain_text}

🔴 TOP Losers
{loss_text}
"""

requests.post(
    webhook_url,
    json={"content": msg}
)

print("Discord notification sent")
