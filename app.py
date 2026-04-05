import pandas as pd
import streamlit as st
import yfinance as yf
import plotly.express as px
from datetime import datetime # ← これを追記

# ========= 1. データ読み込み =========
url = "https://docs.google.com/spreadsheets/d/18PLN9uJHxVZCAvAw92piWCniLlQ2i8Z6dT8ok_jycBI/export?format=csv&gid=0"
df = pd.read_csv(url)

st.set_page_config(layout="wide") # 画面を広く使う設定
st.title("My Portfolio Management")

# ========= 2. 各種データ取得・関数定義 =========

def get_price(ticker):
    if ticker == "CASH": return 1
    try:
        stock = yf.Ticker(ticker)
        return stock.history(period="1d")["Close"].iloc[-1]
    except: return None

def get_fx(symbol, default):
    try:
        fx = yf.Ticker(symbol)
        return fx.history(period="1d")["Close"].iloc[-1]
    except: return default

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

# 為替取得
usd_jpy = get_fx("JPY=X", 150)
vnd_jpy = get_fx("VNDJPY=X", 0.006)

# ========= 3. メイン計算処理 (すべて先に終わらせる) =========

# 基本数値
df["price"] = df["ticker"].apply(get_price)
df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
df["price"] = pd.to_numeric(df["price"], errors="coerce")
df["cost_price"] = pd.to_numeric(df["cost_price"], errors="coerce")
df["value"] = df["price"] * df["quantity"]

# 円建て評価額
df["value_jpy"] = df.apply(lambda r: r["value"] * usd_jpy if r["currency"] == "USD" else (r["value"] * vnd_jpy if r["currency"] == "VND" else r["value"]), axis=1)

# 配当計算
df["div_yield"] = df["ticker"].apply(get_dividend_data)
df["annual_div_jpy"] = df["value_jpy"] * df["div_yield"]
df["after_tax_div_jpy"] = df.apply(calc_after_tax_dividend, axis=1)

# 損益・パフォーマンス
df["profit_pct"] = df.apply(lambda r: 0 if r["ticker"] == "CASH" else (r["price"] - r["cost_price"]) / r["cost_price"] * 100, axis=1)
perf_data = df["ticker"].apply(get_performance)
df["daily_pct"] = [x[0] for x in perf_data]
df["ytd_pct"] = [x[1] for x in perf_data]

# セクター階層作成
df["sector_group"] = df.apply(lambda r: r["sector"] if pd.notna(r["sector"]) and str(r["sector"]).strip() != "" else "未分類", axis=1)

# ========= 4. フィルター設定 (サイドバー) =========
st.sidebar.header("🔍 フィルター設定")
all_owners = df["owner"].unique().tolist() if "owner" in df.columns else ["Unknown"]
selected_owners = st.sidebar.multiselect("名義を選択", all_owners, default=all_owners)

all_accounts = df["account_type"].unique().tolist()
selected_accounts = st.sidebar.multiselect("口座種別を選択", all_accounts, default=all_accounts)

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
current_time = datetime.now().strftime("%Y/%m/%d %H:%M")

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
current_time = datetime.now().strftime("%Y/%m/%d %H:%M")

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

# --- (3) 資産額の表 ---
st.subheader("📝 保有資産一覧")
st.dataframe(df_filtered[["ticker", "display_name", "quantity", "price", "value_jpy", "profit_pct"]].style.format({"profit_pct": "{:.2f}%", "value_jpy": "{:,.0f}"}))

st.markdown("---")

# --- (6) 配当金の表 ---
st.subheader("📈 銘柄別配当データ")
st.dataframe(df_filtered[["ticker", "display_name", "div_yield", "annual_div_jpy", "after_tax_div_jpy"]].style.format({"div_yield": "{:.2%}", "annual_div_jpy": "{:,.0f}", "after_tax_div_jpy": "{:,.0f}"}))