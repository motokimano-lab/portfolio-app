import csv
from io import StringIO

import pandas as pd
import streamlit as st
import yfinance as yf
import plotly.express as px
from datetime import datetime
from zoneinfo import ZoneInfo
from oauth2client.service_account import ServiceAccountCredentials
import gspread
import os
import json
from functions import ( 
    get_fx,
    prepare_base_dataframe,
    save_daily_log_detail,
    get_position_assets_data,
    normalize_positions,
    compare_portfolio,
    compare_latest_logs
)
import functions
# Streamlit can rerun app.py in the same Python process after a Git deploy while
# an imported helper module remains cached. Reload only when a newly deployed
# attribution helper is missing, so app.py and functions.py cannot get out of sync.
if not hasattr(functions, "analyze_attribution_period"):
    import importlib
    functions = importlib.reload(functions)
print(dir(functions))

# ========= 1. データ読み込み =========
def load_positions():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    if "GCP_SERVICE_ACCOUNT" in os.environ:
        creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT"])
    else:
        creds_dict = dict(st.secrets["gcp_service_account"])

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open("portfolio_data")
    sheet = spreadsheet.worksheet("Positions")

    # CSV読み込みと同じ型推論・空欄処理を維持する。
    csv_buffer = StringIO()
    csv.writer(csv_buffer).writerows(sheet.get_all_values())
    csv_buffer.seek(0)
    return pd.read_csv(csv_buffer, dtype={"fund_code": "string"})


df = normalize_positions(load_positions())

df["ticker"] = df["ticker"].astype(str).str.strip()

# 明らかにおかしいticker除外
df = df[df["ticker"].notna()]
df = df[df["ticker"] != ""]
df = df[~df["ticker"].str.isnumeric()]

print("使用ticker一覧:", df["ticker"].unique())

st.set_page_config(layout="wide") # 画面を広く使う設定

st.markdown("""
<style>
@media (max-width: 768px) {

    .stDataFrame {
        font-size: 10px !important;
    }

    .stDataFrame div {
        font-size: 10px !important;
    }
}
</style>
""", unsafe_allow_html=True)

st.title("My Portfolio Management")

# ========= 2. 各種データ取得・関数定義 =========

import time

@st.cache_data(ttl=300)
def load_prices(tickers):
    return get_prices_bulk(tickers)

from dividend_tax import calc_after_tax_dividend

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

@st.cache_data(ttl=3600)
def load_daily_log_v2():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    if "GCP_SERVICE_ACCOUNT" in os.environ:
        creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT"])
    else:
        creds_dict = dict(st.secrets["gcp_service_account"])

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open("portfolio_data")
    sheet = spreadsheet.worksheet("Daily_Log_V2")
    return pd.DataFrame(sheet.get_all_records())

@st.cache_data(ttl=3600)
def load_fund_reference_data(df_v2):
    return functions.build_fund_reference_data(df_v2)

# 為替取得
usd_jpy, usd_error = get_fx("JPY=X", 150)
vnd_jpy, vnd_error = get_fx("VNDJPY=X", 0.006)

# ========= 3. メイン計算処理 =========
df["ticker"] = df["ticker"].astype(str).str.strip()
all_tickers = df["ticker"].unique()

# 株式等はYahoo、投信はYahoo→第2ソース→最終正常値キャッシュで取得する。
asset_data, fund_status = get_position_assets_data(df, include_dividend_details=True)

# 一括取得結果
(
    price_dict,
    div_yield_dict,
    perf_dict,
    price_errors,
    ttm_dividend_dict,
    forecast_dividend_dict,
    forecast_source_dict,
) = asset_data

if fund_status:
    with st.expander("投資信託の基準日・データ取得状態", expanded=True):
        source_labels = {
            "yahoo_finance": "Yahoo!ファイナンス",
            "official_or_provider": "運用会社・提供元",
            "last_good_cache": "最終正常値キャッシュ",
        }
        st.dataframe(pd.DataFrame([
            {"商品名": r.get("product_name", ticker), "基準価額（1万口）": r["price"],
             "基準日": r["base_date"], "前営業日比": r["previous_change"],
             "取得元": source_labels.get(r.get("source"), r.get("source") or "未取得"),
             "取得状態": "成功" if r["success"] else "失敗"}
            for ticker, r in fund_status.items()
        ]), hide_index=True, use_container_width=True)
        for ticker, record in fund_status.items():
            for warning in record["warnings"]:
                st.warning(f"{ticker}: {warning}")
            if not record["success"]:
                st.error(f"{ticker}: {record['error']}。評価額・保存を停止します。")
    if any(not r["success"] for r in fund_status.values()):
        st.stop()

