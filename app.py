import pandas as pd
import streamlit as st
import yfinance as yf
import plotly.express as px
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json


# ========= 1. データ読み込み =========
url = "https://docs.google.com/spreadsheets/d/18PLN9uJHxVZCAvAw92piWCniLlQ2i8Z6dT8ok_jycBI/export?format=csv&gid=0"
df = pd.read_csv(url)

st.set_page_config(layout="wide") # 画面を広く使う設定
st.title("My Portfolio Management")

# ========= 2. 各種データ取得・関数定義 =========

import time

@st.cache_data(ttl=600)
def get_price(ticker, cost_price, fallback_price=None):
    if ticker == "CASH":
        return 1, False

    for attempt in range(3):  # 最大3回 retry
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d")

            if not hist.empty:
                price = hist["Close"].dropna().iloc[-1]
                return price, False

        except Exception as e:
            print(f"{ticker} retry {attempt + 1}/3 failed")
            time.sleep(2)  # 少し待って再試行

    # 3回失敗したら fallback
    if fallback_price is not None:
        return fallback_price, True

    return cost_price, True
@st.cache_data(ttl=3600)
def get_fx(symbol, default):
    try:
        fx = yf.Ticker(symbol)
        return fx.history(period="1d")["Close"].iloc[-1]
    except: return default

@st.cache_data(ttl=3600)
def get_dividend_data(ticker):
    exclude_tickers = ["CASH", "VOO"]
    if ticker in exclude_tickers: return 0.0
    try:
        stock = yf.Ticker(ticker)
        div_yield = stock.info.get('dividendYield', 0)
        if div_yield is None: return 0.0
        if div_yield > 0.2: div_yield = div_yield / 100
        return div_yield
    except: return 0.0

@st.cache_data(ttl=3600)
def get_performance(ticker):
    if ticker == "CASH": return 0.0, 0.0
    try:
        stock = yf.Ticker(ticker)
        hist_daily = stock.history(period="2d")
        daily_pct = ((hist_daily["Close"].iloc[-1] - hist_daily["Close"].iloc[-2]) / hist_daily["Close"].iloc[-2] * 100) if len(hist_daily) >= 2 else 0.0
        ytd_pct = stock.info.get('ytdReturn', 0)
        ytd_pct = (ytd_pct * 100) if ytd_pct is not None else 0.0
        return daily_pct, ytd_pct
    except: return 0.0, 0.0

def calc_after_tax_dividend(row):
    annual_div_jpy = row["annual_div_jpy"]
    ticker = row["ticker"]
    acc_type = row["account_type"]
    currency = row["currency"]
    if annual_div_jpy == 0 or ticker == "VOO": return 0
    if currency == "USD" and acc_type == "特定":
        return annual_div_jpy * 0.90 * (1 - 0.20315)
    if currency == "USD" and acc_type == "NISA":
        return annual_div_jpy * 0.90
    if currency == "JPY":
        return annual_div_jpy if acc_type == "NISA" else annual_div_jpy * (1 - 0.20315)
    return annual_div_jpy * (1 - 0.20315)

@st.cache_data(ttl=3600)
def load_daily_log_detail():

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

    data = sheet.get_all_records()
    df_log = pd.DataFrame(data)

    return df_log

# 為替取得
usd_jpy = get_fx("JPY=X", 150)
vnd_jpy = get_fx("VNDJPY=X", 0.006)

