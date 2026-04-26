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

# 前処理
df = prepare_base_dataframe(
    df,
    usd_jpy,
    vnd_jpy
)

# 価格取得チェック
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

if warning_tickers:
    print(
        f"価格取得失敗（fallback使用）→ "
        f"{', '.join(warning_tickers)}"
    )

# 保存実行
result = save_daily_log_detail(df)
print(result)