# 現金データの定義
price_dict["CASH"] = 1.0
div_yield_dict["CASH"] = 0.0
perf_dict["CASH"] = (0.0, 0.0)
ttm_dividend_dict["CASH"] = 0.0
forecast_dividend_dict["CASH"] = 0.0
forecast_source_dict["CASH"] = "配当なし"

# 各列へのマッピング
df["price"] = df["ticker"].map(lambda x: price_dict.get(x))
df["dividend_ttm_per_share"] = df["ticker"].map(lambda x: ttm_dividend_dict.get(x, 0.0))
df["dividend_forecast_per_share"] = df["ticker"].map(lambda x: forecast_dividend_dict.get(x, 0.0))
df["dividend_forecast_source"] = df["ticker"].map(lambda x: forecast_source_dict.get(x, "未取得"))
df["day_diff_val"] = df["ticker"].map(lambda x: perf_dict.get(x, (0.0, 0.0))[0])
df["day_diff_pct"] = df["ticker"].map(lambda x: perf_dict.get(x, (0.0, 0.0))[1])

# 基本データの計算（為替反映、value_jpyの作成など）
df = prepare_base_dataframe(df, usd_jpy, vnd_jpy)

#cron-jobの自動保存用コード
# --- AUTO SAVE ---
if st.query_params.get("auto_save") == "1":

    st.write("AUTO SAVE TRIGGERED")
    print("AUTO SAVE TRIGGERED")
    # --- 異常検知 ---
    has_price_error = price_errors
    has_fx_error = usd_error or vnd_error

    has_invalid_price = any(
        v is None or v <= 0 for v in price_dict.values()
    )

    has_invalid_df = df["value_jpy"].isna().any()

    st.write("CHECK STATUS:", {
        "price_errors": price_errors,
        "usd_error": usd_error,
        "vnd_error": vnd_error,
        "invalid_price": has_invalid_price,
        "invalid_df": has_invalid_df
    })

    if has_price_error or has_fx_error or has_invalid_price or has_invalid_df:
        st.error("取得エラーのため保存を中止しました")
        st.stop()

    # --- 保存 ---
    creds_dict = dict(st.secrets["gcp_service_account"])
    result = save_daily_log_detail(df, creds_dict)
    detail_result = functions.save_daily_log_v2(df, creds_dict, fund_status)

    st.write("AUTO SAVE DONE:", result, detail_result)
    print("AUTO SAVE DONE:", result)

    st.stop()

# --- 配当計算（実績 / 予想を別々に保持） ---
def _fx_to_jpy(row):
    if row["currency"] == "USD":
        return usd_jpy
    if row["currency"] == "VND":
        return vnd_jpy
    return 1.0

df["fx_to_jpy"] = df.apply(_fx_to_jpy, axis=1)
df["cost_basis_jpy"] = (
    pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    / df["price_unit"]
    * pd.to_numeric(df["cost_price"], errors="coerce").fillna(0)
    * df["fx_to_jpy"]
)

df["annual_div_ttm_jpy"] = (
    pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    / df["price_unit"]
    * pd.to_numeric(df["dividend_ttm_per_share"], errors="coerce").fillna(0)
    * df["fx_to_jpy"]
)
df["annual_div_forecast_jpy"] = (
    pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    / df["price_unit"]
    * pd.to_numeric(df["dividend_forecast_per_share"], errors="coerce").fillna(0)
    * df["fx_to_jpy"]
)

df["div_yield_ttm_current"] = df.apply(
    lambda r: (r["dividend_ttm_per_share"] / r["price"])
    if pd.notna(r["price"]) and r["price"] > 0 else 0.0,
    axis=1,
)
df["div_yield_forecast_current"] = df.apply(
    lambda r: (r["dividend_forecast_per_share"] / r["price"])
    if pd.notna(r["price"]) and r["price"] > 0 else 0.0,
    axis=1,
)
df["div_yield_ttm_cost"] = df.apply(
    lambda r: (r["dividend_ttm_per_share"] / r["cost_price"])
    if pd.notna(r["cost_price"]) and r["cost_price"] > 0 else 0.0,
    axis=1,
)
df["div_yield_forecast_cost"] = df.apply(
    lambda r: (r["dividend_forecast_per_share"] / r["cost_price"])
    if pd.notna(r["cost_price"]) and r["cost_price"] > 0 else 0.0,
    axis=1,
)

