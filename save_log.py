import pandas as pd

from functions import (
    get_fx,
    get_price,
    prepare_base_dataframe,
    save_daily_log_detail,
)

# 元データ読み込み
url = "https://docs.google.com/spreadsheets/d/18PLN9uJHxVZCAvAw92piWCniLlQ2i8Z6dT8ok_jycBI/export?format=csv&gid=0"
df = pd.read_csv(url)

# 為替取得
usd_jpy = get_fx("JPY=X", 150)
vnd_jpy = get_fx("VNDJPY=X", 0.006)

# -------------------------
# 先に price を更新する
# -------------------------

warning_tickers = []

for idx, row in df.iterrows():

    ticker = row["ticker"]

    if ticker == "CASH":
        df.at[idx, "price"] = 1
        continue

    fallback_price = row["cost_price"]

    price, used_fallback = get_price(
        ticker,
        fallback_price
    )

    df.at[idx, "price"] = price

    if used_fallback:
        warning_tickers.append(ticker)

if warning_tickers:
    print(
        f"価格取得失敗（fallback使用）→ "
        f"{', '.join(set(warning_tickers))}"
    )

# -------------------------
# price更新後に前処理
# -------------------------

df = prepare_base_dataframe(
    df,
    usd_jpy,
    vnd_jpy
)

# 保存
result = save_daily_log_detail(df)
print(result)
