"""Native Japanese mutual-fund NAV/distributions, in yen per 10,000 units."""
import csv
from datetime import datetime
from io import StringIO
import math
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from dividend_tax import FOREIGN_WITHHOLDING_RATE


FUND_SPECS = {
    "9I312249": {
        "product_name": "楽天・シュワブ・高配当株式・米国ファンド（四半期決算型）",
        "display_name": "SCHD", "frequency": 4, "provider": "rakuten",
        "latest": "https://www.rakuten-toushin.co.jp/assets/csv/nav_data.csv",
        "history": "https://www.rakuten-toushin.co.jp/assets/csv/chart_100105.csv",
        "distributions": "https://www.rakuten-toushin.co.jp/assets/csv/dividend_100105.csv",
        "provider_code": "100105",
        "foreign_tax_adjustment": True,
        "underlying_foreign_tax_rate": FOREIGN_WITHHOLDING_RATE,
    },
    "03311187": {
        "product_name": "eMAXIS Slim 米国株式（S&P500）",
        "display_name": "SP500", "frequency": 1, "provider": "mufg",
        "latest": "https://developer.am.mufg.jp/fund_information_latest/association_fund_cd/03311187",
        "history": "https://www.am.mufg.jp/fund_file/setteirai/253266.csv",
    },
    "89311199": {
        "product_name": "SBI・V・S&P500インデックス・ファンド",
        "display_name": "SP500", "frequency": 1, "provider": "sbi",
        "history": "https://apl.wealthadvisor.jp/webasp/sbi_am/pc/download.aspx?type=1&fnc=2019092601",
        "distributions": "https://apl.wealthadvisor.jp/xml/chart/funddata/2019092601.xml",
    },
}


def _get(url):
    response = requests.get(url, timeout=(10, 30))
    response.raise_for_status()
    return response


def _number(value):
    number = float(str(value).replace(",", ""))
    if not math.isfinite(number):
        raise ValueError("non-finite official value")
    return number


def _date(value):
    text = str(value).strip()
    fmt = "%Y%m%d" if len(text) == 8 and text.isdigit() else "%Y/%m/%d"
    return pd.Timestamp(datetime.strptime(text, fmt).date())


def _csv_rows(response):
    return [row for row in csv.reader(StringIO(response.content.decode("cp932"))) if row]


def _history(rows):
    records = []
    for row in rows:
        try:
            date = _date(row[0])
        except (ValueError, IndexError):
            continue  # Title/header only; dated rows must have valid numeric NAV.
        nav = _number(row[1])
        if nav <= 0:
            raise ValueError("non-positive official NAV")
        records.append((date, nav))
    result = pd.Series(dict(records), dtype=float).sort_index()
    if len(result) < 2:
        raise ValueError("insufficient official NAV history")
    return result


def _csv_distributions(rows, date_column, amount_column):
    records = []
    for row in rows:
        try:
            date = _date(row[date_column])
        except (ValueError, IndexError):
            continue
        if len(row) <= amount_column:
            raise ValueError("missing distribution field")
        if row[amount_column].strip() == "":
            continue  # Non-decision days; keep explicit zero decision amounts.
        amount = _number(row[amount_column])
        if amount < 0:
            raise ValueError("negative distribution")
        records.append((date, amount))
    if not records:
        raise ValueError("distribution history unavailable")
    return pd.Series(dict(records), dtype=float).sort_index()