# 損益・パフォーマンス
df["profit_pct"] = df.apply(lambda r: 0 if r["ticker"] == "CASH" else (r["price"] - r["cost_price"]) / r["cost_price"] * 100, axis=1)

# --- パフォーマンスデータの流し込み ---
# すでに df["day_diff_pct"] に値を入れているので、それを利用するだけです
df["daily_pct"] = df["day_diff_pct"]
df["daily_val"] = df["day_diff_val"]

# ========= 4. フィルター設定 (サイドバー) =========
st.sidebar.header("🔍 フィルター設定")
all_owners = df["owner"].unique().tolist() if "owner" in df.columns else ["Unknown"]
selected_owners = st.sidebar.multiselect("名義を選択", all_owners, default=all_owners)

all_accounts = df["account_type"].unique().tolist()
selected_accounts = st.sidebar.multiselect("口座種別を選択", all_accounts, default=all_accounts)

st.sidebar.header("💰 配当表示")
dividend_basis = st.sidebar.radio(
    "配当基準",
    ["予想", "過去365日実績"],
    index=0,
    horizontal=True,
)
yield_basis = st.sidebar.radio(
    "利回り基準",
    ["現在株価ベース", "取得価格ベース"],
    index=0,
    horizontal=True,
)
st.sidebar.caption(
    "予想はPhase 1では直近配当と支払頻度から年率換算した推計値です。"
)

annual_div_col = (
    "annual_div_forecast_jpy"
    if dividend_basis == "予想"
    else "annual_div_ttm_jpy"
)
if dividend_basis == "予想":
    selected_yield_col = (
        "div_yield_forecast_current"
        if yield_basis == "現在株価ベース"
        else "div_yield_forecast_cost"
    )
else:
    selected_yield_col = (
        "div_yield_ttm_current"
        if yield_basis == "現在株価ベース"
        else "div_yield_ttm_cost"
    )

# 既存表示ロジックとの互換用エイリアス
df["annual_div_jpy"] = df[annual_div_col]
df["div_yield"] = df[selected_yield_col]
df["after_tax_div_jpy"] = df.apply(calc_after_tax_dividend, axis=1)

st.sidebar.header("📡 データ取得状態")

if price_errors:
    st.warning(f"データ取得失敗（直近の終値や配当が取得できませんでした）: {price_errors}")
else:
    st.sidebar.success("価格データ取得：正常")

div_warning_tickers = []

# 修正後のループ処理
for ticker in all_tickers: # unique_tickers から all_tickers に変更
    # div_yield_dict から値を取得（なければ 0）
    div = div_yield_dict.get(ticker, 0)
    
    # 既存のロジック（配当がない場合の警告リスト作成など）
    if div == 0 and ticker not in fund_status and ticker not in ["CASH", "VOO", "BTC-JPY", "ETH-JPY"]:
        div_warning_tickers.append(ticker)

# --- 配当・データ取得の警告表示 ---
if price_errors:
    # 以前の div_errors の代わりに、一括取得で発生した price_errors を表示します
    st.sidebar.error(f"❌ データ取得失敗: {', '.join(price_errors)}")
    
if div_warning_tickers:
    st.sidebar.warning(f"⚠️ 配当要確認: {', '.join(div_warning_tickers)}")
else:
    st.sidebar.success("配当データ取得：正常")

# --- パフォーマンス取得のエラー表示 ---
# 既に上で errors を使って st.sidebar.error を出しているなら、この if ブロックは丸ごと削除してOKです。
# もし残したい場合は以下のように書き換えます。

if price_errors:
    # 既存の price_errors を使う（perf_errors という名前はもう使いません）
    st.sidebar.warning(f"⚠️ 一部銘柄の騰落データが不完全です: {', '.join(price_errors)}")
else:
    st.sidebar.success("前日比データ取得：正常")

fx_errors = []

if usd_error:
    fx_errors.append("USD/JPY")
if vnd_error:
    fx_errors.append("VND/JPY")

if fx_errors:
    st.sidebar.warning(f"為替取得失敗: {', '.join(fx_errors)}")
else:
    st.sidebar.success("為替データ取得：正常")


# ========= キャッシュ更新 =========
st.sidebar.header("🔄 データ更新")

if st.sidebar.button("最新データを再取得"):
    #キャッシュなしなのでget_performance.clear()
    #キャッシュなしなのでget_dividends.clear()
    
    load_daily_log_detail.clear()
    load_daily_log_v2.clear()
    load_fund_reference_data.clear()

    st.sidebar.success("最新データを再取得します")

