import json
import os
import pandas as pd

from functions import (
    get_fx,
    get_assets_data_bulk,
    prepare_base_dataframe,
    save_daily_log_detail
)

# --- ① データ読み込み（ここ重要）
# 今app.pyでやってる「df作成部分」をコピペ
# （Google Sheetsから読むでもOK）
# 元データ読み込み
url = "https://docs.google.com/spreadsheets/d/18PLN9uJHxVZCAvAw92piWCniLlQ2i8Z6dT8ok_jycBI/export?format=csv&gid=0"
df = pd.read_csv(url)

df = pd.read_csv("data.csv")  # ←仮（あとで合わせる）

# --- ② 為替
usd_jpy, usd_error = get_fx("JPY=X", 150)
vnd_jpy, vnd_error = get_fx("VNDJPY=X", 0.006)

# --- ③ ティッカー処理
df["ticker"] = df["ticker"].astype(str).str.strip()
tickers = df["ticker"].unique()

price_dict, div_dict, perf_dict, errors = get_assets_data_bulk(tickers)

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
