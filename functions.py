import pandas as pd
import yfinance as yf

# --- 為替 ---
def get_fx(symbol, default):
    try:
        df = yf.download(symbol, period="1d", progress=False)
        return df["Close"].iloc[-1], False
    except:
        return default, True


# --- 価格（これだけ使う） ---
def get_prices_bulk(tickers):
    try:
        df = yf.download(
            tickers=tickers,
            period="1d",
            group_by="ticker",
            threads=True,
            progress=False
        )

        price_dict = {}

        for t in tickers:
            try:
                if len(tickers) == 1:
                    price = df["Close"].iloc[-1]
                else:
                    price = df[t]["Close"].iloc[-1]

                price_dict[t] = price
            except:
                price_dict[t] = None

        return price_dict

    except Exception as e:
        print("bulk取得エラー:", e)
        return {t: None for t in tickers}


# --- 配当（ダミー） ---
def get_dividends(tickers):
    return {t: 0.0 for t in tickers}, []


# --- パフォーマンス（ダミー） ---
def get_performance(tickers):
    return {t: (0.0, 0.0) for t in tickers}, []


# --- DataFrame整形 ---
def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = df.copy()

    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["cost_price"] = pd.to_numeric(df["cost_price"], errors="coerce")

    df["value"] = df["price"] * df["quantity"]

    def convert_to_jpy(row):
        if row["currency"] == "USD":
            return row["value"] * usd_jpy
        elif row["currency"] == "VND":
            return row["value"] * vnd_jpy
        return row["value"]

    df["value_jpy"] = df.apply(convert_to_jpy, axis=1)

    df["sector_group"] = df["sector"].fillna("未分類")

    return df

↓↓一旦get_priceを復活↓↓

def get_price(ticker, fallback_price):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d")

        if not hist.empty:
            return hist["Close"].iloc[-1], False
        else:
            return fallback_price, True

    except:
        return fallback_price, True