def prepare_base_dataframe(df, usd_jpy, vnd_jpy):
    df = df.copy()

    # 数値変換
    numeric_cols = ["quantity", "price", "cost_price"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 評価額
    df["value"] = df["price"] * df["quantity"]

    # 円換算
    def convert_to_jpy(row):
        if row["currency"] == "USD":
            return row["value"] * usd_jpy
        elif row["currency"] == "VND":
            return row["value"] * vnd_jpy
        return row["value"]

    df["value_jpy"] = df.apply(convert_to_jpy, axis=1)

    # セクター補完
    df["sector_group"] = df["sector"].apply(
        lambda x: x if pd.notna(x) and str(x).strip() != "" else "未分類"
    )

    return df

# ========= 3. メイン計算処理 (すべて先に終わらせる) =========
# 基本数値
unique_tickers = df["ticker"].unique()

price_dict = {}
warning_tickers = []

for ticker in unique_tickers:
    if ticker == "CASH":
        price_dict[ticker] = 1
    else:
        fallback_price = df.loc[
            df["ticker"] == ticker,
            "cost_price"
        ].iloc[0]

        # get_price() から
        # (price, fallback_used)
        # を受け取る
        price, used_fallback = get_price(
            ticker,
            fallback_price
        )

        price_dict[ticker] = price

        if used_fallback:
            warning_tickers.append(ticker)

df["price"] = df["ticker"].map(price_dict)

df = prepare_base_dataframe(df, usd_jpy, vnd_jpy)

# 配当計算
dividend_dict = {}

for ticker in unique_tickers:
    dividend_dict[ticker] = get_dividend_data(ticker)

df["div_yield"] = df["ticker"].map(dividend_dict)
df["annual_div_jpy"] = df["value_jpy"] * df["div_yield"]
df["after_tax_div_jpy"] = df.apply(calc_after_tax_dividend, axis=1)

# 損益・パフォーマンス
df["profit_pct"] = df.apply(lambda r: 0 if r["ticker"] == "CASH" else (r["price"] - r["cost_price"]) / r["cost_price"] * 100, axis=1)
performance_dict = {}

for ticker in unique_tickers:
    performance_dict[ticker] = get_performance(ticker)

df["daily_pct"] = df["ticker"].map(
    lambda x: performance_dict[x][0]
)

df["ytd_pct"] = df["ticker"].map(
    lambda x: performance_dict[x][1]
)

# ========= 4. フィルター設定 (サイドバー) =========
st.sidebar.header("🔍 フィルター設定")
all_owners = df["owner"].unique().tolist() if "owner" in df.columns else ["Unknown"]
selected_owners = st.sidebar.multiselect("名義を選択", all_owners, default=all_owners)

all_accounts = df["account_type"].unique().tolist()
selected_accounts = st.sidebar.multiselect("口座種別を選択", all_accounts, default=all_accounts)

st.sidebar.header("📡 データ取得状態")

if warning_tickers:
    st.sidebar.warning(
        f"価格取得失敗: {', '.join(warning_tickers)}"
    )
else:
    st.sidebar.success("価格データ取得：正常")

div_warning_tickers = []

for ticker in unique_tickers:
    div = get_dividend_data(ticker)
    dividend_dict[ticker] = div

    if div == 0 and ticker not in ["CASH", "VOO", "BTC-JPY", "ETH-JPY"]:
        div_warning_tickers.append(ticker)

if div_warning_tickers:
    st.sidebar.warning(
        f"配当取得要確認: {', '.join(div_warning_tickers)}"
    )
else:
    st.sidebar.success("配当データ取得：正常")


# ========= キャッシュ更新 =========
st.sidebar.header("🔄 データ更新")

if st.sidebar.button("最新データを再取得"):
    get_price.clear()
    get_performance.clear()
    get_dividend_data.clear()
    get_fx.clear()
    load_daily_log_detail.clear()

    st.sidebar.success("最新データを再取得します")

# データの絞り込み実行
mask = df["account_type"].isin(selected_accounts)
if "owner" in df.columns:
    mask = mask & (df["owner"].isin(selected_owners))
df_filtered = df[mask]

# 資産ツリーマップの色設定
st.sidebar.header("🎨 表示設定")
color_option = st.sidebar.radio("資産ツリーマップの色基準", ["損益率", "年初来比", "前日比"], index=2)
color_map = {"損益率": "profit_pct", "年初来比": "ytd_pct", "前日比": "daily_pct"}
selected_color_col = color_map.get(color_option, "profit_pct")

# ========= 5. 表示セクション =========
# 現在の時刻を取得して、好きな形式の文字列にする
# 実行した瞬間の「年/月/日 時:分」が作成されます
current_time = datetime.now(
    ZoneInfo("Asia/Tokyo")
).strftime("%Y/%m/%d %H:%M")

# --- (A) 対前日の計算 ---
# 実際の列名 "daily_pct" を指定します
# もしスプレッドシートで 1% が 「0.01」 と入力されているなら / 100 は不要です
diff_series = df_filtered['daily_pct'] / 100 

total_jpy = df_filtered["value_jpy"].sum()

# 全体の騰落額 (円) を計算
total_diff_jpy = (df_filtered['value_jpy'] - (df_filtered['value_jpy'] / (1 + diff_series))).sum()
# 全体の騰落率 (%) を計算
# (騰落額 ÷ 前日の総資産額) × 100
previous_total_jpy = total_jpy - total_diff_jpy
total_diff_pct = (total_diff_jpy / previous_total_jpy) * 100 if previous_total_jpy != 0 else 0


# --- (B) 表示用の文字列作成 ---
# プラスの場合は「+」、マイナスの場合は「-」が自動で付きますが、
# 見やすくするために、色分けや記号を整えます
diff_display = f"対前日: {'+' if total_diff_jpy > 0 else ''}{total_diff_jpy:,.0f} 円 ({'+' if total_diff_pct > 0 else ''}{total_diff_pct:.2f}%)"


# --- (1) 総資産額 ---

total_usd = total_jpy / usd_jpy
st.header("🌍 Overall Assets")
col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("  総資産 (円)", f"{total_jpy:,.0f} 円")

with col2:
    # 2番目のカラムに対前日を表示
    # 第3引数（delta）に % を入れると、自動で矢印と色がつきます
    st.metric(
        "対前日比", 
        f"{total_diff_jpy:,.0f} 円", 
        f"{total_diff_pct:.2f}%"
    )

with col3:
    # ここに米ドルの総資産を表示
    st.metric("総資産 (USD)", f"${total_usd:,.0f}")

with col4:
    st.markdown(
        f"""
        <div style="line-height: 0.6; margin-top: 1px;">
                <div style="font-size: 1.0rem;">
                <span style="font-size: 0.1rem; color: gray; margin-right: 1px;"></span>
            </div>
        </div>
        <div style="line-height: 1.8; margin-top: 2px;">    
            <div style="font-size: 1.0rem;">
                <span style="font-size: 0.8rem; color: gray; margin-right: 8px;">USDJPY</span>
                <span style="font-weight: bold;">{usd_jpy:.2f}</span>
                <span style="font-size: 0.7rem; font-weight: normal; margin-left: 2px;">円</span>
            </div>        
            <div style="font-size: 1.0rem;">
                <span style="font-size: 0.8rem; color: gray; margin-right: 8px;">VNDJPY</span>
                <span style="font-weight: bold;">{vnd_jpy:.5f}</span>
                <span style="font-size: 0.7rem; font-weight: normal; margin-left: 2px;">円</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
# --- (2) 資産額ツリーマップ 【最終完成版：集計・一意化・変数網羅】 ---
import numpy as np
import pandas as pd
import plotly.graph_objects as go

# 1. データの準備とクリーニング
df_map = df_filtered.copy()
# セクターの空欄を統一
df_map['sector_group'] = df_map['sector_group'].replace(['', ' ', 'nan', 'None', '未分類'], np.nan)

# ✅ 色の範囲（v_min, v_max）の定義
v_min, v_max = (-3, 3) if color_option == "前日比" else ((-20, 20) if color_option == "年初来比" else (-50, 50))
finviz_colors = [[0.0, "rgb(192, 0, 0)"], [0.5, "rgb(64, 64, 64)"], [1.0, "rgb(0, 128, 0)"]]

# ✅ 【重要】口座（行）ごとのデータを銘柄単位で集計（NISAと特定を合体）
df_grouped = df_map.groupby(['asset_class', 'sector_group', 'display_name'], dropna=False).agg({
    'value_jpy': 'sum',
    selected_color_col: 'mean' # 騰落率は平均を採用
}).reset_index()

# 実行した瞬間の「年/月/日 時:分」が作成されます
current_time = datetime.now(
    ZoneInfo("Asia/Tokyo")
).strftime("%Y/%m/%d %H:%M")

# カスタムラベル（M表記付き）の作成
total_val = df_grouped["value_jpy"].sum()
ac_summary = df_grouped.groupby("asset_class")["value_jpy"].sum().reset_index()

def format_ac_label(row):
    val = row["value_jpy"]
    percent = (val / total_val) * 100 if total_val != 0 else 0
    m_val = val / 1_000_000
    return f"{row['asset_class']} ({percent:.1f}% {m_val:.1f}M)"

ac_labels = {r["asset_class"]: format_ac_label(r) for _, r in ac_summary.iterrows()}
df_grouped["ac_display"] = df_grouped["asset_class"].map(ac_labels)
df_grouped["value_man"] = df_grouped["value_jpy"] / 10000

# 2. 階層データの構築（ids, parents, labels, values, colors）
ids, parents, labels, values, colors = [], [], [], [], []

# (A) ルート
root_id = "Total_Root"
ids.append(root_id); parents.append(""); labels.append(f" {total_jpy/1_000_000:.1f} M - 更新: {current_time}")
values.append(0); colors.append(df_grouped[selected_color_col].mean() if not df_grouped.empty else 0)

# (B) 資産クラス
for ac in df_grouped["ac_display"].unique():
    ids.append(ac); parents.append(root_id); labels.append(ac)
    values.append(0); colors.append(df_grouped[df_grouped["ac_display"] == ac][selected_color_col].mean())

# (C) セクター
df_sectors = df_grouped[df_grouped["sector_group"].notna()]
for (ac, sector), group in df_sectors.groupby(["ac_display", "sector_group"]):
    sector_id = f"Sect-{ac}-{sector}"
    ids.append(sector_id); parents.append(ac); labels.append(sector)
    values.append(0); colors.append(group[selected_color_col].mean())

# (D) 銘柄
for _, row in df_grouped.iterrows():
    ticker = row['display_name']
    ac = row['ac_display']
    sector = row['sector_group']
    
    parent_id = f"Sect-{ac}-{sector}" if pd.notna(sector) else ac
    # 完全にユニークなIDを作成
    unique_id = f"Item-{ac}-{sector}-{ticker}"
    
    ids.append(unique_id); parents.append(parent_id); labels.append(ticker)
    values.append(row['value_man'])
    colors.append(row[selected_color_col])

# 3. 描画
fig_asset = go.Figure(go.Treemap(
    ids=ids, 
    parents=parents, 
    labels=labels, 
    values=values,
    # branchvaluesは指定せず、末端の積み上げに任せることで隙間を消す
    marker=dict(
        colors=colors, 
        colorscale=finviz_colors, 
        cmid=0, 
        cmin=v_min, 
        cmax=v_max, 
        line=dict(width=1, color="black")
    ),
    hovertemplate="<b>%{label}</b><br>合計評価額: %{value:,.0f}万円<br>%{color:.2f}%<extra></extra>",
    texttemplate="<b>%{label}</b><br>%{value:,.0f}",
))

fig_asset.update_layout(
    height=700, 
    margin=dict(t=40, l=10, r=10, b=10),
    title=f"📊 資産構成 ({color_option}表示)"
)
st.plotly_chart(fig_asset, use_container_width=True, key="asset_tree")


# --- (4) 資産構成比率（円グラフ3種） ---

col_p1, col_p2, col_p3 = st.columns(3)

# 共通の色の設定
colors_map = {
    '日本株': '#1f77b4', '米国株': '#ff7f0e', '欧・新興国株': '#2ca02c', '株式': '#00bfff',
    '現金・債券': '#d62728', 'JPY': '#1f77b4', 'USD': '#ff7f0e', 'VND': '#2ca02c'
}

# --- 1. 現金比率（暗号資産を除く） ---
with col_p1:
    st.subheader("💰 現金比率")
    # ✅ 1. まずデータを集計する (ここが漏れていました)
    df_no_crypto = df_filtered[df_filtered['asset_class'] != '暗号資産'].copy()
    df_no_crypto['pie_class'] = df_no_crypto['asset_class'].apply(
        lambda x: '現金・債券' if x == '現金・債券' else '株式'
    )
    pie1_data = df_no_crypto.groupby('pie_class')['value_jpy'].sum()
    
    # ✅ 2. M単位に変換
    pie1_values = pie1_data.values / 1_000_000
    
    fig1 = go.Figure(data=[go.Pie(
        labels=pie1_data.index, 
        values=pie1_values, 
        hole=.4,
        texttemplate="<b>%{label}</b><br>%{percent:.1%}<br>%{value:.1f}M",
        textposition="inside",
        insidetextorientation="horizontal",
        marker=dict(colors=[
        '#00bfff' if x == '株式' else '#d62728' for x in pie1_data.index]),
         # 株式ならライトブルー、それ以外（現金債券）ならレッド
    )])
    fig1.update_layout(showlegend=True, height=400, margin=dict(t=20, b=10, l=10, r=10), 
                      legend=dict(orientation="h",xanchor="right", x=1, yanchor="bottom", y=-0.10))
    st.plotly_chart(fig1, use_container_width=True)

# --- 2. 株式の地域比率 ---
with col_p2:
    st.subheader("🌍 株式地域比率")
    # ✅ 集計
    df_stocks = df_filtered[df_filtered['asset_class'].isin(['日本株', '米国株', '欧・新興国株'])]
    pie2_data = df_stocks.groupby('asset_class')['value_jpy'].sum()
    pie2_values = pie2_data.values / 1_000_000
    
    fig2 = go.Figure(data=[go.Pie(
        labels=pie2_data.index, 
        values=pie2_values, 
        hole=.4,
        texttemplate="<b>%{label}</b><br>%{percent:.1%}<br>%{value:.1f}M",
        textposition="inside",
        insidetextorientation="horizontal",
        marker=dict(colors=[colors_map.get(x) for x in pie2_data.index])
    )])
    fig2.update_traces(textfont_size=12)
    fig2.update_layout(uniformtext_minsize=12, showlegend=True, height=400, margin=dict(t=20, b=10, l=10, r=10),
                      legend=dict(orientation="h",xanchor="right", x=1, yanchor="bottom", y=-0.1))
    st.plotly_chart(fig2, use_container_width=True)

# --- 3. 現金の通貨比率 ---
with col_p3:
    st.subheader("💱 現金通貨比率")
    # ✅ 集計
    df_cash = df_filtered[df_filtered['ticker'] == 'CASH']
    pie3_data = df_cash.groupby('currency')['value_jpy'].sum()
    pie3_values = pie3_data.values / 1_000_000
    
    fig3 = go.Figure(data=[go.Pie(
        labels=pie3_data.index, 
        values=pie3_values, 
        hole=.4,
        texttemplate="<b>%{label}</b><br>%{percent:.1%}<br>%{value:.1f}M",
        textposition="inside",
        insidetextorientation="horizontal",
        marker=dict(colors=[colors_map.get(x, '#7f7f7f') for x in pie3_data.index])
    )])
    fig3.update_traces(textfont_size=12)
    fig3.update_layout(uniformtext_minsize=12, showlegend=True, height=400, margin=dict(t=20, b=10, l=10, r=10),
                      legend=dict(orientation="h",xanchor="right", x=1, yanchor="bottom", y=-0.1))
    st.plotly_chart(fig3, use_container_width=True)

# --- (5) 年間配当金額 ---
total_div_pre = df_filtered["annual_div_jpy"].sum()
total_div_post = df_filtered["after_tax_div_jpy"].sum()
st.markdown("---")
st.header("💰 Dividend Summary")
d1, d2, d3 = st.columns(3)
d1.metric("年間配当（税引前）", f"{total_div_pre:,.0f} 円")
d2.metric("年間配当（税引後）", f"{total_div_post:,.0f} 円")
d3.metric("月平均（税引後）", f"{(total_div_post/12):,.0f} 円")

# --- (6) 配当金のツリーマップ ---
import numpy as np
import pandas as pd
import plotly.graph_objects as go

# 1. データの準備と銘柄単位での集計（NISAと特定を合算）
df_div_map = df_filtered.copy()
df_div_map['sector_group'] = df_div_map['sector_group'].replace(['', ' ', 'nan', 'None', '未分類'], np.nan)

# 銘柄単位で集計（配当額は合計、利回りは平均をとる）
df_div_grouped = df_div_map.groupby(['asset_class', 'sector_group', 'display_name'], dropna=False).agg({
    'annual_div_jpy': 'sum',
    'div_yield': 'mean'
}).reset_index()

# 色の定義
div_colors = [[0.0, "rgb(192, 0, 0)"], [0.5, "rgb(64, 64, 64)"], [1.0, "rgb(0, 128, 0)"]]

# 2. 階層データの構築
d_ids, d_parents, d_labels, d_values, d_colors = [], [], [], [], []

# (A) ルート
d_root_id = "Div_Root"
d_ids.append(d_root_id); d_parents.append(""); d_labels.append(f"年間配当（税引後）: {total_div_post:,.0f} 円")
d_values.append(0) 
d_colors.append(df_div_grouped['div_yield'].mean() if not df_div_grouped.empty else 0)

# (B) 資産クラス
for ac in df_div_grouped["asset_class"].unique():
    d_ids.append(ac); d_parents.append(d_root_id); d_labels.append(ac)
    d_values.append(0)
    d_colors.append(df_div_grouped[df_div_grouped["asset_class"] == ac]['div_yield'].mean())

# (C) セクター
df_div_sectors = df_div_grouped[df_div_grouped["sector_group"].notna()]
for (ac, sector), group in df_div_sectors.groupby(["asset_class", "sector_group"]):
    d_sector_id = f"DivSect-{ac}-{sector}"
    d_ids.append(d_sector_id); d_parents.append(ac); d_labels.append(sector)
    d_values.append(0)
    d_colors.append(group['div_yield'].mean())

# (D) 銘柄
for _, row in df_div_grouped.iterrows():
    ticker = row['display_name']
    ac = row['asset_class']
    sector = row['sector_group']
    
    d_parent_id = f"DivSect-{ac}-{sector}" if pd.notna(sector) else ac
    d_unique_id = f"DivItem-{ac}-{sector}-{ticker}"
    
    d_ids.append(d_unique_id); d_parents.append(d_parent_id); d_labels.append(ticker)
    d_values.append(row['annual_div_jpy']) # サイズは年間配当額
    d_colors.append(row['div_yield'])      # 色は利回り

# 3. 描画
fig_div = go.Figure(go.Treemap(
    ids=d_ids,
    parents=d_parents,
    labels=d_labels,
    values=d_values,
    marker=dict(
        colors=d_colors,
        colorscale=div_colors,
        cmid=0.025, # 2.5%を基準色にする
        cmin=0,
        cmax=0.05,
        colorbar=dict(title="利回り", tickformat=".1%"),
        line=dict(width=1, color="black")
    ),
    hovertemplate="<b>%{label}</b><br>年間配当: %{value:,.0f}円<br>利回り: %{color:.2%}<extra></extra>",
    texttemplate="<b>%{label}</b><br>%{value:,.0f}円",
))

fig_div.update_layout(
    height=700, 
    margin=dict(t=0, l=10, r=10, b=10),
    
)

st.plotly_chart(fig_div, use_container_width=True, key="dividend_tree_new")

# --- (3') 配当構成比率（帯グラフ：列作成・順序固定版） ---
import plotly.express as px

# ✅ 1. 税引後配当の列がなければ、その場で作ってしまう（KeyError対策）
# 資産ツリー等で使ったロジック（例：0.8掛け）に合わせて計算してください
if 'annual_div_post' not in df_filtered.columns:
    # もし既存の annual_div_jpy から計算する場合（一例です）
    # 日本株は20.315%引く、などの細かい判定が面倒なら、
    # シンプルに既存の計算済み変数やロジックをここに適用します。
    df_filtered['annual_div_post'] = df_filtered['annual_div_jpy'] * 0.79685 # 簡易的な税引後計算

div_col = 'annual_div_post'
df_div_share = df_filtered[df_filtered[div_col] > 0].copy()

if not df_div_share.empty:
    # 資産クラスごとに配当額を合計
    div_sum_by_ac = df_div_share.groupby('asset_class')[div_col].sum().reset_index()
    
    # 全体の配当総額に対する比率を計算
    total_div_val = div_sum_by_ac[div_col].sum()
    div_sum_by_ac['share_pct'] = (div_sum_by_ac[div_col] / total_div_val) * 100
    div_sum_by_ac['all'] = ' '

    # 指定された色のマップ
    div_color_map = {
    '日本株': '#1f77b4',
    '米国株': '#ff7f0e', 
    '欧・新興国株': '#2ca02c',
    '現金・債券': '#d62728'}

    # 並び順の固定
    target_order = ['日本株', '米国株', '欧・新興国株', '現金・債券']

    # 2. 帯グラフの作成
    fig_div_share = px.bar(
        div_sum_by_ac,
        x=div_col,
        y='all',
        color='asset_class',
        orientation='h',
        color_discrete_map=div_color_map,
        category_orders={'asset_class': target_order},
        # ✅ textには比率(%)を表示
        text=div_sum_by_ac['share_pct'].apply(lambda x: f'{x:.1f}%'),
        
    )

    # 3. レイアウトの調整
    fig_div_share.update_traces(
        textposition='inside',
        textfont_size=14,
        hovertemplate="<b>%{label}</b><br>税引後配当: %{value:,.0f}円<br>比率: %{text}<extra></extra>"
    )
    
    fig_div_share.update_layout(
        xaxis=dict(showgrid=False, zeroline=False, title='', showticklabels=False), # 下の目盛りも消してスッキリ
        yaxis=dict(showgrid=False, zeroline=False, title=''),
        showlegend=True,
        legend=dict(
            title_text="",
            orientation="h", 
            yanchor="bottom", y=-0.32, 
            xanchor="right", x=0.95,
            traceorder="normal" 
        ),
        height=100, # 高さをさらに抑えてタイトに
        margin=dict(t=0, l=10, r=10, b=30)
    )

    st.plotly_chart(fig_div_share, use_container_width=True, key="div_share_bar_fixed")

else:
    st.info("配当データ（税引後）がありません。")



# ========= 資産推移（積み上げ面グラフ） =========

st.header("📈 資産推移（構成比）")

df_log = load_daily_log_detail()

if not df_log.empty:

    df_log["date"] = pd.to_datetime(df_log["date"])
    df_log = df_log.sort_values("date")

    # ✅ ここが核心：日付 × アセットクラスで集計
    df_grouped = df_log.groupby(["date", "asset_class"])["value_jpy"].sum().reset_index()

    # グラフ
    fig = px.area(
        df_grouped,
        x="date",
        y="value_jpy",
        color="asset_class",
        title="資産構成の推移",
        color_discrete_map={
            "日本株": "#1f77b4",
            "米国株": "#ff7f0e",
            "欧・新興国株": "#2ca02c",
            "現金・債券": "#d62728",
            "暗号資産": "#e377c2"
        },
        category_orders={
            "asset_class": ["日本株", "米国株", "欧・新興国株", "暗号資産", "現金・債券"]
        }
    )

    # 合計ライン（これも再計算）
    df_total = df_log.groupby("date")["value_jpy"].sum().reset_index()

    fig.add_scatter(
        x=df_total["date"],
        y=df_total["value_jpy"],
        mode="lines",
        name="合計",
        line=dict(width=3, color="black")
    )

    st.plotly_chart(fig, use_container_width=True)

else:
    st.info("まだログがありません")


# --- (3) 資産額の表 ---
st.subheader("📝 保有資産一覧")
st.dataframe(df_filtered[["ticker", "display_name", "quantity", "price", "value_jpy", "profit_pct"]].style.format({"profit_pct": "{:.2f}%", "value_jpy": "{:,.0f}"}))

st.markdown("---")

# --- (6) 配当金の表 ---
st.subheader("📈 銘柄別配当データ")
st.dataframe(df_filtered[["ticker", "display_name", "div_yield", "annual_div_jpy", "after_tax_div_jpy"]].style.format({"div_yield": "{:.2%}", "annual_div_jpy": "{:,.0f}", "after_tax_div_jpy": "{:,.0f}"}))



#資産記録
import json

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

    # 既存データ取得
    existing_data = sheet.get_all_values()

    # 今日のデータを除外（上書き用）
    filtered_data = [
        row for row in existing_data
        if len(row) == 0 or row[0] != today
    ]

    # シートを更新
    sheet.clear()
    sheet.update(filtered_data + rows)

    return f"{len(rows)} rows saved (overwrite mode)"
st.markdown("---")
st.subheader("📅 データ記録")

if st.button("📅 今日の資産を記録"):
    with st.spinner("保存中..."):
        result = save_daily_log_detail(df)

    load_daily_log_detail.clear()

    st.success(result)
    
st.header("📊 期間比較（成長分析）")

# df_logが存在し、空でないことを確認
if 'df_log' in locals() and not df_log.empty:
    import plotly.graph_objects as go
    import numpy as np

    # 1. データの準備
    df_comp = df_log.copy()
    # 数値変換と日付変換
    df_comp["value_jpy"] = pd.to_numeric(df_comp["value_jpy"], errors='coerce').fillna(0)
    df_comp["date"] = pd.to_datetime(df_comp["date"]).dt.date
    
    date_list = sorted(df_comp["date"].unique())

    if len(date_list) >= 2:
        col_d1, col_d2 = st.columns(2)
        with col_d1:
            s_date = st.selectbox("比較開始日", date_list, index=len(date_list)-2, key="growth_s")
        with col_d2:
            e_date = st.selectbox("比較終了日", date_list, index=len(date_list)-1, key="growth_e")

        # --- A. 期間データの抽出 ---
        d_start_raw = df_comp[df_comp["date"] == s_date].copy()
        d_end_raw = df_comp[df_comp["date"] == e_date].copy()

        # --- B. 銘柄単位で集計（名義や口座の重複を合算して「10倍問題」を解決） ---
        def summarize_assets(df_target):
            # あなたのアプリの実際の列名 [value_jpy] を使用
            return df_target.groupby(["asset_class", "sector", "display_name"], dropna=False)["value_jpy"].sum().reset_index()

        d_start = summarize_assets(d_start_raw)
        d_end = summarize_assets(d_end_raw)

        # --- C. データのマージ ---
        d_merged = pd.merge(
            d_end, d_start, 
            on=["asset_class", "sector", "display_name"], 
            how="outer", suffixes=("_end", "_start")
        ).fillna(0)

        # 騰落率の計算
        d_merged["diff_val"] = d_merged["value_jpy_end"] - d_merged["value_jpy_start"]
        d_merged["growth_pct"] = d_merged.apply(
            lambda r: (r["diff_val"] / r["value_jpy_start"] * 100) if r["value_jpy_start"] != 0 else 0, 
            axis=1
        )
        d_merged["growth_pct"] = d_merged["growth_pct"].fillna(0)
        d_merged["growth_pct"] = d_merged["growth_pct"].round(2)

        # 2. グラフデータの構築
        ids, parents, labels, values, colors, hover_texts, custom_vals = [], [], [], [], [], [], []
        
        # ルート（全体の合計）
        root_id = "Growth_Root"
        total_end = d_merged["value_jpy_end"].sum()
        total_start = d_merged["value_jpy_start"].sum()
        total_growth = ((total_end - total_start) / total_start * 100) if total_start != 0 else 0
        total_diff = total_end - total_start

        # タイトル文字列（ここは今まで通り）
        title_text = (
            f"{total_end:,.0f}円    "
            f"{total_diff:+,.0f}円({total_growth:+.2f}%)    "
            f"{s_date.strftime('%Y/%m/%d')}  →  {e_date.strftime('%Y/%m/%d')}"
        )
        
        # ✅ valuesに0ではなく合計額(total_end)を入れることで親階層の「0円」を解消
        ids.append(root_id); parents.append(""); labels.append(title_text)
        values.append(0); colors.append(round(total_growth, 2)); hover_texts.append("ポートフォリオ全体")
        custom_vals.append(round(total_growth, 2))

        # アセットクラス単位
        for ac in d_merged["asset_class"].unique():
            ac_df = d_merged[d_merged["asset_class"] == ac]
            ac_id = f"ac|{ac}"
            ac_end = ac_df["value_jpy_end"].sum()
            ac_start = ac_df["value_jpy_start"].sum()
            ac_growth = ((ac_end - ac_start) / ac_start * 100) if ac_start != 0 else 0
            
            ids.append(ac_id); parents.append(root_id); labels.append(ac)
            values.append(0); colors.append(round(ac_growth, 2)); hover_texts.append(f"{ac} 合計")
            custom_vals.append(round(ac_growth, 2))

            if ac in ["日本株", "現金・債券"]:
                for sector in ac_df["sector"].unique():
                    sect_id = f"st|{ac}|{sector}"
                    s_df = ac_df[ac_df["sector"] == sector]
                    s_end = s_df["value_jpy_end"].sum()
                    s_start = s_df["value_jpy_start"].sum()
                    s_growth = ((s_end - s_start) / s_start * 100) if s_start != 0 else 0
                    
                    ids.append(sect_id); parents.append(ac_id); labels.append(sector)
                    values.append(0); colors.append(round(s_growth, 2)); hover_texts.append(f"{sector} 合計")
                    custom_vals.append(round(s_growth, 2))
                    
                    for _, r in s_df.iterrows():
                        item_id = f"it|{r['display_name']}|{sect_id}"
                        ids.append(item_id); parents.append(sect_id); labels.append(r["display_name"])
                        values.append(r["value_jpy_end"]); colors.append(round(r["growth_pct"], 2))
                        hover_texts.append(f"増減額: {r['diff_val']:+,.0f}円")
                        custom_vals.append(round(r["growth_pct"], 2))
            else:
                for _, r in ac_df.iterrows():
                    item_id = f"it|{r['display_name']}|{ac_id}"
                    ids.append(item_id); parents.append(ac_id); labels.append(r["display_name"])
                    values.append(r["value_jpy_end"]); colors.append(round(r["growth_pct"], 2))
                    hover_texts.append(f"増減額: {r['diff_val']:+,.0f}円")
                    custom_vals.append(round(r["growth_pct"], 2))

        # 3. 描画
        fig_growth = go.Figure(go.Treemap(
            ids=ids, 
            parents=parents, 
            labels=labels, 
            values=values,
            # ✅ branchvalues="total" を指定することで、親のサイズを子の合計に一致させる
            branchvalues="remainder",
            marker=dict(
                colors=colors, 
                colorscale=finviz_colors, 
                cmid=0, cmin=-5, cmax=5,
                colorbar=dict(title="騰落率 (%)")
            ),
            # ✅ %{color:+.2f}% を使うことで、確実に小数点2桁に固定します
            hovertemplate="""
            <b>%{label}</b>
            <br>資産額: %{value:,.0f}円
            <br>騰落率: %{customdata:+.2f}%
            <extra></extra>""",
            customdata=custom_vals,
            texttemplate="<b>%{label}</b><br>%{value:,.0f}円<br>%{customdata:+.2f}%"            
        ))
        fig_growth.update_layout(height=700, margin=dict(t=30, b=10, l=10, r=10))
        st.plotly_chart(fig_growth, use_container_width=True, key="growth_treemap_final_v3")

    else:
        st.info("比較するには2つ以上のログデータが必要です。")

