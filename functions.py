import pandas as pd
import yfinance as yf

# --- 為替 ---
def get_fx(symbol, default):
    try:
        fx = yf.Ticker(symbol)
        hist = fx.history(period="5d")

        if hist.empty:
            return float(default), True

        price = float(hist["Close"].iloc[-1])
        return price, False

    except:
        return float(default), True

# --- 価格（これだけ使う） ---
def get_assets_data_bulk(tickers):
    if not tickers:
        return {}, {}, {}, []

    yf_tickers = list(set([
        str(t).strip().upper() for t in tickers 
        if t != "CASH" and pd.notna(t)
    ]))
    
    if not yf_tickers:
        return {}, {}, {}, []

    price_dict = {}
    div_yield_dict = {}
    perf_dict = {}
    errors = []

    try:
        data = yf.download(
            yf_tickers,
            period="1y",
            actions=True,
            group_by='ticker',
            progress=False
        )
    except Exception:
        return {}, {}, {}, yf_tickers

    for t in yf_tickers:
        try:
            # --- データ取得 ---
            if len(yf_tickers) == 1:
                df_t = data
            else:
                if t in data.columns.levels[0]:
                    df_t = data[t]
                else:
                    errors.append(t)
                    continue

            df_t = df_t.dropna(subset=["Close"])

            # --- fallback ---
            if df_t.empty:
                fallback = yf.Ticker(t).history(period="5d")
                if fallback.empty:
                    errors.append(t)
                    continue

                current_price = float(fallback["Close"].iloc[-1])
                annual_div_total = 0

                if len(fallback) >= 2:
                    prev = float(fallback["Close"].iloc[-2])
                    perf_dict[t] = (
                        current_price - prev,
                        (current_price - prev) / prev * 100
                    )
                else:
                    perf_dict[t] = (0.0, 0.0)

            else:
                current_price = float(df_t["Close"].iloc[-1])

                if len(df_t) >= 2:
                    prev_price = float(df_t["Close"].iloc[-2])
                    diff_val = current_price - prev_price
                    diff_pct = (diff_val / prev_price) * 100
                    perf_dict[t] = (diff_val, diff_pct)
                else:
                    perf_dict[t] = (0.0, 0.0)

                annual_div_total = (
                    df_t["Dividends"].sum()
                    if "Dividends" in df_t.columns else 0
                )

            # --- 格納 ---
            for key in [t.upper(), t.lower()]:
                price_dict[key] = current_price
                div_yield_dict[key] = (
                    annual_div_total / current_price
                    if current_price > 0 else 0.0
                )
                if t in perf_dict:
                    perf_dict[key] = perf_dict[t]

        except Exception:
            errors.append(t)
            continue

    return price_dict, div_yield_dict, perf_dict, errors
        
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