def get_mutual_fund_data(fund_code, now=None):
    """Return explicit success/date/warnings; never substitute an ETF price.

    TTM uses (as_of - 365 days, as_of], in Japan's timezone. Zero distribution
    decisions are retained, so an old positive payment is not annualized after
    a later zero decision. Missing distributions are an error, not a zero.
    """
    code = str(fund_code).strip()
    today = pd.Timestamp((now or datetime.now(ZoneInfo("Asia/Tokyo"))).date())
    result = {"fund_code": code, "ticker": "MF:" + code, "price_unit": 10000,
              "success": False, "price": None, "base_date": None,
              "previous_change": None, "previous_change_pct": None,
              "ttm_distribution": None, "forecast_distribution": None,
              "history": None, "distributions": None, "warnings": [],
              "error": None, "stale": False, "fetched_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()}
    try:
        spec = FUND_SPECS[code]
        result["product_name"] = spec["product_name"]
        rows = _csv_rows(_get(spec["history"]))
        history = _history(rows)
        if spec["provider"] == "rakuten":
            latest_rows = _csv_rows(_get(spec["latest"]))
            latest = next(r for r in latest_rows if r[0] == spec["provider_code"])
            date, nav, delta = _date(latest[1]), _number(latest[2]), _number(latest[3])
            distributions = _csv_distributions(_csv_rows(_get(spec["distributions"])), 2, 3)
        elif spec["provider"] == "mufg":
            payload = _get(spec["latest"]).json()
            if payload["result"]["status"] != 200 or payload["result"]["retcount"] != 1:
                raise ValueError("invalid official API result")
            latest = payload["datasets"][0]
            if latest["association_fund_cd"] != code:
                raise ValueError("official API fund code mismatch")
            date, nav, delta = _date(latest["base_date"]), _number(latest["nav"]), _number(latest["cmp_prev_day"])
            distributions = _csv_distributions(rows, 0, 3)
        else:
            date, nav = history.index[-1], float(history.iloc[-1])
            delta = nav - float(history.iloc[-2])
            # Decode the declared Shift_JIS before XML parsing.
            xml_text = _get(spec["distributions"]).content.decode("cp932")
            root = ET.fromstring(xml_text)
            fund = root.find("fund")
            if fund is None or fund.get("code") != "2019092601":
                raise ValueError("official distribution XML fund mismatch")
            decisions = []
            dates = []
            for day in fund.iter("day"):
                day_date = _date(day.get("year") + day.get("month").zfill(2) + day.get("value").zfill(2))
                dates.append(day_date)
                if day.get("divident") is not None:
                    amount = _number(day.get("divident"))
                    if amount < 0:
                        raise ValueError("negative distribution")
                    decisions.append((day_date, amount))
            if not dates or not decisions:
                raise ValueError("distribution XML history unavailable")
            distributions = pd.Series(dict(decisions), dtype=float).sort_index()
            if max(dates) < date:
                result["warnings"].append(f"分配金履歴の最終日 {max(dates):%Y-%m-%d} は基準価額の日付より古い")
        if nav <= 0 or nav - delta <= 0 or date > today:
            raise ValueError("invalid latest official NAV/date")
        result.update(price=nav, base_date=date.date().isoformat(), previous_change=delta,
                      previous_change_pct=delta/(nav-delta)*100)
        if history.index[-1] != date or not math.isclose(float(history.iloc[-1]), nav, abs_tol=0.01):
            # Mixing stale history and fresh NAV could miss a new distribution.
            raise ValueError("latest NAV and history disagree")
        if history.index[0] > today - pd.Timedelta(days=365):
            raise ValueError("less than 365 days of official history")
        if (distributions.index > date).any():
            raise ValueError("distribution after latest valuation date")
        ttm = float(distributions.loc[(distributions.index > today-pd.Timedelta(days=365))
                                     & (distributions.index <= today)].sum())
        forecast = float(distributions.iloc[-1])*spec["frequency"]
        result["stale"] = date < today
        if result["stale"]:
            result["warnings"].append(f"本日付の基準価額は未公表: {date:%Y-%m-%d}（休業日・公表前を含む）")
        result.update(success=True, ttm_distribution=ttm, forecast_distribution=forecast,
                      history=history, distributions=distributions,
                      forecast_source=f"公式直近分配金×{spec['frequency']}回/年（推計・1万口当たり）")
    except (requests.RequestException, ValueError, KeyError, IndexError, StopIteration, ET.ParseError, TypeError) as error:
        # Provider errors do not contain credentials: these are public requests.
        result["error"] = f"公式データ取得失敗: {type(error).__name__}"
    return result
