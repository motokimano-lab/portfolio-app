import json
import os
import pandas as pd
import requests

from functions import (
    get_fx,
    get_assets_data_bulk,
    prepare_base_dataframe,
    save_daily_log_detail,
    load_daily_log_detail,
    compare_latest_logs
)

# --- ① データ読み込み（ここ重要）
url = "https://docs.google.com/spreadsheets/d/18PLN9uJHxVZCAvAw92piWCniLlQ2i8Z6dT8ok_jycBI/export?format=csv&gid=0"
df = pd.read_csv(url)

# --- ② 為替
usd_jpy, usd_error = get_fx("JPY=X", 150)
vnd_jpy, vnd_error = get_fx("VNDJPY=X", 0.006)

# --- ③ ティッカー処理
df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
tickers = df["ticker"].unique()

price_dict, div_dict, perf_dict, errors = get_assets_data_bulk(tickers)

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
if errors or usd_error or vnd_error:
    print("ERROR DETECTED → SKIP SAVE")
    exit()

# --- ⑦ 保存
creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT"])
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

msg = f"""
📊 Portfolio Update

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