# データの絞り込み実行
mask = df["account_type"].isin(selected_accounts)
if "owner" in df.columns:
    mask = mask & (df["owner"].isin(selected_owners))
df_filtered = df[mask].copy()

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
total_div_ttm_pre = df_filtered["annual_div_ttm_jpy"].sum()
total_div_forecast_pre = df_filtered["annual_div_forecast_jpy"].sum()
forecast_vs_ttm = total_div_forecast_pre - total_div_ttm_pre
forecast_vs_ttm_pct = (
    forecast_vs_ttm / total_div_ttm_pre * 100
    if total_div_ttm_pre != 0 else 0
)

st.markdown("---")
st.header("💰 Dividend Summary")
st.caption(f"配当基準: {dividend_basis} ／ 利回り基準: {yield_basis}")
d1, d2, d3, d4 = st.columns(4)
d1.metric("年間配当（税引前）", f"{total_div_pre:,.0f} 円")
d2.metric("年間配当（税引後参考額）", f"{total_div_post:,.0f} 円")
d3.metric("月平均（税引後参考額）", f"{(total_div_post/12):,.0f} 円")
d4.metric(
    "予想－過去365日",
    f"{forecast_vs_ttm:+,.0f} 円",
    f"{forecast_vs_ttm_pct:+.1f}%"
)
if df_filtered["ticker"].isin(["VOO", "SCHD"]).any():
    st.caption("※ VOO・SCHDの旧行は実保有の投資信託を対応ETFで換算した参考値です。")
st.caption(
    "過去365日実績は現在保有数量で換算した年間インカム力で、実際の入金額ではありません。"
    "楽天SCHDの特定口座は全額普通分配・外国税10%・満額控除の二重課税調整モデルを使用します。"
    "NISA・旧NISA・Jr.NISAの国内税は0。税引後参考額は実受取額と異なる場合があります。"
)

print("df columns:", df.columns)
print("df_filtered columns:", df_filtered.columns)

# --- (6) 配当金のツリーマップ ---
import numpy as np
import pandas as pd
import plotly.graph_objects as go

# 1. データの準備と銘柄単位での集計（NISAと特定を合算）
df_div_map = df_filtered.copy()
df_div_map['sector_group'] = df_div_map['sector_group'].replace(['', ' ', 'nan', 'None', '未分類'], np.nan)

# 銘柄単位で集計。
# 利回りは単純平均ではなく、現在評価額または取得総額で加重して算出する。
df_div_map["yield_base_jpy"] = (
    df_div_map["value_jpy"]
    if yield_basis == "現在株価ベース"
    else df_div_map["cost_basis_jpy"]
)

df_div_grouped = df_div_map.groupby(
    ['asset_class', 'sector_group', 'display_name'],
    dropna=False
).agg({
    'annual_div_jpy': 'sum',
    'after_tax_div_jpy': 'sum',
    'yield_base_jpy': 'sum'
}).reset_index()

df_div_grouped["display_div_yield"] = df_div_grouped.apply(
    lambda r: (
        r["annual_div_jpy"] / r["yield_base_jpy"]
        if r["yield_base_jpy"] > 0 else 0.0
    ),
    axis=1
)

# 色の定義
div_colors = [[0.0, "rgb(192, 0, 0)"], [0.5, "rgb(64, 64, 64)"], [1.0, "rgb(0, 128, 0)"]]

# 2. 階層データの構築
d_ids, d_parents, d_labels, d_values, d_colors = [], [], [], [], []

# (A) ルート
d_root_id = "Div_Root"
d_ids.append(d_root_id); d_parents.append(""); d_labels.append(f"{dividend_basis} 年間配当（税引後参考）: {total_div_post:,.0f} 円")
d_values.append(0) 
d_colors.append(df_div_grouped['display_div_yield'].mean() if not df_div_grouped.empty else 0)

# (B) 資産クラス
for ac in df_div_grouped["asset_class"].unique():
    d_ids.append(ac); d_parents.append(d_root_id); d_labels.append(ac)
    d_values.append(0)
    d_colors.append(df_div_grouped[df_div_grouped["asset_class"] == ac]['display_div_yield'].mean())

# (C) セクター
df_div_sectors = df_div_grouped[df_div_grouped["sector_group"].notna()]
for (ac, sector), group in df_div_sectors.groupby(["asset_class", "sector_group"]):
    d_sector_id = f"DivSect-{ac}-{sector}"
    d_ids.append(d_sector_id); d_parents.append(ac); d_labels.append(sector)
    d_values.append(0)
    d_colors.append(group['display_div_yield'].mean())

