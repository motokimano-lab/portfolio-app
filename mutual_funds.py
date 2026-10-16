"""Native Japanese mutual-fund NAV/distributions, in yen per 10,000 units.

Source priority:
1) Yahoo! Finance Japan (valuation/distribution mirror)
2) Fund-provider / official source
3) Repository-backed last-known-good cache

The app never falls back to ETF proxies for mutual-fund valuation.
"""
import csv
from datetime import datetime
from io import StringIO
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import pandas as pd
import requests

from dividend_tax import FOREIGN_WITHHOLDING_RATE


CACHE_PATH = Path(__file__).resolve().parent / "data" / "mutual_fund_cache.json"

YAHOO_HISTORY = "https://finance.yahoo.co.jp/quote/{code}/history"
YAHOO_DIVIDENDS = "https://finance.yahoo.co.jp/quote/{code}/dividendinfo"


FUND_SPECS = {
    "9I312249": {
        "product_name": "楽天・シュワブ・高配当株式・米国ファンド（四半期決算型）",
        "display_name": "SCHD",
        "frequency": 4,
        "provider": "rakuten",
        "latest": "https://www.rakuten-toushin.co.jp/assets/csv/nav_data.csv",
        "history": "https://www.rakuten-toushin.co.jp/assets/csv/chart_100105.csv",
        "distributions": "https://www.rakuten-toushin.co.jp/assets/csv/dividend_100105.csv",
        "provider_code": "100105",
        "foreign_tax_adjustment": True,
        "underlying_foreign_tax_rate": FOREIGN_WITHHOLDING_RATE,
        "underlying_currency": "USD",
        "attribution_proxy": "SCHD",
    },
    "03311187": {
        "product_name": "eMAXIS Slim 米国株式（S&P500）",
        "display_name": "SP500",
        "frequency": 1,
        "provider": "mufg",
        "latest": "https://developer.am.mufg.jp/fund_information_latest/association_fund_cd/03311187",
        "history": "https://www.am.mufg.jp/fund_file/setteirai/253266.csv",
        "underlying_currency": "USD",
        "attribution_proxy": "VOO",
    },
    "89311199": {
        "product_name": "SBI・V・S&P500インデックス・ファンド",
        "display_name": "SP500",
        "frequency": 1,
        "provider": "sbi",
        "history": "https://apl.wealthadvisor.jp/webasp/sbi_am/pc/download.aspx?type=1&fnc=2019092601",
        "distributions": "https://apl.wealthadvisor.jp/xml/chart/funddata/2019092601.xml",
        "underlying_currency": "USD",
        "attribution_proxy": "VOO",
    },
}


