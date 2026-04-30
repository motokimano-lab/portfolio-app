import pandas as pd
import yfinance as yf

# --- 為替 ---
def get_fx(symbol, default):
    try:
        df = yf.download(symbol, period="1d", progress=False)
        price = df["Close"].iloc[-1]
        # ←ここが重要
        price = float(price)
        return price, False
    except:
        return float(default), True

# --- 価格（これだけ使う） ---
def get_prices_bulk(tickers):
    if not tickers:
        return {}
    try:
        # 期間を1dから5dに延ばすと、休日明けでもデータが取りやすくなります
        data = yf.download(tickers, period="5d", group_by='ticker', progress=False)
        
        price_dict = {}
        for t in tickers:
            try:
                if len(tickers) == 1:
                    # 1件のみの場合、列名が直接 "Close" になることがある
                    temp_df = data
                else:
                    temp_df = data[t]
                
                # 直近の有効な（NaNでない）終値を取得
                last_price = temp_df["Close"].dropna().iloc[-1]
                price_dict[t] = float(last_price)
            except:
                price_dict[t] = None
        return price_dict
    except Exception as e:
        print(f"Bulk fetch error: {e}")
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

def clean_tickers(tickers):
    cleaned = []
    for t in tickers:
        if isinstance(t, str) and len(t) > 0:
            if not t.isdigit():  # "1","2"除外
                cleaned.append(t)
    return list(set(cleaned))

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

#一旦get_priceを復活

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