# (D) 銘柄
for _, row in df_div_grouped.iterrows():
    ticker = row['display_name']
    ac = row['asset_class']
    sector = row['sector_group']
    
    d_parent_id = f"DivSect-{ac}-{sector}" if pd.notna(sector) else ac
    d_unique_id = f"DivItem-{ac}-{sector}-{ticker}"
    
    d_ids.append(d_unique_id); d_parents.append(d_parent_id); d_labels.append(ticker)
    d_values.append(row['after_tax_div_jpy']) # サイズは年間配当額
    d_colors.append(row['display_div_yield'])      # 色は選択した利回り

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
        colorbar=dict(
            tickformat=".0%",
            orientation="h", x=1, xanchor="right", y=0.005, yanchor="top",
            len=0.42, thickness=12, outlinewidth=0, tickfont=dict(size=10),
            tickmode="array", tickvals=[0, 0.01, 0.02, 0.03, 0.04, 0.05],
        ),
        line=dict(width=1, color="black")
    ),
    hovertemplate=f"<b>%{{label}}</b><br>年間配当: %{{value:,.0f}}円<br>{yield_basis}: %{{color:.2%}}<extra></extra>",
    texttemplate="<b>%{label}</b><br>%{value:,.0f}円",
))

fig_div.update_layout(
    height=700,
    margin=dict(t=0, l=10, r=10, b=55),
)

st.plotly_chart(fig_div, use_container_width=True, key="dividend_tree_new")

# --- (3') 配当構成比率（帯グラフ：列作成・順序固定版） ---
import plotly.express as px

# Dividend Summary と同じ税引後参考額を使う
div_col = 'after_tax_div_jpy'
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
        hovertemplate="<b>%{label}</b><br>税引後参考額: %{value:,.0f}円<br>比率: %{text}<extra></extra>"
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


# ========= 資産増減の要因分析（Daily_Log_V2） =========
st.header("🔎 資産増減の要因分析")

df_log_v2 = load_daily_log_v2()

if df_log_v2.empty:
    st.info("Daily_Log_V2 の記録がまだありません。")