def _get(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept": "text/html,text/csv,application/json,application/xml,text/xml,text/plain,*/*",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    response = requests.get(url, timeout=(10, 30), headers=headers)
    response.raise_for_status()
    return response


def _number(value):
    text = str(value).replace(",", "").replace("円", "").replace("＋", "+").replace("−", "-")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("non-finite value")
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
            continue
        nav = _number(row[1])
        if nav <= 0:
            raise ValueError("non-positive NAV")
        records.append((date, nav))
    result = pd.Series(dict(records), dtype=float).sort_index()
    if len(result) < 2:
        raise ValueError("insufficient NAV history")
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
            continue
        amount = _number(row[amount_column])
        if amount < 0:
            raise ValueError("negative distribution")
        records.append((date, amount))
    if not records:
        raise ValueError("distribution history unavailable")
    return pd.Series(dict(records), dtype=float).sort_index()


def _html_table_rows(response):
    soup = BeautifulSoup(response.text, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in tr.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    return rows


def _yahoo_nav(code):
    rows = _html_table_rows(_get(YAHOO_HISTORY.format(code=code)))
    records = []
    for row in rows:
        if len(row) < 3:
            continue
        try:
            date = _date(row[0])
            nav = _number(row[1])
            delta_text = re.sub(r"[^0-9+\-.]", "", row[2])
            delta = _number(delta_text)
        except (ValueError, IndexError):
            continue
        if nav <= 0 or nav - delta <= 0:
            continue
        records.append((date, nav, delta))
    if not records:
        raise ValueError("Yahoo NAV table unavailable")
    date, nav, delta = max(records, key=lambda item: item[0])
    return date, nav, delta


def _yahoo_distributions(code):
    rows = _html_table_rows(_get(YAHOO_DIVIDENDS.format(code=code)))
    records = []
    for row in rows:
        if len(row) < 2:
            continue
        try:
            date = _date(row[0])
            amount = _number(row[1])
        except (ValueError, IndexError):
            continue
        if amount < 0:
            raise ValueError("negative distribution")
        records.append((date, amount))
    if not records:
        raise ValueError("Yahoo distribution table unavailable")
    return pd.Series(dict(records), dtype=float).sort_index()


def _official_record(code, spec):
    warnings = []
    rows = _csv_rows(_get(spec["history"]))
    history = _history(rows)

    if spec["provider"] == "rakuten":
        latest_rows = _csv_rows(_get(spec["latest"]))
        latest = next(r for r in latest_rows if r[0] == spec["provider_code"])
        date, nav, delta = _date(latest[1]), _number(latest[2]), _number(latest[3])
        distributions = _csv_distributions(_csv_rows(_get(spec["distributions"])), 2, 3)

    elif spec["provider"] == "mufg":
        distributions = _csv_distributions(rows, 0, 3)
        try:
            payload = _get(spec["latest"]).json()
            if payload["result"]["status"] != 200 or payload["result"]["retcount"] != 1:
                raise ValueError("invalid official API result")
            latest = payload["datasets"][0]
            if latest["association_fund_cd"] != code:
                raise ValueError("official API fund code mismatch")
            date, nav, delta = (
                _date(latest["base_date"]),
                _number(latest["nav"]),
                _number(latest["cmp_prev_day"]),
            )
        except (requests.RequestException, ValueError, KeyError, TypeError):
            date = history.index[-1]
            nav = float(history.iloc[-1])
            delta = nav - float(history.iloc[-2])
            warnings.append("MUFG公式API取得失敗のため、MUFG公式履歴CSVの最新値を使用")

    else:
        date, nav = history.index[-1], float(history.iloc[-1])
        delta = nav - float(history.iloc[-2])
        xml_text = _get(spec["distributions"]).content.decode("cp932")
        root = ET.fromstring(xml_text)
        fund = root.find("fund")
        if fund is None or fund.get("code") != "2019092601":
            raise ValueError("official distribution XML fund mismatch")
        decisions = []
        dates = []
        for day in fund.iter("day"):
            day_date = _date(
                day.get("year")
                + day.get("month").zfill(2)
                + day.get("value").zfill(2)
            )
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
            warnings.append(
                f"分配金履歴の最終日 {max(dates):%Y-%m-%d} は基準価額の日付より古い"
            )

    if history.index[-1] != date or not math.isclose(float(history.iloc[-1]), nav, abs_tol=0.01):
        raise ValueError("latest NAV and history disagree")

    return date, nav, delta, distributions, history, warnings


def get_mutual_fund_distribution_history(fund_code):
    """Return distribution history per 10,000 units.

    Source priority mirrors valuation: Yahoo! Finance Japan first, then the
    fund provider. This helper is used only for total-return attribution; it
    never substitutes an ETF distribution for the actual fund distribution.
    """
    code = str(fund_code).strip()
    try:
        spec = FUND_SPECS[code]
    except KeyError as error:
        raise ValueError("unsupported mutual fund code") from error

    try:
        distributions = _yahoo_distributions(code)
        return distributions.sort_index(), "yahoo_finance", []
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as yahoo_error:
        yahoo_error_name = type(yahoo_error).__name__

    try:
        _, _, _, distributions, _, warnings = _official_record(code, spec)
        return (
            distributions.sort_index(),
            "official_or_provider",
            [f"Yahoo分配金取得失敗({yahoo_error_name})のため第2ソースを使用"] + warnings,
        )
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        StopIteration,
        TypeError,
        ET.ParseError,
    ) as official_error:
        raise ValueError(
            "mutual fund distribution history unavailable: "
            f"yahoo={yahoo_error_name}, official={type(official_error).__name__}"
        ) from official_error


def _business_days_old(base_date, today):
    base = pd.Timestamp(base_date)
    end = pd.Timestamp(today)
    if base >= end:
        return 0
    return len(pd.bdate_range(base + pd.Timedelta(days=1), end))


def _load_cached_record(code, today):
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        item = payload.get("funds", {}).get(code)
        if not item:
            return None
        base_date = pd.Timestamp(item["base_date"])
        if base_date > today:
            return None
        fetched_at = item.get("fetched_at") or payload.get("generated_at")
        return {
            "fund_code": code,
            "ticker": "MF:" + code,
            "price_unit": 10000,
            "success": True,
            "price": float(item["price"]),
            "base_date": item["base_date"],
            "previous_change": float(item["previous_change"]),
            "previous_change_pct": float(item["previous_change_pct"]),
            "ttm_distribution": float(item["ttm_distribution"]),
            "forecast_distribution": float(item["forecast_distribution"]),
            "history": None,
            "distributions": None,
            "warnings": [
                "ライブデータ取得失敗のため、最終正常値キャッシュを使用: "
                + item["base_date"]
            ],
            "error": None,
            "stale": True,
            "business_days_old": _business_days_old(base_date, today),
            "fetched_at": fetched_at,
            "product_name": item["product_name"],
            "forecast_source": item["forecast_source"],
            "source": "last_good_cache",
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _finish_result(result, spec, today, date, nav, delta, distributions, source, warnings=None):
    if nav <= 0 or nav - delta <= 0 or date > today:
        raise ValueError("invalid latest NAV/date")
    if (distributions.index > date).any():
        raise ValueError("distribution after latest valuation date")

    ttm = float(
        distributions.loc[
            (distributions.index > today - pd.Timedelta(days=365))
            & (distributions.index <= today)
        ].sum()
    )
    forecast = float(distributions.iloc[-1]) * spec["frequency"]

    result.update(
        success=True,
        price=nav,
        base_date=date.date().isoformat(),
        previous_change=delta,
        previous_change_pct=delta / (nav - delta) * 100,
        ttm_distribution=ttm,
        forecast_distribution=forecast,
        forecast_source=f"直近分配金×{spec['frequency']}回/年（推計・1万口当たり）",
        source=source,
        business_days_old=_business_days_old(date, today),
    )
    result["warnings"].extend(warnings or [])
    result["stale"] = date < today
    if result["stale"]:
        result["warnings"].append(
            f"本日付の基準価額は未公表: {date:%Y-%m-%d}（休業日・公表前を含む）"
        )
    return result


def get_mutual_fund_data(fund_code, now=None, allow_cache=True):
    """Get native mutual-fund data without ETF substitution.

    Priority is Yahoo! Finance Japan -> official/provider source -> last-good cache.
    """
    code = str(fund_code).strip()
    today = pd.Timestamp((now or datetime.now(ZoneInfo("Asia/Tokyo"))).date())
    result = {
        "fund_code": code,
        "ticker": "MF:" + code,
        "price_unit": 10000,
        "success": False,
        "price": None,
        "base_date": None,
        "previous_change": None,
        "previous_change_pct": None,
        "ttm_distribution": None,
        "forecast_distribution": None,
        "history": None,
        "distributions": None,
        "warnings": [],
        "error": None,
        "stale": False,
        "business_days_old": None,
        "fetched_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "source": None,
    }

    try:
        spec = FUND_SPECS[code]
    except KeyError:
        result["error"] = "未対応ファンドコード"
        return result

    result["product_name"] = spec["product_name"]

    yahoo_error = None
    try:
        date, nav, delta = _yahoo_nav(code)
        distributions = _yahoo_distributions(code)
        return _finish_result(
            result,
            spec,
            today,
            date,
            nav,
            delta,
            distributions,
            "yahoo_finance",
        )
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        yahoo_error = type(error).__name__

    official_error = None
    try:
        date, nav, delta, distributions, history, warnings = _official_record(code, spec)
        result["history"] = history
        result["distributions"] = distributions
        warnings = [f"Yahoo取得失敗({yahoo_error})のため第2ソースを使用"] + warnings
        return _finish_result(
            result,
            spec,
            today,
            date,
            nav,
            delta,
            distributions,
            "official_or_provider",
            warnings,
        )
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        StopIteration,
        ET.ParseError,
        TypeError,
    ) as error:
        official_error = type(error).__name__

    if allow_cache:
        cached = _load_cached_record(code, today)
        if cached is not None:
            cached["warnings"].insert(
                0,
                f"Yahoo取得失敗({yahoo_error})・第2ソース取得失敗({official_error})",
            )
            return cached

    result["error"] = (
        f"全データソース取得失敗: Yahoo={yahoo_error}, secondary={official_error}"
    )
    return result