else:
    v2_dates = sorted(
        pd.to_datetime(df_log_v2["date"], errors="coerce").dropna().dt.date.unique()
    )

    if len(v2_dates) < 2:
        st.info(
            "要因分析は2日分のDaily_Log_V2が貯まると開始します。"
            f" 現在の記録開始日は {v2_dates[0]} です。"
        )
        st.caption(
            "従来の資産推移・任意2日比較はDaily_Logを使うため、この待機期間でも今まで通り利用できます。"
        )
    else:
        preset = st.radio(
            "分析期間",
            ["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL", "任意"],
            horizontal=True,
            key="attribution_period",
        )
        latest_date = v2_dates[-1]

        if preset == "1D":
            selected_start = v2_dates[-2]
            selected_end = latest_date
        elif preset == "1W":
            selected_start = (pd.Timestamp(latest_date) - pd.Timedelta(days=7)).date()
            selected_end = latest_date
        elif preset == "1M":
            selected_start = (pd.Timestamp(latest_date) - pd.DateOffset(months=1)).date()
            selected_end = latest_date
        elif preset == "3M":
            selected_start = (pd.Timestamp(latest_date) - pd.DateOffset(months=3)).date()
            selected_end = latest_date
        elif preset == "YTD":
            selected_start = latest_date.replace(month=1, day=1)
            selected_end = latest_date
        elif preset == "1Y":
            selected_start = (pd.Timestamp(latest_date) - pd.DateOffset(years=1)).date()
            selected_end = latest_date
        elif preset == "ALL":
            selected_start = v2_dates[0]
            selected_end = latest_date
        else:
            col_start, col_end = st.columns(2)
            with col_start:
                selected_start = st.selectbox(
                    "V2開始日",
                    v2_dates[:-1],
                    index=max(0, len(v2_dates) - 2),
                    key="attribution_custom_start",
                )
            with col_end:
                end_candidates = [d for d in v2_dates if d > selected_start]
                selected_end = st.selectbox(
                    "V2終了日",
                    end_candidates,
                    index=len(end_candidates) - 1,
                    key="attribution_custom_end",
                )

        fund_reference, reference_warnings = load_fund_reference_data(df_log_v2)
        for reference_warning in reference_warnings:
            st.warning("投信要因分解: " + reference_warning)

        detail_attr, daily_attr, attr = functions.analyze_attribution_period(
            df_log_v2, selected_start, selected_end, fund_reference
        )

        if detail_attr.empty:
            st.info("選択期間内に連続する2日分以上のV2データがありません。")
        else:
            actual_start = attr["actual_start"]
            actual_end = attr["actual_end"]
            st.caption(
                f"実データ範囲: {actual_start} → {actual_end}。"
                " 日次スナップショットを積み上げて計算しています。"
            )

            col_total, col_flow, col_market, col_fx, col_residual = st.columns(5)
            col_total.metric("総資産増減", f"{attr['total_change']:+,.0f} 円")
            col_flow.metric("数量・資金/売買", f"{attr['flow_effect']:+,.0f} 円")
            col_market.metric("市場要因", f"{attr['market_effect']:+,.0f} 円")
            col_fx.metric("為替要因", f"{attr['fx_effect']:+,.0f} 円")
            col_residual.metric("投信乖離/その他", f"{attr['residual_effect']:+,.0f} 円")

            if attr.get("fund_fx_estimated"):
                st.caption(
                    "円建て米国株投信は、S&P500系=VOO、楽天SCHD=SCHDを米国市場proxyとして、"
                    "USDJPYと組み合わせて市場・為替を推定分離しています。"
                    " 基準価額の評価時刻、信託報酬、配当・税、tracking差などは「投信乖離/その他」に残します。"
                    " 4要因はShapley分解で交差項を公平に配分し、合計は総資産増減と一致します。"
                )
            else:
                st.caption(
                    "市場・為替・数量要因はShapley分解で、合計が総資産増減と一致します。"
                    " 投信proxy参照がない期間は従来方式で計算します。"
                )

            factor_df = pd.DataFrame({
                "要因": ["数量・資金/売買", "市場", "為替", "投信乖離/その他"],
                "寄与額": [
                    attr["flow_effect"], attr["market_effect"], attr["fx_effect"], attr["residual_effect"]
                ],
            })
            fig_factor = px.bar(factor_df, x="要因", y="寄与額", title="期間中の要因別寄与額")
            fig_factor.update_layout(xaxis_title="", yaxis_title="円", margin=dict(t=45, l=10, r=10, b=10))
            st.plotly_chart(fig_factor, use_container_width=True, key="attribution_factors")

            if len(daily_attr) > 1:
                daily_plot = daily_attr.rename(columns={
                    "flow_effect": "数量・資金/売買",
                    "market_effect": "市場",
                    "fx_effect": "為替",
                    "residual_effect": "投信乖離/その他",
                }).melt(
                    id_vars=["date"],
                    value_vars=["数量・資金/売買", "市場", "為替", "投信乖離/その他"],
                    var_name="要因",
                    value_name="寄与額",
                )
                fig_daily = px.bar(daily_plot, x="date", y="寄与額", color="要因", barmode="relative", title="日次の要因別寄与")
                fig_daily.update_layout(xaxis_title="", yaxis_title="円", margin=dict(t=45, l=10, r=10, b=10))
                st.plotly_chart(fig_daily, use_container_width=True, key="attribution_daily")

            asset_attr = functions.summarize_attribution(detail_attr, "asset_class")
            sector_attr = functions.summarize_attribution(detail_attr, "sector")
            name_attr = functions.summarize_attribution(detail_attr, "display_name")

            def _format_attr_table(frame, label_column, label_name, limit=None):
                view = frame.copy()
                view[label_column] = view[label_column].replace("", "未分類")
                view = view.rename(columns={
                    label_column: label_name,
                    "market_effect": "市場寄与",
                    "fx_effect": "為替寄与",
                    "residual_effect": "投信乖離/その他",
                    "investment_effect": "運用寄与",
                    "flow_effect": "数量・資金/売買",
                    "total_change": "総増減",
                })
                columns = [label_name, "市場寄与", "為替寄与", "投信乖離/その他", "運用寄与", "数量・資金/売買", "総増減"]
                if limit is not None:
                    view = view.head(limit)
                return view[columns]

            st.subheader("📦 アセットクラス別の寄与")
            st.dataframe(
                _format_attr_table(asset_attr, "asset_class", "アセットクラス").style.format({
                    "市場寄与": "{:+,.0f}", "為替寄与": "{:+,.0f}",
                    "投信乖離/その他": "{:+,.0f}", "運用寄与": "{:+,.0f}",
                    "数量・資金/売買": "{:+,.0f}", "総増減": "{:+,.0f}",
                }),
                use_container_width=True,
                hide_index=True,
            )

            col_sector, col_name = st.columns(2)
            with col_sector:
                st.subheader("🏭 セクター寄与 TOP")
                sector_display = sector_attr.reindex(
                    sector_attr["investment_effect"].abs().sort_values(ascending=False).index
                )
                st.dataframe(
                    _format_attr_table(sector_display, "sector", "セクター", limit=10).style.format({
                        "市場・NAV寄与": "{:+,.0f}", "為替寄与": "{:+,.0f}",
                        "市場+為替寄与": "{:+,.0f}", "数量・資金/売買": "{:+,.0f}", "総増減": "{:+,.0f}",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

            with col_name:
                st.subheader("🏅 銘柄寄与 TOP")
                name_display = name_attr.reindex(
                    name_attr["investment_effect"].abs().sort_values(ascending=False).index
                )
                st.dataframe(
                    _format_attr_table(name_display, "display_name", "銘柄", limit=10).style.format({
                        "市場・NAV寄与": "{:+,.0f}", "為替寄与": "{:+,.0f}",
                        "市場+為替寄与": "{:+,.0f}", "数量・資金/売買": "{:+,.0f}", "総増減": "{:+,.0f}",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )


# --- (3) 資産額の表 ---
st.subheader("📝 保有資産一覧")
st.dataframe(df_filtered[["ticker", "display_name", "quantity", "price", "value_jpy", "profit_pct"]].style.format({"profit_pct": "{:.2f}%", "value_jpy": "{:,.0f}"}))

st.markdown("---")

# --- (6) 配当金の表 ---
st.subheader("📈 銘柄別配当データ")
if fund_status:
    st.caption("投資信託の単位配当は1万口当たり、株式・ETFは1株当たりです。")
df_div_table = df_filtered[[
    "ticker",
    "display_name",
    "div_yield",
    "annual_div_jpy",
    "after_tax_div_jpy",
    "dividend_ttm_per_share",
    "dividend_forecast_per_share",
    "dividend_forecast_source",
    "price_unit",
]].copy()
df_div_table = df_div_table.rename(columns={
    "div_yield": f"{yield_basis}利回り",
    "annual_div_jpy": f"年間配当（{dividend_basis}・税引前）",
    "after_tax_div_jpy": f"年間配当（{dividend_basis}・税引後参考）",
    "price_unit": "価格単位",
    "dividend_ttm_per_share": "単位配当（過去365日）",
    "dividend_forecast_per_share": "単位配当（予想）",
    "dividend_forecast_source": "予想根拠",
})
st.dataframe(
    df_div_table.style.format({
        f"{yield_basis}利回り": "{:.2%}",
        f"年間配当（{dividend_basis}・税引前）": "{:,.0f}",
        f"年間配当（{dividend_basis}・税引後参考）": "{:,.0f}",
        "単位配当（過去365日）": "{:,.4f}",
        "単位配当（予想）": "{:,.4f}",
    })
)



#資産記録
import json

#functionsに移植済み

st.markdown("---")
st.subheader("📅 データ記録")

if st.button("📅 今日の資産を記録"):
    with st.spinner("保存中..."):

        # 👇 ここに追加
        if "GCP_SERVICE_ACCOUNT" in os.environ:
            creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT"])
        else:
            creds_dict = dict(st.secrets["gcp_service_account"])

        # 👇 引数に渡す
        result = save_daily_log_detail(df, creds_dict)
        detail_result = functions.save_daily_log_v2(df, creds_dict, fund_status)
    load_daily_log_detail.clear()
    load_daily_log_v2.clear()
    load_fund_reference_data.clear()

    st.success(result + " / " + detail_result)
    
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

        d_merged, summary = compare_portfolio(
        df_log,
        s_date,
        e_date
        )

        if summary["migration_names"]:
            st.warning("投信の評価方法変更を含む期間です（" + "・".join(summary["migration_names"]) +
                       "）。資産増減にはETF換算から実基準価額への移行差が含まれます。"
                       "対象銘柄の騰落率・ランキングは表示を控えています。")
        total_start = summary["total_start"]
        total_end = summary["total_end"]
        total_diff = summary["total_diff"]
        total_growth = summary["total_growth"]

        top_gainers = summary["top_gainers"]
        top_losers = summary["top_losers"]

        top_diff_up = summary["top_diff_up"]
        top_diff_down = summary["top_diff_down"]

        sector_summary = summary["sector_summary"]
        asset_summary = summary["asset_summary"]

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

        
       # --- 騰落率ランキング ---
        st.markdown("---")
        st.subheader("📈 騰落率ランキング")

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### 🟢 値上がり率 TOP10")

            df_gain = top_gainers.copy()
            df_gain["growth_pct"] = df_gain["growth_pct"].round(2)
            df_gain["diff_val"] = (df_gain["diff_val"].astype(int).map(lambda x: f"{x:,}")
            )

            st.dataframe(
                df_gain.rename(columns={
                    "display_name": "銘柄",
                    "growth_pct": "騰落率(%)",
                    "diff_val": "増減額(円)"
                })[[
                    "銘柄",
                    "騰落率(%)",
                    "増減額(円)"
                ]]
                .reset_index(drop=True)
            )

        with col2:
            st.markdown("### 🔴 値下がり率 TOP10")

            df_loss = top_losers.copy()
            df_loss["growth_pct"] = df_loss["growth_pct"].round(2)
            df_loss["diff_val"] = (df_loss["diff_val"].astype(int).map(lambda x: f"{x:,}")
            )

            st.dataframe(
                df_loss.rename(columns={
                    "display_name": "銘柄",
                    "growth_pct": "騰落率(%)",
                    "diff_val": "増減額(円)"
                })[[
                    "銘柄",
                    "騰落率(%)",
                    "増減額(円)"
                ]]
                .reset_index(drop=True)
            )

        
        # --- 増減額ランキング ---
        st.markdown("---")
        st.subheader("💰 増減額ランキング")

        col3, col4 = st.columns(2)

        with col3:
            st.markdown("### 🚀 資産増加額 TOP10")

            df_up = top_diff_up.copy()
            df_up["growth_pct"] = df_up["growth_pct"].round(2)
            df_up["diff_val"] = (df_up["diff_val"].astype(int).map(lambda x: f"{x:,}")
            )

            st.dataframe(
                df_up.rename(columns={
                    "display_name": "銘柄",
                    "growth_pct": "騰落率(%)",
                    "diff_val": "増減額(円)"
                })[[
                    "銘柄",
                    "騰落率(%)",
                    "増減額(円)"
                ]]
                .reset_index(drop=True)
            )

        with col4:
            st.markdown("### 📉 資産減少額 TOP10")

            df_down = top_diff_down.copy()
            df_down["growth_pct"] = df_down["growth_pct"].round(2)
            df_down["diff_val"] = (df_down["diff_val"].astype(int).map(lambda x: f"{x:,}")
            )

            st.dataframe(
                df_down.rename(columns={
                    "display_name": "銘柄",
                    "growth_pct": "騰落率(%)",
                    "diff_val": "増減額(円)"
                })[[
                    "銘柄",
                    "騰落率(%)",
                    "増減額(円)"
                ]]
                .reset_index(drop=True)
            )

      # --- セクター＆アセット分析 ---
        st.markdown("---")

        col5, col6 = st.columns(2)

        with col5:
            st.subheader("🏭 日本株セクター別騰落率")

            sector_display = sector_summary.copy()

            sector_display["growth_pct"] = (
                sector_display["growth_pct"]
                .round(2)
            )

            sector_display["diff_val"] = (
                sector_display["diff_val"]
                .astype(int)
                .map(lambda x: f"{x:,}")
            )

            st.dataframe(
                sector_display.rename(columns={
                    "sector": "セクター",
                    "growth_pct": "騰落率(%)",
                    "diff_val": "増減額(円)"
                })[[
                    "セクター",
                    "騰落率(%)",
                    "増減額(円)"
                ]]
                .reset_index(drop=True)
            )

        with col6:
            st.subheader("📦 アセットクラス別騰落率")

            asset_display = asset_summary.copy()

            asset_display["growth_pct"] = (
                asset_display["growth_pct"]
                .round(2)
            )

            asset_display["diff_val"] = (
                asset_display["diff_val"]
                .astype(int)
                .map(lambda x: f"{x:,}")
            )

            st.dataframe(
                asset_display.rename(columns={
                    "asset_class": "アセットクラス",
                    "growth_pct": "騰落率(%)",
                    "diff_val": "増減額(円)"
                })[[
                    "アセットクラス",
                    "騰落率(%)",
                    "増減額(円)"
                ]]
                .reset_index(drop=True)
            )      

    else:
        st.info("比較するには2つ以上のログデータが必要です。")

